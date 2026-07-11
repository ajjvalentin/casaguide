"""Recherche de POI via l'API Overpass (OpenStreetMap).

Étape 2 du pipeline (§5.1). Pour chaque catégorie du seed (poi_categories),
on interroge Overpass dans le rayon default_radius_m autour du logement.

Deux catégories n'ont pas de tags OSM fiables et sont traitées par l'étape
Claude (recherche web) : food_delivery, babysitter.
"""
from __future__ import annotations

import math
import time

import httpx

from .settings import settings

# Catégorie CasaGuide -> sélecteurs de tags OSM (union)
CATEGORY_SELECTORS: dict[str, list[str]] = {
    "hospital":        ['"amenity"="hospital"'],
    "pharmacy":        ['"amenity"="pharmacy"'],
    "doctor":          ['"amenity"="doctors"', '"amenity"="dentist"'],
    "police":          ['"amenity"="police"'],
    "veterinary":      ['"amenity"="veterinary"'],
    "supermarket":     ['"shop"="supermarket"'],
    "market":          ['"amenity"="marketplace"'],
    "bakery":          ['"shop"="bakery"'],
    "atm":             ['"amenity"="atm"'],
    "post_office":     ['"amenity"="post_office"'],
    "mall":            ['"shop"="mall"'],
    "laundry":         ['"shop"="laundry"', '"shop"="dry_cleaning"'],
    "restaurant":      ['"amenity"="restaurant"'],
    "bar":             ['"amenity"="bar"', '"amenity"="pub"'],
    "cafe":            ['"amenity"="cafe"'],
    "beach":           ['"natural"="beach"'],
    "sight":           ['"tourism"="attraction"', '"tourism"="museum"'],
    "family_activity": ['"leisure"="water_park"', '"tourism"="theme_park"',
                        '"leisure"="playground"'],
    "sport":           ['"leisure"="sports_centre"', '"leisure"="golf_course"'],
    "taxi":            ['"amenity"="taxi"'],
    "bus_stop":        ['"highway"="bus_stop"'],
    "train_station":   ['"railway"="station"'],
    "airport":         ['"aeroway"="aerodrome"'],
    "parking":         ['"amenity"="parking"'],
    "rental":          ['"amenity"="bicycle_rental"', '"amenity"="car_rental"'],
}

# Catégories sans tags OSM exploitables -> enrichies par Claude uniquement
CLAUDE_ONLY_CATEGORIES = {"food_delivery", "babysitter"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Distance à vol d'oiseau en mètres (utile pour trier et pour le fallback)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


def _build_query(selectors: list[str], lat: float, lon: float, radius_m: int) -> str:
    clauses = "".join(
        f'nwr[{sel}](around:{radius_m},{lat},{lon});' for sel in selectors
    )
    return (
        f"[out:json][timeout:{settings.overpass_timeout_s}];"
        f"({clauses});out center tags;"
    )


def _parse_elements(elements: list[dict], lat0: float, lon0: float) -> list[dict]:
    pois = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue  # un POI sans nom n'a pas d'intérêt dans le guide
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None or lon is None:
            continue
        addr = ", ".join(filter(None, [
            " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
            tags.get("addr:city"),
        ])) or None
        pois.append({
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "address": addr,
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "website": tags.get("website") or tags.get("contact:website"),
            "opening_hours": tags.get("opening_hours"),
            "source": "osm",
            "source_ref": f'{el.get("type", "node")}/{el.get("id")}',
            "crow_m": haversine_m(lat0, lon0, float(lat), float(lon)),
        })
    # Doublons OSM (même nom à < 100 m) : on garde le plus proche
    seen: dict[str, dict] = {}
    for p in sorted(pois, key=lambda p: p["crow_m"]):
        key = p["name"].lower()
        if key not in seen or p["crow_m"] < seen[key]["crow_m"] - 100:
            seen.setdefault(key, p)
    return sorted(seen.values(), key=lambda p: p["crow_m"])


def fetch_category(category: str, lat: float, lon: float, radius_m: int,
                   client: httpx.Client | None = None) -> list[dict]:
    """POI d'une catégorie, triés par distance, limités à max_pois_per_category.

    Bascule automatiquement sur les miroirs Overpass en cas d'erreur HTTP
    (le serveur principal renvoie des 406 aux clients automatisés depuis 2026).
    """
    selectors = CATEGORY_SELECTORS.get(category)
    if not selectors:
        return []
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    query = _build_query(selectors, lat, lon, radius_m)
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    try:
        last_error: Exception | None = None
        for url in (settings.overpass_url, *settings.overpass_mirrors):
            try:
                resp = client.post(url, data={"data": query}, headers=headers)
                resp.raise_for_status()
                pois = _parse_elements(resp.json().get("elements", []), lat, lon)
                return pois[: settings.max_pois_per_category]
            except httpx.HTTPError as exc:
                last_error = exc
                continue  # miroir suivant
        raise last_error or RuntimeError("Aucun serveur Overpass joignable")
    finally:
        if own_client:
            client.close()
        time.sleep(settings.politeness_delay_s)  # politesse envers les serveurs publics
