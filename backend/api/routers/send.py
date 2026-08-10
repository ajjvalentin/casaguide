"""« Envoyer par Holaguia » — l'email HTML du guide part du backend (V2-23d, volet 1).

La fenêtre « Envoyer le guide » (V2-23c) produisait jusqu'ici des `mailto:`/`wa.me`
en texte brut. Ce canal-ci envoie un email HTML soigné (gabarit transactionnel
V2-08, sable/encre/mer, bouton d'action) depuis le serveur, **synchrone** (le
propriétaire attend la confirmation) :

    POST /api/properties/{id}/send-guide     → envoie l'email (séjour ou vitrine)
    GET  /api/properties/{id}/last-send      → dernier envoi (fenêtre : « envoyé le… »)

Le token est assuré **côté serveur** (`ensure_stay_token`/`ensure_showcase_token`,
mêmes fabriques idempotentes que la fenêtre) : le client n'envoie jamais d'URL, et
le `guide_token` éternel ne voyage jamais dans le lien du locataire (`/b/`) ni de
la vitrine (`/v/`). Chaque envoi RÉUSSI est tracé dans `guide_sends` (mémoire du
J-7, volet 2). Une panne SMTP renvoie une erreur propre (jamais un 500 nu) et
**aucune** ligne `guide_sends` n'est écrite.

Ce volet est la fondation de l'envoi automatique J-7 (volet 2, hors périmètre) :
les gabarits localisés (`emails.guide_stay_email`/`guide_showcase_email`) et le
traçage sont réutilisables tels quels par un futur planificateur.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from .. import care, emails, guidesend, repo
from ..config import settings
from ..deps import Conn, CurrentOwner, Mailer, OwnedProperty
from ..schemas import LastSendOut, MarkSentOut, SendGuideIn, SendGuideOut

log = logging.getLogger("casaguide.send")

router = APIRouter(prefix="/api/properties/{property_id}", tags=["partage"])


def _public_base(request: Request) -> str:
    """Origine publique des liens (même règle que le poster/les emails, V2-08)."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _resolve_lang(conn, prop: dict, requested: str | None,
                  natural: str) -> str:
    """Langue d'envoi : `requested` seulement si c'est une langue offerte par le
    guide (publiée + registre) — sinon 422 (l'appelant a demandé une langue que le
    produit ne sait pas servir). None → langue « naturelle » de la cible."""
    if not requested:
        return natural
    if requested not in guidesend.offered_langs(conn, prop):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Langue « {requested} » non disponible pour ce guide.")
    return requested


@router.post("/send-guide", response_model=SendGuideOut)
def send_guide(payload: SendGuideIn, conn: Conn, owner: CurrentOwner,
               prop: OwnedProperty, mailer: Mailer, request: Request):
    """Envoie l'email HTML du guide (séjour ou vitrine) depuis le backend. Envoi
    **synchrone** : la réponse ne revient qu'après l'envoi SMTP. Panne SMTP →
    **502** propre (jamais un 500 nu), aucune ligne `guide_sends`."""
    base = _public_base(request)
    pid = str(prop["id"])

    if payload.kind == "stay":
        if not payload.booking_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Un séjour (booking_id) est requis pour un envoi de séjour.")
        booking = repo.get_booking(conn, pid, payload.booking_id)
        if not booking:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Séjour introuvable")
        # Garde de bon sens (V2-23d volet 2) : un séjour ANNULÉ produirait un lien
        # mort — refus explicite. Les natures restent permises en manuel (le
        # propriétaire sait ce qu'il fait) ; seul le cycle de vie annulé bloque.
        if booking.get("status") == "cancelled":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Ce séjour est annulé : son lien ne mène nulle part.")
        recipient = payload.recipient or care.effective_email(booking)
        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Ce séjour n'a pas d'email. Ajoutez-en un ou saisissez un "
                       "destinataire.")
        # Langue « naturelle » du lien séjour : guest_lang si offerte, sinon source.
        natural = guidesend.stay_natural_lang(conn, prop, booking)
        lang = _resolve_lang(conn, prop, payload.lang, natural)
        token = repo.ensure_stay_token(conn, pid, payload.booking_id)
        if not token:                      # course/suppression concurrente
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Séjour introuvable")
        email = guidesend.build_stay_email(conn, prop, booking, token=token,
                                           base=base, lang=lang)
        booking_id = str(booking["id"])
    else:  # showcase
        if not payload.recipient:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Un destinataire est requis pour un envoi de vitrine.")
        recipient = payload.recipient
        natural = prop.get("default_lang") or "fr"
        lang = _resolve_lang(conn, prop, payload.lang, natural)
        token = repo.ensure_showcase_token(conn, str(owner["id"]), pid)
        if not token:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Logement introuvable")
        api_base = f"/v/{token}"
        url = guidesend.link(base, api_base, lang, natural)
        email = guidesend.build_localized(
            conn, lang, lambda: emails.guide_showcase_email(
                property_name=prop["name"], url=url,
                image_url=guidesend.target_image_url(conn, prop, base, api_base),
                lang=lang))
        booking_id = None

    # Envoi SYNCHRONE — le propriétaire attend la confirmation. Panne SMTP →
    # 502 propre (jamais un 500 nu), AUCUNE trace écrite.
    try:
        mailer.send(recipient, email)
    except Exception:  # noqa: BLE001 — on transforme en erreur HTTP propre
        log.warning("Envoi du guide vers %s échoué.", recipient, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="L'email n'a pas pu être envoyé. Réessayez dans un instant.")

    row = repo.record_guide_send(conn, property_id=pid, booking_id=booking_id,
                                 kind=payload.kind, lang=lang, recipient=recipient,
                                 origin="manual")
    conn.commit()   # écriture + envoi : commit explicite (piège V2-16b)
    return SendGuideOut(recipient=recipient, kind=payload.kind, lang=lang,
                        sent_at=row["sent_at"])


@router.post("/bookings/{booking_id}/mark-sent", response_model=MarkSentOut)
def mark_sent(booking_id: str, conn: Conn, prop: OwnedProperty):
    """« Marquer envoyé ✓ » du J-7 assisté WhatsApp (V2-32 volet 1).

    wa.me n'offre AUCUNE confirmation technique : le geste est **déclaratif**. Le
    propriétaire, après avoir ouvert WhatsApp et envoyé le guide, marque le séjour
    ici → une ligne `guide_sends` origin='whatsapp_assisted' (kind='stay',
    `lang`=langue effective, `recipient`=téléphone). Le registre devient alors le
    verrou : l'email automatique du lendemain est supprimé (invariant 18, note V2-32).

    **Idempotent** : une ligne kind='stay' existe déjà (manuel/auto/whatsapp) → 200
    sans doublon (`already=True`). Garde multi-tenant : 404 si le séjour n'appartient
    pas au logement du propriétaire."""
    pid = str(prop["id"])
    booking = repo.get_booking(conn, pid, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Séjour introuvable")
    # Registre déjà posé pour ce séjour → rien à faire (idempotent, jamais de double).
    existing = repo.last_guide_send(conn, pid, kind="stay", booking_id=booking_id)
    if existing:
        return MarkSentOut(already=True, recipient=existing["recipient"],
                           lang=existing["lang"], sent_at=existing["sent_at"])
    phone = care.effective_phone(booking)
    if not phone:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Ce séjour n'a pas de téléphone : rien à marquer comme envoyé.")
    lang = guidesend.stay_natural_lang(conn, prop, booking)
    row = repo.record_guide_send(conn, property_id=pid, booking_id=booking_id,
                                 kind="stay", lang=lang, recipient=phone,
                                 origin="whatsapp_assisted")
    conn.commit()   # écriture du registre : commit explicite (piège V2-16b)
    return MarkSentOut(already=False, recipient=phone, lang=lang,
                       sent_at=row["sent_at"])


@router.get("/last-send", response_model=LastSendOut)
def last_send(conn: Conn, prop: OwnedProperty, kind: str = "stay",
              booking_id: str | None = None):
    """Dernier envoi du guide pour une cible (fenêtre d'envoi : « envoyé le… »).
    kind='stay' (avec booking_id) ou 'showcase'. `sent=false` si jamais envoyé."""
    if kind not in ("stay", "showcase"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="kind invalide")
    if kind == "stay" and not booking_id:
        return LastSendOut(sent=False)
    row = repo.last_guide_send(conn, str(prop["id"]), kind=kind,
                               booking_id=booking_id)
    if not row:
        return LastSendOut(sent=False)
    return LastSendOut(sent=True, recipient=row["recipient"], lang=row["lang"],
                       sent_at=row["sent_at"])
