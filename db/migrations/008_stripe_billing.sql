-- Migration 008 — Facturation Stripe (V2-05b)
--
-- Deux ajouts, tous deux idempotents (rejouables à chaque déploiement via
-- deploy.sh) :
--
--  1. `plans.stripe_price_id` — l'identifiant du Price Stripe correspondant au
--     plan payant (NULL pour 'free' et tant que `ops/stripe_sync_products.py`
--     n'a pas été lancé). Le prix reste défini EN BASE (`price_month_cts`) et est
--     *synchronisé vers* Stripe par le script de sync — jamais l'inverse
--     (invariant : les prix vivent dans `plans`, Stripe en est le miroir).
--
--  2. Table `stripe_events` — journal d'idempotence des webhooks. Stripe rejoue
--     ses événements (au moins une fois) : un `event.id` déjà présent est ignoré
--     silencieusement. Le webhook est la SEULE source de vérité de
--     `subscriptions.status/plan_id/current_period_end` ; ce journal garantit
--     qu'un même événement n'est traité qu'une fois.

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS stripe_price_id TEXT;   -- Price Stripe (NULL = non synchronisé)

CREATE TABLE IF NOT EXISTS stripe_events (
    id           TEXT PRIMARY KEY,                   -- event.id Stripe (evt_...)
    type         TEXT NOT NULL,                      -- ex. checkout.session.completed
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(), -- première réception
    processed_at TIMESTAMPTZ                         -- NULL tant que non traité
);
