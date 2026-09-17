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
import threading
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from enrich import geocode as _geocode

from .. import guest_guides, repo
from ..config import settings
from ..deps import Conn, Mailer, Stripe

log = logging.getLogger("casaguide.guest_pay")
router = APIRouter(prefix="/api/guest-guides", tags=["guide-voyageur"])

# Politesse Nominatim (1 req/s) pour le géocodage PUBLIC du tunnel : throttle global
# best-effort (le client débounce déjà). Sérialise les appels sortants côté serveur.
_GEO_LOCK = threading.Lock()
_GEO_LAST = [0.0]
_GEO_MIN_INTERVAL_S = 1.1


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


class OfferOut(BaseModel):
    price_cts: int
    currency: str


class GeocodeIn(BaseModel):
    address_line1: str | None = Field(default=None, max_length=250)
    postal_code: str | None = Field(default=None, max_length=20)
    city: str = Field(min_length=1, max_length=120)
    country_code: str = Field(min_length=2, max_length=2)


class GeocodeOut(BaseModel):
    found: bool
    # `lat`/`lon` = OÙ CENTRER LA CARTE, toujours — le point géocodé si `found`, sinon le
    # repère de départ du placement manuel (V2-68c). `None` seulement si même le pays est
    # resté introuvable.
    lat: float | None = None
    lon: float | None = None
    accuracy: str | None = None     # rooftop | street | city | mismatch (si found)
    mismatch: bool = False          # commune/CP incohérents (V2-46) → ajuster le point
    hint_level: str | None = None   # city | postal | country — repli V2-68c (si !found)


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/offer", response_model=OfferOut)
def offer():
    """Prix de l'offre « Guide Voyageur » (lu de la config, jamais codé en dur côté
    front) — la page des offres et le tunnel l'affichent depuis ici."""
    return OfferOut(price_cts=settings.guest_guide_price_cts,
                    currency=settings.guest_guide_currency)


class DemoOut(BaseModel):
    token: str | None = None


@router.get("/demo", response_model=DemoOut)
def demo(conn: Conn):
    """Token du guide de DÉMONSTRATION vivant (V2-58) — la vitrine y branche son
    iframe/lien. `token=null` si la démo n'est pas encore générée (bloc masqué)."""
    return DemoOut(token=repo.get_demo_guide_token(conn))


@router.post("/geocode", response_model=GeocodeOut)
def geocode_address(payload: GeocodeIn):
    """Géocodage PUBLIC pré-checkout (V2-54 C) : situe l'adresse pour que le vacancier
    AJUSTE le point sur la carte avant de payer. `mismatch=True` ou `accuracy='city'` →
    invite à vérifier/déplacer le point. Le point ajusté part au checkout
    (`accuracy='manual'`, pas de re-géocodage à la génération). Throttlé (politesse
    Nominatim).

    V2-68c : un échec ne ferme JAMAIS le parcours. `found=False` est servi AVEC un
    REPÈRE de départ (`lat`/`lon` + `hint_level` = commune, code postal ou pays) pour
    que la carte s'ouvre là où le client reconnaît quelque chose et pose son point.
    Aucune exception ne remonte en 500 : une panne de géocodage se traite comme une
    adresse introuvable — placement manuel."""
    with _GEO_LOCK:
        wait = _GEO_MIN_INTERVAL_S - (time.monotonic() - _GEO_LAST[0])
        if wait > 0:
            time.sleep(wait)
        geo = None
        try:
            geo = _geocode.geocode(street=payload.address_line1,
                                   postalcode=payload.postal_code, city=payload.city,
                                   country_code=payload.country_code)
        except Exception:  # noqa: BLE001 — GeocodeError, mais aussi réseau/HTTP/quota
            log.info("Géocodage voyageur infructueux (%s, %s) — repli sur un repère.",
                     payload.city, payload.country_code, exc_info=True)
        hint = None
        if geo is None:
            try:
                hint = _geocode.coarse_locate(city=payload.city,
                                              postalcode=payload.postal_code,
                                              country_code=payload.country_code)
            except Exception:  # noqa: BLE001 — best-effort : sans repère, carte au large
                log.info("Aucun repère de départ pour %s/%s.", payload.city,
                         payload.country_code, exc_info=True)
        _GEO_LAST[0] = time.monotonic()
    if geo is None:
        if hint is None:
            return GeocodeOut(found=False)
        return GeocodeOut(found=False, lat=hint["lat"], lon=hint["lon"],
                          hint_level=hint["level"])
    return GeocodeOut(found=True, lat=geo["lat"], lon=geo["lon"],
                      accuracy=geo.get("accuracy"),
                      mismatch=geo.get("accuracy") == "mismatch")


class NeighborhoodOut(BaseModel):
    name: str
    lat: float
    lon: float


@router.post("/neighborhoods", response_model=list[NeighborhoodOut])
def neighborhoods(payload: GeocodeIn):
    """Quartiers NOMMÉS d'une grande ville (V2-68 p2) : quand le géocodage ne rend qu'un
    centroïde (accuracy 'city'), le tunnel propose d'ancrer le guide sur un quartier
    (Shibuya, Asakusa…) plutôt qu'une préfecture muette. Liste vide pour une petite
    commune sans quartier (le tunnel retombe sur l'ajustement du point). Throttlé."""
    from enrich import overpass as _overpass
    with _GEO_LOCK:
        wait = _GEO_MIN_INTERVAL_S - (time.monotonic() - _GEO_LAST[0])
        if wait > 0:
            time.sleep(wait)
        try:
            geo = _geocode.geocode(street=payload.address_line1,
                                   postalcode=payload.postal_code, city=payload.city,
                                   country_code=payload.country_code)
        except _geocode.GeocodeError:
            return []
        finally:
            _GEO_LAST[0] = time.monotonic()
    try:
        places = _overpass.nearby_neighborhoods(geo["lat"], geo["lon"])
    except Exception:  # noqa: BLE001 — best-effort : pas de quartiers → ajustement du point
        log.info("Quartiers non résolus pour %s (%s).", payload.city,
                 payload.country_code, exc_info=True)
        return []
    return [NeighborhoodOut(name=p["name"], lat=p["lat"], lon=p["lon"]) for p in places]


@router.post("/checkout", response_model=CheckoutOut)
def create_guest_checkout(payload: GuestCheckoutIn, conn: Conn, request: Request,
                          gateway: Stripe):
    """Ouvre un Checkout Stripe one-shot (mode payment) et crée la commande `pending`.
    Le montant vient de la config (jamais codé en dur). Renvoie l'URL de paiement."""
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Paiement indisponible pour le moment.")
    # SEUIL DE PRÉCISION AVANT PAIEMENT (V2-68 p1) : sans point ajusté, on situe et on
    # REFUSE un ancrage trop vague (centroïde administratif) — jamais un guide invendable.
    # Le tunnel envoie un point ajusté (accepté) ; cette garde couvre les appels directs
    # et le script ops (« jamais contournable »).
    if payload.lat is None or payload.lon is None:
        try:
            geo = _geocode.geocode(street=payload.address_line1,
                                   postalcode=payload.postal_code, city=payload.city,
                                   country_code=payload.country_code)
        except _geocode.GeocodeError:
            geo = None
        if geo is None or not _geocode.is_precise_enough(geo.get("accuracy")):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "imprecise_location",
                        "message": "Indiquez votre rue ou déplacez le point sur votre "
                                   "lieu de séjour."})
    order = repo.create_guest_order(
        conn, email=str(payload.email), lang=payload.lang, ip=_client_ip(request),
        city=payload.city, country_code=payload.country_code,
        address_line1=payload.address_line1, postal_code=payload.postal_code,
        region=payload.region, lat=payload.lat, lon=payload.lon)
    base = _public_base(request)
    success_url = f"{base}/#/voyageur/merci/{order['token']}"
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
