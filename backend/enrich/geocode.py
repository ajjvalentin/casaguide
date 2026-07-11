"""Géocodage d'une adresse via Nominatim (OpenStreetMap).

Étape 1 du pipeline (§5.1 du CdC). Essaie plusieurs stratégies, de la plus
précise à la plus grossière (échelle de repli), car les adresses résidentielles
ne sont pas toujours cartographiées dans OSM :

  1. recherche structurée rue + numéro + code postal + ville  -> rooftop/street
  2. recherche structurée rue sans numéro                     -> street
  3. code postal + ville                                      -> city
  4. ville seule                                              -> city

Une précision 'city' signifie que le propriétaire devra positionner le point
sur la carte dans le back-office (prévu au CdC, champ geocode_accuracy).
"""
from __future__ import annotations

import re

import httpx

from .settings import settings

_ACCURACY = {
    "building": "rooftop", "house": "rooftop", "residential": "rooftop",
    "road": "street", "street": "street",
}


class GeocodeError(Exception):
    pass


def _strip_house_number(street: str) -> str:
    """'Calle San Ignacio 23' -> 'Calle San Ignacio' ; '23 Rue X' -> 'Rue X'."""
    s = re.sub(r"[,\s]+\d+[a-zA-Z]?\s*$", "", street)
    s = re.sub(r"^\s*\d+[a-zA-Z]?[,\s]+", "", s)
    return s.strip() or street


def _search(params: dict, country_code: str, client: httpx.Client) -> dict | None:
    resp = client.get(
        settings.nominatim_url,
        params={**params, "countrycodes": country_code.lower(),
                "format": "jsonv2", "limit": 1, "addressdetails": 0},
        headers={"User-Agent": settings.user_agent},
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def geocode(address: str | None = None, country_code: str = "ES",
            client: httpx.Client | None = None, *,
            street: str | None = None, postalcode: str | None = None,
            city: str | None = None) -> dict:
    """Retourne {"lat", "lon", "accuracy", "display_name", "source"}.

    Passer de préférence les composants (street/postalcode/city) pour activer
    l'échelle de repli ; `address` libre reste accepté (rétro-compatibilité).
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        attempts: list[tuple[dict, str | None]] = []
        if street and city:
            full = {"street": street, "city": city}
            if postalcode:
                full["postalcode"] = postalcode
            attempts.append((full, None))                       # 1. précis
            no_num = _strip_house_number(street)
            if no_num != street:
                attempts.append(({"street": no_num, "city": city}, "street"))  # 2.
        if postalcode and city:
            attempts.append(({"q": f"{postalcode} {city}"}, "city"))           # 3.
        if city:
            attempts.append(({"q": city}, "city"))                             # 4.
        if address:
            attempts.insert(0, ({"q": address}, None))          # requête libre d'abord

        for params, forced_accuracy in attempts:
            r = _search(params, country_code, client)
            if not r:
                continue
            accuracy = forced_accuracy or _ACCURACY.get(
                r.get("type", ""), _ACCURACY.get(r.get("class", ""), "city"))
            return {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "accuracy": accuracy,
                "display_name": r.get("display_name", ""),
                "source": "nominatim",
            }

        tried = address or f"{street}, {postalcode}, {city}"
        raise GeocodeError(f"Adresse introuvable (toutes stratégies) : {tried!r}")
    finally:
        if own_client:
            client.close()
