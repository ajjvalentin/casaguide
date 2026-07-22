-- Migration 007 — Rattrapage : un abonnement 'free' pour tout compte sans abonnement (V2-05a)
--
-- Le modèle plans/subscriptions (CdC §10) existe depuis le schéma initial et
-- l'inscription crée déjà une ligne `subscriptions` (api/routers/auth.py). Cette
-- migration est un FILET DE SÉCURITÉ : tout `owner` sans aucun abonnement (compte
-- créé hors du flux d'inscription — import manuel, futur OAuth, base partielle)
-- reçoit un abonnement sur le plan gratuit.
--
-- Décision (V2-05a) : les comptes rattrapés sont posés en `status='active'`, PAS
-- `trialing` — il n'y a aucune logique d'essai dans cette mission ; le plan
-- gratuit est un palier permanent, pas une période d'essai. En V2-05b, le webhook
-- Stripe deviendra la seule source de vérité du `status` des plans payants.
--
-- Idempotent ET sûr au rejeu (deploy.sh relance les migrations à chaque
-- déploiement) : le `NOT EXISTS` ne crée jamais de second abonnement pour un
-- compte qui en a déjà un (gratuit OU payant) → aucun downgrade, aucun doublon.

INSERT INTO subscriptions (owner_id, plan_id, status)
SELECT o.id, 'free', 'active'
FROM owners o
WHERE NOT EXISTS (
    SELECT 1 FROM subscriptions s WHERE s.owner_id = o.id
);
