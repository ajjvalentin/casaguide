"""Calcul des distances et temps de trajet (étape 4 du pipeline, §5.1).

Stratégie : OSRM (table service) pour la voiture et la marche, avec repli
sur une estimation haversine si le service est indisponible.

Disjoncteur : un serveur OSRM qui échoue est écarté pour le reste du
processus (variable _UNAVAILABLE) — on ne perd pas un timeout par catégorie
quand un service public est en panne, l'estimation prend le relais.

Les résultats sont stockés dans pois.* : aucun appel réseau côté voyageur.
"""
from __future__ import annotations

import httpx

from .overpass import haversine_m
from .settings import settings

# Vitesses de repli (estimation)
_WALK_KMH = 4.8
_DRIVE_KMH = 40.0
_DETOUR = 1.3  # facteur route réelle / vol d'oiseau

_OSRM_TIMEOUT_S = 8
_UNAVAILABLE: set[str] = set()  # serveurs écartés après un premier échec


def _osrm_table(base_url: str, origin: tuple[float, float],
                dests: list[tuple[float, float]], client: httpx.Client) -> list[dict] | None:
    """Interroge le service /table d'OSRM. Retourne [{dist_m, dur_s}] ou None si échec."""
    if base_url in _UNAVAILABLE:
        return None
    coords = ";".join(f"{lon},{lat}" for lat, lon in [origin, *dests])
    url = f"{base_url}/table/v1/driving/{coords}"
    try:
        resp = client.get(url, params={
            "sources": "0",
            "annotations": "duration,distance",
        }, headers={"User-Agent": settings.user_agent}, timeout=_OSRM_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            return None
        durations = data["durations"][0][1:]   # ligne 0 = origine -> chaque destination
        distances = data["distances"][0][1:]
        return [
            {"dist_m": round(d) if d is not None else None,
             "dur_s": round(t) if t is not None else None}
            for d, t in zip(distances, durations)
        ]
    except (httpx.HTTPError, KeyError, IndexError, TypeError):
        _UNAVAILABLE.add(base_url)  # disjoncteur : plus d'appels vers ce serveur
        return None


def compute_distances(origin: tuple[float, float], pois: list[dict],
                      client: httpx.Client | None = None) -> None:
    """Complète chaque POI avec dist_walk_m / walk_min / dist_drive_m / drive_min.

    Modifie les dicts en place. `origin` = (lat, lon) du logement.
    """
    if not pois:
        return
    own_client = client is None
    client = client or httpx.Client(timeout=_OSRM_TIMEOUT_S)
    try:
        dests = [(p["lat"], p["lon"]) for p in pois]
        drive = _osrm_table(settings.osrm_drive_url, origin, dests, client)
        walk = _osrm_table(settings.osrm_walk_url, origin, dests, client)

        for i, p in enumerate(pois):
            crow = p.get("crow_m") or haversine_m(origin[0], origin[1], p["lat"], p["lon"])
            est_m = round(crow * _DETOUR)

            d = drive[i] if drive else None
            p["dist_drive_m"] = (d or {}).get("dist_m") or est_m
            dur = (d or {}).get("dur_s")
            p["drive_min"] = round(dur / 60) if dur else max(1, round(est_m / 1000 / _DRIVE_KMH * 60))

            w = walk[i] if walk else None
            p["dist_walk_m"] = (w or {}).get("dist_m") or est_m
            wdur = (w or {}).get("dur_s")
            p["walk_min"] = round(wdur / 60) if wdur else max(1, round(est_m / 1000 / _WALK_KMH * 60))
    finally:
        if own_client:
            client.close()
