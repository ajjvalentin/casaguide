"""Relances de fin d'essai J-7 / J-2 (V2-18a, ops/send_trial_reminders.py).

Contre le vrai PostgreSQL. Horloge et mailer injectés (aucun réseau). Vérifie la
sélection exacte des fenêtres, l'idempotence (double exécution = zéro doublon) et
le caractère best-effort (un échec SMTP ne bloque rien et n'est pas stampé)."""
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "ops"))

from api import repo, security  # noqa: E402
from enrich.settings import settings  # noqa: E402
import send_trial_reminders as reminders  # noqa: E402

NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


class FakeMailer:
    """Mailer inspectable (aucun réseau). `fail=True` simule une panne SMTP."""
    def __init__(self, fail: bool = False):
        self.sent: list[tuple] = []
        self.fail = fail

    def send(self, to, email):
        if self.fail:
            raise RuntimeError("SMTP indisponible")
        self.sent.append((to, email))


@pytest.fixture()
def conn():
    created: list[str] = []
    c = psycopg.connect(settings.db_dsn, row_factory=dict_row)
    c._created = created  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        c.rollback()
        for oid in created:
            c.execute("DELETE FROM owners WHERE id = %s", (oid,))
        c.commit()
        c.close()


def _make_trial(conn, *, ends, status="trialing") -> str:
    row = repo.create_owner(
        conn, email=f"{uuid.uuid4()}@casaguide-trial-test.com",
        password_hash=security.hash_password("x"), full_name="Essai Test",
        company_name=None, phone=None, locale="fr")
    oid = str(row["id"])
    conn._created.append(oid)
    repo.create_subscription(conn, oid, "trial", status=status, trial_ends_at=ends)
    conn.commit()
    return oid


def _sub_id(conn, oid) -> str:
    return conn.execute(
        "SELECT id FROM subscriptions WHERE owner_id = %s", (oid,)).fetchone()["id"]


def _run(conn, mailer, **kw):
    return reminders.run_reminders(conn, mailer, base_url="https://holaguia.com",
                                   now=NOW, **kw)


# ── Sélection des fenêtres ────────────────────────────────────────────────────

def test_j7_window_selects_trial_within_seven_days(conn):
    oid = _make_trial(conn, ends=NOW + timedelta(days=5))
    m = FakeMailer()
    sent = _run(conn, m)
    assert sent == {7: 1, 2: 0}
    assert len(m.sent) == 1 and "termine" in m.sent[0][1].subject
    # Stampé : la 7d est renseignée, la 2d non.
    row = conn.execute(
        "SELECT reminder_7d_sent_at, reminder_2d_sent_at FROM subscriptions WHERE owner_id=%s",
        (oid,)).fetchone()
    assert row["reminder_7d_sent_at"] is not None
    assert row["reminder_2d_sent_at"] is None


def test_j2_window_only_when_seven_already_sent(conn):
    oid = _make_trial(conn, ends=NOW + timedelta(days=1))
    repo.mark_trial_reminder_sent(conn, _sub_id(conn, oid), 7, now=NOW)  # J-7 déjà fait
    conn.commit()
    m = FakeMailer()
    sent = _run(conn, m)
    assert sent == {7: 0, 2: 1}   # seule la J-2 part


def test_not_due_and_expired_and_paid_are_skipped(conn):
    _make_trial(conn, ends=NOW + timedelta(days=10))                 # trop tôt
    _make_trial(conn, ends=NOW - timedelta(days=1))                  # déjà expiré
    _make_trial(conn, ends=NOW + timedelta(days=3), status="active") # payant (pas trialing)
    m = FakeMailer()
    assert _run(conn, m) == {7: 0, 2: 0}
    assert m.sent == []


# ── Idempotence ───────────────────────────────────────────────────────────────

def test_double_run_does_not_resend(conn):
    _make_trial(conn, ends=NOW + timedelta(days=6))
    m = FakeMailer()
    assert _run(conn, m) == {7: 1, 2: 0}
    assert _run(conn, m) == {7: 0, 2: 0}   # 2e passage : rien
    assert len(m.sent) == 1


# ── Best-effort : un échec SMTP ne bloque ni ne stampe ───────────────────────

def test_smtp_failure_is_not_stamped_and_retries(conn):
    oid = _make_trial(conn, ends=NOW + timedelta(days=4))
    # 1er passage : SMTP en panne → aucun envoi, aucun stamp, pas d'exception.
    sent = _run(conn, FakeMailer(fail=True))
    assert sent == {7: 0, 2: 0}
    row = conn.execute(
        "SELECT reminder_7d_sent_at FROM subscriptions WHERE owner_id=%s",
        (oid,)).fetchone()
    assert row["reminder_7d_sent_at"] is None
    # 2e passage : SMTP rétabli → la relance part enfin (réessai).
    m = FakeMailer()
    assert _run(conn, m) == {7: 1, 2: 0}
    assert len(m.sent) == 1
