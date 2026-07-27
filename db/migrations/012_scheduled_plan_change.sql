-- Migration 012 — Changement d'offre programmé à l'échéance (V2-18e)
--
-- Un DOWNGRADE (offre cible moins chère) ne prend PAS effet immédiatement : le
-- plan courant reste actif jusqu'à `current_period_end`, la nouvelle offre
-- démarre à cette date (politique produit V2-18e). Pour afficher ce changement
-- programmé côté back-office (bandeau « Solo à partir du JJ/MM — Annuler »), on
-- mémorise en base l'offre cible et sa date d'effet.
--
--   subscriptions.scheduled_plan_id   — id du plan qui prendra le relais à
--     l'échéance (NULL = aucun changement programmé).
--   subscriptions.scheduled_change_at — date d'effet du changement (fin de la
--     période en cours ; NULL si aucun changement programmé).
--
-- INVARIANT 12 (V2-18e) : ces deux colonnes sont écrites EXCLUSIVEMENT par le
-- webhook Stripe (événements `subscription_schedule.*` + transition de phase),
-- jamais par un endpoint synchrone (l'UI demande, Stripe programme, le webhook
-- écrit — corollaire des invariants 9/11). Elles sont purement informatives :
-- l'accès aux quotas ne dépend QUE de `plan_id`.
--
-- Idempotent (IF NOT EXISTS) et sûr au rejeu.

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS scheduled_plan_id   TEXT REFERENCES plans(id),
    ADD COLUMN IF NOT EXISTS scheduled_change_at TIMESTAMPTZ;
