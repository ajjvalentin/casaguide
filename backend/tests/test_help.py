"""Tests d'intégration du journal des recherches d'aide (V2-31, volet 3a).

Même harnais que test_api : vrai PostgreSQL, tout le code API de production.
Couvre : la recherche est journalisée avec le BON compte de résultats ; le
zéro-résultat est enregistré (c'est la métrique de santé de l'index) ; l'auteur
est bien le propriétaire authentifié ; la route exige une authentification.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")
os.environ.setdefault("CASAGUIDE_JWT_SECRET",
                      "test-secret-not-for-prod-0123456789-abcdefghij")
os.environ.setdefault(
    "CASAGUIDE_SECRET_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
os.environ.setdefault("CASAGUIDE_PBKDF2_ITER", "10000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("MEDIA_ROOT",
                      os.path.join(tempfile.gettempdir(), "casaguide-test-media"))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.deps import get_mailer  # noqa: E402
from api.mailer import ConsoleMailer  # noqa: E402
from api.main import app  # noqa: E402
from enrich.settings import settings  # noqa: E402


@pytest.fixture()
def client():
    app.dependency_overrides[get_mailer] = lambda: ConsoleMailer()
    emails: list[str] = []
    c = TestClient(app)
    c.created_emails = emails  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()
    with psycopg.connect(settings.db_dsn) as conn:
        for email in emails:
            # ON DELETE CASCADE : le journal de recherche disparaît avec le compte.
            conn.execute("DELETE FROM owners WHERE email = %s", (email,))
        conn.commit()


def _register(client) -> tuple[dict, str]:
    """Renvoie (headers d'auth, owner_id) d'un nouveau propriétaire."""
    email = f"{uuid.uuid4()}@casaguide-test.com"
    r = client.post("/api/auth/register", json={
        "email": email, "password": "password123", "full_name": "Aide"})
    assert r.status_code == 201, r.text
    client.created_emails.append(email)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, email


def _count_for(owner_email: str, query: str) -> list[tuple]:
    with psycopg.connect(settings.db_dsn) as conn:
        return conn.execute(
            """SELECT hs.query, hs.results_count
                 FROM help_searches hs JOIN owners o ON o.id = hs.owner_id
                WHERE o.email = %s ORDER BY hs.searched_at""",
            (owner_email,)).fetchall()


def test_search_is_logged_with_result_count(client):
    h, email = _register(client)
    r = client.post("/api/help/searches", headers=h,
                    json={"query": "envoyer le guide", "results_count": 3})
    assert r.status_code == 204
    rows = _count_for(email, "envoyer le guide")
    assert rows == [("envoyer le guide", 3)]


def test_zero_result_is_logged_health_metric(client):
    """Le zéro-résultat est le signal utile : il DOIT être journalisé."""
    h, email = _register(client)
    r = client.post("/api/help/searches", headers=h,
                    json={"query": "zorglubwxyz", "results_count": 0})
    assert r.status_code == 204
    rows = _count_for(email, "zorglubwxyz")
    assert rows == [("zorglubwxyz", 0)]


def test_search_requires_auth(client):
    r = client.post("/api/help/searches",
                    json={"query": "wifi", "results_count": 1})
    assert r.status_code == 401


def test_search_rejects_empty_query(client):
    h, _ = _register(client)
    r = client.post("/api/help/searches", headers=h,
                    json={"query": "", "results_count": 0})
    assert r.status_code == 422
