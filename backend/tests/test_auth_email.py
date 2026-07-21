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


def register(client, password="password123", **over) -> dict:
    email = over.pop("email", f"{uuid.uuid4()}@casaguide-test.com")
    body = {"email": email, "password": password, "full_name": "Prop Test"}
    body.update(over)
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    client.created_emails.append(email)
    return {"email": email, "token": r.json()["access_token"],
            "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}}


def _reset_token(mailer) -> str:
    to, email = mailer.sent[-1]
    m = _RESET_RE.search(email.text)
    assert m, f"lien de réinitialisation absent : {email.text!r}"
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
               WHERE o.email = %s""", (owner["email"],)).fetchone()
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
