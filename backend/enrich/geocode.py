"""Géocodage d'une adresse via Nominatim (OpenStreetMap).

Étape 1 du pipeline (§5.1 du CdC). Retourne (lat, lon, accuracy).
La précision est déduite du type de résultat Nominatim :
  - building/house  -> 'rooftop'  (idéal)
  - road/street     -> 'street'   (acceptable, à confirmer par le propriétaire)
  - autre           -> 'city'     (le propriétaire doit placer le point à la main)
"""
from __future__ import annotations

import httpx

from .settings import settings

_ACCURACY = {
    "building": "rooftop", "house": "rooftop", "residential": "rooftop",
    "road": "street", "street": "street",
}


class GeocodeError(Exception):
    pass


def geocode(address: str, country_code: str, client: httpx.Client | None = None) -> dict:
    """Retourne {"lat", "lon", "accuracy", "display_name", "source"}."""
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(
            settings.nominatim_url,
            params={
                "q": address,
                "countrycodes": country_code.lower(),
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 0,
            },
            headers={"User-Agent": settings.user_agent},
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            raise GeocodeError(f"Adresse introuvable : {address!r}")
        r = results[0]
        accuracy = _ACCURACY.get(r.get("type", ""), _ACCURACY.get(r.get("class", ""), "city"))
        return {
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "accuracy": accuracy,
            "display_name": r.get("display_name", ""),
            "source": "nominatim",
        }
    finally:
        if own_client:
            client.close()
