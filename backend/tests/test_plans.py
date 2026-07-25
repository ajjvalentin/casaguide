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
from datetime import datetime, timedelta, timezone
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


def test_quota_properties_pro_includes_addons(conn):
    """Grille V2-18b : Pro = 6 logements inclus, plus l'add-on (`addon_qty`).
    Le plafond effectif est `max_properties + addon_qty` (via effective_entitlements)."""
    oid = _make_owner(conn, "pro")    # max_properties = 6, addon_qty = 0
    for _ in range(6):
        _add_property(conn, oid)
    q = plans.check_quota(conn, oid, "properties")
    assert not q.ok and q.used == 6 and q.limit == 6   # 6 inclus, plafond atteint

    # 2 add-ons (écrits par le webhook) → plafond porté à 8
    conn.execute("UPDATE subscriptions SET addon_qty = 2 WHERE owner_id = %s", (oid,))
    conn.commit()
    q2 = plans.check_quota(conn, oid, "properties")
    assert q2.ok and q2.limit == 8 and q2.remaining == 2


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

def test_cap_target_langs_unlimited_on_all_plans(conn):
    """Grille V2-18b : `features.langs='all'` → illimité (max_langs None) sur tous
    les plans, y compris le gratuit ; toutes les cibles passent."""
    for plan_id in ("free", "solo", "pro", "trial"):
        plan = _seed_plan(conn, plan_id)
        assert plans.max_langs(plan) is None
        six = ["en", "es", "de", "nl", "it", "pt"]
        assert plans.cap_target_langs(plan, six) == six


def test_cap_target_langs_still_caps_a_numeric_plan(conn):
    """La mécanique de plafonnement reste valable pour un plan à `langs` numérique
    (sécurité / futurs plans) : langs=2 → 1 cible max."""
    plan = {"features": {"langs": 2}}
    assert plans.max_langs(plan) == 2
    assert plans.cap_target_langs(plan, ["en", "es", "de"]) == ["en"]


def test_watermark_flag_follows_plan(conn):
    # V2-18a (décision 25/07) : watermark présent sur essai/gratuit/solo, absent pro.
    assert plans.wants_watermark(_seed_plan(conn, "trial")) is True
    assert plans.wants_watermark(_seed_plan(conn, "free")) is True
    assert plans.wants_watermark(_seed_plan(conn, "solo")) is True
    assert plans.wants_watermark(_seed_plan(conn, "pro")) is False


def test_unknown_resource_raises(conn):
    oid = _make_owner(conn, "free")
    with pytest.raises(ValueError):
        plans.check_quota(conn, oid, "bananas")


# ── Modèle d'essai : expiration & droits effectifs (V2-18a) ──────────────────

def _set_trial(conn, oid, ends, *, status="trialing", plan_id="trial"):
    conn.execute(
        """UPDATE subscriptions SET plan_id = %s, status = %s, trial_ends_at = %s
           WHERE owner_id = %s""",
        (plan_id, status, ends, oid))
    conn.commit()


def test_is_trial_expired_only_for_past_trialing():
    """Pure : expiré ssi statut 'trialing' ET échéance dépassée. Un plan payant /
    gratuit / sans échéance n'est jamais expiré (l'accès ne dépend que du plan)."""
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    past = now - timedelta(days=1)
    future = now + timedelta(days=5)
    assert plans.is_trial_expired({"status": "trialing", "trial_ends_at": past}, now=now) is True
    assert plans.is_trial_expired({"status": "trialing", "trial_ends_at": future}, now=now) is False
    assert plans.is_trial_expired({"status": "trialing", "trial_ends_at": None}, now=now) is False
    assert plans.is_trial_expired({"status": "active", "trial_ends_at": past}, now=now) is False
    assert plans.is_trial_expired(None, now=now) is False


def test_effective_entitlements_trial_active(conn):
    oid = _make_owner(conn, "trial")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    _set_trial(conn, oid, now + timedelta(days=10))
    ent = plans.effective_entitlements(conn, oid, now=now)
    assert ent["plan"]["id"] == "trial"
    assert ent["on_trial"] is True and ent["trial_expired"] is False
    assert plans.can_write(conn, oid, now=now) is True


def test_effective_entitlements_trial_expired_is_read_only(conn):
    oid = _make_owner(conn, "trial")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    _set_trial(conn, oid, now - timedelta(days=1))
    ent = plans.effective_entitlements(conn, oid, now=now)
    assert ent["trial_expired"] is True and ent["read_only"] is True
    assert ent["on_trial"] is False
    assert plans.can_write(conn, oid, now=now) is False


# ── Guide équipe : gating & grand-père (V2-18b) ──────────────────────────────

def test_staff_access_follows_plan_and_grandfather(conn):
    """`staff_access` : exclusif Pro (staff_guide=true), aperçu essai
    ('preview'), fermé solo/free — sauf clause de grand-père (invariant 3)."""
    assert plans.staff_access(_seed_plan(conn, "pro")) is True
    assert plans.staff_access(_seed_plan(conn, "trial")) is True    # 'preview'
    assert plans.staff_access(_seed_plan(conn, "solo")) is False
    assert plans.staff_access(_seed_plan(conn, "free")) is False
    # Grand-père : un compte solo/free existant garde l'accès quel que soit le plan.
    assert plans.staff_access(_seed_plan(conn, "solo"), grandfathered=True) is True
    assert plans.staff_access(_seed_plan(conn, "free"), grandfathered=True) is True


def test_effective_entitlements_exposes_addons_and_staff(conn):
    """`effective_entitlements` expose `addon_qty`, `max_properties` effectif et le
    droit staff (point d'entrée unique des droits, V2-18b)."""
    oid = _make_owner(conn, "pro")
    conn.execute("UPDATE subscriptions SET addon_qty = 3, staff_grandfathered = FALSE "
                 "WHERE owner_id = %s", (oid,))
    conn.commit()
    ent = plans.effective_entitlements(conn, oid)
    assert ent["addon_qty"] == 3
    assert ent["max_properties"] == 6 + 3          # base Pro + add-ons
    assert ent["staff_access"] is True             # Pro
    assert ent["staff_grandfathered"] is False


def test_effective_entitlements_grandfathered_free_keeps_staff(conn):
    """Un compte 'free' grand-périsé conserve l'accès au guide équipe (invariant 3)."""
    oid = _make_owner(conn, "free")
    conn.execute("UPDATE subscriptions SET staff_grandfathered = TRUE "
                 "WHERE owner_id = %s", (oid,))
    conn.commit()
    ent = plans.effective_entitlements(conn, oid)
    assert ent["staff_grandfathered"] is True and ent["staff_access"] is True
    # Sans grand-père, le même plan 'free' n'y a pas droit.
    conn.execute("UPDATE subscriptions SET staff_grandfathered = FALSE "
                 "WHERE owner_id = %s", (oid,))
    conn.commit()
    assert plans.effective_entitlements(conn, oid)["staff_access"] is False


def test_paid_plan_never_read_only_even_with_stale_trial_end(conn):
    """Sécurité : un plan payant (statut 'active') reste inscriptible même si une
    vieille échéance d'essai traîne — l'expiration ne s'applique qu'à 'trialing'."""
    oid = _make_owner(conn, "solo")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    _set_trial(conn, oid, now - timedelta(days=30), status="active", plan_id="solo")
    ent = plans.effective_entitlements(conn, oid, now=now)
    assert ent["trial_expired"] is False
    assert plans.can_write(conn, oid, now=now) is True
