"""Fusion inter-sources OSM ↔ Overture (V2-52 volet 1) — passe PURE et testable.

Décision de sources (benchmark 2026-09-09, trois terrains) : **OSM le factuel,
Overture le commercial**. Ce module porte les trois gains SÛRS et bornés du volet 1,
tous en mémoire (aucune écriture, aucun réseau) — l'appelant (pipeline) fait le reste
(distances OSRM, plafonds V2-44, dédup V2-40, règles V2-50, upsert) :

  1. **Appariement inter-sources** (`same_place`) — le matcher éprouvé au benchmark
     (V2-48) : distance < 75 m ET similarité de nom (Dice trigrammes) ≥ 0,55. Un même
     lieu s'écrit un peu différemment d'une source à l'autre → seuil plus bas que le
     0,70 intra-OSM de la dédup V2-40.

  2. **Enrichissement de contacts** (`enrich_osm_contacts`, gain 3) — un POI OSM
     retenu par la moisson normale reçoit tél/site d'Overture quand l'appariement est
     sûr. **Politique de confiance par champ** : nom/position/horaires restent OSM
     (curation communautaire, précision contributive, horaires OSM-only) ; seuls
     tél/site manquants sont comblés depuis Overture (prioritaire quand il les a).
     La provenance est tracée dans `completion_meta._overture` (jamais un écrasement).

  3. **Candidats de comblement** (`build_fill_candidates`, gains 1 & 2) — banques
     Overture pour `atm`, et candidats manquants quand une catégorie commerciale est
     vide/sous le minimum (famine rurale résolue par la SOURCE, pas par le rayon). Ces
     candidats entrent avec `source='overture'`, `source_ref='gers:<id>'`, et sont
     ensuite SOUMIS À TOUTE LA CHAÎNE existante par l'appelant.

Duplication assumée avec le benchmark : le benchmark (`ops/source_benchmark.py`)
importe `same_place`/`name_similarity` d'ici — une seule source de vérité du matcher.
"""
from __future__ import annotations

from . import dedup, overpass

# ── Seuils inter-sources (benchmark V2-48) ───────────────────────────────────
MATCH_DIST_M = 75.0          # deux lieux « le même » inter-sources si < 75 m…
NAME_SIM_THRESHOLD = 0.55    # …ET similarité de nom (Dice trigrammes) ≥ 0,55.

# Catégories COMMERCIALES en périmètre volet 1 (le factuel géographique et le
# volumique restent hors sujet — cf. brief). Le comblement (gain 2) et l'appariement
# (gain 3) ne concernent QUE ces codes ; `atm` est en plus AUGMENTÉ (gain 1, banques).
SCOPE: frozenset[str] = frozenset((
    "supermarket", "bakery", "atm", "restaurant", "bar", "cafe", "mall", "market",
    "laundry", "rental"))

# Champs comblés par appariement (gain 3). Nom/position/horaires restent OSM.
_CONTACT_FIELDS = ("phone", "website")


# ── Appariement (partagé avec le benchmark) ──────────────────────────────────

def name_similarity(a: str | None, b: str | None) -> float:
    """Similarité de nom inter-sources : Dice sur trigrammes du nom normalisé ∈ [0,1]."""
    return dedup._dice(dedup._norm(a), dedup._norm(b))


def same_place(a: dict, b: dict, *, dist_m: float = MATCH_DIST_M,
               name_thr: float = NAME_SIM_THRESHOLD) -> bool:
    """« Le même lieu » entre deux sources : distance < seuil ET similarité de nom ≥
    seuil (V2-48). Faux si une coordonnée manque."""
    la, lo, lb, lob = a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")
    if None in (la, lo, lb, lob):
        return False
    if overpass.haversine_m(la, lo, lb, lob) >= dist_m:
        return False
    return name_similarity(a.get("name"), b.get("name")) >= name_thr


def _nonempty(v) -> bool:
    return bool(v.strip()) if isinstance(v, str) else v is not None


# ── Gain 3 : enrichissement de contacts par appariement ──────────────────────

def _fill_contacts(osm: dict, ovt: dict, today: str) -> bool:
    """Comble les contacts NULL du POI OSM depuis l'Overture apparié (jamais un
    écrasement). Trace la provenance dans `completion_meta._overture`. Renvoie True si
    au moins un champ a été comblé."""
    filled: list[str] = []
    for f in _CONTACT_FIELDS:
        if not _nonempty(osm.get(f)) and _nonempty(ovt.get(f)):
            osm[f] = ovt[f]
            filled.append(f)
    if not filled:
        return False
    meta = dict(osm.get("completion_meta") or {})
    mark = dict(meta.get("_overture") or {})
    mark["source_ref"] = ovt.get("source_ref")
    mark["verified_on"] = today
    mark["fields"] = sorted(set((mark.get("fields") or []) + filled))
    meta["_overture"] = mark
    osm["completion_meta"] = meta
    return True


def enrich_osm_contacts(osm_pois: list[dict], overture: list[dict],
                        *, today: str) -> tuple[list[dict], int, set[int]]:
    """Gain 3. Pour chaque POI OSM, cherche un Overture apparié (le plus proche non
    encore consommé) et comble ses contacts manquants. Renvoie `(pois, n_enrichis,
    indices Overture consommés)`. Un Overture apparié est « consommé » MÊME s'il n'a rien
    apporté (c'est le même lieu → il ne doit pas être re-proposé au comblement, gain 2)."""
    consumed: set[int] = set()
    enriched = 0
    for p in osm_pois:
        best_j = _nearest_match(p, overture, consumed)
        if best_j is None:
            continue
        consumed.add(best_j)
        if _fill_contacts(p, overture[best_j], today):
            enriched += 1
    return osm_pois, enriched, consumed


def _nearest_match(p: dict, overture: list[dict], consumed: set[int]) -> int | None:
    """Index de l'Overture apparié le plus proche (non consommé), ou None."""
    best_j, best_d = None, None
    la, lo = p.get("lat"), p.get("lon")
    if la is None or lo is None:
        return None
    for j, v in enumerate(overture):
        if j in consumed or v.get("lat") is None or v.get("lon") is None:
            continue
        d = overpass.haversine_m(la, lo, v["lat"], v["lon"])
        if d >= MATCH_DIST_M:
            continue
        if name_similarity(p.get("name"), v.get("name")) < NAME_SIM_THRESHOLD:
            continue
        if best_d is None or d < best_d:
            best_j, best_d = j, d
    return best_j


# ── Gains 1 & 2 : candidats de comblement Overture ───────────────────────────

def build_fill_candidates(overture: list[dict], consumed: set[int], *, code: str,
                          lat0: float, lon0: float, radius_m: int, limit: int,
                          today: str) -> list[dict]:
    """Gains 1 (banques atm) & 2 (comblement). Construit des POI `source='overture'`
    à partir des Overture NON consommés (pas déjà appariés à un OSM), dans le rayon, non
    génériques, les `limit` PLUS PROCHES. Marque le crypto en dépriorité pour `atm`
    (V2-47). Sans distances OSRM (l'appelant les calcule) — porte `crow_m` pour la dédup."""
    scored: list[tuple[float, dict]] = []
    for j, v in enumerate(overture):
        if j in consumed:
            continue
        name = v.get("name")
        if not name or v.get("lat") is None or v.get("lon") is None:
            continue
        if overpass.is_generic_name(name):
            continue
        crow = overpass.haversine_m(lat0, lon0, v["lat"], v["lon"])
        if crow > radius_m:
            continue
        scored.append((float(crow), v))
    scored.sort(key=lambda t: t[0])
    out: list[dict] = []
    for crow, v in scored[:max(0, limit)]:
        poi = {
            "name": v["name"], "lat": v["lat"], "lon": v["lon"],
            "address": None, "locality": None,
            "phone": v.get("phone"), "website": v.get("website"),
            "opening_hours": None, "cuisine": None,
            "description_md": None,
            "source": "overture", "source_ref": v.get("source_ref"),
            "category": code, "crow_m": int(crow),
            "completion_meta": {"_overture": {"origin": "fill", "verified_on": today}},
        }
        if code == "atm" and overpass._is_crypto_atm(
                {"name": v.get("name") or "", "operator": v.get("operator") or ""}):
            poi["_priority"] = 1     # crypto dépriorisé derrière les banques (V2-47)
        out.append(poi)
    return out


def cap_after_fusion(pois: list[dict], limit: int) -> list[dict]:
    """Plafonne une catégorie APRÈS ajout Overture aux `limit` plus pertinents : tri par
    (priorité, temps de trajet) → les banques passent devant le crypto, le proche devant
    le lointain. Appliqué UNIQUEMENT quand Overture a contribué (non-régression : une
    catégorie OSM seule est déjà plafonnée à la moisson)."""
    return sorted(pois, key=lambda p: (p.get("_priority", 0), dedup._travel(p)))[:limit]
