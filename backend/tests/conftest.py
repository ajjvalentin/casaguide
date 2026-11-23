"""Fixtures partagées de la suite backend.

Overture (V2-52) est un flux réseau sortant (DuckDB/S3) → DÉSACTIVÉ par défaut dans
toute la suite (doctrine « aucun réseau requis »). Les tests qui l'exercent l'activent
explicitement ET injectent un fetcher factice (aucun DuckDB). Cette fixture autouse
garantit qu'aucun test hérité ne déclenche un fetch réel si le flag `.env` traîne.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from enrich.settings import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _overture_off_by_default():
    prev = settings.overture_enabled
    settings.overture_enabled = False
    try:
        yield
    finally:
        settings.overture_enabled = prev


@pytest.fixture(autouse=True)
def _nominatim_no_wait():
    """V2-79b : la file d'attente Nominatim (1 req/s, backoff sur 429) ne doit jamais
    faire dormir la suite — les tests qui l'exercent injectent leur propre horloge."""
    prev = (settings.nominatim_min_interval_s, settings.nominatim_backoff_s)
    settings.nominatim_min_interval_s, settings.nominatim_backoff_s = 0.0, 0.0
    try:
        yield
    finally:
        settings.nominatim_min_interval_s, settings.nominatim_backoff_s = prev


@pytest.fixture(autouse=True)
def _overpass_public_by_default():
    """V2-93b : une clé Overpass posée dans le `.env` local (instance payante) ne doit
    NI changer le mode des tests hérités (serveur + miroirs) NI partir vers le service
    réel. Les tests du mode à clé la posent eux-mêmes (clé sentinelle)."""
    prev = (settings.overpass_api_key, settings.overpass_url)
    settings.overpass_api_key = ""
    if "overpass.ch" in (settings.overpass_url or ""):
        settings.overpass_url = "https://overpass.kumi.systems/api/interpreter"
    try:
        yield
    finally:
        settings.overpass_api_key, settings.overpass_url = prev
