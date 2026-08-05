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

from .. import care, emails, i18n, repo
from ..config import settings
from ..deps import Conn, CurrentOwner, Mailer, OwnedProperty
from ..schemas import LastSendOut, SendGuideIn, SendGuideOut

log = logging.getLogger("casaguide.send")

router = APIRouter(prefix="/api/properties/{property_id}", tags=["partage"])


def _public_base(request: Request) -> str:
    """Origine publique des liens (même règle que le poster/les emails, V2-08)."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _offered_langs(conn, prop: dict) -> list[str]:
    """Langues réellement offertes par CE guide : langue source + langues publiées
    du logement, bornées au REGISTRE (invariant 15). Le registre fait foi."""
    registry = repo.published_language_codes(conn)
    default = (prop.get("default_lang") or "fr")
    prop_langs = [l for l in (prop.get("published_langs") or []) if l in registry]
    return [default] + [l for l in prop_langs if l != default]


def _resolve_lang(conn, prop: dict, requested: str | None,
                  natural: str) -> str:
    """Langue d'envoi : `requested` seulement si c'est une langue offerte par le
    guide (publiée + registre) — sinon 422 (l'appelant a demandé une langue que le
    produit ne sait pas servir). None → langue « naturelle » de la cible."""
    if not requested:
        return natural
    if requested not in _offered_langs(conn, prop):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Langue « {requested} » non disponible pour ce guide.")
    return requested


def _target_image_url(conn, prop: dict, base: str, api_base: str) -> str:
    """Vignette de la cible pour l'email (« og-image de la cible ») : la **photo de
    couverture** désignée d'abord (V2-30, si servable), sinon la première photo du
    logement (niveau logement d'abord, cf. `guide_media`), sinon l'image de marque
    générée. URL absolue servie via le préfixe de la cible (`/b/…` ou `/v/…`) — jamais
    un `/g/{guide_token}` (le token éternel ne fuite pas)."""
    media = repo.guide_media(conn, str(prop["id"]))
    cover_id = prop.get("cover_media_id")
    if cover_id:
        for m in media:
            if str(m["id"]) == str(cover_id) and m["kind"] == "photo":
                return f"{base}{api_base}/media/{m['id']}"
    for m in media:
        if m["kind"] == "photo":
            return f"{base}{api_base}/media/{m['id']}"
    return f"{base}{api_base}/og-image.png"


def _link(base: str, api_base: str, lang: str, natural: str) -> str:
    """Lien de la cible dans la langue choisie : `?lang=` seulement si elle diffère
    de la langue par défaut de la page (sinon le lien nu suffit — §3.5)."""
    if lang == natural:
        return f"{base}{api_base}"
    return f"{base}{api_base}?lang={lang}"


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
        recipient = payload.recipient or care.effective_email(booking)
        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Ce séjour n'a pas d'email. Ajoutez-en un ou saisissez un "
                       "destinataire.")
        # Langue « naturelle » du lien séjour : guest_lang si offerte, sinon source.
        gl = (booking.get("guest_lang") or "").lower()
        natural = gl if gl in _offered_langs(conn, prop) else \
            (prop.get("default_lang") or "fr")
        lang = _resolve_lang(conn, prop, payload.lang, natural)
        token = repo.ensure_stay_token(conn, pid, payload.booking_id)
        if not token:                      # course/suppression concurrente
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Séjour introuvable")
        api_base = f"/b/{token}"
        url = _link(base, api_base, lang, natural)
        # Accueil au PRÉNOM (copie actée « Bonjour {prénom}, »).
        first_name = (booking.get("guest_name") or "").strip().split()[:1]
        email = _build_localized(conn, lang, lambda: emails.guide_stay_email(
            property_name=prop["name"], guest_name=(first_name[0] if first_name else None),
            start=booking["starts_on"].strftime("%d/%m"),
            end=booking["ends_on"].strftime("%d/%m"),
            url=url, image_url=_target_image_url(conn, prop, base, api_base),
            lang=lang))
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
        url = _link(base, api_base, lang, natural)
        email = _build_localized(conn, lang, lambda: emails.guide_showcase_email(
            property_name=prop["name"], url=url,
            image_url=_target_image_url(conn, prop, base, api_base), lang=lang))
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
                                 kind=payload.kind, lang=lang, recipient=recipient)
    conn.commit()   # écriture + envoi : commit explicite (piège V2-16b)
    return SendGuideOut(recipient=recipient, kind=payload.kind, lang=lang,
                        sent_at=row["sent_at"])


def _build_localized(conn, lang: str, build):
    """Construit un email en posant l'overlay i18n de la langue effective (langues
    supplémentaires nl/de/it/sq → `ui_translations`, clés `email.*`) le temps de la
    construction. FR/EN/ES → overlay vide, rendu depuis le code."""
    tok = i18n.set_overlay(repo.ui_translations(conn, lang))
    try:
        return build()
    finally:
        i18n.reset_overlay(tok)


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
