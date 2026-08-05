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
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("MEDIA_ROOT",
                      os.path.join(tempfile.gettempdir(), "casaguide-test-media"))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.deps import (get_calendar_fetcher, get_mailer,  # noqa: E402
                      get_translation_runner)
from api.mailer import ConsoleMailer  # noqa: E402
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
    mailer = ConsoleMailer()
    app.dependency_overrides[get_mailer] = lambda: mailer
    # Publication d'un guide → tâche de traduction : neutralisée (aucun réseau).
    app.dependency_overrides[get_translation_runner] = lambda: (
        lambda *a, **k: None)
    emails: list[str] = []
    c = TestClient(app)
    c.created_emails = emails  # type: ignore[attr-defined]
    c.mailer = mailer          # type: ignore[attr-defined]
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

def test_complete_and_qualify_unqualified_to_reservation(client):
    h = _register(client)
    pid = _make_property(client, h)
    FEED.text = _ical(_vevent("blk@a", "20260901", "20260905", "Not available"))
    add = client.post(f"/api/properties/{pid}/calendars", headers=h,
                      json={"platform": "airbnb", "ical_url": "https://x/c.ics"})
    assert add.status_code == 201
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    b = view["bookings"][0]
    assert b["nature"] == "unqualified" and b["status"] == "active"
    assert b["is_direct"] is False
    # Compléter + qualifier en réservation.
    r = client.patch(f"/api/properties/{pid}/bookings/{b['id']}", headers=h,
                     json={"nature": "reservation", "guest_name": "Martin",
                           "checkin_time": "17:00"})
    assert r.status_code == 200
    bb = r.json()
    assert bb["nature"] == "reservation" and bb["guest_name"] == "Martin"
    assert bb["eff_checkin_time"] == "17:00:00"


def test_direct_booking_defaults_to_reservation_nature(client):
    h = _register(client)
    pid = _make_property(client, h)
    b = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-20"}).json()
    assert b["nature"] == "reservation" and b["status"] == "active"


def test_rotation_signal_graded_in_view(client):
    """§2.2 : la vue du propriétaire porte le signal de rotation gradué (aide à la
    décision avant d'accorder une arrivée). Ambre → recommandation d'effectif."""
    h = _register(client)
    pid = _make_property(client, h)
    client.patch(f"/api/properties/{pid}", headers=h, json={"care_rules": {
        "turnaround": {"person_hours_full_occupancy": 6, "max_cleaners": 2,
                       "comfort_margin_hours": 1, "parallel_efficiency": 0.75}}})
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-10", "ends_on": "2026-08-20", "nature": "reservation"})
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-20", "ends_on": "2026-08-27", "nature": "reservation",
        "guest_count": 4})
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    sig = view["rotations"][0]["signal"]
    assert sig["level"] == "amber"                    # fenêtre 5 h, charge 6 h
    assert sig["recommended_cleaners"] == 2


def test_staff_planning_appears_in_cahier(client):
    """§2 : le cahier /s/ porte la frise des préparations (fenêtres + interventions
    en cours de séjour), coordonnées visibles pour un séjour à venir (RGPD)."""
    h = _register(client)
    r = client.post("/api/properties", json={
        "name": "Villa Ballarin", "address_line1": "C. Ejemplo 1",
        "city": "Orihuela Costa", "country_code": "ES"}, headers=h)
    prop = r.json()
    pid, staff_token = prop["id"], prop["staff_token"]
    # Séjour à venir de 14 nuits (à distance de la date du jour) → fenêtre + draps J+8.
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2027-06-10", "ends_on": "2027-06-24", "nature": "reservation",
        "guest_count": 6, "guest_name": "Famille Martin",
        "guest_contact": "+34 600 99 88 77"})
    s = client.get(f"/s/{staff_token}")
    assert s.status_code == 200
    assert "Préparations à venir" in s.text
    assert "À préparer" in s.text
    assert "Welcome pack pour 6" in s.text
    assert "Changement de draps" in s.text            # intervention en cours de séjour
    assert "+34 600 99 88 77" in s.text               # coordonnées (séjour à venir)


def test_link_mirror_block_hides_it_from_view(client):
    """§0.5 : rattacher un bloc importé à un séjour direct le retire de la vue
    (et donc du chevauchement), sans jamais le supprimer."""
    h = _register(client)
    pid = _make_property(client, h)
    # Séjour direct hors plateforme.
    direct = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19",
        "guest_name": "Location directe"}).json()
    # Bloc miroir importé qui double ces dates → chevauchement au départ.
    FEED.text = _ical(_vevent("blk@a", "20260812", "20260819", "Reserved"))
    client.post(f"/api/properties/{pid}/calendars", headers=h,
                json={"platform": "vrbo", "ical_url": "https://x/c.ics"})
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view["bookings"]) == 2 and len(view["overlaps"]) == 1
    block = next(b for b in view["bookings"] if not b["is_direct"])
    # Rattachement du bloc au séjour direct.
    r = client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                     json={"linked_booking_id": direct["id"]})
    assert r.status_code == 200
    view2 = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view2["bookings"]) == 1          # le bloc a disparu de la liste
    assert view2["overlaps"] == []              # plus de faux chevauchement
    assert view2["bookings"][0]["id"] == direct["id"]


def test_cannot_link_booking_to_itself(client):
    h = _register(client)
    pid = _make_property(client, h)
    b = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19"}).json()
    r = client.patch(f"/api/properties/{pid}/bookings/{b['id']}", headers=h,
                     json={"linked_booking_id": b["id"]})
    assert r.status_code == 422


# ── V2-23f : le rattachement absorbe la substance du bloc ─────────────────────

def _add_guest_request(booking_id: str, *, status: str = "pending") -> None:
    """Insère une demande d'origine 'guest' directement en base (elle viendrait en
    prod du POST public /g|/b/{token}/requests, hors périmètre de ce test)."""
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            """INSERT INTO booking_requests
                   (booking_id, label, quantity, origin, status)
               VALUES (%s, 'Ménage supplémentaire', 1, 'guest', %s)""",
            (booking_id, status))
        conn.commit()


def _make_master_and_block(client, h, pid):
    """Un séjour maître minimal (champs vides) et un bloc riche à rattacher."""
    master = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19",
        "guest_name": "Location directe"}).json()
    block = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19",
        "guest_name": "Tracy Russel", "guest_phone": "+34 600 111 222",
        "guest_email": "tracy@example.com", "guest_lang": "en",
        "guest_count": 4, "children_ages": [3, 7], "notes": "Arrivée tardive",
        "luggage_drop_time": "11:00", "keybox_code": "4242"}).json()
    return master, block


def test_link_absorbs_requests_and_empty_fields(client):
    """§0.5 / V2-23f : au rattachement, le maître absorbe les demandes du bloc
    (pending ET accepted) et ses coordonnées quand les siennes sont vides — jamais
    de perte silencieuse (cas Tracy Russel, 05/08)."""
    h = _register(client)
    pid = _make_property(client, h)
    master, block = _make_master_and_block(client, h, pid)
    # Deux demandes propriétaire sur le bloc (pending + accepted) et une du voyageur.
    client.post(f"/api/properties/{pid}/bookings/{block['id']}/requests", headers=h,
                json={"label": "Lit bébé", "quantity": 1, "status": "pending"})
    client.post(f"/api/properties/{pid}/bookings/{block['id']}/requests", headers=h,
                json={"label": "Draps", "quantity": 2, "status": "accepted"})
    _add_guest_request(block["id"])                      # guest pending → badge

    r = client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                     json={"linked_booking_id": master["id"]})
    assert r.status_code == 200

    # Les TROIS demandes vivent désormais sur le maître (transfert, pas perte).
    reqs = client.get(f"/api/properties/{pid}/bookings/{master['id']}/requests",
                      headers=h).json()
    assert {rq["label"] for rq in reqs} == {"Lit bébé", "Draps",
                                            "Ménage supplémentaire"}
    assert len(reqs) == 3
    # Le bloc n'en porte plus aucune.
    block_reqs = client.get(
        f"/api/properties/{pid}/bookings/{block['id']}/requests", headers=h).json()
    assert block_reqs == []

    # Le bandeau (calendrier) montre la demande voyageur en attente sur le maître.
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view["bookings"]) == 1                    # le bloc a disparu
    mv = view["bookings"][0]
    assert mv["id"] == master["id"]
    assert mv["pending_guest_requests"] == 1

    # Coordonnées absorbées (le maître était vide sur tous ces champs).
    assert mv["guest_phone"] == "+34 600 111 222"
    assert mv["guest_email"] == "tracy@example.com"
    assert mv["guest_lang"] == "en"
    assert mv["guest_count"] == 4
    assert mv["children_ages"] == [3, 7]
    assert mv["notes"] == "Arrivée tardive"
    assert mv["luggage_drop_time"] == "11:00:00"
    # Le code de boîte à clés (bytea chiffré) a suivi tel quel.
    kb = client.get(f"/api/properties/{pid}/bookings/{master['id']}/keybox",
                    headers=h).json()
    assert kb["keybox_code"] == "4242"


def test_link_never_overwrites_master_fields(client):
    """Un champ déjà renseigné sur le maître n'est JAMAIS écrasé (esprit inv. 13)."""
    h = _register(client)
    pid = _make_property(client, h)
    # Maître déjà garni de ses propres coordonnées.
    master = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19",
        "guest_phone": "+34 900 000 000", "guest_email": "owner@master.com",
        "guest_count": 2, "keybox_code": "0000"}).json()
    block = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19",
        "guest_phone": "+34 600 111 222", "guest_email": "tracy@example.com",
        "guest_count": 4, "keybox_code": "4242"}).json()
    client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                 json={"linked_booking_id": master["id"]})
    mv = client.get(f"/api/properties/{pid}/calendar", headers=h).json()["bookings"][0]
    assert mv["guest_phone"] == "+34 900 000 000"        # inchangé
    assert mv["guest_email"] == "owner@master.com"
    assert mv["guest_count"] == 2
    kb = client.get(f"/api/properties/{pid}/bookings/{master['id']}/keybox",
                    headers=h).json()
    assert kb["keybox_code"] == "0000"                   # le code du maître reste


def test_detach_does_not_migrate_back(client):
    """Le détachement (linked_booking_id→NULL) ne rejoue rien à l'envers : demandes
    et champs migrés RESTENT sur le maître (le transfert est un acte, pas un miroir)."""
    h = _register(client)
    pid = _make_property(client, h)
    master, block = _make_master_and_block(client, h, pid)
    client.post(f"/api/properties/{pid}/bookings/{block['id']}/requests", headers=h,
                json={"label": "Lit bébé", "quantity": 1, "status": "pending"})
    client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                 json={"linked_booking_id": master["id"]})
    # Détachement.
    r = client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                     json={"linked_booking_id": None})
    assert r.status_code == 200
    # La demande reste sur le maître ; le bloc reste vide.
    assert len(client.get(
        f"/api/properties/{pid}/bookings/{master['id']}/requests",
        headers=h).json()) == 1
    assert client.get(
        f"/api/properties/{pid}/bookings/{block['id']}/requests",
        headers=h).json() == []
    # Le bloc réapparaît dans la vue (détaché), sans redonner ses coordonnées.
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert len(view["bookings"]) == 2


def test_link_is_idempotent(client):
    """Re-rattacher le même bloc ne double aucune demande ni ne réécrit un champ."""
    h = _register(client)
    pid = _make_property(client, h)
    master, block = _make_master_and_block(client, h, pid)
    client.post(f"/api/properties/{pid}/bookings/{block['id']}/requests", headers=h,
                json={"label": "Lit bébé", "quantity": 1, "status": "pending"})
    for _ in range(3):
        r = client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                         json={"linked_booking_id": master["id"]})
        assert r.status_code == 200
    reqs = client.get(f"/api/properties/{pid}/bookings/{master['id']}/requests",
                      headers=h).json()
    assert len(reqs) == 1                                # zéro double


def test_link_to_foreign_booking_is_404(client):
    """La cible de rattachement doit appartenir au même logement (garde existante)."""
    h = _register(client)
    pid = _make_property(client, h)
    other_pid = _make_property(client, h)
    foreign = client.post(f"/api/properties/{other_pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19"}).json()
    block = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-19"}).json()
    r = client.patch(f"/api/properties/{pid}/bookings/{block['id']}", headers=h,
                     json={"linked_booking_id": foreign["id"]})
    assert r.status_code == 404


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


# ── Voyageurs & règles d'entretien (V2-23b, volet 1) ─────────────────────────

def test_property_seeds_default_care_rules_and_catalog(client):
    """À la création : care_rules par défaut + catalogue amorcé (§1.1/§1.2)."""
    h = _register(client)
    pid = _make_property(client, h)
    prop = client.get(f"/api/properties/{pid}", headers=h).json()
    assert prop["care_rules"]["linen_change_from_day"] == 8
    # hommes-heures laissés à null (à obtenir d'André).
    assert prop["care_rules"]["turnaround"]["person_hours_full_occupancy"] is None
    types = client.get(f"/api/properties/{pid}/request-types", headers=h).json()
    codes = {t["code"] for t in types}
    assert {"lit_bebe", "chaise_haute", "parasol", "lit_appoint"} <= codes


def test_care_rules_editable_via_patch(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.patch(f"/api/properties/{pid}", headers=h, json={
        "care_rules": {"linen_change_from_day": 5, "welcome_pack": "none",
                       "turnaround": {"person_hours_full_occupancy": 6}}})
    assert r.status_code == 200
    cr = r.json()["care_rules"]
    assert cr["linen_change_from_day"] == 5 and cr["welcome_pack"] == "none"
    assert cr["turnaround"]["person_hours_full_occupancy"] == 6


def test_booking_carries_guest_count_and_children(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-20",
        "guest_name": "Famille", "guest_contact": "+34 600",
        "guest_count": 6, "children_ages": [1, 3, 14]})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["guest_count"] == 6 and b["children_count"] == 3
    assert b["children_ages"] == [1, 3, 14]


def test_missing_info_surfaces_in_calendar_view(client):
    """§0.6 — un séjour futur occupé mais incomplet est signalé."""
    h = _register(client)
    pid = _make_property(client, h)
    # Séjour de 14 nuits, sans voyageurs ni contact → relance draps + voyageurs.
    future = dt.date.today() + dt.timedelta(days=20)
    client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": future.isoformat(),
        "ends_on": (future + dt.timedelta(days=14)).isoformat(),
        "guest_name": "Sans contact", "nature": "reservation"})
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    codes = {m["code"] for m in view["bookings"][0]["missing_info"]}
    assert "guest_count_missing" in codes
    assert "phone_missing" in codes         # §3.0 : le téléphone cale le rendez-vous


def test_booking_requests_crud(client):
    h = _register(client)
    pid = _make_property(client, h)
    bid = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-20"}).json()["id"]
    types = client.get(f"/api/properties/{pid}/request-types", headers=h).json()
    lit = next(t for t in types if t["code"] == "lit_bebe")
    # Crée une demande depuis le catalogue → libellé recopié, origin='owner'.
    r = client.post(f"/api/properties/{pid}/bookings/{bid}/requests", headers=h,
                    json={"request_type_id": lit["id"], "quantity": 1})
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["label"] == "Lit bébé" and req["origin"] == "owner"
    assert req["status"] == "accepted"
    lst = client.get(f"/api/properties/{pid}/bookings/{bid}/requests",
                     headers=h).json()
    assert len(lst) == 1
    # Suppression.
    assert client.delete(f"/api/properties/{pid}/requests/{req['id']}",
                         headers=h).status_code == 204
    assert client.get(f"/api/properties/{pid}/bookings/{bid}/requests",
                      headers=h).json() == []


def test_accepted_request_appears_in_interventions(client):
    h = _register(client)
    pid = _make_property(client, h)
    bid = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-08-12", "ends_on": "2026-08-26",
        "guest_count": 6, "nature": "reservation"}).json()["id"]
    types = client.get(f"/api/properties/{pid}/request-types", headers=h).json()
    lit = next(t for t in types if t["code"] == "lit_bebe")
    client.post(f"/api/properties/{pid}/bookings/{bid}/requests", headers=h,
                json={"request_type_id": lit["id"], "quantity": 1})
    inter = client.get(f"/api/properties/{pid}/bookings/{bid}/interventions",
                       headers=h).json()
    kinds = {i["kind"] for i in inter}
    assert "arrival" in kinds and "linen_change" in kinds
    arrival = next(i for i in inter if i["kind"] == "arrival")
    assert any("Lit bébé" in t for t in arrival["tasks"])
    assert any("Welcome pack pour 6" in t for t in arrival["tasks"])


def test_request_type_add_and_deactivate(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/request-types", headers=h,
                    json={"code": "rehausseur", "label": "Réhausseur"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    # Doublon de code → 409.
    assert client.post(f"/api/properties/{pid}/request-types", headers=h,
                       json={"code": "rehausseur", "label": "X"}).status_code == 409
    # Désactivation (jamais suppression → garde l'historique).
    r2 = client.patch(f"/api/properties/{pid}/request-types/{tid}", headers=h,
                      json={"is_active": False})
    assert r2.status_code == 200 and r2.json()["is_active"] is False
    active = client.get(f"/api/properties/{pid}/request-types", headers=h).json()
    assert any(t["id"] == tid and not t["is_active"] for t in active)


# ── Volet 3, §3.0 — coordonnées séparées (téléphone / email / langue) ─────────

def test_booking_carries_split_contact_fields(client):
    h = _register(client)
    pid = _make_property(client, h)
    r = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": "2026-09-01", "ends_on": "2026-09-08",
        "guest_name": "Meier", "guest_phone": "+41 79 123 45 67",
        "guest_email": "meier@example.com", "guest_lang": "de"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["guest_phone"] == "+41 79 123 45 67"
    assert b["guest_email"] == "meier@example.com"
    assert b["guest_lang"] == "de"
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    assert view["bookings"][0]["guest_lang"] == "de"


# ── Volet 3, §3.1 — demande de service du voyageur → planning ─────────────────

def _publish_with_cleaning(client, h, pid):
    """Rend B_cleaning (section « requestable ») visible et publie le guide.
    Renvoie le guide_token public."""
    client.put(f"/api/properties/{pid}/sections/B_cleaning", headers=h,
               json={"content": {"linen": "Draps fournis"}, "is_visible": True})
    client.patch(f"/api/properties/{pid}", headers=h, json={"status": "published"})
    return client.get(f"/api/properties/{pid}", headers=h).json()["guide_token"]


def _booking_covering_today(client, h, pid):
    today = dt.date.today()
    r = client.post(f"/api/properties/{pid}/bookings", headers=h, json={
        "starts_on": (today - dt.timedelta(days=1)).isoformat(),
        "ends_on": (today + dt.timedelta(days=5)).isoformat(),
        "guest_name": "En séjour", "nature": "reservation", "guest_count": 4})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_guest_request_creates_pending_and_notifies(client):
    h = _register(client)
    pid = _make_property(client, h)
    token = _publish_with_cleaning(client, h, pid)
    bid = _booking_covering_today(client, h, pid)

    client.mailer.sent.clear()   # ignore l'email de vérification d'inscription
    r = client.post(f"/g/{token}/requests",
                    json={"section": "B_cleaning", "note": "Draps le mercredi svp"})
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Ménage / draps supplémentaires"

    # La demande est rattachée au séjour en cours, en attente, d'origine 'guest'.
    reqs = client.get(f"/api/properties/{pid}/bookings/{bid}/requests",
                      headers=h).json()
    assert len(reqs) == 1
    assert reqs[0]["origin"] == "guest" and reqs[0]["status"] == "pending"
    assert reqs[0]["note"] == "Draps le mercredi svp"

    # Le propriétaire est notifié (badge + email best-effort).
    assert len(client.mailer.sent) == 1
    to, email = client.mailer.sent[0]
    assert "demande" in email.subject.lower()
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    row = next(b for b in view["bookings"] if b["id"] == bid)
    assert row["pending_guest_requests"] == 1


def test_guest_request_rate_limited(client):
    h = _register(client)
    pid = _make_property(client, h)
    token = _publish_with_cleaning(client, h, pid)
    _booking_covering_today(client, h, pid)
    assert client.post(f"/g/{token}/requests",
                       json={"section": "B_cleaning"}).status_code == 200
    # Deuxième demande immédiate → 429 (anti-abus par guide, §3.1).
    assert client.post(f"/g/{token}/requests",
                       json={"section": "B_cleaning"}).status_code == 429


def test_guest_request_unknown_or_hidden_section_404(client):
    h = _register(client)
    pid = _make_property(client, h)
    token = _publish_with_cleaning(client, h, pid)
    _booking_covering_today(client, h, pid)
    # A_checkin n'est pas « requestable » → 404 (rien ne fait foi).
    assert client.post(f"/g/{token}/requests",
                       json={"section": "A_checkin"}).status_code == 404


def test_guest_request_without_booking_409(client):
    h = _register(client)
    pid = _make_property(client, h)
    token = _publish_with_cleaning(client, h, pid)
    client.mailer.sent.clear()   # ignore l'email de vérification d'inscription
    # Guide publié mais aucun séjour enregistré → rien à quoi rattacher.
    r = client.post(f"/g/{token}/requests", json={"section": "B_cleaning"})
    assert r.status_code == 409
    assert client.mailer.sent == []   # aucune notification si rien n'est créé


def test_owner_accepts_guest_request(client):
    h = _register(client)
    pid = _make_property(client, h)
    token = _publish_with_cleaning(client, h, pid)
    bid = _booking_covering_today(client, h, pid)
    client.post(f"/g/{token}/requests", json={"section": "B_cleaning"})
    rid = client.get(f"/api/properties/{pid}/bookings/{bid}/requests",
                     headers=h).json()[0]["id"]
    # Le propriétaire accepte → la demande passe 'accepted'.
    r = client.patch(f"/api/properties/{pid}/requests/{rid}", headers=h,
                     json={"status": "accepted"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    # Elle n'est plus en attente dans le calendrier.
    view = client.get(f"/api/properties/{pid}/calendar", headers=h).json()
    row = next(b for b in view["bookings"] if b["id"] == bid)
    assert row["pending_guest_requests"] == 0
