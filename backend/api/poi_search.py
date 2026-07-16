"""Recherche de lieux via Nominatim pour l'ajout manuel de POI (M-22).

Proxy côté serveur (le navigateur n'appelle jamais Nominatim directement) :
biais géographique autour du logement, User-Agent requis, **politesse 1 req/s**
(politique d'usage OSM, cf. settings.politeness_delay_s). Client httpx injectable
→ tests sans réseau. La catégorie CasaGuide est **devinée** depuis le couple
class/type OSM renvoyé par Nominatim (inversion de `overpass.CATEGORY_TAGS`),
avec repli 'sight' : le propriétaire peut toujours la corriger avant validation.
"""
from __future__ import annotations

import time

import httpx

from enrich.overpass import CATEGORY_TAGS
from enrich.settings import settings

DEFAULT_CATEGORY = "sight"

# Inversion (clé, valeur) OSM → category_code CasaGuide. Le premier code déclarant
# un tag l'emporte (les tuples sont peu ambigus dans CATEGORY_TAGS).
_OSM_TO_CATEGORY: dict[tuple[str, str], str] = {}
for _code, _tags in CATEGORY_TAGS.items():
    for _k, _v in _tags:
        _OSM_TO_CATEGORY.setdefault((_k, _v), _code)


def guess_category(osm_key: str | None, osm_type: str | None) -> str:
    """Devine un `category_code` CasaGuide depuis le couple class/type OSM
    (repli 'sight' — jamais d'erreur, le propriétaire ajuste si besoin)."""
    if not osm_key or not osm_type:
        return DEFAULT_CATEGORY
    return _OSM_TO_CATEGORY.get((osm_key, osm_type), DEFAULT_CATEGORY)


# Horodatage du dernier appel Nominatim (politesse à l'échelle du process).
_last_call_at = 0.0


def _throttle(sleep=time.sleep, now=time.monotonic) -> None:
    """Politesse Nominatim : au plus une requête toutes les `politeness_delay_s`
    (1 s par défaut). `sleep`/`now` injectables → aucun vrai sommeil en test
    (les tests posent politeness_delay_s = 0)."""
    global _last_call_at
    delay = settings.politeness_delay_s
    if delay > 0:
        wait = delay - (now() - _last_call_at)
        if wait > 0:
            sleep(wait)
    _last_call_at = now()


def _candidate(row: dict) -> dict:
    """Normalise un résultat Nominatim en candidat POID (nom, adresse, position,
    catégorie devinée, téléphone/site depuis extratags)."""
    extra = row.get("extratags") or {}
    display = row.get("display_name") or ""
    name = row.get("name") or (display.split(",")[0].strip() if display else "")
    return {
        "name": name,
        "address": display or None,
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "category_code": guess_category(row.get("class") or row.get("category"),
                                        row.get("type")),
        "phone": extra.get("phone") or extra.get("contact:phone"),
        "website": extra.get("website") or extra.get("contact:website"),
    }


def search(query: str, lat: float | None, lon: float | None,
           client: httpx.Client | None = None, limit: int = 6) -> list[dict]:
    """Interroge Nominatim et renvoie jusqu'à `limit` candidats normalisés.

    Biais (non contraignant) sur une viewbox autour du logement pour remonter en
    priorité les lieux proches. Client httpx injectable (tests)."""
    query = (query or "").strip()
    if not query:
        return []
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        params: dict = {"q": query, "format": "jsonv2", "addressdetails": 1,
                        "extratags": 1, "limit": limit}
        if lat is not None and lon is not None:
            d = 0.4  # ~40 km : biais de proximité, sans exclure les résultats hors cadre
            params["viewbox"] = f"{lon - d},{lat - d},{lon + d},{lat + d}"
            params["bounded"] = 0
        _throttle()
        resp = client.get(settings.nominatim_url, params=params,
                          headers={"User-Agent": settings.user_agent})
        resp.raise_for_status()
        rows = resp.json()
    finally:
        if own_client:
            client.close()
    return [_candidate(r) for r in rows if r.get("lat") and r.get("lon")]
