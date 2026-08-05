-- V2-23c volet 2 — Surcharge du code de boîte à clés par séjour.
--
-- Le logement porte le code par défaut (`property_secrets.keybox_code_enc`). Un
-- SÉJOUR peut le REMPLACER le temps de sa fenêtre : c'est la « surcharge welcome
-- pack » (V2-23b) appliquée aux secrets — le logement porte le défaut, le séjour
-- peut le remplacer, le moteur lit à travers.
--
-- Même régime de chiffrement que `property_secrets` : AES applicatif (api/crypto),
-- clé hors base, jamais en clair (invariant 5). La surcharge n'est servie que par
-- le lien de séjour `/b/{stay_token}/secrets` — elle meurt donc à J+8 avec la page
-- (le lien maison `/g/` sert toujours le code du logement, acté).
--
-- NULL = pas de surcharge → code du logement (comportement actuel préservé). Aucun
-- backfill nécessaire. Idempotente (rejeu sûr).
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS keybox_code_enc BYTEA;
