"""Traitement des événements webhook Stripe (V2-05b).

Le webhook est la **seule source de vérité** de l'état des abonnements
(`plan_id` / `status` / `current_period_end`). Ce module contient la logique de
dispatch, isolée de FastAPI pour être testable directement : `process_event(conn,
event)` reçoit un événement déjà **vérifié** (signature) et **désérialisé** (dict
JSON simple) et applique l'effet en base.

Événements traités (les autres sont accusés puis ignorés) :
  - checkout.session.completed        → rattache le Customer/abonnement Stripe
  - customer.subscription.created/updated → plan + statut + fin de période (autorité)
  - customer.subscription.deleted     → retour au plan 'free' (non destructif)
  - invoice.payment_failed            → statut 'past_due' (accès conservé, grâce)

Invariants : aucune donnée n'est jamais supprimée (un retour à 'free' ne fait que
rebasculer `plan_id`) ; le prix ne pilote que le mapping price→plan, jamais un
montant en dur.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import repo

log = logging.getLogger("casaguide.billing")

# Statut Stripe → statut interne (schéma : trialing|active|past_due|canceled).
# NB : l'accès aux quotas ne dépend QUE de `plan_id` (pas du statut) ; le statut
# est surtout informatif (page « Mon abonnement »). On reste donc conservateur.
_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
    "paused": "past_due",
}


def map_status(stripe_status: str) -> str:
    """Traduit un statut Stripe en statut interne (repli 'past_due' si inconnu :
    on ne débloque jamais un accès sur un statut qu'on ne comprend pas)."""
    return _STATUS_MAP.get(stripe_status, "past_due")


def _period_end(subscription: dict):
    """Fin de période de facturation (timestamptz UTC) à partir de l'objet
    subscription Stripe. Le champ a migré au niveau de l'item de facturation
    dans les versions récentes de l'API : on lit les deux emplacements."""
    ts = subscription.get("current_period_end")
    if ts is None:
        items = (subscription.get("items") or {}).get("data") or []
        if items:
            ts = items[0].get("current_period_end")
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _price_id(subscription: dict) -> str | None:
    items = (subscription.get("items") or {}).get("data") or []
    if not items:
        return None
    return ((items[0].get("price") or {}).get("id"))


def _resolve_owner_by_customer(conn, customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    sub = repo.get_subscription_by_customer_id(conn, customer_id)
    return str(sub["owner_id"]) if sub else None


# ── Handlers ─────────────────────────────────────────────────────────────────

def _on_checkout_completed(conn, obj: dict) -> str:
    """Fin du Checkout : garantit le lien Customer↔propriétaire (posé aussi à la
    création de la session — belt & suspenders). Le plan/statut réels arrivent
    par l'événement subscription.created/updated qui suit."""
    customer_id = obj.get("customer")
    owner_id = obj.get("client_reference_id")
    if owner_id and customer_id:
        # Ne (re)lie que si le propriétaire existe réellement.
        if repo.get_owner(conn, owner_id):
            repo.set_subscription_customer(conn, owner_id, customer_id)
            return "checkout_linked"
    # Repli : résolution par customer_id déjà connu.
    if _resolve_owner_by_customer(conn, customer_id):
        return "checkout_linked"
    log.warning("checkout.session.completed non résolu (customer=%s)", customer_id)
    return "unresolved"


def _on_subscription_upsert(conn, obj: dict) -> str:
    """Création/mise à jour d'abonnement : écriture d'AUTORITÉ (plan, statut,
    fin de période)."""
    customer_id = obj.get("customer")
    owner_id = _resolve_owner_by_customer(conn, customer_id)
    if not owner_id:
        log.warning("subscription.upsert : owner introuvable (customer=%s)",
                    customer_id)
        return "unresolved"
    price_id = _price_id(obj)
    plan = repo.get_plan_by_stripe_price_id(conn, price_id) if price_id else None
    if plan is None:
        log.warning("subscription.upsert : prix inconnu %s (aucun plan) — ignoré",
                    price_id)
        return "unknown_price"
    status = map_status(obj.get("status", ""))
    repo.update_subscription_from_stripe(
        conn, owner_id, plan_id=plan["id"], status=status,
        stripe_subscription_id=obj.get("id"),
        current_period_end=_period_end(obj))
    return f"subscription_{status}"


def _on_subscription_deleted(conn, obj: dict) -> str:
    """Annulation effective : retour au plan gratuit (aucune donnée supprimée —
    logements/traductions excédentaires deviennent lecture seule, invariant V2-05a)."""
    owner_id = _resolve_owner_by_customer(conn, obj.get("customer"))
    if not owner_id:
        log.warning("subscription.deleted : owner introuvable (customer=%s)",
                    obj.get("customer"))
        return "unresolved"
    repo.update_subscription_from_stripe(
        conn, owner_id, plan_id="free", status="active",
        stripe_subscription_id=None, current_period_end=None)
    return "downgraded_free"


def _on_payment_failed(conn, obj: dict) -> str:
    """Échec de paiement : statut 'past_due' (l'accès reste ouvert le temps des
    relances Stripe ; l'annulation, elle, passera par subscription.deleted)."""
    owner_id = _resolve_owner_by_customer(conn, obj.get("customer"))
    if not owner_id:
        log.warning("invoice.payment_failed : owner introuvable (customer=%s)",
                    obj.get("customer"))
        return "unresolved"
    repo.set_subscription_status(conn, owner_id, "past_due")
    return "past_due"


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_upsert,
    "customer.subscription.updated": _on_subscription_upsert,
    "customer.subscription.deleted": _on_subscription_deleted,
    "invoice.payment_failed": _on_payment_failed,
}


def process_event(conn, event: dict) -> str:
    """Applique l'effet d'un événement (déjà vérifié + désérialisé). Renvoie une
    étiquette d'action (utile aux logs et aux tests). Un type non géré est
    simplement 'ignored' (le webhook renverra tout de même 200)."""
    etype = event.get("type", "")
    handler = _HANDLERS.get(etype)
    if handler is None:
        return "ignored"
    obj = (event.get("data") or {}).get("object") or {}
    return handler(conn, obj)
