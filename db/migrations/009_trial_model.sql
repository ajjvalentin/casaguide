-- Migration 009 — Modèle d'essai : échéance d'abonnement (V2-18a, volet 1)
--
-- L'essai de 21 jours remplace le plan gratuit à l'inscription (décisions du
-- 25/07). Le *plan* 'trial' et les drapeaux de fonctionnalités (watermark solo,
-- pdf_export) vivent dans `db/seed.sql` (source de vérité des plans, rejouée en
-- DERNIER par deploy.sh — une définition ici serait écrasée). Cette migration ne
-- porte donc que le changement de SCHÉMA que le seed ne peut pas faire :
--
--   `subscriptions.trial_ends_at` — échéance de l'essai (NULL hors essai). Un
--   abonnement `status='trialing'` dont `trial_ends_at < now()` est « expiré » :
--   l'expiration est calculée À LA LECTURE (api/plans.is_trial_expired), jamais
--   par un job qui basculerait des lignes (invariant V2-18a : pas d'état à
--   maintenir, pas de course).
--
-- Idempotent (IF NOT EXISTS) et sûr au rejeu : les comptes existants gardent
-- leur plan et leur statut (`trial_ends_at` reste NULL → jamais « expiré »).

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;
