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
from ..schemas import (AddonIn, AddonOut, CancelScheduledOut, ChangePlanIn,
                       ChangePlanOut, CheckoutIn, CheckoutOut, PlanOut,
                       PortalOut, QuotaGaugeOut, ScheduledChangeOut,
                       SubscriptionOut, UsageOut)

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
    ent = plans.effective_entitlements(conn, owner_id)   # point d'entrée unique
    plan = ent["plan"]
    sub = plans.get_subscription(conn, owner_id)

    usage = UsageOut(
        properties=QuotaGaugeOut(
            used=repo.count_properties(conn, owner_id),
            limit=ent["max_properties"]),   # base + add-ons (V2-18b)
        enrichments=QuotaGaugeOut(
            used=repo.count_owner_jobs_current_month(conn, owner_id),
            limit=plan.get("enrich_quota")),
        langs=QuotaGaugeOut(
            # Langue source (toujours publiée) + le plus de cibles publiées sur
            # un des logements ; plafond = features.langs (source comprise).
            used=1 + repo.max_published_langs_count(conn, owner_id),
            limit=plans.max_langs(plan)),
    )
    # Downgrade programmé à l'échéance (V2-18e) : offre cible + date d'effet,
    # posés par le webhook (invariant 12). On enrichit du nom lisible de l'offre.
    scheduled = None
    if sub and sub.get("scheduled_plan_id"):
        target = repo.get_plan_by_id(conn, sub["scheduled_plan_id"])
        scheduled = ScheduledChangeOut(
            plan=sub["scheduled_plan_id"],
            plan_name=(target["name"] if target else sub["scheduled_plan_id"]),
            effective_at=sub.get("scheduled_change_at"))

    return SubscriptionOut(
        plan=PlanOut(**plan),
        status=(sub["status"] if sub else "active"),
        usage=usage,
        has_stripe_customer=bool(sub and sub.get("stripe_customer_id")),
        on_trial=ent["on_trial"],
        trial_expired=ent["trial_expired"],
        trial_ends_at=ent["trial_ends_at"],
        addon_qty=ent["addon_qty"],
        current_period_end=(sub.get("current_period_end") if sub else None),
        scheduled_change=scheduled)


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
    # Un abonné payant déjà actif ne repasse JAMAIS par le Checkout (risque de
    # double abonnement / double facturation) : il change d'offre in-place via
    # /billing/change-plan (V2-18d). Le webhook reste seule autorité ; on ne fait
    # que LIRE l'état local pour refuser proprement.
    if plans.has_active_paid_subscription(conn, owner_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "already_subscribed",
                    "message": "Vous avez déjà un abonnement payant actif. "
                               "Utilisez « changer d'offre » pour le modifier."})
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


# ── Add-on « logement supplémentaire » (V2-18b) ──────────────────────────────

@router.post("/billing/addons", response_model=AddonOut)
def update_addons(payload: AddonIn, conn: Conn, owner: CurrentOwner,
                  gateway: Stripe):
    """Ajuste la quantité de logements supplémentaires (add-on Pro). Réservé à un
    abonnement **Pro actif** (409 sinon). Met à jour l'item d'add-on de
    l'abonnement Stripe (proration par défaut) et **n'écrit RIEN en base** : la
    quantité effective revient par le webhook `customer.subscription.updated`
    (seule autorité, invariant 1). Le front affiche « mise à jour en cours »
    jusqu'au rafraîchissement post-webhook."""
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le paiement en ligne n'est pas disponible.")

    owner_id = str(owner["id"])
    plan = plans.get_plan(conn, owner_id)
    sub = plans.get_subscription(conn, owner_id)
    stripe_sub_id = sub.get("stripe_subscription_id") if sub else None
    if plan["id"] != "pro" or not stripe_sub_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Les logements supplémentaires nécessitent un abonnement Pro actif.")
    addon_price_id = plan.get("addon_stripe_price_id")
    if not addon_price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="L'option « logement supplémentaire » n'est pas encore disponible.")

    try:
        gateway.set_addon_quantity(subscription_id=stripe_sub_id,
                                   addon_price_id=addon_price_id,
                                   quantity=payload.quantity)
    except billing_stripe.StripeError as exc:
        log.error("Échec de mise à jour de l'add-on Stripe (owner=%s) : %s",
                  owner_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Le service de paiement est momentanément indisponible.")
    return AddonOut(requested_quantity=payload.quantity)


# ── Changement d'offre in-place d'un abonné payant actif (V2-18d) ────────────

@router.post("/billing/change-plan", response_model=ChangePlanOut)
def change_plan(payload: ChangePlanIn, conn: Conn, owner: CurrentOwner,
                gateway: Stripe):
    """Change l'offre d'un abonné **payant déjà actif** (409 sinon) en modifiant
    l'abonnement Stripe EXISTANT — jamais un nouveau Checkout, qui créerait un
    doublon. Le SENS décide de la politique (V2-18e, invariant 12) :

    - **UPGRADE** (offre cible plus chère) : effet **IMMÉDIAT** avec prorata
      (`change_plan`, le temps déjà payé est crédité).
    - **DOWNGRADE** (offre cible moins chère) : effet **À L'ÉCHÉANCE**
      (`schedule_downgrade`) — le plan courant reste actif jusqu'à
      `current_period_end`, la nouvelle offre (sans add-on) démarre à cette date.

    **N'écrit RIEN en base** : le webhook posera plan_id/addon_qty (à l'échéance
    pour un downgrade) et le changement programmé (invariants 9/12). Le front
    affiche « mise à jour en cours » / « changement programmé » jusqu'au
    rafraîchissement."""
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le paiement en ligne n'est pas disponible.")

    owner_id = str(owner["id"])
    sub = plans.has_active_paid_subscription(conn, owner_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_subscribed",
                    "message": "Aucun abonnement payant actif à modifier."})
    current = plans.get_plan(conn, owner_id)

    target = repo.get_plan_by_id(conn, payload.plan)
    if target is None or target["price_month_cts"] <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Offre payante invalide.")
    if target["id"] == current["id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "already_on_plan",
                    "message": "Vous êtes déjà sur cette offre."})
    new_price_id = target.get("stripe_price_id")
    if not new_price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cette offre n'est pas encore disponible au paiement.")

    # Sens du changement : le prix de BASE de l'offre (jamais le total effectif
    # avec add-ons) tranche upgrade vs downgrade — cohérent avec le front.
    is_downgrade = target["price_month_cts"] < current.get("price_month_cts", 0)
    try:
        if is_downgrade:
            # À l'échéance : le plan courant (et ses add-ons) court jusqu'à la fin
            # de période, la nouvelle offre (sans add-on) démarre ensuite.
            gateway.schedule_downgrade(
                subscription_id=sub["stripe_subscription_id"],
                new_price_id=new_price_id)
        else:
            # Immédiat avec prorata. L'add-on n'existe que sur Pro : on retire ses
            # items si la cible n'en a pas (jamais le cas d'un upgrade, garde-fou).
            gateway.change_plan(
                subscription_id=sub["stripe_subscription_id"],
                new_price_id=new_price_id,
                addon_price_id=current.get("addon_stripe_price_id"),
                remove_addon=not target.get("addon_property_price_cts"))
    except billing_stripe.StripeError as exc:
        log.error("Échec du changement d'offre Stripe (owner=%s) : %s",
                  owner_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Le service de paiement est momentanément indisponible.")
    return ChangePlanOut(
        target_plan=target["id"],
        direction="downgrade" if is_downgrade else "upgrade",
        effective_at=(sub.get("current_period_end") if is_downgrade else None))


@router.post("/billing/cancel-scheduled-change", response_model=CancelScheduledOut)
def cancel_scheduled_change(conn: Conn, owner: CurrentOwner, gateway: Stripe):
    """Annule un downgrade **programmé** encore non pris en compte (V2-18e). Libère
    le Subscription Schedule côté Stripe (l'offre en cours est conservée).
    **N'écrit RIEN en base** : l'effacement du programmé revient par le webhook
    `subscription_schedule.released` (seule autorité, invariant 12)."""
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le paiement en ligne n'est pas disponible.")

    owner_id = str(owner["id"])
    sub = plans.has_active_paid_subscription(conn, owner_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_subscribed",
                    "message": "Aucun abonnement payant actif à modifier."})
    if not sub.get("scheduled_plan_id"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "no_scheduled_change",
                    "message": "Aucun changement d'offre programmé à annuler."})
    try:
        gateway.cancel_scheduled_change(
            subscription_id=sub["stripe_subscription_id"])
    except billing_stripe.StripeError as exc:
        log.error("Échec de l'annulation du changement programmé Stripe "
                  "(owner=%s) : %s", owner_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Le service de paiement est momentanément indisponible.")
    return CancelScheduledOut()


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
