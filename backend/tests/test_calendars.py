"""Tests du calendrier des séjours (V2-23a, volet 1) — parser + moteur de synchro.

Le parser (`api.ical`) est pur (aucun réseau). Le moteur (`api.calendars`) est
exercé contre le vrai PostgreSQL (comme test_api) avec un **fetcher iCal simulé**
(aucun appel sortant). Couvre les invariants de la mission :
  #2  idempotence par UID (2 synchros = même état, zéro doublon) ;
      un événement disparu → 'cancelled' (conservé) ;
      les champs saisis à la main ne sont jamais écrasés par une synchro ;
  #1  l'URL iCal est chiffrée en base (jamais en clair) ;
  #3  un flux en erreur est enregistré, jamais bloquant.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import uuid
from pathlib import Path

# Environnement AVANT import des modules api (config/crypto lisent l'env à l'import).
os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")
os.environ.setdefault(
    "CASAGUIDE_SECRET_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")  # 32 o hex

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import calendars, crypto, ical, repo  # noqa: E402
from enrich.settings import settings  # noqa: E402


# ── Fabriques iCal ───────────────────────────────────────────────────────────

def _vevent(uid: str, start: str, end: str, summary: str) -> str:
    return (f"BEGIN:VEVENT\nUID:{uid}\n"
            f"DTSTART;VALUE=DATE:{start}\nDTEND;VALUE=DATE:{end}\n"
            f"SUMMARY:{summary}\nEND:VEVENT\n")


def _ical(*vevents: str) -> str:
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//EN\n"
            + "".join(vevents) + "END:VCALENDAR\n")


def _feeder(text: str):
    """Fetcher iCal simulé : renvoie toujours `text`, ignore l'URL."""
    return lambda url: text


# ── Parser (pur, aucun réseau) ───────────────────────────────────────────────

def test_parse_all_day_dtend_is_exclusive_departure_day():
    """Événement journée entière : DTEND est le jour du départ (exclusif iCal)."""
    events = ical.parse_events(_ical(
        _vevent("a@x", "20260812", "20260831", "Reserved")))
    assert len(events) == 1
    ev = events[0]
    assert ev.starts_on == dt.date(2026, 8, 12)
    assert ev.ends_on == dt.date(2026, 8, 31)     # départ le 31 (nuits 12→30)
    assert ev.status == "confirmed"


def test_parse_blocked_heuristic():
    events = ical.parse_events(_ical(
        _vevent("b@x", "20260901", "20260905", "CLOSED - Not available")))
    assert events[0].status == "blocked"


def test_parse_datetime_event_normalized_to_dates():
    """Un VEVENT en datetime (avec fuseau) est ramené au jour, sans planter."""
    ics = _ical("BEGIN:VEVENT\nUID:dt@x\n"
                "DTSTART:20260610T150000Z\nDTEND:20260615T100000Z\n"
                "SUMMARY:Booking\nEND:VEVENT\n")
    ev = ical.parse_events(ics)[0]
    assert ev.starts_on == dt.date(2026, 6, 10)
    assert ev.ends_on == dt.date(2026, 6, 15)


def test_parse_missing_dtend_is_one_night():
    ics = _ical("BEGIN:VEVENT\nUID:noend@x\n"
                "DTSTART;VALUE=DATE:20261001\nSUMMARY:Bloc\nEND:VEVENT\n")
    ev = ical.parse_events(ics)[0]
    assert ev.ends_on == ev.starts_on + dt.timedelta(days=1)


def test_parse_malformed_raises_icalerror():
    with pytest.raises(ical.ICalError):
        ical.parse_events("<html>not a calendar</html>")


def test_parse_empty_calendar_is_not_an_error():
    assert ical.parse_events(_ical()) == []


# ── Analyse : chevauchements, rotations, masquage (pur, aucun réseau) ─────────

def _bk(bid, start, end, status="confirmed", ci=None, co=None):
    return {"id": bid, "starts_on": dt.date(*start), "ends_on": dt.date(*end),
            "status": status, "checkin_time": ci, "checkout_time": co}


def test_overlap_detects_recovering_confirmed_pairs():
    a = _bk("a", (2026, 8, 10), (2026, 8, 20))
    b = _bk("b", (2026, 8, 15), (2026, 8, 25))     # recouvre a
    c = _bk("c", (2026, 9, 1), (2026, 9, 5))       # disjoint
    pairs = calendars.compute_overlaps([a, b, c])
    assert len(pairs) == 1
    assert {pairs[0][0]["id"], pairs[0][1]["id"]} == {"a", "b"}


def test_touching_stays_are_not_overlap_but_rotation():
    """Départ = arrivée le même jour : PAS un chevauchement (rotation)."""
    a = _bk("a", (2026, 8, 10), (2026, 8, 20))     # départ le 20
    b = _bk("b", (2026, 8, 20), (2026, 8, 27))     # arrivée le 20
    assert calendars.compute_overlaps([a, b]) == []
    # Heures standard : arrivée (checkin) 15:00, départ (checkout) 10:00.
    rots = calendars.compute_rotations([a, b], dt.time(15, 0), dt.time(10, 0))
    assert len(rots) == 1
    r = rots[0]
    assert r["on"] == dt.date(2026, 8, 20)
    assert r["departing"] == "a" and r["arriving"] == "b"
    assert r["gap_minutes"] == 300                 # checkout 10:00 → checkin 15:00 = 5 h


def test_rotation_uses_adjusted_times_when_present():
    a = _bk("a", (2026, 8, 10), (2026, 8, 20), co=dt.time(11, 0))
    b = _bk("b", (2026, 8, 20), (2026, 8, 27), ci=dt.time(16, 0))
    rots = calendars.compute_rotations([a, b], dt.time(15, 0), dt.time(10, 0))
    assert rots[0]["gap_minutes"] == 300           # checkout 11:00 → checkin 16:00 = 5 h


def test_blocked_and_cancelled_excluded_from_overlap():
    a = _bk("a", (2026, 8, 10), (2026, 8, 20))
    b = _bk("b", (2026, 8, 12), (2026, 8, 22), status="blocked")
    c = _bk("c", (2026, 8, 12), (2026, 8, 22), status="cancelled")
    assert calendars.compute_overlaps([a, b, c]) == []


def test_mask_url_never_reveals_the_url():
    masked = calendars.mask_url(
        "https://www.airbnb.com/calendar/ical/12345.ics?s=secret-token-abcd")
    assert "secret-token" not in masked
    assert "airbnb.com" in masked


# ── Fixtures base réelle ─────────────────────────────────────────────────────

@pytest.fixture()
def conn():
    c = psycopg.connect(settings.db_dsn)
    c.row_factory = psycopg.rows.dict_row
    emails: list[str] = []
    c._emails = emails  # type: ignore[attr-defined]
    yield c
    c.rollback()
    for email in emails:
        c.execute("DELETE FROM owners WHERE email = %s", (email,))
    c.commit()
    c.close()


def _make_property(conn) -> str:
    """Crée un propriétaire + un logement, renvoie l'id du logement."""
    email = f"{uuid.uuid4()}@casaguide-test.com"
    conn._emails.append(email)  # type: ignore[attr-defined]
    owner = conn.execute(
        "INSERT INTO owners (email, password_hash, full_name) "
        "VALUES (%s, 'x', 'Cal Test') RETURNING id", (email,)).fetchone()
    prop = conn.execute(
        "INSERT INTO properties (owner_id, name, address_line1, city, "
        "country_code) VALUES (%s, 'Villa', 'Rue 1', 'Ville', 'ES') "
        "RETURNING id", (owner["id"],)).fetchone()
    return str(prop["id"])


def _make_calendar(conn, property_id: str, url: str = "https://x/cal.ics",
                   platform: str = "airbnb") -> dict:
    cal = repo.create_calendar(conn, property_id, platform=platform,
                               ical_url_enc=crypto.encrypt(url))
    # get_calendar renvoie la ligne AVEC l'URL chiffrée (nécessaire à la synchro).
    return repo.get_calendar(conn, property_id, str(cal["id"]))


# ── URL iCal chiffrée en base (invariant 1) ──────────────────────────────────

def test_ical_url_is_encrypted_in_db(conn):
    pid = _make_property(conn)
    url = "https://airbnb.com/calendar/ical/secret-12345.ics"
    cal = repo.create_calendar(conn, pid, platform="airbnb",
                               ical_url_enc=crypto.encrypt(url))
    raw = conn.execute(
        "SELECT ical_url_enc FROM property_calendars WHERE id = %s",
        (cal["id"],)).fetchone()["ical_url_enc"]
    assert url.encode() not in bytes(raw)              # jamais en clair
    assert crypto.decrypt(raw) == url                  # déchiffrable côté serveur
    # La liste « publique » des flux n'expose pas l'URL chiffrée.
    listed = repo.list_calendars(conn, pid)
    assert "ical_url_enc" not in listed[0]


# ── Import & idempotence (invariant 2) ───────────────────────────────────────

def test_first_sync_imports_events(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    feed = _ical(_vevent("u1@a", "20260812", "20260831", "Reserved"),
                 _vevent("u2@a", "20260901", "20260905", "Not available"))
    res = calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    assert res.ok and res.created == 2 and res.total == 2
    rows = repo.list_bookings(conn, pid)
    assert len(rows) == 2
    by_uid = {r["external_uid"]: r for r in rows}
    assert by_uid["u1@a"]["status"] == "confirmed"
    assert by_uid["u1@a"]["source"] == "airbnb"
    assert by_uid["u2@a"]["status"] == "blocked"
    # Le flux est horodaté 'ok'.
    listed = repo.list_calendars(conn, pid)[0]
    assert listed["last_sync_status"] == "ok" and listed["last_sync_at"] is not None


def test_second_sync_is_idempotent(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    feed = _ical(_vevent("u1@a", "20260812", "20260831", "Reserved"))
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    res2 = calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    assert res2.ok and res2.created == 0 and res2.updated == 1
    assert len(repo.list_bookings(conn, pid)) == 1        # aucun doublon


def test_updated_dates_are_reflected(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("u1@a", "20260812", "20260831", "Reserved"))))
    calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("u1@a", "20260812", "20260902", "Reserved"))))  # départ repoussé
    b = repo.list_bookings(conn, pid)[0]
    assert b["ends_on"] == dt.date(2026, 9, 2)


# ── Disparition → cancelled (jamais supprimé) ────────────────────────────────

def test_disappeared_event_is_cancelled_not_deleted(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("keep@a", "20260812", "20260815", "Reserved"),
              _vevent("gone@a", "20260901", "20260905", "Reserved"))))
    # 2e flux : 'gone' a disparu.
    res = calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("keep@a", "20260812", "20260815", "Reserved"))))
    assert res.cancelled == 1
    rows = {r["external_uid"]: r for r in repo.list_bookings(conn, pid)}
    assert len(rows) == 2                                  # conservé, pas supprimé
    assert rows["gone@a"]["status"] == "cancelled"
    assert rows["keep@a"]["status"] == "confirmed"


def test_reappearing_cancelled_event_is_revived(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    feed = _ical(_vevent("u1@a", "20260812", "20260815", "Reserved"))
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    calendars.sync_calendar(conn, cal, fetch=_feeder(_ical()))          # disparaît
    assert repo.list_bookings(conn, pid)[0]["status"] == "cancelled"
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))             # revient
    assert repo.list_bookings(conn, pid)[0]["status"] == "confirmed"


# ── Champs manuels préservés (invariant 2) ───────────────────────────────────

def test_manual_fields_survive_sync(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    feed = _ical(_vevent("u1@a", "20260812", "20260831", "Reserved"))
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    b = repo.list_bookings(conn, pid)[0]
    # Le propriétaire complète le séjour importé à la main.
    repo.update_booking(conn, pid, str(b["id"]), {
        "guest_name": "Dupont", "guest_contact": "+34 600 000 000",
        "checkin_time": dt.time(16, 0), "notes": "Arrivée tardive"})
    # Une nouvelle synchro ne doit rien écraser des champs manuels.
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    b2 = repo.list_bookings(conn, pid)[0]
    assert b2["guest_name"] == "Dupont"
    assert b2["guest_contact"] == "+34 600 000 000"
    assert b2["checkin_time"] == dt.time(16, 0)
    assert b2["notes"] == "Arrivée tardive"


def test_manual_blocked_promotion_survives_sync(conn):
    """Un 'blocked' promu 'confirmed' à la main n'est pas rétrogradé par la synchro."""
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    feed = _ical(_vevent("u1@a", "20260901", "20260905", "Not available"))
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))
    b = repo.list_bookings(conn, pid)[0]
    assert b["status"] == "blocked"
    repo.update_booking(conn, pid, str(b["id"]),
                        {"status": "confirmed", "guest_name": "Martin"})
    calendars.sync_calendar(conn, cal, fetch=_feeder(feed))            # toujours 'blocked' côté flux
    b2 = repo.list_bookings(conn, pid)[0]
    assert b2["status"] == "confirmed" and b2["guest_name"] == "Martin"


# ── Erreurs non bloquantes (invariant 3) ─────────────────────────────────────

def test_malformed_feed_records_error_not_raises(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    res = calendars.sync_calendar(conn, cal, fetch=_feeder("<html>oops</html>"))
    assert res.status == "error" and res.error
    listed = repo.list_calendars(conn, pid)[0]
    assert listed["last_sync_status"] == "error"
    assert listed["sync_error"]


def test_network_error_is_non_blocking(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)

    def boom(url):
        raise httpx.ConnectError("connection refused")

    res = calendars.sync_calendar(conn, cal, fetch=boom)
    assert res.status == "error"
    # Le message ne divulgue jamais l'URL (secret, invariant 1).
    assert "https://" not in (res.error or "")


def test_one_bad_feed_does_not_block_others(conn):
    pid = _make_property(conn)
    good = _make_calendar(conn, pid, url="https://good/cal.ics", platform="airbnb")
    bad = _make_calendar(conn, pid, url="https://bad/cal.ics", platform="vrbo")

    def fetch(url):
        # good → flux valide ; bad → réseau KO. On ne connaît pas l'URL claire ici
        # (chiffrée), on distingue via l'ordre : deux flux, l'un lève.
        raise_for = fetch.calls
        fetch.calls += 1
        if raise_for == 0:
            return _ical(_vevent("g1@a", "20260812", "20260815", "Reserved"))
        raise httpx.ConnectError("refused")
    fetch.calls = 0

    results = calendars.sync_property(conn, pid, fetch=fetch)
    statuses = sorted(r.status for r in results)
    assert statuses == ["error", "ok"]                    # l'un échoue, l'autre passe
    assert len(repo.list_bookings(conn, pid)) == 1        # le bon flux a bien importé


# ── delete_calendar : séjours conservés (cancelled), flux retiré ─────────────

def test_delete_calendar_cancels_its_bookings(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("u1@a", "20260812", "20260815", "Reserved"))))
    assert repo.delete_calendar(conn, pid, str(cal["id"])) is True
    assert repo.list_calendars(conn, pid) == []           # flux retiré
    rows = repo.list_bookings(conn, pid)
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"  # séjour conservé


# ── delete_booking : direct supprimé, importé annulé ─────────────────────────

def test_delete_direct_booking_hard_deletes(conn):
    pid = _make_property(conn)
    b = repo.create_booking(conn, pid, {
        "starts_on": dt.date(2026, 7, 1), "ends_on": dt.date(2026, 7, 5),
        "source": "direct", "status": "confirmed", "guest_name": "Test"})
    assert repo.delete_booking(conn, pid, str(b["id"])) == "deleted"
    assert repo.list_bookings(conn, pid) == []


def test_delete_imported_booking_cancels(conn):
    pid = _make_property(conn)
    cal = _make_calendar(conn, pid)
    calendars.sync_calendar(conn, cal, fetch=_feeder(
        _ical(_vevent("u1@a", "20260812", "20260815", "Reserved"))))
    b = repo.list_bookings(conn, pid)[0]
    assert repo.delete_booking(conn, pid, str(b["id"])) == "cancelled"
    rows = repo.list_bookings(conn, pid)
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"
