"""Tests de la couche d'accès aux plans & abonnements (V2-05a, api/plans.py).

Contre le vrai PostgreSQL (les plans du seed sont la source de vérité). On crée
des comptes jetables, on leur attribue un plan, et on vérifie les décisions de
quota. Aucune limite n'est jamais codée en dur dans les tests : on lit ce que le
seed déclare (free/solo/pro).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")
os.environ.setdefault("CASAGUIDE_JWT_SECRET",
                      "test-secret-not-for-prod-0123456789-abcdefghij")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import plans, repo, security  # noqa: E402
from enrich.settings import settings  # noqa: E402


@pytest.fixture()
def conn():
    created: list[str] = []
    c = psycopg.connect(settings.db_dsn, row_factory=dict_row)
    c._created_owner_ids = created  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        c.rollback()
        for oid in created:
            c.execute("DELETE FROM owners WHERE id = %s", (oid,))
        c.commit()
        c.close()


def _make_owner(conn, plan_id: str | None = "free") -> str:
    """Crée un compte jetable ; lui attribue `plan_id` (None = aucun abonnement,
    pour tester le repli). Renvoie l'owner_id."""
    row = repo.create_owner(
        conn, email=f"{uuid.uuid4()}@casaguide-plans-test.com",
        password_hash=security.hash_password("x"), full_name="Plan Test",
        company_name=None, phone=None, locale="fr")
    oid = str(row["id"])
    conn._created_owner_ids.append(oid)
    if plan_id is not None:
        repo.create_subscription(conn, oid, plan_id)
    conn.commit()
    return oid


def _seed_plan(conn, plan_id: str) -> dict:
    return repo.get_plan_by_id(conn, plan_id)


# ── get_plan : les trois plans du seed + repli ───────────────────────────────

@pytest.mark.parametrize("plan_id", ["free", "solo", "pro"])
def test_get_plan_returns_seeded_plan(conn, plan_id):
    oid = _make_owner(conn, plan_id)
    plan = plans.get_plan(conn, oid)
    seeded = _seed_plan(conn, plan_id)
    assert plan["id"] == plan_id
    assert plan["max_properties"] == seeded["max_properties"]
    assert plan["enrich_quota"] == seeded["enrich_quota"]
    assert plan["features"]["langs"] == seeded["features"]["langs"]


def test_get_plan_falls_back_to_free_without_subscription(conn):
    """Un compte sans abonnement (état incohérent) ne débloque jamais l'illimité :
    repli sur le plan gratuit."""
    oid = _make_owner(conn, plan_id=None)
    plan = plans.get_plan(conn, oid)
    assert plan["id"] == plans.FALLBACK_PLAN_ID == "free"
    assert plan["max_properties"] == _seed_plan(conn, "free")["max_properties"]


def test_get_subscription_returns_latest(conn):
    oid = _make_owner(conn, "free")
    repo.create_subscription(conn, oid, "pro")  # abonnement plus récent
    conn.commit()
    sub = plans.get_subscription(conn, oid)
    assert sub["plan_id"] == "pro"


# ── Quotas : logements ────────────────────────────────────────────────────────

def _add_property(conn, owner_id: str) -> str:
    prop = repo.create_property(conn, owner_id, {
        "name": "Test", "address_line1": "Rue X", "city": "Ville",
        "country_code": "ES", "default_lang": "fr"})
    conn.commit()
    return str(prop["id"])


def test_quota_properties_free_limited(conn):
    oid = _make_owner(conn, "free")   # max_properties = 1
    q0 = plans.check_quota(conn, oid, "properties")
    assert q0.ok and q0.used == 0 and q0.limit == 1
    _add_property(conn, oid)
    q1 = plans.check_quota(conn, oid, "properties")
    assert not q1.ok and q1.used == 1 and q1.remaining == 0


def test_quota_properties_pro_unlimited(conn):
    oid = _make_owner(conn, "pro")    # max_properties = NULL (illimité)
    for _ in range(3):
        _add_property(conn, oid)
    q = plans.check_quota(conn, oid, "properties")
    assert q.ok and q.limit is None and q.remaining is None


# ── Quotas : enrichissements (mensuel, par logement) ─────────────────────────

def test_quota_enrichments_counts_month_per_property(conn):
    oid = _make_owner(conn, "free")   # enrich_quota = 1
    pid = _add_property(conn, oid)
    q0 = plans.check_quota(conn, oid, "enrichments", property_id=pid)
    assert q0.ok and q0.used == 0 and q0.limit == 1
    repo.create_pending_job(conn, pid, "initial")  # 1 job ce mois-ci
    conn.commit()
    q1 = plans.check_quota(conn, oid, "enrichments", property_id=pid)
    assert not q1.ok and q1.used == 1


def test_quota_enrichments_requires_property_id(conn):
    oid = _make_owner(conn, "free")
    with pytest.raises(ValueError):
        plans.check_quota(conn, oid, "enrichments")


# ── Langues : plafonnement des cibles (langue source comprise) ───────────────

def test_cap_target_langs_free_allows_none(conn):
    free = _seed_plan(conn, "free")   # langs = 1 → aucune cible
    assert plans.max_langs(free) == 1
    assert plans.cap_target_langs(free, ["en", "es"]) == []


def test_cap_target_langs_paid_allows_up_to_limit(conn):
    pro = _seed_plan(conn, "pro")     # langs = 5 → 4 cibles max
    assert plans.cap_target_langs(pro, ["en", "es"]) == ["en", "es"]
    six = ["en", "es", "de", "nl", "it", "pt"]
    assert plans.cap_target_langs(pro, six) == six[:4]


def test_watermark_flag_follows_plan(conn):
    assert plans.wants_watermark(_seed_plan(conn, "free")) is True
    assert plans.wants_watermark(_seed_plan(conn, "solo")) is False
    assert plans.wants_watermark(_seed_plan(conn, "pro")) is False


def test_unknown_resource_raises(conn):
    oid = _make_owner(conn, "free")
    with pytest.raises(ValueError):
        plans.check_quota(conn, oid, "bananas")
