"""Acquisition Overture Maps (thème `places`) pour le pipeline (V2-52 volet 1).

Extrait la logique de SOURCE, rendue réutilisable : le mapping de taxonomie
(`ops/overture_category_map.json`, éprouvé au benchmark V2-48), la détection de
release S3, et le fetch DuckDB/S3 par bbox. Le benchmark de sources
(`ops/source_benchmark.py`, LECTURE SEULE) importe ces fonctions pour ne pas
dupliquer ; le pipeline d'enrichissement (`enrich/pipeline.py`) les appelle pour
fusionner Overture au commercial (banques, comblement des catégories vides,
enrichissement de contacts — V2-52).

LECTURE de S3 uniquement — aucune écriture base, aucun appel LLM (le mapping est
DÉTERMINISTE). Le fetch est INJECTABLE (`connect`) → tests sans réseau ; le
pipeline injecte de toute façon un fetcher factice en test (aucun DuckDB requis).

Le mapping JSON est le SEUL SIÈGE de la correspondance de taxonomie (une source de
vérité, partagée avec le benchmark) : le complèter/corriger se fait dans le fichier,
jamais dans le code.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger("casaguide.overture")

_HERE = Path(__file__).resolve().parent                      # backend/enrich/
_REPO_ROOT = _HERE.parents[1]                                # racine du dépôt
# Le mapping vit dans ops/ (artefact partagé avec le benchmark). Surchargeable.
_DEFAULT_MAP = _REPO_ROOT / "ops" / "overture_category_map.json"


# ── Mapping de taxonomie (artefact partagé, V2-48c) ──────────────────────────

def load_category_map(path: Path | None = None) -> dict:
    """Charge l'artefact de mapping (structure EXACT + SUFFIXE sur valeurs PLATES,
    V2-48c). Tolère l'ancien format {rules:[{prefix,code}]} (rétrocompat). Chemin par
    défaut : `ops/overture_category_map.json` (env `CASAGUIDE_OVERTURE_CATEGORY_MAP`)."""
    if path is None:
        env = os.getenv("CASAGUIDE_OVERTURE_CATEGORY_MAP")
        path = Path(env) if env else _DEFAULT_MAP
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ig = data.get("ignore") or {}
    ignore = {"exact": {t.lower() for t in (ig.get("exact") or [])},
              "keywords": [k.lower() for k in (ig.get("keywords") or [])]}
    if "exact" in data or "suffix" in data:
        return {"exact": {k.lower(): v for k, v in (data.get("exact") or {}).items()},
                "suffix": data.get("suffix") or [], "ignore": ignore}
    exact: dict[str, str] = {}
    for r in data.get("rules") or []:
        seg = r["prefix"].rsplit(".", 1)[-1]
        exact[seg] = r["code"]
    return {"exact": exact, "suffix": [], "ignore": ignore}


def map_overture_place(place: dict, cmap: dict) -> str | None:
    """Catégorie d'un lieu Overture (V2-79c) : la FEUILLE d'abord (`category`), puis ses
    ANCÊTRES de la plus spécifique à la plus générale (`category_hierarchy`, schéma
    taxonomy ≥ 2026-09). Une feuille renommée par Overture (« car_rental_service ») se
    rattrape ainsi par un parent connu (« vehicle_rental_service »), sans attendre une
    mise à jour de la table."""
    code = map_overture_category(place.get("category"), cmap)
    if code:
        return code
    for anc in reversed(place.get("category_hierarchy") or []):
        code = map_overture_category(anc, cmap)
        if code:
            return code
    return None


def map_overture_category(primary: str | None, cmap: dict) -> str | None:
    """Valeur Overture `categories.primary` → notre `code` (poi_categories). Prend le
    DERNIER segment pointé (robuste aux schémas plat et pointé), EXACT d'abord, SUFFIXE
    ensuite. None si aucune correspondance (jamais tordu)."""
    p = (primary or "").strip().lower()
    if not p:
        return None
    token = p.rsplit(".", 1)[-1]
    code = cmap.get("exact", {}).get(token)
    if code:
        return code
    for rule in cmap.get("suffix", []):
        if token.endswith(rule["suffix"]):
            return rule["code"]
    return None


def is_ignored(primary: str | None, cmap: dict) -> bool:
    """La catégorie Overture est-elle « hors sujet PAR NATURE » (hôtel, salon…) ? Un
    lieu MAPPÉ n'est jamais ignoré (le mapping prime)."""
    p = (primary or "").strip().lower()
    if not p:
        return False
    token = p.rsplit(".", 1)[-1]
    ig = cmap.get("ignore") or {}
    if token in ig.get("exact", set()):
        return True
    return any(kw in token for kw in ig.get("keywords", []))


def classify_category(primary: str | None, cmap: dict) -> str:
    """État d'une catégorie Overture : « mapped », « ignored » ou « gap » (lacune)."""
    if map_overture_category(primary, cmap) is not None:
        return "mapped"
    return "ignored" if is_ignored(primary, cmap) else "gap"


# ── Détection / validation de release S3 (V2-48b) ────────────────────────────

# Format RÉEL d'une release Overture sur S3 : AAAA-MM-JJ.N (ex. 2026-08-19.0).
_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_RELEASE_IN_PATH = re.compile(r"/release/(\d{4}-\d{2}-\d{2}\.\d+)/")
_OVERTURE_S3 = "s3://overturemaps-us-west-2/release"


def _duckdb_connect():
    """Connexion DuckDB prête pour Overture (httpfs + spatial, S3 anonyme us-west-2)."""
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "duckdb requis pour le fetch Overture réel — `pip install duckdb` (ou "
            "deploy.sh l'installe via ops/requirements.txt).") from exc
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    _bound_memory(con)
    return con


def _bound_memory(con) -> None:
    """Contrainte MÉMOIRE (V2-84 — VPS 3,8 Go) : plafond DuckDB, débordement EN FLUX sur
    disque, parallélisme borné, ordre d'insertion libéré (le COPY streame au lieu de
    tamponner). Réglages lus de la config, jamais en dur."""
    from .settings import settings   # noqa: PLC0415 — import différé (pas de cycle)
    tmp = Path(settings.overture_cache_dir) / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}';")
    con.execute(f"SET temp_directory='{tmp}';")
    con.execute(f"SET threads={int(settings.duckdb_threads)};")
    con.execute("SET preserve_insertion_order=false;")


def _release_from_path(path: str) -> str | None:
    """Extrait le segment de release « AAAA-MM-JJ.N » d'un chemin S3 Overture."""
    m = _RELEASE_IN_PATH.search(str(path))
    return m.group(1) if m else None


def latest_overture_release(connect: Callable = _duckdb_connect) -> str:
    """Dernière release Overture disponible sur S3. `connect` injectable (tests)."""
    con = connect()
    try:
        log.info("· détection de la release Overture (listing S3)…")
        rows = con.execute(
            f"SELECT DISTINCT file FROM "
            f"glob('{_OVERTURE_S3}/*/theme=places/type=place/*.parquet')").fetchall()
    finally:
        con.close()
    releases = sorted({rel for (f,) in rows if (rel := _release_from_path(f))})
    if not releases:
        raise RuntimeError(
            "Aucune release Overture détectée sur S3 — passez une release AAAA-MM-JJ.N "
            "explicitement (dernière visible sur overturemaps.org/download).")
    return releases[-1]


def resolve_release(release: str | None,
                    detector: Callable[[], str] = latest_overture_release) -> str:
    """Valide/résout la release. Valeur explicite → doit respecter AAAA-MM-JJ.N ;
    absente → détection de la dernière (réseau)."""
    if release:
        if _RELEASE_RE.match(release):
            return release
        raise ValueError(
            f"release Overture invalide : {release!r}. Format attendu AAAA-MM-JJ.N "
            f"(ex. 2026-08-19.0).")
    return detector()


# ── Fetch bbox DuckDB (production — porte l'id GERS pour `source_ref`) ─────────

def _bbox(lat: float, lon: float, radius_m: int) -> tuple[float, float, float, float]:
    """Bbox (minlon, minlat, maxlon, maxlat) autour d'un point pour un rayon donné."""
    import math
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


# ── V2-79c : le SCHÉMA de la release est DÉTECTÉ, jamais supposé ──────────────
#
# Constat (05/10, release 2026-09-23.1) : Overture a SUPPRIMÉ la colonne `categories`
# (remplacée par `basic_category` + `taxonomy{primary, hierarchy, alternates}`). La
# requête codée en dur `categories.primary` levait une BinderException ; le journal ne
# gardait que le TYPE de l'exception et l'étiquetait « géométrie incompatible » — le
# repli WKB, inutile (la géométrie était native), échouait à son tour, et chaque guide
# se faisait sans Overture. On lit donc le schéma (DESCRIBE, métadonnées parquet
# seulement) et on construit la requête depuis ce qui EXISTE.

def detect_schema(con, src: str) -> dict:
    """Expressions SQL adaptées au schéma RÉEL de la source :
    `{"geom": …, "category": …, "hierarchy": …, "id": …, "columns": {nom: type}}`.
    Géométrie : `geometry` si le type est GEOMETRY (duckdb-spatial récent), sinon
    `ST_GeomFromWKB(geometry)` (WKB/BLOB). Catégorie : `taxonomy.primary` (schéma ≥
    2026-09), sinon `categories.primary` (ancien), sinon `basic_category`, sinon NULL.
    Lève RuntimeError si ni nom ni géométrie ne sont lisibles."""
    cols = {r[0]: str(r[1]) for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{src}', hive_partitioning=1)").fetchall()}
    if "geometry" not in cols or "names" not in cols:
        raise RuntimeError("Schéma Overture inattendu : colonnes 'geometry'/'names' "
                           f"absentes (vues : {', '.join(sorted(cols))})")
    gtype = cols["geometry"].upper()
    geom = "geometry" if gtype.startswith("GEOMETRY") else "ST_GeomFromWKB(geometry)"
    if "taxonomy" in cols:
        category, hierarchy = "taxonomy.primary", "taxonomy.hierarchy"
    elif "categories" in cols:
        category, hierarchy = "categories.primary", "NULL"
    elif "basic_category" in cols:
        category, hierarchy = "basic_category", "NULL"
    else:
        log.warning("Overture : aucune colonne de catégorie reconnue (%s)",
                    ", ".join(sorted(cols)))
        category, hierarchy = "NULL", "NULL"
    return {"geom": geom, "category": category, "hierarchy": hierarchy,
            "id": "id" if "id" in cols else "NULL", "columns": cols}


def _places_src(release: str, source: str | None = None) -> str:
    return source or f"{_OVERTURE_S3}/{release}/theme=places/type=place/*"


def _places_sql(release: str, schema: dict, bbox: tuple,
                source: str | None = None) -> str:
    """Requête Overture `places` — comme le benchmark, PLUS l'`id` GERS (→ `source_ref`
    stable, idempotence de l'upsert) et la HIÉRARCHIE de catégorie (V2-79c). Expressions
    issues de `detect_schema`. `source` surchargeable (parquet local, tests)."""
    minlon, minlat, maxlon, maxlat = bbox
    g = schema["geom"]
    return f"""
        SELECT names.primary AS name,
               ST_Y({g}) AS lat, ST_X({g}) AS lon,
               {schema["category"]} AS category,
               phones[1] AS phone, websites[1] AS website,
               {schema["id"]} AS gers_id,
               {schema["hierarchy"]} AS hierarchy
        FROM read_parquet('{_places_src(release, source)}', filename=true,
                          hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {minlon} AND {maxlon}
          AND bbox.ymin BETWEEN {minlat} AND {maxlat}
        """


def _query_places(con, release: str, bbox: tuple,
                  source: str | None = None) -> list[tuple]:
    """Exécute la requête `places` sur le schéma DÉTECTÉ (V2-79c). En cas d'échec, le
    MESSAGE réel de DuckDB remonte (plus seulement son type — c'est ce qui avait fait
    chercher un problème de géométrie inexistant)."""
    src = _places_src(release, source)
    schema = detect_schema(con, src)
    try:
        return con.execute(_places_sql(release, schema, bbox, source)).fetchall()
    except Exception as exc:  # noqa: BLE001 — message réel, pas seulement le type
        msg = " ".join(str(exc).split())[:300]
        raise RuntimeError(f"Lecture Overture impossible ({type(exc).__name__}) : {msg} "
                           f"[géométrie={schema['geom']}, catégorie="
                           f"{schema['category']}]") from exc


# ── V2-84 : cache par zone (parquet local) ─────────────────────────────────────
#
# Étude V2-83 : une lecture S3 par génération coûte 15 à 92 s (et a lâché le 05/10) ; un
# extrait local de zone pèse ~10 Mo et répond en millisecondes. Une zone = un parquet
# `<cache_dir>/<id>.parquet` + une fiche `<id>.json` (release d'origine, date de
# construction, bbox, nombre de lieux). Zones déclarées en CONFIGURATION.

def load_zones(path: str | Path | None = None) -> list[dict]:
    """Zones du cache (`ops/overture_zones.json`, ou `CASAGUIDE_OVERTURE_ZONES`). Une zone
    mal formée est ignorée avec un avertissement — jamais un crash de génération."""
    from .settings import settings   # noqa: PLC0415
    p = Path(path or settings.overture_zones_file)
    if not p.exists():
        return []
    out = []
    for z in json.loads(p.read_text(encoding="utf-8")).get("zones") or []:
        bbox = z.get("bbox")
        if (isinstance(z.get("id"), str) and re.fullmatch(r"[a-z0-9_]+", z["id"])
                and isinstance(bbox, list) and len(bbox) == 4
                and bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            out.append({"id": z["id"], "name": z.get("name") or z["id"],
                        "bbox": [float(v) for v in bbox]})
        else:
            log.warning("Zone Overture ignorée (mal formée) : %r", z)
    return out


def _cache_paths(zone_id: str, cache_dir: str | Path | None = None) -> tuple[Path, Path]:
    from .settings import settings   # noqa: PLC0415
    d = Path(cache_dir or settings.overture_cache_dir)
    return d / f"{zone_id}.parquet", d / f"{zone_id}.json"


def cache_meta(zone_id: str, cache_dir: str | Path | None = None) -> dict | None:
    """Fiche d'un cache construit (None s'il n'existe pas ou est incomplet)."""
    pq, meta = _cache_paths(zone_id, cache_dir)
    if not (pq.exists() and meta.exists()):
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_zone_cache(zone: dict, *, release: str | None = None,
                     cache_dir: str | Path | None = None,
                     connect: Callable = _duckdb_connect,
                     now: Callable | None = None,
                     source: str | None = None) -> dict:
    """(Re)construit le cache d'une zone : filtre S3 sur la bbox de la zone, ÉCRIT EN FLUX
    un parquet local (COPY … TO, mémoire plafonnée, débordement disque), puis la fiche.
    ATOMIQUE : écrit `*.tmp` puis renomme — une génération concurrente lit l'ancien cache
    entier ou le nouveau entier, jamais un fichier à moitié écrit. Renvoie la fiche."""
    import datetime as _dt   # noqa: PLC0415
    import time as _time     # noqa: PLC0415
    release = release if source else resolve_release(release)
    pq, meta_p = _cache_paths(zone["id"], cache_dir)
    pq.parent.mkdir(parents=True, exist_ok=True)
    tmp = pq.with_suffix(".parquet.tmp")
    x0, y0, x1, y1 = zone["bbox"]
    con = connect()
    t0 = _time.monotonic()
    try:
        con.execute(f"""COPY (SELECT * FROM read_parquet('{_places_src(release, source)}',
                                                          hive_partitioning=1)
                             WHERE bbox.xmin BETWEEN {x0} AND {x1}
                               AND bbox.ymin BETWEEN {y0} AND {y1})
                        TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
    finally:
        con.close()
    tmp.replace(pq)
    built = (now() if now else _dt.datetime.now(_dt.timezone.utc)).isoformat(
        timespec="seconds")
    meta = {"zone": zone["id"], "name": zone["name"], "bbox": zone["bbox"],
            "release": release, "built_at": built, "places": rows,
            "size_bytes": pq.stat().st_size,
            "build_seconds": round(_time.monotonic() - t0, 1)}
    tmp_meta = meta_p.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp_meta.replace(meta_p)
    return meta


def _contains(zone_bbox: list, query_bbox: tuple) -> bool:
    zx0, zy0, zx1, zy1 = zone_bbox
    qx0, qy0, qx1, qy1 = query_bbox
    return zx0 <= qx0 and zy0 <= qy0 and qx1 <= zx1 and qy1 <= zy1


def find_cached_zone(query_bbox: tuple, *, zones: list[dict] | None = None,
                     cache_dir: str | Path | None = None) -> tuple[dict, dict] | None:
    """Zone CONSTRUITE dont la bbox contient ENTIÈREMENT la requête → `(zone, fiche)`.
    Une couverture partielle ne compte pas (des lieux manqueraient en bord de zone) : on
    lit alors S3. La plus petite zone couvrante gagne (la plus spécifique)."""
    best = None
    for z in (zones if zones is not None else load_zones()):
        if not _contains(z["bbox"], query_bbox):
            continue
        meta = cache_meta(z["id"], cache_dir)
        if meta is None:
            continue
        area = (z["bbox"][2] - z["bbox"][0]) * (z["bbox"][3] - z["bbox"][1])
        if best is None or area < best[0]:
            best = (area, z, meta)
    return (best[1], best[2]) if best else None


def cache_age_days(meta: dict, now: Callable | None = None) -> int | None:
    import datetime as _dt   # noqa: PLC0415
    try:
        built = _dt.datetime.fromisoformat(meta["built_at"])
    except (KeyError, TypeError, ValueError):
        return None
    cur = now() if now else _dt.datetime.now(_dt.timezone.utc)
    if built.tzinfo is None:
        built = built.replace(tzinfo=_dt.timezone.utc)
    return max(0, (cur - built).days)


def fetch_places_traced(lat: float, lon: float, radius_m: int, *,
                        release: str | None = None,
                        connect: Callable = _duckdb_connect,
                        zones: list[dict] | None = None,
                        cache_dir: str | Path | None = None,
                        now: Callable | None = None) -> tuple[list[dict], dict]:
    """V2-84 — LECTURE LOCALE D'ABORD : le cache de la zone qui couvre la bbox, sinon S3
    (comportement d'avant, inchangé). Renvoie `(lieux, trace)` ; la trace dit d'où l'on
    a lu (`source` = `cache` | `s3`), la zone, la release, l'âge et si le cache est périmé
    (> `overture_cache_max_age_days`). Une release EXPLICITE (`CASAGUIDE_OVERTURE_RELEASE`)
    différente de celle du cache force la lecture S3 (on lit ce qui est demandé)."""
    from .settings import settings   # noqa: PLC0415
    import time as _time             # noqa: PLC0415
    bbox = _bbox(lat, lon, radius_m)
    hit = find_cached_zone(bbox, zones=zones, cache_dir=cache_dir)
    if hit is not None and (release is None or release == hit[1].get("release")):
        zone, meta = hit
        pq, _ = _cache_paths(zone["id"], cache_dir)
        t0 = _time.monotonic()
        con = connect()
        try:
            rows = _query_places(con, meta.get("release") or "local", bbox, source=str(pq))
        finally:
            con.close()
        age = cache_age_days(meta, now)
        trace = {"source": "cache", "zone": zone["id"], "release": meta.get("release"),
                 "age_days": age,
                 "stale": age is None or age > settings.overture_cache_max_age_days,
                 "seconds": round(_time.monotonic() - t0, 2)}
        return _rows_to_places(rows), trace
    t0 = _time.monotonic()
    release = resolve_release(release)
    con = connect()
    try:
        rows = _query_places(con, release, bbox)
    finally:
        con.close()
    return _rows_to_places(rows), {"source": "s3", "release": release,
                                   "seconds": round(_time.monotonic() - t0, 2)}


def fetch_places(lat: float, lon: float, radius_m: int, *,
                 release: str | None = None,
                 connect: Callable = _duckdb_connect) -> list[dict]:
    """Extraction BBOX du thème `places` d'Overture via DuckDB (pushdown bbox S3). Ne
    télécharge JAMAIS la release complète. Renvoie des dicts prêts pour la fusion :
    `{name, lat, lon, category, phone, website, source_ref}` (`source_ref` = « gers:<id> »).
    Résout la release si absente (réseau). Requiert `duckdb`. (Lecture S3 SEULE — le
    pipeline passe par `fetch_places_traced`, qui lit d'abord le cache de zone.)"""
    release = resolve_release(release)
    con = connect()
    try:
        rows = _query_places(con, release, _bbox(lat, lon, radius_m))
    finally:
        con.close()
    return _rows_to_places(rows)


def _rows_to_places(rows: list[tuple]) -> list[dict]:
    out: list[dict] = []
    for name, plat, plon, category, phone, website, gers, hierarchy in rows:
        if name is None or plat is None or plon is None:
            continue
        out.append({
            "name": name, "lat": float(plat), "lon": float(plon),
            "category": category,
            # V2-79c : ascendance taxonomique (feuille → racine), pour le mapping.
            "category_hierarchy": list(hierarchy) if hierarchy else None,
            "phone": (phone or None), "website": (website or None),
            "source_ref": f"gers:{gers}" if gers else None,
        })
    return out
