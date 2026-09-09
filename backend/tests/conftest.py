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
