"""Tests d'intégration du mot de passe oublié et de la vérification d'email
(V2-08) contre le vrai PostgreSQL, avec un mailer injectable inspectable.

Couvre : jeton HACHÉ en base (jamais en clair), expiration, usage unique,
anti-énumération (même réponse compte inexistant), cadence par email,
réinitialisation effective (login avec le nouveau mdp, ancien invalide),
vérification (flag posé, renvoi, comptes existants grand-périsés).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

# Environnement AVANT import des modules api (mêmes valeurs que test_api).
os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")
os.environ.setdefault("CASAGUIDE_JWT_SECRET",
                      "test-secret-not-for-prod-0123456789-abcdefghij")
os.environ.setdefault(
    "CASAGUIDE_SECRET_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
os.environ.setdefault("CASAGUIDE_PBKDF2_ITER", "10000")
os.environ.setdefault("MEDIA_ROOT",
                      os.path.join(tempfile.gettempdir(), "casaguide-test-media"))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import security  # noqa: E402
from api.config import settings as api_settings  # noqa: E402
from api.deps import get_mailer  # noqa: E402
from api.main import app  # noqa: E402
from api.mailer import ConsoleMailer  # noqa: E402
from enrich.settings import settings as enrich_settings  # noqa: E402

_RESET_RE = re.compile(r"/#/reset/([A-Za-z0-9_-]+)")
_VERIFY_RE = re.compile(r"/#/verify/([A-Za-z0-9_-]+)")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def mailer():
    m = ConsoleMailer()
    app.dependency_overrides[get_mailer] = lambda: m
    yield m
    app.dependency_overrides.clear()


@pytest.fixture()
def client(mailer):
    emails: list[str] = []
    c = TestClient(app)
    c.created_emails = emails  # type: ignore[attr-defined]
    c.mailer = mailer          # type: ignore[attr-defined]
    yield c
    with psycopg.connect(enrich_settings.db_dsn) as conn:
        for email in emails:
            conn.execute("DELETE FROM owners WHERE email = %s", (email,))
        conn.commit()


def register(client, password="password123", clear_mail=True, **over) -> dict:
    email = over.pop("email", f"{uuid.uuid4()}@casaguide-test.com")
    body = {"email": email, "password": password, "full_name": "Prop Test"}
    body.update(over)
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    client.created_emails.append(email)
    # L'inscription envoie un email de vérification (V2-08) : on vide la boîte
    # par défaut pour que les assertions suivantes partent d'un état propre.
    if clear_mail:
        client.mailer.sent.clear()
    return {"email": email, "token": r.json()["access_token"],
            "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}}


def _reset_token(mailer) -> str:
    to, email = mailer.sent[-1]
    m = _RESET_RE.search(email.text)
    assert m, f"lien de réinitialisation absent : {email.text!r}"
    return m.group(1)


def _verify_token(mailer) -> str:
    to, email = mailer.sent[-1]
    m = _VERIFY_RE.search(email.text)
    assert m, f"lien de vérification absent : {email.text!r}"
    return m.group(1)


def _db():
    return psycopg.connect(enrich_settings.db_dsn)


# ── Mot de passe oublié ──────────────────────────────────────────────────────

def test_forgot_sends_email_with_hashed_token(client):
    owner = register(client)
    r = client.post("/api/auth/forgot", json={"email": owner["email"]})
    assert r.status_code == 200
    neutral = r.json()["message"]

    # Un email est parti, à la bonne adresse, sujet de réinitialisation.
    assert len(client.mailer.sent) == 1
    to, email = client.mailer.sent[0]
    assert to == owner["email"]
    assert "réinitialisation" in email.subject.lower()

    raw = _reset_token(client.mailer)
    # En base : SEULE l'empreinte est stockée (jamais le jeton en clair).
    with _db() as conn:
        row = conn.execute(
            """SELECT o.email AS oemail, pr.token_hash, pr.purpose, pr.used_at
               FROM password_resets pr JOIN owners o ON o.id = pr.owner_id
               WHERE o.email = %s AND pr.purpose = 'reset'""",
            (owner["email"],)).fetchone()
    assert row is not None
    assert row[1] != raw                                   # pas de clair
    assert row[1] == security.hash_reset_token(raw)        # bien l'empreinte
    assert row[2] == "reset" and row[3] is None

    # La réponse neutre est bien un message générique (pas d'info sur le compte).
    assert "compte" in neutral.lower()


def test_forgot_unknown_email_same_response_no_email(client):
    known = register(client)
    r_known = client.post("/api/auth/forgot", json={"email": known["email"]})
    client.mailer.sent.clear()

    r_unknown = client.post("/api/auth/forgot",
                            json={"email": "nobody-xyz@casaguide-test.com"})
    # Anti-énumération : statut ET message identiques, aucun email pour l'inconnu.
    assert r_unknown.status_code == r_known.status_code == 200
    assert r_unknown.json()["message"] == r_known.json()["message"]
    assert client.mailer.sent == []


def test_forgot_rate_limited_per_email(client):
    owner = register(client)
    r1 = client.post("/api/auth/forgot", json={"email": owner["email"]})
    r2 = client.post("/api/auth/forgot", json={"email": owner["email"]})
    assert r1.status_code == r2.status_code == 200
    # Deux réponses identiques mais UN SEUL email / UN SEUL jeton (cadence 2 min).
    assert len(client.mailer.sent) == 1
    with _db() as conn:
        n = conn.execute(
            """SELECT count(*) FROM password_resets pr JOIN owners o
               ON o.id = pr.owner_id WHERE o.email = %s AND pr.purpose = 'reset'""",
            (owner["email"],)).fetchone()[0]
    assert n == 1


def test_reset_replaces_password_and_old_invalid(client):
    owner = register(client, password="oldpassword1")
    client.post("/api/auth/forgot", json={"email": owner["email"]})
    raw = _reset_token(client.mailer)

    r = client.post("/api/auth/reset",
                    json={"token": raw, "password": "brandnew99"})
    assert r.status_code == 200

    # Ancien mot de passe rejeté, nouveau accepté.
    assert client.post("/api/auth/login",
                       json={"email": owner["email"], "password": "oldpassword1"}
                       ).status_code == 401
    assert client.post("/api/auth/login",
                       json={"email": owner["email"], "password": "brandnew99"}
                       ).status_code == 200


def test_reset_token_single_use(client):
    owner = register(client)
    client.post("/api/auth/forgot", json={"email": owner["email"]})
    raw = _reset_token(client.mailer)

    assert client.post("/api/auth/reset",
                       json={"token": raw, "password": "firstchange1"}
                       ).status_code == 200
    # Rejoué : jeton déjà consommé → 400.
    again = client.post("/api/auth/reset",
                        json={"token": raw, "password": "secondchange2"})
    assert again.status_code == 400


def test_reset_expired_token(client):
    owner = register(client)
    client.post("/api/auth/forgot", json={"email": owner["email"]})
    raw = _reset_token(client.mailer)
    # Force l'expiration en base.
    with _db() as conn:
        conn.execute(
            """UPDATE password_resets SET expires_at = now() - interval '1 minute'
               WHERE owner_id = (SELECT id FROM owners WHERE email = %s)""",
            (owner["email"],))
        conn.commit()
    r = client.post("/api/auth/reset",
                    json={"token": raw, "password": "whatever12"})
    assert r.status_code == 400


def test_reset_invalid_token(client):
    r = client.post("/api/auth/reset",
                    json={"token": "not-a-real-token", "password": "whatever12"})
    assert r.status_code == 400


def test_reset_rejects_short_password(client):
    owner = register(client)
    client.post("/api/auth/forgot", json={"email": owner["email"]})
    raw = _reset_token(client.mailer)
    r = client.post("/api/auth/reset", json={"token": raw, "password": "short"})
    assert r.status_code == 422  # règle min 8 caractères (Pydantic)


# ── Vérification d'email ─────────────────────────────────────────────────────

def test_register_sends_verification_email_and_flag_false(client):
    # Inscription SANS vider la boîte : on veut inspecter l'email de vérification.
    owner = register(client, clear_mail=False)
    assert len(client.mailer.sent) == 1
    to, email = client.mailer.sent[0]
    assert to == owner["email"]
    assert "email" in email.subject.lower()
    assert _VERIFY_RE.search(email.text), "lien /#/verify/ attendu"
    # Nouveau compte : non vérifié.
    me = client.get("/api/auth/me", headers=owner["headers"])
    assert me.status_code == 200 and me.json()["email_verified"] is False


def test_verify_email_sets_flag(client):
    owner = register(client, clear_mail=False)
    token = _verify_token(client.mailer)
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    me = client.get("/api/auth/me", headers=owner["headers"])
    assert me.json()["email_verified"] is True


def test_verify_email_idempotent_second_click(client):
    owner = register(client, clear_mail=False)
    token = _verify_token(client.mailer)
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    # Second clic sur le même lien : toujours un succès (idempotent), reste vérifié.
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    me = client.get("/api/auth/me", headers=owner["headers"])
    assert me.json()["email_verified"] is True


def test_verify_invalid_token(client):
    r = client.post("/api/auth/verify-email", json={"token": "pas-un-jeton"})
    assert r.status_code == 400


def test_verify_expired_token(client):
    owner = register(client, clear_mail=False)
    token = _verify_token(client.mailer)
    with _db() as conn:
        conn.execute(
            """UPDATE password_resets SET expires_at = now() - interval '1 minute'
               WHERE owner_id = (SELECT id FROM owners WHERE email = %s)
                 AND purpose = 'verify'""", (owner["email"],))
        conn.commit()
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 400


def test_resend_verification_sends_new_email(client):
    owner = register(client)          # boîte vidée
    r = client.post("/api/auth/resend-verification", headers=owner["headers"])
    assert r.status_code == 200
    assert len(client.mailer.sent) == 1
    assert _VERIFY_RE.search(client.mailer.sent[0][1].text)


def test_resend_when_already_verified_no_email(client):
    owner = register(client, clear_mail=False)
    token = _verify_token(client.mailer)
    client.post("/api/auth/verify-email", json={"token": token})
    client.mailer.sent.clear()
    r = client.post("/api/auth/resend-verification", headers=owner["headers"])
    assert r.status_code == 200
    assert "déjà vérifiée" in r.json()["message"]
    assert client.mailer.sent == []  # aucun email superflu


def test_resend_requires_auth(client):
    assert client.post("/api/auth/resend-verification").status_code == 401


def test_migration_grandfathers_only_accounts_without_verify_token(client):
    """La migration 006 marque vérifiés les comptes SANS jeton de vérification
    (comptes d'avant V2-08) et laisse intacts ceux qui en ont un (inscrits
    depuis, en attente de vérification)."""
    migration = (Path(__file__).resolve().parents[2]
                 / "db" / "migrations" / "006_grandfather_email_verified.sql").read_text()
    e_old = f"{uuid.uuid4()}@casaguide-test.com"       # « ancien » compte, sans jeton
    e_new = f"{uuid.uuid4()}@casaguide-test.com"       # « nouveau » compte, avec jeton
    client.created_emails.extend([e_old, e_new])
    with _db() as conn:
        old = conn.execute(
            """INSERT INTO owners (email, full_name, email_verified)
               VALUES (%s, 'Ancien', FALSE) RETURNING id""", (e_old,)).fetchone()[0]
        new = conn.execute(
            """INSERT INTO owners (email, full_name, email_verified)
               VALUES (%s, 'Nouveau', FALSE) RETURNING id""", (e_new,)).fetchone()[0]
        conn.execute(
            """INSERT INTO password_resets (owner_id, token_hash, purpose, expires_at)
               VALUES (%s, %s, 'verify', now() + interval '1 hour')""",
            (new, "hash-quelconque"))
        conn.execute(migration)
        verified = dict(conn.execute(
            "SELECT id, email_verified FROM owners WHERE id = ANY(%s)",
            ([old, new],)).fetchall())
        conn.commit()
    assert verified[old] is True   # grand-périsé
    assert verified[new] is False  # laissé en attente
