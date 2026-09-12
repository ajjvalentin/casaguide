"""Offre « Guide Voyageur » — paiement one-shot Stripe (V2-54 Mission B).

Parcours SANS COMPTE : le vacancier saisit une adresse, paie 2,90 € (Checkout Stripe,
mode `payment`), et reçoit le lien de son guide par e-mail. Le WEBHOOK est la seule
source de vérité (doctrine V2-27) : la génération ne démarre qu'au paiement confirmé
(`api/routers/billing.stripe_webhook` → `guest_guides.on_guest_checkout_paid`). Ici :
créer le Checkout, suivre la commande, reprendre après échec, renvoyer le guide.

Sans Stripe configuré, `/checkout` répond 503 (le reste de l'app est intact).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from .. import guest_guides, repo
from ..config import settings
from ..deps import Conn, Mailer, Stripe

log = logging.getLogger("casaguide.guest_pay")
router = APIRouter(prefix="/api/guest-guides", tags=["guide-voyageur"])


def _public_base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _client_ip(request: Request) -> str | None:
    """IP du client (anti-abus). Derrière Caddy, l'origine réelle est en
    X-Forwarded-For (Caddy la pose) ; repli sur l'adresse de la connexion."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


# ── Schémas ───────────────────────────────────────────────────────────────────

class GuestCheckoutIn(BaseModel):
    email: EmailStr
    city: str = Field(min_length=1, max_length=120)
    country_code: str = Field(min_length=2, max_length=2)
    address_line1: str | None = Field(default=None, max_length=250)
    postal_code: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=120)
    lat: float | None = None
    lon: float | None = None
    lang: str = "fr"


class CheckoutOut(BaseModel):
    url: str
    token: str


class GuestOrderOut(BaseModel):
    status: str                    # pending|paid|generating|done|failed
    guide_url: str | None = None   # présent seulement quand done


class GuestRetryIn(BaseModel):
    lat: float | None = None
    lon: float | None = None
    address_line1: str | None = Field(default=None, max_length=250)


class ResendIn(BaseModel):
    email: EmailStr


class OkOut(BaseModel):
    ok: bool = True


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/checkout", response_model=CheckoutOut)
def create_guest_checkout(payload: GuestCheckoutIn, conn: Conn, request: Request,
                          gateway: Stripe):
    """Ouvre un Checkout Stripe one-shot (mode payment) et crée la commande `pending`.
    Le montant vient de la config (jamais codé en dur). Renvoie l'URL de paiement."""
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Paiement indisponible pour le moment.")
    order = repo.create_guest_order(
        conn, email=str(payload.email), lang=payload.lang, ip=_client_ip(request),
        city=payload.city, country_code=payload.country_code,
        address_line1=payload.address_line1, postal_code=payload.postal_code,
        region=payload.region, lat=payload.lat, lon=payload.lon)
    base = _public_base(request)
    success_url = f"{base}/#/voyageur/livraison/{order['token']}"
    cancel_url = f"{base}/#/voyageur?annule=1"
    product_name = f"Guide Voyageur Holaguia — {payload.city}"
    session_id, url = gateway.create_guest_checkout_session(
        amount_cts=settings.guest_guide_price_cts,
        currency=settings.guest_guide_currency,
        product_name=product_name, email=str(payload.email),
        success_url=success_url, cancel_url=cancel_url,
        metadata={"kind": "guest_guide", "order_id": str(order["id"])})
    repo.attach_guest_order_session(conn, str(order["id"]), session_id)
    conn.commit()
    return CheckoutOut(url=url, token=order["token"])


@router.get("/orders/{token}", response_model=GuestOrderOut)
def get_order(token: str, conn: Conn, request: Request):
    """Suivi de la commande (écran de livraison du tunnel). Le lien du guide n'est
    exposé QUE lorsque la génération est terminée."""
    order = repo.get_guest_order_by_token(conn, token)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commande introuvable.")
    guide_url = None
    if order["status"] == "done" and order["guide_token"]:
        guide_url = f"{_public_base(request)}/g/{order['guide_token']}"
    return GuestOrderOut(status=order["status"], guide_url=guide_url)


@router.post("/orders/{token}/retry", response_model=OkOut)
def retry_order(token: str, payload: GuestRetryIn, conn: Conn, request: Request,
                background: BackgroundTasks, mailer: Mailer):
    """Reprise après échec (§3) : le paiement est acquis, on ré-enclenche la
    génération (point éventuellement ajusté). JAMAIS de génération sans paiement
    confirmé (une commande `pending` est refusée)."""
    order = repo.get_guest_order_by_token(conn, token)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commande introuvable.")
    if order["status"] == "done":
        return OkOut()                 # déjà livrée (idempotent)
    if order["status"] not in ("paid", "failed"):
        # pending = paiement non confirmé → pas de génération (invariant paiement).
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "not_paid",
                    "message": "Le paiement de cette commande n'est pas confirmé."})
    guest_guides.retry_paid_order(order, background, mailer,
                                  base_url=_public_base(request),
                                  lat=payload.lat, lon=payload.lon,
                                  address=payload.address_line1)
    return OkOut()


@router.post("/resend", response_model=OkOut)
def resend(payload: ResendIn, conn: Conn, request: Request, mailer: Mailer):
    """« Renvoyer mon guide » : renvoie le dernier guide livré pour un e-mail.
    Réponse 200 CONSTANTE (anti-énumération) ; cadence anti-abus appliquée dans
    `resend_guide` — un e-mail inconnu ou trop récent ne renvoie rien, silencieusement."""
    guest_guides.resend_guide(conn, str(payload.email), mailer=mailer,
                              base_url=_public_base(request))
    return OkOut()
