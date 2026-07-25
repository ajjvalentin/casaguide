-- Migration 011 — Grille tarifaire, add-on logements, gating staff (V2-18b, volet 1)
--
-- Les PRIX et les FEATURES (langs, staff_guide, addon_property_price_cts) vivent
-- dans db/seed.sql (source de vérité des plans, rejouée en DERNIER par
-- deploy.sh — une définition ici serait écrasée). Cette migration ne porte donc
-- que les changements de SCHÉMA que le seed ne peut pas faire :
--
--   plans.addon_property_price_cts — prix mensuel (centimes) d'un logement
--     supplémentaire (add-on Pro) ; NULL = pas d'add-on sur ce plan.
--   plans.addon_stripe_price_id    — Price Stripe de l'add-on (écrit par
--     ops/stripe_sync_products.py, comme stripe_price_id).
--   subscriptions.addon_qty        — quantité d'add-on effective. Écrite
--     EXCLUSIVEMENT par le webhook Stripe (invariant 1 : l'UI demande, Stripe
--     dispose, le webhook écrit). Défaut 0.
--   subscriptions.staff_grandfathered — clause de grand-père du guide équipe
--     (/s/, invariant 3) : true pour tous les comptes EXISTANT à la date de la
--     migration (personne ne perd l'accès), false pour les nouveaux (défaut).
--
-- Idempotent (IF NOT EXISTS) et sûr au rejeu.

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS addon_property_price_cts INT,    -- NULL = pas d'add-on
    ADD COLUMN IF NOT EXISTS addon_stripe_price_id     TEXT;  -- Price Stripe de l'add-on

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS addon_qty INT NOT NULL DEFAULT 0;

-- staff_grandfathered : on ajoute la colonne avec DEFAULT true → toutes les
-- lignes EXISTANTES sont backfillées à true (grand-père, invariant 3). On
-- rebascule ensuite le DEFAULT à false → les futurs abonnements suivent la
-- grille. Au rejeu, ADD COLUMN IF NOT EXISTS est sauté (les valeurs déjà posées
-- sont préservées) ; SET DEFAULT false reste idempotent.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS staff_grandfathered BOOL NOT NULL DEFAULT true;
ALTER TABLE subscriptions
    ALTER COLUMN staff_grandfathered SET DEFAULT false;
