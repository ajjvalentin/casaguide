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
  - subscription_schedule.created/updated → downgrade programmé (offre + date, V2-18e)
  - subscription_schedule.released/canceled/completed/aborted → efface le programmé

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


def _resolve_plan_and_addons(conn, subscription: dict):
    """Résout (plan principal, addon_qty) à partir de TOUS les items de
    l'abonnement Stripe (V2-18b). Le plan vient de l'item dont le price est un
    `plans.stripe_price_id` ; la quantité d'add-on est la somme des items dont le
    price est un `plans.addon_stripe_price_id`. Renvoie (None, 0) si aucun item
    ne correspond à un plan connu (prix non synchronisé)."""
    items = (subscription.get("items") or {}).get("data") or []
    plan = None
    addon_qty = 0
    for it in items:
        price_id = (it.get("price") or {}).get("id")
        if not price_id:
            continue
        main = repo.get_plan_by_stripe_price_id(conn, price_id)
        if main is not None:
            plan = main
            continue
        addon = repo.get_plan_by_addon_stripe_price_id(conn, price_id)
        if addon is not None:
            addon_qty += int(it.get("quantity") or 0)
    return plan, addon_qty


def _resolve_owner_by_customer(conn, customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    sub = repo.get_subscription_by_customer_id(conn, customer_id)
    return str(sub["owner_id"]) if sub else None


def _phase_main_plan(conn, phase: dict):
    """Plan principal d'une phase de Subscription Schedule (V2-18e) : le premier
    item dont le price est un `plans.stripe_price_id` connu. None si aucun (prix
    non synchronisé)."""
    for it in (phase.get("items") or []):
        price = it.get("price")
        price_id = price if isinstance(price, str) else (price or {}).get("id")
        if not price_id:
            continue
        plan = repo.get_plan_by_stripe_price_id(conn, price_id)
        if plan is not None:
            return plan
    return None


def _resolve_scheduled_change(conn, schedule: dict, *, now: datetime | None = None):
    """Résout le downgrade programmé (offre cible, date d'effet) à partir d'un
    objet Subscription Schedule (V2-18e). On cherche la **prochaine phase future**
    (start_date > maintenant, la plus proche) et on en déduit le plan principal.

    Renvoie (plan_id, effective_at) si un changement programmé existe, sinon
    (None, None) — cas d'un schedule sans phase future (déjà basculé / libéré /
    prix inconnu)."""
    ref = (now or datetime.now(timezone.utc)).timestamp()
    upcoming = None
    for phase in (schedule.get("phases") or []):
        start = phase.get("start_date")
        if start is None or start <= ref:
            continue
        if upcoming is None or start < upcoming.get("start_date"):
            upcoming = phase
    if upcoming is None:
        return None, None
    plan = _phase_main_plan(conn, upcoming)
    if plan is None:
        return None, None
    effective_at = datetime.fromtimestamp(int(upcoming["start_date"]), tz=timezone.utc)
    return plan["id"], effective_at


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
    fin de période).

    V2-18e : si le plan APPLIQUÉ correspond au changement programmé, la bascule a
    pris effet (transition de phase du schedule) → on efface le programmé pour que
    le bandeau back-office disparaisse sans attendre l'événement `.released`."""
    customer_id = obj.get("customer")
    sub_row = repo.get_subscription_by_customer_id(conn, customer_id)
    if not sub_row:
        log.warning("subscription.upsert : owner introuvable (customer=%s)",
                    customer_id)
        return "unresolved"
    owner_id = str(sub_row["owner_id"])
    plan, addon_qty = _resolve_plan_and_addons(conn, obj)
    if plan is None:
        log.warning("subscription.upsert : aucun prix connu dans les items — ignoré")
        return "unknown_price"
    status = map_status(obj.get("status", ""))
    repo.update_subscription_from_stripe(
        conn, owner_id, plan_id=plan["id"], status=status,
        stripe_subscription_id=obj.get("id"),
        current_period_end=_period_end(obj), addon_qty=addon_qty)
    if sub_row.get("scheduled_plan_id") == plan["id"]:
        repo.clear_scheduled_change(conn, owner_id)   # bascule effectuée
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
        stripe_subscription_id=None, current_period_end=None, addon_qty=0)
    repo.clear_scheduled_change(conn, owner_id)   # plus rien à programmer (V2-18e)
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


def _on_schedule_upsert(conn, obj: dict) -> str:
    """Création/mise à jour d'un Subscription Schedule (V2-18e) : mémorise le
    downgrade programmé (offre cible + date d'effet) pour le bandeau back-office.
    Purement informatif — n'affecte ni `plan_id` ni l'accès (invariant 12). Si le
    schedule n'a plus de phase future (déjà basculé / prix inconnu), on efface."""
    owner_id = _resolve_owner_by_customer(conn, obj.get("customer"))
    if not owner_id:
        log.warning("subscription_schedule.upsert : owner introuvable (customer=%s)",
                    obj.get("customer"))
        return "unresolved"
    plan_id, effective_at = _resolve_scheduled_change(conn, obj)
    if plan_id and effective_at:
        repo.set_scheduled_change(conn, owner_id, plan_id, effective_at)
        return "scheduled"
    repo.clear_scheduled_change(conn, owner_id)
    return "schedule_cleared"


def _on_schedule_ended(conn, obj: dict) -> str:
    """Fin d'un Subscription Schedule (release/cancel/complete/abort, V2-18e) :
    efface le downgrade programmé (annulé, ou pris en compte). Non destructif."""
    owner_id = _resolve_owner_by_customer(conn, obj.get("customer"))
    if not owner_id:
        log.warning("subscription_schedule.ended : owner introuvable (customer=%s)",
                    obj.get("customer"))
        return "unresolved"
    repo.clear_scheduled_change(conn, owner_id)
    return "schedule_cleared"


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_upsert,
    "customer.subscription.updated": _on_subscription_upsert,
    "customer.subscription.deleted": _on_subscription_deleted,
    "invoice.payment_failed": _on_payment_failed,
    "subscription_schedule.created": _on_schedule_upsert,
    "subscription_schedule.updated": _on_schedule_upsert,
    "subscription_schedule.released": _on_schedule_ended,
    "subscription_schedule.canceled": _on_schedule_ended,
    "subscription_schedule.completed": _on_schedule_ended,
    "subscription_schedule.aborted": _on_schedule_ended,
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
