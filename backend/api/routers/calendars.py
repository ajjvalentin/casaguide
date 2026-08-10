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

import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from .. import calendars, care, crypto, guidesend, repo
from ..deps import (CalendarFetcher, Conn, OwnedProperty, get_calendar_fetcher,
                    require_write_access)
from ..schemas import (
    BookingIn, BookingInheritIn, BookingKeyboxOut, BookingOut, BookingRequestIn,
    BookingRequestOut, BookingRequestUpdate, BookingUpdate, CalendarCreateOut,
    CalendarIn, CalendarOut, CalendarViewOut, DeleteBookingOut, InterventionOut,
    OverlapOut, RequestTypeIn, RequestTypeOut, RequestTypeUpdate, RotationOut,
    SyncNowOut, SyncResultOut, WhatsAppQueueEntry,
)

# Libellé humain de la plateforme pour le message de succession (V2-23h) — jamais
# un code brut dans le texte proposé au propriétaire.
_PLATFORM_LABELS = {"airbnb": "Airbnb", "vrbo": "Vrbo / Abritel",
                    "booking": "Booking"}

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


def _encode_keybox(code: str | None) -> bytes | None:
    """Chiffre la surcharge du code de boîte à clés d'un séjour (V2-23c volet 2),
    ou None si vide (repli sur le code du logement). Exige la clé UNIQUEMENT si une
    valeur est réellement fournie (un séjour sans surcharge ne dépend pas du
    chiffrement)."""
    if not code:
        return None
    _require_crypto()
    return crypto.encrypt(code)


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


def _short_range(start: _dt.date, end: _dt.date) -> str:
    """Plage compacte et humaine (« 07–22.08 » ; mois/années différents étalés) —
    pour le libellé de succession, jamais un uid (V2-23h, principe 0.4)."""
    if start.year == end.year and start.month == end.month:
        return f"{start.day:02d}–{end.day:02d}.{start.month:02d}"
    if start.year == end.year:
        return f"{start.day:02d}.{start.month:02d} → {end.day:02d}.{end.month:02d}"
    return (f"{start.day:02d}.{start.month:02d}.{start.year} → "
            f"{end.day:02d}.{end.month:02d}.{end.year}")


def _succession_payload(source: dict) -> dict:
    """Bandeau/signal de succession (V2-23h) construit côté serveur — UNE source de
    vérité, langage humain, jamais d'uid. Le front n'affiche que `message` + un
    bouton « Reprendre la fiche » ciblant `source_id`."""
    rng = _short_range(source["starts_on"], source["ends_on"])
    name = (source.get("guest_name") or "").strip()
    label = f"{name} · {rng}" if name else f"séjour du {rng}"
    platform = _PLATFORM_LABELS.get(source.get("source"), "la plateforme")
    return {
        "source_id": str(source["id"]),
        "source_label": label,
        "message": (f"Ce séjour remplace peut-être « {label} », annulé par le "
                    f"flux {platform}. Reprendre sa fiche ?"),
    }


def _booking_out(b: dict, default_in, default_out, *,
                 care_rules: dict | None = None,
                 today: _dt.date | None = None,
                 pending_requests: int = 0,
                 auto_send_guide: bool = False,
                 guide_already_sent: bool = False,
                 succession: dict | None = None) -> BookingOut:
    eff_in, eff_out = calendars.effective_times(b, default_in, default_out)
    is_direct = b["calendar_id"] is None and b["external_uid"] is None
    # Relance active (§0.6) : ce qui manque pour préparer ce séjour — dont l'email
    # manquant pour l'envoi automatique du guide à J-7 (V2-23d volet 2). Le motif
    # « email manquant » s'éteint dès que le guide est parti par n'importe quel canal
    # (guide_already_sent, V2-32 volet 1 §4).
    missing = care.missing_info(b, care_rules or {},
                                today=today or _dt.date.today(),
                                auto_send_guide=auto_send_guide,
                                guide_already_sent=guide_already_sent)
    return BookingOut(
        id=b["id"], calendar_id=b["calendar_id"], starts_on=b["starts_on"],
        ends_on=b["ends_on"], checkin_time=b["checkin_time"],
        checkout_time=b["checkout_time"], eff_checkin_time=eff_in,
        eff_checkout_time=eff_out,
        luggage_drop_time=b["luggage_drop_time"],
        luggage_until_time=b["luggage_until_time"],
        source=b["source"], external_uid=b["external_uid"], is_direct=is_direct,
        guest_name=b["guest_name"], guest_contact=b["guest_contact"],
        guest_phone=b["guest_phone"], guest_email=b["guest_email"],
        guest_lang=b["guest_lang"],
        notes=b["notes"], nature=b["nature"], status=b["status"],
        linked_booking_id=b["linked_booking_id"],
        dates_overridden=b["dates_overridden"],
        feed_starts_on=b["feed_starts_on"], feed_ends_on=b["feed_ends_on"],
        guest_count=b["guest_count"],
        children_count=care.children_count(b),
        children_ages=list(b["children_ages"] or []),
        pending_guest_requests=pending_requests,
        missing_info=missing, succession=succession)


# ── Vue « Séjours » (une seule charge pour tout rendre) ──────────────────────

@router.get("/calendar", response_model=CalendarViewOut)
def calendar_view(conn: Conn, prop: OwnedProperty):
    """Flux + séjours + chevauchements + rotations + heures standard."""
    pid = str(prop["id"])
    default_in = prop["default_checkin_time"]
    default_out = prop["default_checkout_time"]
    care_rules = prop["care_rules"]
    today = _dt.date.today()
    # Un bloc miroir **rattaché** (§0.5) disparaît de la liste et du planning : il
    # double un séjour déjà présent (une seule arrivée pour l'équipe). Jamais
    # supprimé pour autant — la synchro le recréerait ; il reste en base, masqué.
    bookings = [b for b in repo.list_bookings(conn, pid)
                if b["linked_booking_id"] is None]
    overlaps = calendars.compute_overlaps(bookings)
    rotations = calendars.compute_rotations(bookings, default_in, default_out)
    by_id = {str(b["id"]): b for b in bookings}
    # Demandes du voyageur EN ATTENTE par séjour (§3.1) → badge dans le calendrier ;
    # et l'ensemble des séjours PORTEURS d'au moins une demande (V2-23h : une demande
    # suffit à faire la « substance » d'un prédécesseur annulé).
    pending: dict[str, int] = {}
    requested_ids: set[str] = set()
    for r in repo.list_requests_for_property(conn, pid):
        bid = str(r["booking_id"])
        requested_ids.add(bid)
        if r["origin"] == "guest" and r["status"] == "pending":
            pending[bid] = pending.get(bid, 0) + 1
    # Succession d'identifiants (V2-23h) : un nouvel import vierge remplace peut-être
    # un prédécesseur annulé par une modification de réservation plateforme. Détecté
    # AU RENDU (aucun état de plus) ; proposé, jamais automatique.
    succession: dict[str, dict] = {}
    for b in bookings:
        cand = care.find_succession_candidate(b, bookings,
                                               requested_ids=requested_ids)
        if cand is not None:
            succession[str(b["id"])] = _succession_payload(cand)
    # V2-32 volet 1 — file des guides à envoyer par WhatsApp (J-7 assisté) et registre
    # d'idempotence. `sent_ids` = séjours dont le guide est déjà parti (tout canal) :
    # il retire ces séjours de la file ET éteint la relance « email manquant » (§4).
    sent_ids = repo.stay_sent_booking_ids(conn, pid)
    whatsapp_queue = _whatsapp_queue(conn, prop, bookings, sent_ids, today)
    return CalendarViewOut(
        property_id=pid,
        default_checkin_time=default_in, default_checkout_time=default_out,
        calendars=[_calendar_out(c) for c in repo.list_calendars_with_url(conn, pid)],
        bookings=[_booking_out(b, default_in, default_out, care_rules=care_rules,
                               today=today,
                               pending_requests=pending.get(str(b["id"]), 0),
                               auto_send_guide=prop["auto_send_guide"],
                               guide_already_sent=str(b["id"]) in sent_ids,
                               succession=succession.get(str(b["id"])))
                  for b in bookings],
        overlaps=[OverlapOut(a=a["id"], b=b["id"]) for a, b in overlaps],
        rotations=[RotationOut(**r, signal=_rotation_signal(
            r, by_id, care_rules, default_in, default_out)) for r in rotations],
        whatsapp_queue=whatsapp_queue)


def _whatsapp_queue(conn, prop: dict, bookings: list[dict], sent_ids: set[str],
                    today: _dt.date) -> list[WhatsAppQueueEntry]:
    """File des guides prêts à envoyer par WhatsApp pour CE logement (V2-32 volet 1).
    On réutilise le moteur PUR `care.select_auto_sends` (une seule source de vérité
    des critères J-7) en enrichissant chaque séjour du contexte logement attendu, puis
    on projette sa sortie `to_whatsapp`. La langue effective (guest_lang si offerte,
    sinon langue du logement) est calculée une fois via les langues offertes du guide
    (invariant 15)."""
    offered = set(guidesend.offered_langs(conn, prop))
    default_lang = (prop.get("default_lang") or "fr")
    published = prop["status"] == "published"
    candidates = []
    for b in bookings:
        c = dict(b)
        c["auto_send_guide"] = prop["auto_send_guide"]
        c["published"] = published
        c["already_sent"] = str(b["id"]) in sent_ids
        candidates.append(c)
    plan = care.select_auto_sends(candidates, today=today)
    out = []
    for b in plan.to_whatsapp:
        gl = (b.get("guest_lang") or "").lower()
        lang = gl if gl in offered else default_lang
        out.append(WhatsAppQueueEntry(
            booking_id=b["id"], guest_name=b.get("guest_name"),
            starts_on=b["starts_on"], ends_on=b["ends_on"], lang=lang,
            phone=care.effective_phone(b)))
    return out


def _rotation_signal(rot: dict, by_id: dict, care_rules: dict | None,
                     default_in, default_out) -> dict | None:
    """Signal de rotation gradué (§2.2) pour le calendrier du propriétaire — aide
    à décider AVANT d'accorder une arrivée anticipée. Le calcul part de l'échéance
    la plus proche : un **dépôt de bagages** de l'arrivant avance l'échéance et peut
    faire basculer une rotation confortable en rotation serrée."""
    arr = by_id.get(str(rot["arriving"]))
    dep = by_id.get(str(rot["departing"]))
    if not arr or not dep:
        return None
    checkin = arr.get("checkin_time") or default_in
    deadline = arr.get("luggage_drop_time") or checkin
    _, checkout = calendars.effective_times(dep, default_in, default_out)
    window = int((_dt.datetime.combine(rot["on"], deadline)
                  - _dt.datetime.combine(rot["on"], checkout)).total_seconds() // 60)
    return care.turnaround_signal(care_rules or {}, arr.get("guest_count"), window)


# ── Séjours (saisie directe, complétion, suppression) ────────────────────────

@router.post("/bookings", response_model=BookingOut,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_write_access)])
def create_booking(payload: BookingIn, conn: Conn, prop: OwnedProperty):
    if payload.ends_on <= payload.starts_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le départ doit être postérieur à l'arrivée (au moins une nuit).")
    data = payload.model_dump()
    data["keybox_code_enc"] = _encode_keybox(data.pop("keybox_code"))
    b = repo.create_booking(conn, str(prop["id"]), data)
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"],
                        care_rules=prop["care_rules"],
                        auto_send_guide=prop["auto_send_guide"])


@router.patch("/bookings/{booking_id}", response_model=BookingOut,
              dependencies=[Depends(require_write_access)])
def update_booking(booking_id: str, payload: BookingUpdate, conn: Conn,
                   prop: OwnedProperty):
    fields = payload.model_dump(exclude_unset=True)
    # Surcharge du code de boîte à clés (V2-23c volet 2) : chiffrée avant stockage ;
    # présente et vide → NULL (repli sur le code du logement).
    if "keybox_code" in fields:
        fields["keybox_code_enc"] = _encode_keybox(fields.pop("keybox_code"))
    # Rattachement d'un bloc miroir (§0.5) : la cible doit être un autre séjour du
    # même logement (jamais soi-même). `None` = détacher (toujours autorisé).
    if fields.get("linked_booking_id") is not None:
        target = str(fields["linked_booking_id"])
        if target == booking_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Un séjour ne peut pas être rattaché à lui-même.")
        if not repo.get_booking(conn, str(prop["id"]), target):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Séjour de rattachement introuvable.")
        fields["linked_booking_id"] = target
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
        # V2-23g — dates ajustées à la main : le propriétaire qui **déplace
        # réellement** les dates d'un séjour IMPORTÉ en prend possession → la
        # synchro cessera de les rafraîchir (marqueur automatique, l'intention EST
        # la modification, aucun geste en plus). On ne pose le marqueur QUE si une
        # date change effectivement : le front renvoie toujours starts_on/ends_on
        # dans le PATCH (même quand seul le nom change) — sans cette comparaison,
        # toute édition figerait les dates. Une saisie directe (pas de flux) n'est
        # jamais concernée : rien ne l'écrase. « Reprendre les dates du flux » passe
        # par un chemin dédié (reset-dates), jamais par ici.
        if cur["external_uid"] is not None and (
                start != cur["starts_on"] or end != cur["ends_on"]):
            fields["dates_overridden"] = True
    b = repo.update_booking(conn, str(prop["id"]), booking_id, fields)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    # V2-23f — le rattachement absorbe la substance du bloc : ses demandes et ses
    # coordonnées migrent vers le maître AVANT que le bloc ne disparaisse de la vue
    # (jamais de perte silencieuse). Point UNIQUE : les deux chemins d'UI (modale
    # §0.5 et bandeau de chevauchement §0.4) passent par ce PATCH.
    if fields.get("linked_booking_id") is not None:
        repo.absorb_block_into_master(conn, str(prop["id"]), booking_id,
                                      fields["linked_booking_id"])
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"],
                        care_rules=prop["care_rules"],
                        auto_send_guide=prop["auto_send_guide"])


@router.post("/bookings/{booking_id}/reset-dates", response_model=BookingOut,
             dependencies=[Depends(require_write_access)])
def reset_booking_dates(booking_id: str, conn: Conn, prop: OwnedProperty):
    """« Reprendre les dates du flux » (V2-23g) : réaligne un séjour importé sur les
    dernières dates annoncées par le flux et **redonne la main à la synchro**
    (`dates_overridden` → FALSE). Réservé aux séjours **importés** ; une saisie
    directe n'a pas de flux → 404. Chemin dédié, distinct du PATCH (qui reposerait
    le marqueur), pour que la reprise soit un geste net et réversible."""
    b = repo.reset_booking_to_feed_dates(conn, str(prop["id"]), booking_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour importé introuvable")
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"],
                        care_rules=prop["care_rules"],
                        auto_send_guide=prop["auto_send_guide"])


@router.post("/bookings/{booking_id}/inherit", response_model=BookingOut,
             dependencies=[Depends(require_write_access)])
def inherit_booking(booking_id: str, payload: BookingInheritIn, conn: Conn,
                    prop: OwnedProperty):
    """Reprise de fiche à la succession d'identifiants (V2-23h) : le NOUVEAU séjour
    hérite de la fiche de son prédécesseur ANNULÉ (réémis sous un nouvel uid par la
    plateforme). Proposition validée par le propriétaire (jamais automatique).

    `source_id` validé : même logement, 'cancelled', importé. Diffère du
    rattachement d'un bloc miroir (V2-23f) sur deux points GRAVÉS (cf.
    `repo.inherit_booking_fiche`) : pas de `linked_booking_id` (succession, pas
    miroir) et le registre `guide_sends` n'est JAMAIS hérité (le nouveau lien /b/
    doit être VIVANT)."""
    pid = str(prop["id"])
    if not repo.get_booking(conn, pid, booking_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    source_id = str(payload.source_id)
    if source_id == booking_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Un séjour ne peut hériter de lui-même.")
    source = repo.get_booking(conn, pid, source_id)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour source introuvable.")
    if source["status"] != "cancelled" or source["external_uid"] is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La source d'une reprise doit être un séjour importé et annulé.")
    repo.inherit_booking_fiche(conn, pid, source_id, booking_id)
    b = repo.get_booking(conn, pid, booking_id)
    return _booking_out(b, prop["default_checkin_time"],
                        prop["default_checkout_time"],
                        care_rules=prop["care_rules"],
                        auto_send_guide=prop["auto_send_guide"])


@router.get("/bookings/{booking_id}/keybox", response_model=BookingKeyboxOut)
def get_booking_keybox(booking_id: str, conn: Conn, prop: OwnedProperty):
    """Surcharge déchiffrée du code de boîte à clés d'un séjour, pour pré-remplir la
    modale (V2-23c volet 2). Réservé au propriétaire (`OwnedProperty`) — même chemin
    d'édition que les secrets du logement (`GET .../secrets`). La surcharge ne
    transite JAMAIS par `GET /calendar` ni `BookingOut` : c'est l'unique lecture.
    404 si le séjour n'appartient pas au logement (garde-fou multi-tenant)."""
    row = repo.get_booking_keybox_enc(conn, str(prop["id"]), booking_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    enc = row["keybox_code_enc"]
    if enc is not None:
        _require_crypto()
    return BookingKeyboxOut(keybox_code=crypto.decrypt(enc))


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


# ── Catalogue de demandes particulières (V2-23b, §1.2) ───────────────────────

def _request_type_out(t: dict) -> RequestTypeOut:
    return RequestTypeOut(id=t["id"], code=t["code"], label=t["label"],
                          sort_order=t["sort_order"], is_active=t["is_active"])


def _booking_request_out(r: dict) -> BookingRequestOut:
    return BookingRequestOut(
        id=r["id"], booking_id=r["booking_id"],
        request_type_id=r["request_type_id"], label=r["label"],
        quantity=r["quantity"], note=r["note"], origin=r["origin"],
        status=r["status"])


@router.get("/request-types", response_model=list[RequestTypeOut])
def list_request_types(conn: Conn, prop: OwnedProperty):
    """Catalogue de demandes du logement (lit bébé, chaise haute…)."""
    return [_request_type_out(t)
            for t in repo.list_request_types(conn, str(prop["id"]))]


@router.post("/request-types", response_model=RequestTypeOut,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_write_access)])
def create_request_type(payload: RequestTypeIn, conn: Conn, prop: OwnedProperty):
    if repo.get_request_type_by_code(conn, str(prop["id"]), payload.code):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ce code de demande existe déjà pour ce logement.")
    t = repo.create_request_type(conn, str(prop["id"]), code=payload.code,
                                 label=payload.label, sort_order=payload.sort_order)
    return _request_type_out(t)


@router.patch("/request-types/{type_id}", response_model=RequestTypeOut,
              dependencies=[Depends(require_write_access)])
def update_request_type(type_id: str, payload: RequestTypeUpdate, conn: Conn,
                        prop: OwnedProperty):
    t = repo.update_request_type(conn, str(prop["id"]), type_id,
                                 payload.model_dump(exclude_unset=True))
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Type de demande introuvable")
    return _request_type_out(t)


# ── Demandes rattachées à un séjour (V2-23b, §1.2) ───────────────────────────

@router.get("/bookings/{booking_id}/requests",
            response_model=list[BookingRequestOut])
def list_booking_requests(booking_id: str, conn: Conn, prop: OwnedProperty):
    if not repo.get_booking(conn, str(prop["id"]), booking_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    return [_booking_request_out(r)
            for r in repo.list_booking_requests(conn, booking_id)]


@router.post("/bookings/{booking_id}/requests", response_model=BookingRequestOut,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_write_access)])
def create_booking_request(booking_id: str, payload: BookingRequestIn, conn: Conn,
                           prop: OwnedProperty):
    if not repo.get_booking(conn, str(prop["id"]), booking_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    # Résout le libellé : celui saisi, sinon celui du type du catalogue (recopié
    # pour survivre à une suppression future du type).
    label = payload.label
    type_id = str(payload.request_type_id) if payload.request_type_id else None
    if type_id:
        t = repo.get_request_type(conn, str(prop["id"]), type_id)
        if not t:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Type de demande introuvable")
        label = label or t["label"]
    if not label:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Précisez un type de demande ou un libellé.")
    r = repo.create_booking_request(
        conn, booking_id, request_type_id=type_id, label=label,
        quantity=payload.quantity, note=payload.note, origin="owner",
        status=payload.status)
    return _booking_request_out(r)


@router.patch("/requests/{request_id}", response_model=BookingRequestOut,
              dependencies=[Depends(require_write_access)])
def update_booking_request(request_id: str, payload: BookingRequestUpdate,
                           conn: Conn, prop: OwnedProperty):
    if not repo.get_booking_request(conn, str(prop["id"]), request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Demande introuvable")
    r = repo.update_booking_request(conn, request_id,
                                    payload.model_dump(exclude_unset=True))
    return _booking_request_out(r)


@router.delete("/requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_write_access)])
def delete_booking_request(request_id: str, conn: Conn, prop: OwnedProperty):
    if not repo.delete_booking_request(conn, str(prop["id"]), request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Demande introuvable")
    return None


# ── Interventions calculées pour un séjour (V2-23b, §1.3) ────────────────────

@router.get("/bookings/{booking_id}/interventions",
            response_model=list[InterventionOut])
def booking_interventions(booking_id: str, conn: Conn, prop: OwnedProperty):
    """Liste des interventions d'entretien calculées (jamais stockées) pour un
    séjour : préparation d'arrivée quantifiée, changements de draps en cours de
    séjour, demandes acceptées. La nature pilote la préparation (invariant 14)."""
    b = repo.get_booking(conn, str(prop["id"]), booking_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    reqs = repo.list_booking_requests(conn, booking_id)
    interventions = care.plan_interventions(b, prop["care_rules"], requests=reqs)
    return [InterventionOut(**i.as_dict()) for i in interventions]
