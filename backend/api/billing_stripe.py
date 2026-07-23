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


class StripeGateway(Protocol):
    """Interface minimale utilisée par les routers de facturation."""

    def get_or_create_customer(self, *, owner_id: str, email: str,
                               existing_customer_id: str | None) -> str: ...

    def create_checkout_session(self, *, customer_id: str, price_id: str,
                                owner_id: str, success_url: str,
                                cancel_url: str) -> str: ...

    def create_portal_session(self, *, customer_id: str,
                              return_url: str) -> str: ...

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
