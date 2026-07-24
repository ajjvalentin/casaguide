"""Catalogue des plans, abonnement, et paiement Stripe (V2-05a puis V2-05b).

  - `GET  /api/plans`             catalogue public (inscription — prix depuis la base).
  - `GET  /api/subscription`      plan courant + jauges (page « Mon abonnement »).
  - `POST /api/billing/checkout`  session Stripe Checkout pour un plan payant (V2-05b).
  - `POST /api/stripe/webhook`    webhooks Stripe — SEULE source de vérité (V2-05b).

Le portail client (`POST /api/billing/portal`) est ajouté au volet 3.

Invariants Stripe (V2-05b) : le webhook est la seule autorité sur l'état des
abonnements ; le success_url ne modifie JAMAIS l'abonnement (le front n'affiche
qu'un bandeau « confirmation en cours »). Signature vérifiée + idempotence sur
chaque webhook. Sans Stripe configuré, les endpoints de paiement répondent 503.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from .. import billing_stripe, plans, repo, stripe_events
from ..config import settings
from ..deps import Conn, CurrentOwner, Stripe
from ..schemas import (CheckoutIn, CheckoutOut, PlanOut, PortalOut,
                       QuotaGaugeOut, SubscriptionOut, UsageOut)

log = logging.getLogger("casaguide.billing")

router = APIRouter(prefix="/api", tags=["billing"])


def _public_base(request: Request) -> str:
    """Origine publique pour les URL de retour Checkout (prod : holaguia.com ;
    à défaut, l'origine de la requête)."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


@router.get("/plans", response_model=list[PlanOut])
def list_plans(conn: Conn):
    """Catalogue public des offres (par prix croissant). Pas d'auth : le
    formulaire d'inscription l'affiche avant toute session."""
    return repo.list_plans(conn)


@router.get("/subscription", response_model=SubscriptionOut)
def my_subscription(conn: Conn, owner: CurrentOwner):
    """Plan courant du propriétaire + jauges d'utilisation. Les limites viennent
    du plan (base), jamais du code (invariant 8)."""
    owner_id = str(owner["id"])
    plan = plans.get_plan(conn, owner_id)
    sub = plans.get_subscription(conn, owner_id)

    usage = UsageOut(
        properties=QuotaGaugeOut(
            used=repo.count_properties(conn, owner_id),
            limit=plan.get("max_properties")),
        enrichments=QuotaGaugeOut(
            used=repo.count_owner_jobs_current_month(conn, owner_id),
            limit=plan.get("enrich_quota")),
        langs=QuotaGaugeOut(
            # Langue source (toujours publiée) + le plus de cibles publiées sur
            # un des logements ; plafond = features.langs (source comprise).
            used=1 + repo.max_published_langs_count(conn, owner_id),
            limit=plans.max_langs(plan)),
    )
    return SubscriptionOut(
        plan=PlanOut(**plan),
        status=(sub["status"] if sub else "active"),
        usage=usage,
        has_stripe_customer=bool(sub and sub.get("stripe_customer_id")))


# ── Paiement : session Checkout (V2-05b, volet 2) ────────────────────────────

@router.post("/billing/checkout", response_model=CheckoutOut)
def create_checkout(payload: CheckoutIn, conn: Conn, owner: CurrentOwner,
                    gateway: Stripe, request: Request):
    """Crée une session Stripe Checkout (mode subscription) pour un plan payant
    et renvoie son URL. Le plan gratuit ou inconnu est refusé (422). Le prix vient
    du Price Stripe synchronisé (`plans.stripe_price_id`) — jamais codé en dur."""
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le paiement en ligne n'est pas encore disponible.")

    plan = repo.get_plan_by_id(conn, payload.plan)
    if plan is None or plan["price_month_cts"] <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Offre payante invalide.")
    price_id = plan.get("stripe_price_id")
    if not price_id:
        # Plan payant pas encore synchronisé vers Stripe (ops/stripe_sync_products).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cette offre n'est pas encore disponible au paiement.")

    owner_id = str(owner["id"])
    sub = plans.get_subscription(conn, owner_id)
    existing_customer = sub.get("stripe_customer_id") if sub else None
    base = _public_base(request)
    try:
        customer_id = gateway.get_or_create_customer(
            owner_id=owner_id, email=owner["email"],
            existing_customer_id=existing_customer)
        # Rattache le Customer AVANT de créer la session : la résolution owner par
        # customer_id côté webhook fonctionne alors quel que soit l'ordre d'arrivée
        # des événements (invariant 1 : le webhook reste la seule autorité d'état).
        repo.set_subscription_customer(conn, owner_id, customer_id)
        url = gateway.create_checkout_session(
            customer_id=customer_id, price_id=price_id, owner_id=owner_id,
            success_url=f"{base}/#/abonnement?checkout=success",
            cancel_url=f"{base}/#/abonnement?checkout=cancel")
    except billing_stripe.StripeError as exc:
        log.error("Échec de création du Checkout Stripe (owner=%s) : %s",
                  owner_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Le service de paiement est momentanément indisponible.")
    return CheckoutOut(url=url)


# ── Portail client Stripe (V2-05b, volet 3) ─────────────────────────────────

@router.post("/billing/portal", response_model=PortalOut)
def create_portal(conn: Conn, owner: CurrentOwner, gateway: Stripe,
                  request: Request):
    """Ouvre une session du portail client Stripe (moyens de paiement, factures,
    annulation). 409 si le propriétaire n'a jamais eu de Customer Stripe (jamais
    payé) — il n'y a alors rien à gérer."""
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le portail de facturation n'est pas disponible.")

    owner_id = str(owner["id"])
    sub = plans.get_subscription(conn, owner_id)
    customer_id = sub.get("stripe_customer_id") if sub else None
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucun abonnement payant à gérer pour l'instant.")

    try:
        url = gateway.create_portal_session(
            customer_id=customer_id,
            return_url=f"{_public_base(request)}/#/abonnement")
    except billing_stripe.StripeError as exc:
        log.error("Échec d'ouverture du portail Stripe (owner=%s) : %s",
                  owner_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Le service de paiement est momentanément indisponible.")
    return PortalOut(url=url)


# ── Webhooks Stripe : la seule source de vérité (V2-05b, volet 2) ────────────

@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, conn: Conn, gateway: Stripe):
    """Reçoit les événements Stripe. Signature obligatoire (400 sinon),
    idempotence par `stripe_events` (un rejeu est accusé mais non retraité). Met
    à jour `subscriptions` (plan/statut/fin de période). Un type non géré est
    accusé (200) sans traitement."""
    if gateway is None or not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook Stripe non configuré.")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = gateway.construct_event(payload, sig)
    except billing_stripe.SignatureError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Signature de webhook invalide.")
    except (ValueError, json.JSONDecodeError):
        # Secret non configuré (déjà couvert) ou payload illisible après vérif.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Événement webhook illisible.")

    event_id = event.get("id")
    event_type = event.get("type", "")
    if not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Événement sans identifiant.")

    # Idempotence : un event déjà reçu (rejeu Stripe) est accusé sans retraitement.
    if not repo.stripe_event_begin(conn, event_id, event_type):
        return {"received": True, "duplicate": True}

    action = stripe_events.process_event(conn, event)
    repo.stripe_event_mark_processed(conn, event_id)
    log.info("Webhook Stripe %s (%s) → %s", event_type, event_id, action)
    return {"received": True}
