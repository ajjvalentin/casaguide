"""Passerelle Stripe (V2-05b) — Checkout, portail client, vérification webhook.

Même motif que le mailer (V2-08) : une interface étroite (`StripeGateway`)
injectable via `deps.get_stripe`, avec une implémentation réelle
(`LiveStripeGateway`, adossée à la librairie `stripe`) construite au démarrage
seulement si la clé API est présente. Sans clé, `deps.get_stripe` renvoie None
et les routers de facturation répondent 503 — le reste de l'app est intact.

Invariants de la mission :
  - Le webhook est la SEULE source de vérité des abonnements : cette passerelle
    ne fait qu'exécuter des opérations Stripe et vérifier des signatures ; toute
    écriture de `subscriptions` se fait dans le handler de webhook, jamais ici.
  - Aucun secret en dur : la clé API et le secret de signature viennent de
    l'environnement (`settings`), même régime .env que le SMTP.
  - Aucun appel réseau dans la suite de tests : les tests injectent une fausse
    passerelle ; la vérification de signature (pure crypto, sans réseau) reste,
    elle, exercée par le vrai code via `stripe.Webhook.construct_event`.

Les montants et devises ne sont jamais codés ici : ils viennent de la table
`plans` (`price_month_cts`) et sont poussés vers Stripe par
`ops/stripe_sync_products.py`.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

import stripe

log = logging.getLogger("casaguide.billing")

# Devise unique du MVP (les montants viennent de `plans.price_month_cts`).
CURRENCY = "eur"

# Exceptions Stripe ré-exportées pour que les routers les attrapent sans importer
# directement la librairie (découplage).
StripeError = stripe.StripeError
SignatureError = stripe.SignatureVerificationError


def _phase_price_id(item) -> str | None:
    """Id du Price d'un item de phase de Subscription Schedule (V2-18e). Dans les
    phases de schedule, `item.price` est déjà l'id (chaîne) ; on tolère aussi un
    objet Price (`.id`) par robustesse — un StripeObject réel n'est PAS un dict
    (OPS-1), donc on n'accède qu'aux attributs."""
    price = getattr(item, "price", None)
    if isinstance(price, str):
        return price
    return getattr(price, "id", None)


class StripeGateway(Protocol):
    """Interface minimale utilisée par les routers de facturation."""

    def get_or_create_customer(self, *, owner_id: str, email: str,
                               existing_customer_id: str | None) -> str: ...

    def create_checkout_session(self, *, customer_id: str, price_id: str,
                                owner_id: str, success_url: str,
                                cancel_url: str) -> str: ...

    def create_portal_session(self, *, customer_id: str,
                              return_url: str) -> str: ...

    def set_addon_quantity(self, *, subscription_id: str, addon_price_id: str,
                           quantity: int) -> None: ...

    def change_plan(self, *, subscription_id: str, new_price_id: str,
                    addon_price_id: str | None, remove_addon: bool) -> None: ...

    def schedule_downgrade(self, *, subscription_id: str,
                           new_price_id: str) -> None: ...

    def cancel_scheduled_change(self, *, subscription_id: str) -> None: ...

    def construct_event(self, payload: bytes, sig_header: str) -> dict: ...


class LiveStripeGateway:
    """Implémentation réelle adossée à la librairie `stripe`.

    Utilise un `StripeClient` (pas d'état global `stripe.api_key`) pour les appels
    réseau, et `stripe.Webhook.construct_event` (statique, pur calcul) pour
    vérifier la signature des webhooks."""

    def __init__(self, *, api_key: str, webhook_secret: str | None) -> None:
        self._client = stripe.StripeClient(api_key)
        self._webhook_secret = webhook_secret

    # ── Client Stripe (rattaché au propriétaire par metadata owner_id) ───────
    def get_or_create_customer(self, *, owner_id: str, email: str,
                               existing_customer_id: str | None) -> str:
        """Retourne l'id du Customer Stripe du propriétaire, le créant au besoin.
        `existing_customer_id` évite un doublon si une souscription antérieure en
        a déjà créé un."""
        if existing_customer_id:
            return existing_customer_id
        customer = self._client.v1.customers.create({
            "email": email,
            "metadata": {"owner_id": owner_id},
        })
        return customer.id

    # ── Session Checkout (mode subscription) ─────────────────────────────────
    def create_checkout_session(self, *, customer_id: str, price_id: str,
                                owner_id: str, success_url: str,
                                cancel_url: str) -> str:
        session = self._client.v1.checkout.sessions.create({
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": owner_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
        })
        return session.url

    # ── Portail client (cartes, factures, annulation) ────────────────────────
    def create_portal_session(self, *, customer_id: str,
                              return_url: str) -> str:
        session = self._client.v1.billing_portal.sessions.create({
            "customer": customer_id,
            "return_url": return_url,
        })
        return session.url

    # ── Add-on « logement supplémentaire » : quantité de l'item d'abonnement ──
    def set_addon_quantity(self, *, subscription_id: str, addon_price_id: str,
                           quantity: int) -> None:
        """Aligne la quantité de l'item d'add-on de l'abonnement Stripe sur
        `quantity` (proration Stripe par défaut). Crée l'item à la première
        demande, met à jour la quantité ensuite, le supprime à 0.

        N'écrit RIEN en base : l'effet revient via le webhook
        `customer.subscription.updated` (seule autorité, invariant 1). Surface
        StripeClient vérifiée par introspection (OPS-1b) : `subscriptions.retrieve`,
        `subscription_items.create/update/delete`."""
        sub = self._client.v1.subscriptions.retrieve(subscription_id)
        items = (getattr(sub, "items", None) or {})
        data = getattr(items, "data", None) or []
        existing = next(
            (it for it in data
             if getattr(getattr(it, "price", None), "id", None) == addon_price_id),
            None)
        if quantity <= 0:
            if existing is not None:
                self._client.v1.subscription_items.delete(existing.id)
            return
        if existing is not None:
            self._client.v1.subscription_items.update(
                existing.id, {"quantity": quantity})
        else:
            self._client.v1.subscription_items.create({
                "subscription": subscription_id,
                "price": addon_price_id,
                "quantity": quantity,
            })

    # ── Changement d'offre d'un abonné payant actif (V2-18d) ─────────────────
    def change_plan(self, *, subscription_id: str, new_price_id: str,
                    addon_price_id: str | None, remove_addon: bool) -> None:
        """Bascule l'abonnement Stripe EXISTANT vers `new_price_id` (proration
        Stripe par défaut : le temps déjà payé est crédité). Le price principal
        (item dont le price n'est PAS l'add-on) est échangé ; si la cible n'a pas
        d'add-on (`remove_addon`), les items d'add-on sont supprimés.

        N'écrit RIEN en base : l'effet revient via le webhook
        `customer.subscription.updated` (seule autorité, invariant 9). Surface
        StripeClient vérifiée par introspection (OPS-1b) : `subscriptions.retrieve`
        et `subscriptions.update` (jamais `modify`)."""
        sub = self._client.v1.subscriptions.retrieve(subscription_id)
        items = getattr(sub, "items", None) or {}
        data = getattr(items, "data", None) or []
        updates: list[dict] = []
        for it in data:
            price_id = getattr(getattr(it, "price", None), "id", None)
            is_addon = addon_price_id is not None and price_id == addon_price_id
            if is_addon:
                if remove_addon:
                    updates.append({"id": it.id, "deleted": True})
            else:
                # Item du plan principal → échange du price (proration).
                updates.append({"id": it.id, "price": new_price_id})
        self._client.v1.subscriptions.update(subscription_id, {
            "items": updates,
            "proration_behavior": "create_prorations",
        })

    # ── Downgrade PROGRAMMÉ à l'échéance (V2-18e) ─────────────────────────────
    def schedule_downgrade(self, *, subscription_id: str,
                           new_price_id: str) -> None:
        """Programme une bascule vers `new_price_id` à la FIN de la période en
        cours (pas d'effet immédiat, pas de prorata : le temps déjà payé reste
        acquis). Utilise un **Subscription Schedule** : phase 1 = l'offre actuelle
        jusqu'à `current_period_end`, phase 2 = la nouvelle offre (seule, sans
        add-on → les items d'add-on tombent à l'échéance). `end_behavior=release`
        rend la main à un abonnement normal une fois la bascule effectuée.

        N'écrit RIEN en base : la programmation (offre cible + date) revient par
        le webhook `subscription_schedule.created/updated`, et la bascule
        effective par `customer.subscription.updated` à l'échéance (seule
        autorité, invariants 9/12). Surface StripeClient vérifiée par
        introspection (OPS-1b) : `subscriptions.retrieve`,
        `subscription_schedules.create/retrieve/update`."""
        sub = self._client.v1.subscriptions.retrieve(subscription_id)
        schedule_id = getattr(sub, "schedule", None)
        if schedule_id:
            # Un schedule existe déjà (re-programmation) : on le réutilise.
            schedule = self._client.v1.subscription_schedules.retrieve(schedule_id)
        else:
            schedule = self._client.v1.subscription_schedules.create(
                {"from_subscription": subscription_id})
        phases = getattr(schedule, "phases", None) or []
        if not phases:
            raise StripeError("schedule sans phase courante")
        current = phases[0]
        cur_items = [{"price": _phase_price_id(it),
                      "quantity": getattr(it, "quantity", 1) or 1}
                     for it in (getattr(current, "items", None) or [])]
        self._client.v1.subscription_schedules.update(schedule.id, {
            "end_behavior": "release",
            "proration_behavior": "none",   # downgrade : jamais de prorata
            "phases": [
                {"items": cur_items,
                 "start_date": getattr(current, "start_date", None),
                 "end_date": getattr(current, "end_date", None)},
                {"items": [{"price": new_price_id, "quantity": 1}]},
            ],
        })

    def cancel_scheduled_change(self, *, subscription_id: str) -> None:
        """Annule un downgrade programmé encore non pris en compte : libère le
        Subscription Schedule (`release`) → l'abonnement reste sur l'offre en
        cours, la phase future est écartée. Sans schedule attaché, no-op.

        N'écrit RIEN en base : l'effacement revient par le webhook
        `subscription_schedule.released` (invariant 12)."""
        sub = self._client.v1.subscriptions.retrieve(subscription_id)
        schedule_id = getattr(sub, "schedule", None)
        if schedule_id:
            self._client.v1.subscription_schedules.release(schedule_id)

    # ── Vérification de signature d'un webhook ───────────────────────────────
    def construct_event(self, payload: bytes, sig_header: str) -> dict:
        """Vérifie la signature puis renvoie l'événement en **dict simple**.

        Lève `SignatureError` (signature/secret invalide) ou `ValueError`
        (secret non configuré / payload illisible) — le router traduit en 400.
        On parse le payload brut *après* vérification : aucun accès aux internals
        de la librairie, l'événement est un dict JSON standard."""
        if not self._webhook_secret:
            raise ValueError("CASAGUIDE_STRIPE_WEBHOOK_SECRET non configuré")
        # Vérifie la signature (lève si invalide). On ignore l'objet renvoyé.
        stripe.Webhook.construct_event(payload, sig_header, self._webhook_secret)
        return json.loads(payload)


def build_stripe(settings) -> StripeGateway | None:
    """Construit la passerelle si la clé API est présente, sinon None (mode
    dégradé : endpoints billing → 503). Le secret de webhook peut manquer même
    si la clé API est là (déploiement en deux temps) : `construct_event` le
    signalera alors clairement."""
    if not settings.stripe_configured:
        return None
    return LiveStripeGateway(
        api_key=settings.stripe_secret_key,
        webhook_secret=settings.stripe_webhook_secret,
    )
