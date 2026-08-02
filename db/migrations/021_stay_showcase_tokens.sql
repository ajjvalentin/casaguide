-- Migration 021 — Lien de séjour & lien vitrine (V2-23c, volet 1)
--
-- Jusqu'ici le guide n'avait qu'UN lien : `guide_token` (le « lien maison »),
-- éternel, identique pour tous, montrant les vrais secrets à quiconque le
-- possède et rattachant les demandes de service au séjour EN COURS à l'instant
-- du clic (devinette). Depuis que le guide est interactif (V2-23b volet 3),
-- c'est intenable : envoyer le guide à l'avance au voyageur du 12.09 accroche
-- ses demandes au séjour d'un autre, le code de boîte à clés ne peut pas changer
-- d'un séjour à l'autre, et rien ne peut être montré à un prospect sans lui
-- donner les codes de la maison.
--
-- On introduit DEUX nouveaux tokens (le lien maison SURVIT tel quel) :
--   bookings.stay_token       — lien de SÉJOUR (`/b/{stay_token}`, préfixe
--     dédié : le logement est résolu côté serveur, le guide_token éternel ne
--     voyage JAMAIS dans l'URL envoyée au locataire), personnalisé, rattachement
--     certain des demandes, expire J+7 après le départ.
--   properties.showcase_token — lien VITRINE (`/v/{showcase_token}`), n'expose
--     JAMAIS un secret réel (valeurs d'exemple), pour prospect/annonce/démo.
--
-- Préfixes publics réservés (table gravée dans CLAUDE.md) :
--   /g/ maison · /s/ équipe · /v/ vitrine · /b/ séjour
--
-- Tokens générés À LA DEMANDE (premier usage dans la fenêtre d'envoi, volet 3),
-- avec la même fabrique haute entropie que guide_token/staff_token (≥ 128 bits).
-- NULL = jamais partagé → AUCUN backfill nécessaire. Le token de séjour vit sur
-- le séjour : il meurt avec lui (séjour annulé/supprimé → 404, on ne révèle rien).
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). La contrainte UNIQUE tolère plusieurs
-- NULL (Postgres). Sûr au rejeu.

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS stay_token TEXT UNIQUE;      -- lien de séjour (≥ 128 bits)

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS showcase_token TEXT UNIQUE;  -- lien vitrine (≥ 128 bits)
