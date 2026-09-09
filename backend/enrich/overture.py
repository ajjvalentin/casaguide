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
    return con


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


def _places_sql(release: str, geom_expr: str, bbox: tuple,
                source: str | None = None) -> str:
    """Requête Overture `places` — comme le benchmark, PLUS l'`id` GERS (→ `source_ref`
    stable, idempotence de l'upsert). `source` surchargeable (parquet local, tests)."""
    minlon, minlat, maxlon, maxlat = bbox
    src = source or f"{_OVERTURE_S3}/{release}/theme=places/type=place/*"
    return f"""
        SELECT names.primary AS name,
               ST_Y({geom_expr}) AS lat, ST_X({geom_expr}) AS lon,
               categories.primary AS category,
               phones[1] AS phone, websites[1] AS website,
               id AS gers_id
        FROM read_parquet('{src}', filename=true, hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {minlon} AND {maxlon}
          AND bbox.ymin BETWEEN {minlat} AND {maxlat}
        """


def _query_places(con, release: str, bbox: tuple,
                  source: str | None = None) -> list[tuple]:
    """Exécute la requête `places` — géométrie NATIVE d'abord, repli WKB (V2-48b)."""
    errors = []
    for geom_expr in ("geometry", "ST_GeomFromWKB(geometry)"):
        try:
            return con.execute(_places_sql(release, geom_expr, bbox, source)).fetchall()
        except Exception as exc:  # noqa: BLE001 — incompat de type geometry → repli
            errors.append(f"{geom_expr}: {type(exc).__name__}")
            log.info("· géométrie « %s » incompatible (%s) — repli…",
                     geom_expr, type(exc).__name__)
    raise RuntimeError("Lecture de la géométrie Overture impossible (natif ET WKB) : "
                       + " ; ".join(errors))


def fetch_places(lat: float, lon: float, radius_m: int, *,
                 release: str | None = None,
                 connect: Callable = _duckdb_connect) -> list[dict]:
    """Extraction BBOX du thème `places` d'Overture via DuckDB (pushdown bbox S3). Ne
    télécharge JAMAIS la release complète. Renvoie des dicts prêts pour la fusion :
    `{name, lat, lon, category, phone, website, source_ref}` (`source_ref` = « gers:<id> »).
    Résout la release si absente (réseau). Requiert `duckdb`."""
    release = resolve_release(release)
    con = connect()
    try:
        rows = _query_places(con, release, _bbox(lat, lon, radius_m))
    finally:
        con.close()
    out: list[dict] = []
    for name, plat, plon, category, phone, website, gers in rows:
        if name is None or plat is None or plon is None:
            continue
        out.append({
            "name": name, "lat": float(plat), "lon": float(plon),
            "category": category,
            "phone": (phone or None), "website": (website or None),
            "source_ref": f"gers:{gers}" if gers else None,
        })
    return out
