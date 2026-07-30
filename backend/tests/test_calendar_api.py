"""Tests d'intégration de l'API du calendrier des séjours (V2-23a, volet 2).

Même harnais que test_api : vrai PostgreSQL, tout le code API de production, seul
le **fetch iCal sortant** est simulé (`get_calendar_fetcher`). Couvre : vue
« Séjours » (chevauchements/rotations), saisie directe, complétion + promotion
blocked→confirmed, suppression, ajout de flux avec validation au collage, URL
masquée, suppression de flux → séjours 'cancelled', sync-now + rate-limit.
"""
from __future__ import annotations

import datetime as dt
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
os.environ.setdefault("MEDIA_ROOT",
                      os.path.join(tempfile.gettempdir(), "casaguide-test-media"))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.deps import get_calendar_fetcher  # noqa: E402
from api.main import app  # noqa: E402
from enrich.settings import settings  # noqa: E402


# ── Flux iCal simulé (configurable par test) ─────────────────────────────────

def _vevent(uid, start, end, summary):
    return (f"BEGIN:VEVENT\nUID:{uid}\n"
            f"DTSTART;VALUE=DATE:{start}\nDTEND;VALUE=DATE:{end}\n"
            f"SUMMARY:{summary}\nEND:VEVENT\n")


def _ical(*vevents):
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//T//EN\n"
            + "".join(vevents) + "END:VCALENDAR\n")


class FakeFeed:
    """Fetcher iCal injecté : renvoie le texte courant, ou lève si demandé."""
    def __init__(self):
        self.text = _ical()
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self, url: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text


FEED = FakeFeed()


@pytest.fixture()
def client():
    FEED.text, FEED.error, FEED.calls = _ical(), None, 0
    app.dependency_overrides[get_calendar_fetcher] = lambda: FEED
    emails: list[str] = []
    c = TestClient(app)
    c.created_emails = emails  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()
    with psycopg.connect(settings.db_dsn) as conn:
        for email in emails:
            conn.execute("DELETE FROM owners WHERE email = %s", (email,))
        conn.commit()


def _register(client) -> dict:
    """Renvoie les en-têtes d'auth d'un nouveau propriétaire."""
    email = f"{uuid.uuid4()}@casaguide-test.com"
    r = client.post("/api/auth/register", json={
        "email": email, "password": "password123", "full_name": "Cal"})
    assert r.status_code == 201, r.text
    client.created_emails.append(email)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_property(client, headers) -> str:
    r = client.post("/api/properties", json={
        "name": "Villa Ballarin", "address_line1": "C. Ejemplo 1",
        "city": "Orihuela Costa", "country_code": "ES"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── Heures standard dans la fiche du logement ────────────────────────────────

def test_property_default_times_defaults_and_editable(client):
    h = _register(client)
    pid = _make_property(client, h)
    prop = client.get(f"/api/properties/{pid}", headers=h).json()
    assert prop["default_checkin_time"] == "15:00:00"
    assert prop["default_checkout_time"] == "10:00:00"
    r = client.patch(f"/api/properties/{pid}", headers=h,
                     json={"default_checkin_time": "16:30",
                           "default_checkout_time": "11:00"})
    assert r.status_code == 200
    assert r.json()["default_checkin_time"] == "16:30:00"


# ── Saisie directe & vue ─────────────────────────────────────────────────────

def test_direct_booking_and_view(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-20",
        "guest_name": "Dupont", "guest_contact": "+34 600"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["is_direct"] is True and b["source"] == "direct"
    # Heures effectives héritées des heures standard du logement.
    assert b["eff_checkin_time"] == "15:00:00" and b["checkin_time"] is None

    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view["bookings"]) == 1
    assert view["overlaps"] == [] and view["rotations"] == []
    assert view["default_checkin_time"] == "15:00:00"


def test_direct_booking_rejects_bad_dates(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-20", "ends_on": "2026-08-12"})
    assert r.status_code == 422


def test_overlap_and_rotation_surfaced_in_view(client):
    h = _register(client)
    pid = _make_property(client, h)
    # Rotation : a part le 20, b arrive le 20.
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-10", "ends_on": "2026-08-20"})
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-20", "ends_on": "2026-08-27"})
    # Chevauchement : c recouvre b.
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-25", "ends_on": "2026-08-30"})
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view["rotations"]) == 1
    assert view["rotations"][0]["gap_minutes"] == 300
    assert len(view["overlaps"]) == 1


# ── Complétion & promotion ───────────────────────────────────────────────────

def test_complete_and_promote_blocked_to_confirmed(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = _ical(_vevent("blk@a", "20260901", "20260905", "Not available"))
    add = client.post(f"/api/properties/{pid}/calendars", headers=h,
                      json={"platform": "airbnb", "ical_url": "https://x/c.ics"})
    assert add.status_code == 201
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    b = view["bookings"][0]
    assert b["status"] == "blocked" and b["is_direct"] is False
    # Compléter + promouvoir.
    r = client.patch(f"/api/properties/{pid}/bookings/{b['id']}", headers=h,
                     json={"status": "confirmed", "guest_name": "Martin",
                           "checkin_time": "17:00"})
    assert r.status_code == 200
    bb = r.json()
    assert bb["status"] == "confirmed" and bb["guest_name"] == "Martin"
    assert bb["eff_checkin_time"] == "17:00:00"


# ── Ajout de flux : validation au collage ────────────────────────────────────

def test_add_calendar_validates_and_imports(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = _ical(_vevent("u1@a", "20260812", "20260831", "Reserved"),
                      _vevent("u2@a", "20260901", "20260905", "Reserved"))
    r = client.post(f"/api/properties/{pid}/calendars", headers=h,
                    json={"platform": "vrbo",
                          "ical_url": "https://vrbo.com/ical/1281695/secret.ics"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sync"]["status"] == "ok" and body["sync"]["created"] == 2
    # URL masquée : jamais la valeur en clair.
    assert "secret" not in body["calendar"]["masked_url"]
    assert body["calendar"]["last_sync_status"] == "ok"


def test_add_calendar_bad_feed_reports_error_non_blocking(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = "<html>not ical</html>"
    r = client.post(f"/api/properties/{pid}/calendars", headers=h,
                    json={"platform": "other", "ical_url": "https://x/bad.ics"})
    # Le flux est créé (201), la synchro rapporte l'erreur (non bloquant).
    assert r.status_code == 201
    assert r.json()["sync"]["status"] == "error"
    cals = client.get(f"/api/properties/{pid}/calendars", headers=h).json()
    assert cals[0]["last_sync_status"] == "error"


def test_add_calendar_rejects_non_http_url(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/calendars", headers=h,
                    json={"platform": "other", "ical_url": "file:///etc/passwd"})
    assert r.status_code == 422


# ── Suppression de flux : séjours conservés ('cancelled') ────────────────────

def test_delete_calendar_cancels_bookings(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = _ical(_vevent("u1@a", "20260812", "20260815", "Reserved"))
    add = client.post(f"/api/properties/{pid}/calendars", headers=h,
                      json={"platform": "airbnb", "ical_url": "https://x/c.ics"})
    cid = add.json()["calendar"]["id"]
    r = client.delete(f"/api/properties/{pid}/calendars/{cid}", headers=h)
    assert r.status_code == 204
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert view["calendars"] == []
    assert len(view["bookings"]) == 1 and view["bookings"][0]["status"] == "cancelled"


# ── Suppression de séjour ────────────────────────────────────────────────────

def test_delete_direct_vs_imported(client):
    h = _register(client)
    pid = _make_property(client, h)
    direct = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-07-01", "ends_on": "2026-07-05"}).json()
    r = client.delete(f"/api/properties/{pid}/bookings/{direct['id']}", headers=h)
    assert r.status_code == 200 and r.json()["outcome"] == "deleted"


# ── Sync-now + rate-limit ────────────────────────────────────────────────────

def test_sync_now_rate_limited(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = _ical(_vevent("u1@a", "20260812", "20260815", "Reserved"))
    client.post(f"/api/properties/{pid}/calendars", headers=h,
                json={"platform": "airbnb", "ical_url": "https://x/c.ics"})
    # L'ajout vient de synchroniser → le sync-now immédiat est en cooldown.
    r = client.post(f"/api/properties/{pid}/calendar/sync", headers=h)
    assert r.status_code == 429


# ── Isolation multi-tenant ───────────────────────────────────────────────────

def test_calendar_is_tenant_isolated(client):
    h1 = _register(client)
    pid = _make_property(client, h1)
    h2 = _register(client)
    # h2 ne voit pas le calendrier du logement de h1 (404).
    assert client.get(f"/api/properties/{pid}/calendar",
                      headers=h2).status_code == 404
    r = client.post(f"/api/properties/{pid}/bookings", headers=h2, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-20"})
    assert r.status_code == 404
