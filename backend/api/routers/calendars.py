"""Calendrier des séjours : flux iCal & séjours (V2-23a).

Accessible à **tous les plans** dans cette session (les raffinements de gating —
planning staff exclusif Pro — viendront avec V2-23b). Toutes les écritures
passent par `require_write_access` (lecture seule à l'expiration d'essai, V2-18a).

L'URL iCal d'un flux est un **secret** (invariant 1) : chiffrée AES en base,
jamais renvoyée en clair (affichage masqué `mask_url`), jamais journalisée. Les
endpoints qui touchent aux URL répondent 503 si le chiffrement n'est pas
configuré (même motif que les secrets wifi).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from .. import calendars, crypto, repo
from ..deps import (CalendarFetcher, Conn, OwnedProperty, get_calendar_fetcher,
                    require_write_access)
from ..schemas import (
    BookingIn, BookingOut, BookingUpdate, CalendarCreateOut, CalendarIn,
    CalendarOut, CalendarViewOut, DeleteBookingOut, OverlapOut, RotationOut,
    SyncNowOut, SyncResultOut,
)

router = APIRouter(prefix="/api/properties/{property_id}", tags=["calendrier"])

# Cadence minimale entre deux « Synchroniser maintenant » d'un même logement :
# les plateformes ne rafraîchissent pas plus vite ; évite de marteler le service.
SYNC_COOLDOWN_S = 20.0


# ── Sérialisation ────────────────────────────────────────────────────────────

def _require_crypto():
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement non configuré (CASAGUIDE_SECRET_KEY absente)")


def _calendar_out(cal: dict) -> CalendarOut:
    """Vue masquée d'un flux (URL jamais en clair). `cal` peut porter
    `ical_url_enc` (on la masque) — on ne la recopie JAMAIS dans la réponse."""
    enc = cal.get("ical_url_enc")
    if enc is not None and crypto.is_configured():
        masked = calendars.mask_url(crypto.decrypt(enc) or "")
    else:
        masked = "…"
    return CalendarOut(
        id=cal["id"], platform=cal["platform"], masked_url=masked,
        last_sync_at=cal["last_sync_at"], last_sync_status=cal["last_sync_status"],
        sync_error=cal["sync_error"], created_at=cal["created_at"])


def _booking_out(b: dict, default_in, default_out) -> BookingOut:
    eff_in, eff_out = calendars.effective_times(b, default_in, default_out)
    is_direct = b["calendar_id"] is None and b["external_uid"] is None
    return BookingOut(
        id=b["id"], calendar_id=b["calendar_id"], starts_on=b["starts_on"],
        ends_on=b["ends_on"], checkin_time=b["checkin_time"],
        checkout_time=b["checkout_time"], eff_checkin_time=eff_in,
        eff_checkout_time=eff_out, source=b["source"],
        external_uid=b["external_uid"], is_direct=is_direct,
        guest_name=b["guest_name"], guest_contact=b["guest_contact"],
        notes=b["notes"], status=b["status"])


# ── Vue « Séjours » (une seule charge pour tout rendre) ──────────────────────

@router.get("/calendar", response_model=CalendarViewOut)
def calendar_view(conn: Conn, prop: OwnedProperty):
    """Flux + séjours + chevauchements + rotations + heures standard."""
    pid = str(prop["id"])
    default_in = prop["default_checkin_time"]
    default_out = prop["default_checkout_time"]
    bookings = repo.list_bookings(conn, pid)
    overlaps = calendars.compute_overlaps(bookings)
    rotations = calendars.compute_rotations(bookings, default_in, default_out)
    return CalendarViewOut(
        property_id=pid,
        default_checkin_time=default_in, default_checkout_time=default_out,
        calendars=[_calendar_out(c) for c in repo.list_calendars_with_url(conn, pid)],
        bookings=[_booking_out(b, default_in, default_out) for b in bookings],
        overlaps=[OverlapOut(a=a["id"], b=b["id"]) for a, b in overlaps],
        rotations=[RotationOut(**r) for r in rotations])


# ── Séjours (saisie directe, complétion, suppression) ────────────────────────

@router.post("/bookings", response_model=BookingOut,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_write_access)])
def create_booking(payload: BookingIn, conn: Conn, prop: OwnedProperty):
    if payload.ends_on <= payload.starts_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le départ doit être postérieur à l'arrivée (au moins une nuit).")
    b = repo.create_booking(conn, str(prop["id"]), payload.model_dump())
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"])


@router.patch("/bookings/{booking_id}", response_model=BookingOut,
              dependencies=[Depends(require_write_access)])
def update_booking(booking_id: str, payload: BookingUpdate, conn: Conn,
                   prop: OwnedProperty):
    fields = payload.model_dump(exclude_unset=True)
    # Cohérence des dates si l'une des deux est modifiée.
    if "starts_on" in fields or "ends_on" in fields:
        cur = repo.get_booking(conn, str(prop["id"]), booking_id)
        if not cur:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Séjour introuvable")
        start = fields.get("starts_on", cur["starts_on"])
        end = fields.get("ends_on", cur["ends_on"])
        if end <= start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Le départ doit être postérieur à l'arrivée.")
    b = repo.update_booking(conn, str(prop["id"]), booking_id, fields)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"])


@router.delete("/bookings/{booking_id}", response_model=DeleteBookingOut,
               dependencies=[Depends(require_write_access)])
def delete_booking(booking_id: str, conn: Conn, prop: OwnedProperty):
    """Saisie directe → supprimée ; séjour importé → annulé (conservé)."""
    outcome = repo.delete_booking(conn, str(prop["id"]), booking_id)
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    return DeleteBookingOut(outcome=outcome)


# ── Flux de calendrier (iCal) ────────────────────────────────────────────────

@router.get("/calendars", response_model=list[CalendarOut])
def list_calendars(conn: Conn, prop: OwnedProperty):
    return [_calendar_out(c)
            for c in repo.list_calendars_with_url(conn, str(prop["id"]))]


@router.post("/calendars", response_model=CalendarCreateOut,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_write_access)])
def add_calendar(payload: CalendarIn, conn: Conn, prop: OwnedProperty,
                 fetcher: Annotated[CalendarFetcher, Depends(get_calendar_fetcher)]):
    """Ajoute un flux et **valide au collage** : fetch + parse + 1re synchro
    immédiate → l'UI affiche « N séjours importés » ou l'erreur exacte."""
    _require_crypto()
    url = payload.ical_url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="L'URL du flux iCal doit commencer par http:// ou https://.")
    cal = repo.create_calendar(conn, str(prop["id"]), platform=payload.platform,
                               ical_url_enc=crypto.encrypt(url))
    full = repo.get_calendar(conn, str(prop["id"]), str(cal["id"]))
    res = calendars.sync_calendar(conn, full, fetch=fetcher)
    refreshed = repo.get_calendar(conn, str(prop["id"]), str(cal["id"]))
    return CalendarCreateOut(calendar=_calendar_out(refreshed),
                             sync=SyncResultOut(**res.as_dict()))


@router.delete("/calendars/{calendar_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_write_access)])
def delete_calendar(calendar_id: str, conn: Conn, prop: OwnedProperty):
    """Supprime un flux ; ses séjours passent 'cancelled' (jamais supprimés)."""
    if not repo.delete_calendar(conn, str(prop["id"]), calendar_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Flux introuvable")
    return None


@router.post("/calendar/sync", response_model=SyncNowOut,
             dependencies=[Depends(require_write_access)])
def sync_now(conn: Conn, prop: OwnedProperty,
             fetcher: Annotated[CalendarFetcher, Depends(get_calendar_fetcher)]):
    """« Synchroniser maintenant » : re-synchronise **tous** les flux du logement.
    Rate-limité (cooldown) — les plateformes ne rafraîchissent pas plus vite."""
    _require_crypto()
    pid = str(prop["id"])
    recent = repo.recent_calendar_sync_seconds(conn, pid)
    if recent is not None and recent < SYNC_COOLDOWN_S:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Synchronisation déjà effectuée à l'instant. Réessayez dans "
                   "quelques secondes.")
    results = calendars.sync_property(conn, pid, fetch=fetcher)
    return SyncNowOut(
        calendars=len(results),
        ok=sum(1 for r in results if r.ok),
        errors=sum(1 for r in results if not r.ok),
        created=sum(r.created for r in results),
        updated=sum(r.updated for r in results),
        cancelled=sum(r.cancelled for r in results))
