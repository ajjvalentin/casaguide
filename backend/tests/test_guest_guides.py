"""Offre « Guide Voyageur » (V2-54) — couche données : fiches guest, cache de
proximité, anti-abus, exclusion des listings/quotas. Contre le vrai PostGIS."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from api import repo  # noqa: E402
from enrich.settings import settings  # noqa: E402

# Alicante, loin de toute fiche existante des autres tests.
LAT, LON = 38.3452, -0.4810


@pytest.fixture()
def conn():
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as c:
        created: list[str] = []
        yield c, created
        c.rollback()  # au cas où un test a laissé la transaction en erreur
        for pid in created:
            c.execute("DELETE FROM guest_guide_generations WHERE property_id = %s", (pid,))
            c.execute("DELETE FROM properties WHERE id = %s", (pid,))
        c.execute("DELETE FROM guest_guide_generations WHERE email = %s",
                  ("abuse@test.local",))
        c.commit()


def _make_guest(c, created, *, lat=LAT, lon=LON, publish=True) -> dict:
    prop = repo.create_guest_property(
        c, name="Guide — Alicante", city="Alicante", country_code="ES",
        lat=lat, lon=lon)
    created.append(str(prop["id"]))
    if publish:
        c.execute("UPDATE properties SET status='published' WHERE id=%s", (prop["id"],))
    c.commit()
    return prop


def test_create_guest_property_is_system_owned_and_flagged(conn):
    c, created = conn
    prop = _make_guest(c, created)
    assert prop["guest_guide"] is True
    assert prop["lat"] == pytest.approx(LAT) and prop["lon"] == pytest.approx(LON)
    # Possédée par l'owner système (résolu par e-mail, jamais un propriétaire réel).
    owner_id = c.execute("SELECT owner_id FROM properties WHERE id=%s",
                         (prop["id"],)).fetchone()["owner_id"]
    assert str(owner_id) == repo.guest_guide_owner_id(c)


def test_cache_resurfaces_recent_nearby_guide(conn):
    c, created = conn
    prop = _make_guest(c, created)
    # < 100 m (≈ 50 m au sud) et récent → resservi.
    hit = repo.find_recent_guest_guide_near(c, LAT - 0.0004, LON, 100, 30)
    assert hit is not None and str(hit["id"]) == str(prop["id"])
    # > 100 m (≈ 500 m) → aucun cache.
    assert repo.find_recent_guest_guide_near(c, LAT + 0.005, LON, 100, 30) is None
    # Trop vieux (> 30 j) → aucun cache, même à la même position.
    c.execute("UPDATE properties SET created_at = now() - interval '40 days' "
              "WHERE id=%s", (prop["id"],))
    c.commit()
    assert repo.find_recent_guest_guide_near(c, LAT, LON, 100, 30) is None


def test_cache_ignores_unpublished_guides(conn):
    c, created = conn
    prop = _make_guest(c, created, publish=False)  # brouillon (génération en cours)
    assert repo.find_recent_guest_guide_near(c, LAT, LON, 100, 30) is None
    assert str(prop["id"]) in created  # bien créée, juste pas resservie


def test_anti_abuse_generation_counters(conn):
    c, created = conn
    assert repo.count_guest_generations(c, email="abuse@test.local") == 0
    repo.record_guest_generation(c, "abuse@test.local", "1.2.3.4", None)
    repo.record_guest_generation(c, "abuse@test.local", "1.2.3.4", None)
    c.commit()
    assert repo.count_guest_generations(c, email="abuse@test.local") == 2
    assert repo.count_guest_generations(c, ip="1.2.3.4") == 2
    # Fenêtre : rien au-delà de l'horizon demandé (0 h → aucune).
    assert repo.count_guest_generations(c, email="abuse@test.local", within_hours=0) == 0


def test_guest_guides_excluded_from_owner_listings_and_quota(conn):
    c, created = conn
    prop = _make_guest(c, created)
    sys_id = repo.guest_guide_owner_id(c)
    # L'owner système ne « voit » aucun guide voyageur dans ses listings/quota
    # (défense en profondeur — il ne se connecte jamais au back-office de toute façon).
    listed_ids = {str(p["id"]) for p in repo.list_properties(c, sys_id)}
    assert str(prop["id"]) not in listed_ids
    assert repo.count_properties(c, sys_id) == 0
