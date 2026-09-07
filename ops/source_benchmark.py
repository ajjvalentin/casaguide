#!/usr/bin/env python3
"""Benchmark de SOURCES — Overture Maps vs OSM sur trois terrains (V2-48). LECTURE SEULE.

Décision stratégique : cantonner OSM au factuel géographique (V2-47) et instruire,
chiffres à l'appui, le choix d'une source complémentaire pour les catégories
COMMERCIALES et QUALITATIVES. Candidat : Overture Maps (thème `places`, licence
permissive, hébergeable dans notre PostGIS). Ce script MESURE, il ne décide pas et
n'intègre RIEN.

STRICTEMENT en lecture seule sur la base (coordonnées des 3 propriétés + POI existants
pour le recouvrement). AUCUNE écriture (ni POI, ni `api_costs` — mapping DÉTERMINISTE,
zéro appel LLM). Produit un rapport markdown + un CSV par terrain.

Trois terrains (diversité morphologique + terrain connu) :
  · Villa Ballarin   — urbanisation côtière ES (La Zenia)
  · CASA MURCIA TEST 4 — centre-ville dense ES
  · Villa Ardon      — village alpin CH (Valais), résolu par nom

MÉTHODE (documentée dans le rapport) :
  1. Acquisition Overture par BBOX autour de chaque origine (rayon = max des rayons des
     catégories comparées, plafonné à 25 km). Extraction bbox UNIQUEMENT (jamais la
     release complète) ; garde-fou disque avant écriture. DuckDB (httpfs+spatial) sur les
     parquet S3 Overture, avec pushdown bbox. Le fetch est INJECTABLE → tests sans réseau.
  2. Mapping de taxonomie Overture→nos codes : `ops/overture_category_map.json`
     (artefact réutilisable) ; catégories sans correspondance listées en ANNEXE.
  3. Appariement : deux lieux « le même » si distance < 75 m ET similarité de nom
     (Dice trigrammes ≥ seuil documenté).
  4. Métriques par terrain × catégorie : volumes OSM (notre moisson en base, TOUS
     statuts — cf. CAVEAT) vs Overture ; recouvrement / uniques ; complétude
     (% tél/site/horaires) ; lisibilité des noms (% non génériques).
  5. Sondes qualitatives nominatives (Catedral, Real Casino, ATM bancaires, MUyBICI…).

CAVEAT VOLUME (affiché dans le rapport) : le « volume OSM » est notre moisson EN BASE
(plafonnée à 8/catégorie, arbitrée), PAS l'univers OSM brut → les « uniques Overture »
sont mécaniquement gonflés. La complétude, la lisibilité (par lieu) et les SONDES portent
la décision, pas le volume brut.

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/source_benchmark.py
    …/source_benchmark.py --release 2026-08-19.0        # release Overture explicite
    …/source_benchmark.py --dry-run                     # plan, aucun fetch réseau

La release Overture par défaut est DÉTECTÉE sur S3 (format réel AAAA-MM-JJ.N, ex.
2026-08-19.0 — jamais « AAAA-MM »). Requiert `duckdb` (installé par deploy.sh via
ops/requirements.txt). Charge `backend/.env` (OPS-1).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import dict_row

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                       # ops/
sys.path.insert(0, str(_HERE.parent / "backend"))    # enrich.*
import opsenv  # noqa: E402
from enrich import dedup, overpass  # noqa: E402

log = logging.getLogger("casaguide.source_benchmark")

# Terrains : (clé, nom ou id, résolution). Ballarin/Murcia par id ; Ardon par nom.
TERRAINS = [
    {"key": "ballarin", "label": "Villa Ballarin (La Zenia, ES — côtier)",
     "id": "77294c04-57a7-4aeb-b681-994730f72b43"},
    {"key": "murcia", "label": "CASA MURCIA TEST 4 (centre-ville dense, ES)",
     "id": "6522073e-2cc2-445e-83c6-d1fd60b556f3"},
    {"key": "ardon", "label": "Villa Ardon (Valais, CH — village alpin)",
     "name_like": "%ardon%"},
]

# Catégories COMMERCIALES / QUALITATIVES en périmètre (V2-48). EXCLUES : airport,
# train_station, bus_stop, parking, charging_station, pharmacy, hospital, police
# (OSM y est roi — constat acquis, pas la question).
IN_SCOPE = ("supermarket", "bakery", "atm", "restaurant", "bar", "cafe", "mall",
            "market", "laundry", "sight", "family_activity", "sport", "rental")

# Familles pour la grille de décision (agrégation du rapport).
FAMILIES = {
    "Restauration": ("restaurant", "bar", "cafe"),
    "Commerces & services du quotidien": ("supermarket", "bakery", "atm", "mall",
                                          "market", "laundry"),
    "Tourisme & loisirs": ("sight", "family_activity", "sport"),
    "Location": ("rental",),
}

MATCH_DIST_M = 75.0          # deux lieux « le même » si < 75 m…
NAME_MATCH_THRESHOLD = 0.55  # …ET similarité de nom (Dice trigrammes) ≥ 0,55 (inter-sources :
#                              un même lieu s'écrit un peu différemment d'une source à l'autre,
#                              seuil plus bas que le 0,70 intra-OSM de V2-40).
BBOX_MAX_RADIUS_M = 25000    # plafond du rayon d'extraction bbox (garde-fou volume/disque)
MIN_FREE_DISK_MB = 200       # refuse d'écrire si moins d'espace libre (garde-fou)
MIN_MAPPED_PCT = 30.0        # V2-48c : sous ce seuil de lieux catégorisés reconnus, le
#                              mapping est cassé → échec bruyant (jamais de grille trompeuse).


# ── Mapping de taxonomie (artefact réutilisable) ──────────────────────────────

def load_category_map(path: Path | None = None) -> dict:
    """Charge l'artefact de mapping (V2-48c : structure EXACT + SUFFIXE sur valeurs
    PLATES). Tolère l'ancien format {rules:[{prefix,code}]} (rétrocompat)."""
    path = path or (_HERE / "overture_category_map.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "exact" in data or "suffix" in data:
        return {"exact": {k.lower(): v for k, v in (data.get("exact") or {}).items()},
                "suffix": data.get("suffix") or []}
    # Ancien format à préfixes pointés → converti en suffixes/exacts sur le dernier segment.
    exact: dict[str, str] = {}
    suffix: list[dict] = []
    for r in data.get("rules") or []:
        seg = r["prefix"].rsplit(".", 1)[-1]
        exact[seg] = r["code"]
    return {"exact": exact, "suffix": suffix}


def map_overture_category(primary: str | None, cmap: dict) -> str | None:
    """Valeur Overture `categories.primary` → notre `code`. V2-48c : la taxonomie d'août
    est PLATE (« restaurant », « bank_credit_union »…). On prend le DERNIER segment pointé
    (robuste à l'ancien schéma « eat_and_drink.restaurant » comme au nouveau), puis EXACT
    d'abord (tapas_bar→restaurant avant le suffixe _bar), SUFFIXE ensuite (« *_restaurant »
    → restaurant). None si rien → annexe (jamais tordu)."""
    p = (primary or "").strip().lower()
    if not p:
        return None
    token = p.rsplit(".", 1)[-1]        # « eat_and_drink.restaurant » → « restaurant »
    code = cmap.get("exact", {}).get(token)
    if code:
        return code
    for rule in cmap.get("suffix", []):
        if token.endswith(rule["suffix"]):
            return rule["code"]
    return None


# ── Appariement & métriques (PUR) ─────────────────────────────────────────────

def _name_sim(a: str | None, b: str | None) -> float:
    return dedup._dice(dedup._norm(a), dedup._norm(b))


def places_match(a: dict, b: dict) -> bool:
    """« Le même lieu » : distance < 75 m ET similarité de nom ≥ seuil (V2-48)."""
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return False
    d = overpass.haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    return d < MATCH_DIST_M and _name_sim(a.get("name"), b.get("name")) >= NAME_MATCH_THRESHOLD


def match_sets(osm: list[dict], overture: list[dict]) -> dict:
    """Recouvrement entre deux jeux d'une même catégorie. Appariement glouton (chaque
    lieu apparié au plus une fois). Renvoie {overlap, osm_unique, overture_unique}."""
    used_ovt: set[int] = set()
    overlap = 0
    for o in osm:
        for j, v in enumerate(overture):
            if j in used_ovt:
                continue
            if places_match(o, v):
                used_ovt.add(j)
                overlap += 1
                break
    return {"overlap": overlap,
            "osm_unique": len(osm) - overlap,
            "overture_unique": len(overture) - len(used_ovt)}


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def completeness(places: list[dict]) -> dict:
    """% de lieux avec téléphone / site / horaires (métadonnées)."""
    n = len(places)
    return {
        "phone_pct": _pct(sum(1 for p in places if (p.get("phone") or "").strip()), n),
        "website_pct": _pct(sum(1 for p in places if (p.get("website") or "").strip()), n),
        "hours_pct": _pct(sum(1 for p in places if p.get("has_hours")), n),
    }


def readability(places: list[dict]) -> float:
    """% de noms NON génériques (heuristique V2-44 `is_generic_name`, documentée)."""
    n = len(places)
    non_generic = sum(1 for p in places if not overpass.is_generic_name(p.get("name")))
    return _pct(non_generic, n)


@dataclass
class CatMetrics:
    code: str
    osm: list[dict]
    overture: list[dict]
    overlap: int = 0
    osm_unique: int = 0
    overture_unique: int = 0
    osm_completeness: dict = field(default_factory=dict)
    ovt_completeness: dict = field(default_factory=dict)
    osm_readability: float = 0.0
    ovt_readability: float = 0.0
    recommendation: str = ""


def compute_category_metrics(code: str, osm: list[dict], overture: list[dict]) -> CatMetrics:
    m = CatMetrics(code=code, osm=osm, overture=overture)
    match = match_sets(osm, overture)
    m.overlap, m.osm_unique, m.overture_unique = (
        match["overlap"], match["osm_unique"], match["overture_unique"])
    m.osm_completeness, m.ovt_completeness = completeness(osm), completeness(overture)
    m.osm_readability, m.ovt_readability = readability(osm), readability(overture)
    m.recommendation = recommend(m)
    return m


def recommend(m: CatMetrics) -> str:
    """Recommandation SUGGÉRÉE (heuristique documentée — André tranche). Pondère volume,
    apports uniques et complétude/lisibilité, en tenant compte du CAVEAT volume (OSM
    plafonné) : on ne conclut « Overture remplace » que si OSM est réellement vide."""
    n_osm, n_ovt = len(m.osm), len(m.overture)
    ovt_richer = (m.ovt_completeness.get("phone_pct", 0)
                  + m.ovt_completeness.get("website_pct", 0)
                  > m.osm_completeness.get("phone_pct", 0)
                  + m.osm_completeness.get("website_pct", 0))
    if n_osm == 0 and n_ovt == 0:
        return "web-discovery (ni OSM ni Overture ne portent la connaissance)"
    if n_osm == 0 and n_ovt > 0:
        return "Overture remplace (OSM vide, Overture présent)"
    if m.overture_unique >= max(3, n_osm) and ovt_richer:
        return "Overture complète (apports uniques nombreux + métadonnées plus riches)"
    if m.overture_unique > 0 and ovt_richer:
        return "Overture complète (fusion avec dédup inter-sources)"
    if n_ovt <= n_osm and not ovt_richer:
        return "OSM seul (Overture n'ajoute rien de décisif)"
    return "à arbitrer (signaux mitigés — voir métriques)"


# ── Sondes qualitatives nominatives ───────────────────────────────────────────

def _name_present(places: list[dict], needle: str) -> list[str]:
    """Lieux dont le nom CONTIENT le motif normalisé (substring — sonde large/brute)."""
    n = dedup._norm(needle)
    return [p["name"] for p in places
            if p.get("name") and n in dedup._norm(p.get("name"))]


def _name_match_exact_first(places: list[dict], needle: str) -> list[str]:
    """Sonde nominative RESSERRÉE (V2-48c) : exact-d'abord. On cherche d'abord une
    ÉGALITÉ de nom normalisé, sinon un nom qui contient le motif comme SÉQUENCE DE TOKENS
    contiguë (phrase). Évite les faux positifs « Catedral Consultores » / « Gran Casino de
    Ceuta » qu'un simple substring remontait."""
    want = dedup._norm(needle)
    want_toks = want.split()
    exact = [p["name"] for p in places if dedup._norm(p.get("name")) == want]
    if exact:
        return exact
    out = []
    for p in places:
        toks = dedup._norm(p.get("name")).split()
        # phrase : les tokens du motif apparaissent contigus dans le nom.
        if any(toks[i:i + len(want_toks)] == want_toks
               for i in range(len(toks) - len(want_toks) + 1)) and p.get("name"):
            out.append(p["name"])
    return out


def run_probes(terrain_key: str, osm: list[dict], overture: list[dict],
               cmap: dict) -> list[dict]:
    """Sondes nominatives par terrain (le terrain connu sert à trancher). Renvoie une
    liste de {question, answer}. Appariement EXACT-D'ABORD (V2-48c)."""
    probes: list[dict] = []
    ovt_exact = lambda needle: _name_match_exact_first(overture, needle)  # noqa: E731

    if terrain_key == "murcia":
        cat = ovt_exact("Catedral de Murcia") or ovt_exact("Santa Iglesia Catedral de Murcia")
        probes.append({"question": "Catedral de Murcia dans Overture ?",
                       "answer": ("OUI — " + ", ".join(cat[:3])) if cat else "NON"})
        casino = ovt_exact("Real Casino de Murcia") or ovt_exact("Casino de Murcia")
        probes.append({"question": "Real Casino de Murcia dans Overture ?",
                       "answer": ("OUI — " + ", ".join(casino[:3])) if casino else "NON"})
        # ATM bancaires vs crypto : les banques Overture (bank_credit_union → atm)
        # répondent au crypto-only d'OSM (V2-48c : sonde corrigée, lit le mapping atm).
        atm_ovt = [p for p in overture
                   if map_overture_category(p.get("category"), cmap) == "atm"]
        crypto = [p["name"] for p in atm_ovt
                  if overpass._is_crypto_atm({"name": p.get("name") or "",
                                              "operator": p.get("operator") or ""})]
        banks = [p for p in atm_ovt if p["name"] not in crypto]
        probes.append({"question": "Distributeurs : banques nommées dans Overture "
                       "(là où OSM ne montrait que du crypto) ?",
                       "answer": f"{len(banks)} banque(s) / {len(crypto)} crypto sur "
                                 f"{len(atm_ovt)} ATM Overture"})
        muy = _name_present(overture, "muybici") or _name_present(overture, "muy bici")
        probes.append({"question": "MUyBICI : granularité Overture (station ou système) ?",
                       "answer": (f"{len(muy)} entrée(s)" if muy else "absent d'Overture")})
    elif terrain_key == "ballarin":
        resto_ovt = [p for p in overture
                     if map_overture_category(p.get("category"), cmap) == "restaurant"]
        resto_osm = [p for p in osm if p.get("category_code") == "restaurant"]
        probes.append({"question": "Restaurants La Zenia : Overture vs notre moisson ?",
                       "answer": f"{len(resto_ovt)} Overture / {len(resto_osm)} en base"})
    elif terrain_key == "ardon":
        # Verdict fondé sur les lieux MAPPÉS EN PÉRIMÈTRE (V2-48c : plus sur un mapping vide).
        mapped = [p for p in overture
                  if map_overture_category(p.get("category"), cmap) is not None]
        probes.append({"question": "Tenue en zone rurale alpine (Overture réputé urbain) ?",
                       "answer": f"{len(mapped)} lieu(x) Overture en périmètre sur "
                                 f"{len(overture)} bruts — "
                                 f"{'faible' if len(mapped) < 20 else 'correcte'}"})
    return probes


# ── Acquisition Overture (DuckDB) — INJECTABLE ───────────────────────────────

def _bbox(lat: float, lon: float, radius_m: int) -> tuple[float, float, float, float]:
    """Bbox (minlon, minlat, maxlon, maxlat) autour d'un point pour un rayon donné."""
    dlat = radius_m / 111_320.0
    import math
    dlon = radius_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


# Format RÉEL d'une release Overture sur S3 : AAAA-MM-JJ.N (ex. 2026-08-19.0) — PAS
# « AAAA-MM » (V2-48b : l'ancien défaut ne matchait aucun chemin S3).
_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_RELEASE_IN_PATH = re.compile(r"/release/(\d{4}-\d{2}-\d{2}\.\d+)/")
_OVERTURE_S3 = "s3://overturemaps-us-west-2/release"


def _duckdb_connect():
    """Connexion DuckDB prête pour Overture (httpfs + spatial, S3 anonyme us-west-2).
    Message explicite si `duckdb` manque (installé par deploy.sh via ops/requirements.txt)."""
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "duckdb requis pour le fetch Overture réel — `pip install duckdb` (ou "
            "deploy.sh l'installe désormais via ops/requirements.txt)." ) from exc
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    return con


def _release_from_path(path: str) -> str | None:
    """Extrait le segment de release « AAAA-MM-JJ.N » d'un chemin S3 Overture, où qu'il
    soit (« …/release/2026-08-19.0/theme=places/… »)."""
    m = _RELEASE_IN_PATH.search(str(path))
    return m.group(1) if m else None


def latest_overture_release(connect: Callable = _duckdb_connect) -> str:
    """Dernière release Overture disponible sur S3. `connect` injectable (tests).

    V2-48c : le glob SHALLOW `release/*` renvoyait 0 ligne (« Aucune release détectée »)
    car S3 n'a AUCUN objet à ce niveau — seulement des sous-préfixes ; DuckDB `glob`
    liste des OBJETS. On globe donc les fichiers du thème `places` et on EXTRAIT le
    segment de release (Python-side, distinct)."""
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
            "Aucune release Overture détectée sur S3 — passez --release AAAA-MM-JJ.N "
            "explicitement (dernière release visible sur overturemaps.org/download).")
    return releases[-1]


def resolve_release(release: str | None,
                    detector: Callable[[], str] = latest_overture_release) -> str:
    """Valide/résout la release. Une valeur explicite DOIT respecter « AAAA-MM-JJ.N »
    (message d'erreur montrant le format sinon) ; absente → détection de la dernière."""
    if release:
        if _RELEASE_RE.match(release):
            return release
        raise ValueError(
            f"--release invalide : {release!r}. Format attendu AAAA-MM-JJ.N "
            f"(ex. 2026-08-19.0). Omettez --release pour détecter automatiquement la "
            f"dernière release disponible sur S3.")
    return detector()


def _places_sql(release: str, geom_expr: str, bbox: tuple,
                source: str | None = None) -> str:
    """Requête Overture `places` pour une expression de géométrie donnée (V2-48b :
    `geometry` natif d'abord, repli `ST_GeomFromWKB(geometry)` pour les vieux
    duckdb-spatial). `source` (chemin `read_parquet`) surchargeable pour les tests
    (parquet local au lieu du chemin S3 de la release)."""
    minlon, minlat, maxlon, maxlat = bbox
    src = source or f"{_OVERTURE_S3}/{release}/theme=places/type=place/*"
    return f"""
        SELECT names.primary AS name,
               ST_Y({geom_expr}) AS lat, ST_X({geom_expr}) AS lon,
               categories.primary AS category,
               phones[1] AS phone, websites[1] AS website
        FROM read_parquet('{src}', filename=true, hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {minlon} AND {maxlon}
          AND bbox.ymin BETWEEN {minlat} AND {maxlat}
        """


def _query_places(con, release: str, bbox: tuple,
                  source: str | None = None) -> list[tuple]:
    """Exécute la requête `places` en essayant la géométrie NATIVE (duckdb-spatial
    récent expose `geometry` en GEOMETRY) puis, en repli, le WKB (`ST_GeomFromWKB`).
    V2-48b : `ST_GeomFromWKB(geometry)` échouait sur duckdb 1.5.5 (geometry natif)."""
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


def fetch_overture_duckdb(lat: float, lon: float, radius_m: int,
                          release: str) -> list[dict]:
    """Extraction BBOX du thème `places` d'Overture via DuckDB (httpfs+spatial), pushdown
    bbox sur les parquet S3. Garde-fou disque AVANT toute écriture. Ne télécharge JAMAIS
    la release complète (filtre bbox poussé dans la requête). Requiert `duckdb`."""
    free_mb = shutil.disk_usage(_HERE).free / (1024 * 1024)
    if free_mb < MIN_FREE_DISK_MB:
        raise RuntimeError(f"Espace disque insuffisant ({free_mb:.0f} Mo < "
                           f"{MIN_FREE_DISK_MB} Mo) — extraction refusée.")
    con = _duckdb_connect()
    try:
        rows = _query_places(con, release, _bbox(lat, lon, radius_m))
    finally:
        con.close()
    out: list[dict] = []
    for name, plat, plon, category, phone, website in rows:
        out.append({"name": name, "lat": plat, "lon": plon, "category": category,
                    "phone": phone, "website": website, "has_hours": False,
                    "operator": None})
    return out


# ── Lecture DB (SELECT seul) ─────────────────────────────────────────────────

def resolve_terrain(conn, terrain: dict) -> dict | None:
    if terrain.get("id"):
        row = conn.execute(
            "SELECT id::text AS id, name, city, country_code, ST_Y(geom) AS lat, "
            "ST_X(geom) AS lon FROM properties WHERE id = %s",
            (terrain["id"],)).fetchone()
    else:
        row = conn.execute(
            "SELECT id::text AS id, name, city, country_code, ST_Y(geom) AS lat, "
            "ST_X(geom) AS lon FROM properties WHERE name ILIKE %s "
            "AND geom IS NOT NULL ORDER BY created_at LIMIT 1",
            (terrain["name_like"],)).fetchone()
    return row


def load_osm_pois(conn, property_id: str) -> list[dict]:
    """POI OSM en base (TOUS statuts, source osm) — l'univers moissonné existant."""
    return conn.execute(
        """SELECT name, category_code, ST_Y(geom) AS lat, ST_X(geom) AS lon,
                  phone, website, (opening_hours IS NOT NULL) AS has_hours, status
           FROM pois WHERE property_id = %s AND source = 'osm'""",
        (property_id,)).fetchall()


def category_radii(conn) -> dict[str, int]:
    return {r["code"]: r["default_radius_m"] for r in conn.execute(
        "SELECT code, default_radius_m FROM poi_categories").fetchall()}


# ── Orchestration (aucune écriture) ───────────────────────────────────────────

def run_terrain(prop: dict, osm_all: list[dict], overture_all: list[dict],
                radii: dict[str, int], cmap: dict) -> dict:
    """Calcule métriques + sondes pour un terrain. PUR (données déjà chargées/fetchées)."""
    plat, plon = prop["lat"], prop["lon"]
    # Overture mappé à nos codes.
    for v in overture_all:
        v["_code"] = map_overture_category(v.get("category"), cmap)
        v["_dist"] = (overpass.haversine_m(plat, plon, v["lat"], v["lon"])
                      if v.get("lat") is not None else None)
    cats: list[CatMetrics] = []
    for code in IN_SCOPE:
        radius = radii.get(code, 10000)
        osm = [p for p in osm_all if p["category_code"] == code]
        ovt = [v for v in overture_all
               if v.get("_code") == code and v.get("_dist") is not None
               and v["_dist"] <= radius]
        cats.append(compute_category_metrics(code, osm, ovt))
    probes = run_probes(prop["_key"], osm_all, overture_all, cmap)
    # Annexe : catégories Overture rencontrées SANS correspondance (top occurrences).
    unmapped: dict[str, int] = {}
    for v in overture_all:
        if v.get("_code") is None and v.get("category"):
            unmapped[v["category"]] = unmapped.get(v["category"], 0) + 1
    # Métrique de SANTÉ du mapping (V2-48c) : % de lieux CATÉGORISÉS que l'on reconnaît.
    # Dénominateur = lieux à catégorie non nulle (un lieu sans catégorie ne prouve rien).
    # 0 % = mapping cassé (l'artefact du run V2-48). Le garde-fou vit dans run_benchmark.
    with_cat = [v for v in overture_all if (v.get("category") or "").strip()]
    mapped = [v for v in with_cat if v.get("_code") is not None]
    mapped_pct = _pct(len(mapped), len(with_cat))
    return {"prop": prop, "cats": cats, "probes": probes, "unmapped": unmapped,
            "overture_total": len(overture_all), "overture_with_cat": len(with_cat),
            "overture_mapped": len(mapped), "mapped_pct": mapped_pct}


def _family_of(code: str) -> str:
    for fam, codes in FAMILIES.items():
        if code in codes:
            return fam
    return "Autres"


def render_report(results: list[dict], release: str, when: str) -> str:
    L: list[str] = []
    L.append("# Benchmark de sources — Overture Maps vs OSM (V2-48)")
    L.append("")
    L.append(f"- Release Overture : `{release}` · exécuté le {when}")
    L.append(f"- Appariement : distance < {int(MATCH_DIST_M)} m ET similarité de nom "
             f"(Dice trigrammes) ≥ {NAME_MATCH_THRESHOLD}.")
    L.append(f"- Périmètre : {', '.join(IN_SCOPE)} (exclus : airport/train/pharmacy/"
             f"hospital/police — OSM y est roi).")
    L.append("")
    L.append("> **CAVEAT VOLUME** : le « volume OSM » est notre MOISSON EN BASE "
             "(plafonnée à 8/catégorie, tous statuts), pas l'univers OSM brut → les "
             "« uniques Overture » sont mécaniquement gonflés. La **complétude**, la "
             "**lisibilité** (par lieu) et les **sondes** portent la décision.")
    L.append("")
    for r in results:
        p = r["prop"]
        L.append(f"## {p['_label']}")
        L.append(f"`{p['id']}` — {p.get('city') or '?'} ({p.get('country_code') or '?'})")
        # Santé du mapping (V2-48c) : % de lieux catégorisés reconnus (0 % = mapping cassé).
        L.append(f"- **Santé du mapping : {r.get('mapped_pct', 0)} %** de lieux "
                 f"catégorisés reconnus ({r.get('overture_mapped', 0)}/"
                 f"{r.get('overture_with_cat', 0)} ; {r.get('overture_total', 0)} bruts). "
                 f"Seuil d'alerte : {MIN_MAPPED_PCT:.0f} %.")
        L.append("")
        L.append("| Catégorie | OSM | Overture | Recouvr. | Uniq. OSM | Uniq. Ovt | "
                 "Tél OSM/Ovt | Site OSM/Ovt | Horaires OSM/Ovt | Lisib. OSM/Ovt | Reco |")
        L.append("|---|--:|--:|--:|--:|--:|---|---|---|---|---|")
        for c in r["cats"]:
            oc, vc = c.osm_completeness, c.ovt_completeness
            L.append(
                f"| {c.code} | {len(c.osm)} | {len(c.overture)} | {c.overlap} | "
                f"{c.osm_unique} | {c.overture_unique} | "
                f"{oc['phone_pct']}/{vc['phone_pct']} % | "
                f"{oc['website_pct']}/{vc['website_pct']} % | "
                f"{oc['hours_pct']}/{vc['hours_pct']} % | "
                f"{c.osm_readability}/{c.ovt_readability} % | {c.recommendation} |")
        L.append("")
        if r["probes"]:
            L.append("**Sondes qualitatives**")
            for pr in r["probes"]:
                L.append(f"- {pr['question']} → **{pr['answer']}**")
            L.append("")
        if r["unmapped"]:
            top = sorted(r["unmapped"].items(), key=lambda kv: -kv[1])[:15]
            L.append("<details><summary>Annexe : catégories Overture sans "
                     "correspondance (top 15)</summary>")
            L.append("")
            for cat, n in top:
                L.append(f"- `{cat}` × {n}")
            L.append("")
            L.append("</details>")
            L.append("")
    # Grille de décision agrégée par famille (recommandation majoritaire).
    L.append("## Grille de décision (agrégée par famille)")
    L.append("")
    L.append("| Famille | Recommandation dominante (terrains × catégories) |")
    L.append("|---|---|")
    for fam in FAMILIES:
        tally: dict[str, int] = {}
        for r in results:
            for c in r["cats"]:
                if _family_of(c.code) == fam:
                    head = c.recommendation.split(" (")[0]
                    tally[head] = tally.get(head, 0) + 1
        if tally:
            dominant = max(tally.items(), key=lambda kv: kv[1])
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(tally.items(),
                                                             key=lambda kv: -kv[1]))
            L.append(f"| {fam} | **{dominant[0]}** ({detail}) |")
    L.append("")
    L.append("*Recommandations SUGGÉRÉES (heuristique `recommend`, documentée) — aide à "
             "la décision, aucune intégration production dans cette mission.*")
    L.append("")
    return "\n".join(L)


def render_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["terrain", "category", "osm_count", "overture_count", "overlap",
                "osm_unique", "overture_unique", "osm_phone_pct", "ovt_phone_pct",
                "osm_website_pct", "ovt_website_pct", "osm_hours_pct", "ovt_hours_pct",
                "osm_readability_pct", "ovt_readability_pct", "recommendation"])
    key = result["prop"]["_key"]
    for c in result["cats"]:
        oc, vc = c.osm_completeness, c.ovt_completeness
        w.writerow([key, c.code, len(c.osm), len(c.overture), c.overlap, c.osm_unique,
                    c.overture_unique, oc["phone_pct"], vc["phone_pct"],
                    oc["website_pct"], vc["website_pct"], oc["hours_pct"], vc["hours_pct"],
                    c.osm_readability, c.ovt_readability, c.recommendation])
    return buf.getvalue()


class MappingHealthError(RuntimeError):
    """Le mapping Overture reconnaît trop peu de lieux → grille trompeuse (V2-48c)."""


def check_mapping_health(results: list[dict], min_pct: float = MIN_MAPPED_PCT) -> None:
    """Garde-fou V2-48c : si un terrain mappe moins de `min_pct` % de ses lieux
    CATÉGORISÉS, on ÉCHOUE BRUYAMMENT plutôt que de produire une grille trompeuse (le run
    V2-48 mappait 0 % — mapping contre l'ancienne taxonomie). Lève `MappingHealthError`."""
    bad = [r for r in results if r["overture_with_cat"] and r["mapped_pct"] < min_pct]
    if bad:
        detail = " ; ".join(f"{r['prop']['_key']} {r['mapped_pct']}% "
                            f"({r['overture_mapped']}/{r['overture_with_cat']})"
                            for r in bad)
        raise MappingHealthError(
            f"Mapping Overture insuffisant (< {min_pct:.0f} % de lieux catégorisés "
            f"reconnus) : {detail}. La taxonomie a probablement changé — mettez à jour "
            f"ops/overture_category_map.json (cf. annexes du rapport) avant de conclure.")


def run_benchmark(conn, fetch: Callable[[float, float, int, str], list[dict]], *,
                  release: str, out_dir: Path, when: str | None = None) -> str:
    """Orchestration : résout les terrains, charge l'OSM (base), fetch Overture (injecté),
    calcule, écrit rapport + CSV. Renvoie le chemin du rapport. AUCUNE écriture DB.
    Lève `MappingHealthError` (avant d'écrire) si le mapping est manifestement cassé."""
    cmap = load_category_map()
    radii = category_radii(conn)
    max_radius = min(BBOX_MAX_RADIUS_M, max(radii.get(c, 10000) for c in IN_SCOPE))
    results: list[dict] = []
    for terrain in TERRAINS:
        prop = resolve_terrain(conn, terrain)
        if prop is None or prop.get("lat") is None:
            log.warning("⚠ terrain non résolu / non positionné : %s", terrain["key"])
            continue
        prop["_key"], prop["_label"] = terrain["key"], terrain["label"]
        osm = load_osm_pois(conn, prop["id"])
        log.info("· %s : %d POI OSM en base ; fetch Overture (rayon %d m)…",
                 terrain["key"], len(osm), max_radius)
        overture = fetch(prop["lat"], prop["lon"], max_radius, release)
        r = run_terrain(prop, osm, overture, radii, cmap)
        log.info("· %s : %d lieux Overture, %d catégorisés, %.1f %% mappés",
                 terrain["key"], r["overture_total"], r["overture_with_cat"],
                 r["mapped_pct"])
        results.append(r)

    # Garde-fou de santé AVANT d'écrire quoi que ce soit (jamais de grille trompeuse).
    check_mapping_health(results)

    when = when or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    report = render_report(results, release, when)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.date.today().isoformat()
    report_path = out_dir / f"source_benchmark_{stamp}.md"
    report_path.write_text(report, encoding="utf-8")
    for r in results:
        csv_path = out_dir / f"source_benchmark_{r['prop']['_key']}_{stamp}.csv"
        csv_path.write_text(render_csv(r), encoding="utf-8")
        log.info("· CSV → %s", csv_path)
    return str(report_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark de sources Overture vs OSM sur 3 terrains (V2-48, "
                    "lecture seule).")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--release", default=None,
                        help="release Overture AAAA-MM-JJ.N (ex. 2026-08-19.0). À défaut, "
                             "la dernière disponible est détectée sur S3.")
    parser.add_argument("--out", default=None, help="répertoire de sortie (défaut : ops/).")
    parser.add_argument("--dry-run", action="store_true",
                        help="résout les terrains et affiche le plan, AUCUN fetch réseau.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    dsn = args.dsn or os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        if args.dry_run:
            for terrain in TERRAINS:
                prop = resolve_terrain(conn, terrain)
                where = (f"{prop['lat']:.4f},{prop['lon']:.4f}"
                         if prop and prop.get("lat") is not None else "NON RÉSOLU")
                log.info("· %s → %s", terrain["key"], where)
            log.info("· DRY-RUN : aucun fetch Overture, aucun fichier écrit.")
            return 0
        try:
            release = resolve_release(args.release)
        except ValueError as exc:      # format explicite invalide
            log.error("✗ %s", exc)
            return 3
        except RuntimeError as exc:    # détection impossible (réseau/duckdb)
            log.error("✗ détection de la release Overture impossible : %s", exc)
            return 4
        log.info("· release Overture : %s", release)
        out_dir = Path(args.out) if args.out else _HERE
        try:
            path = run_benchmark(conn, fetch_overture_duckdb, release=release,
                                 out_dir=out_dir)
        except MappingHealthError as exc:   # V2-48c : mapping cassé → échec bruyant
            log.error("✗ %s", exc)
            return 5
    log.info("✔ rapport → %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
