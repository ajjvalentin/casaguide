-- Migration 006 — Grand-père des comptes existants pour la vérification d'email (V2-08)
--
-- La vérification d'email n'existe qu'à partir de V2-08 : tout compte créé
-- AVANT ne doit jamais voir le bandeau « vérifiez votre email » (ne pas gêner
-- André et le testeur en cours). On marque donc vérifiés les comptes qui n'ont
-- JAMAIS reçu de jeton de vérification.
--
-- Idempotent ET sûr au rejeu (deploy.sh relance les migrations à chaque
-- déploiement) : à partir de V2-08, toute inscription émet un jeton purpose='verify'.
-- Un compte non vérifié qui POSSÈDE un tel jeton est un nouveau compte en attente
-- → il n'est jamais grand-périsé. Le garde `email_verified = FALSE` rend l'UPDATE
-- inopérant sur les comptes déjà vérifiés.

UPDATE owners o SET email_verified = TRUE, updated_at = now()
WHERE o.email_verified = FALSE
  AND NOT EXISTS (
      SELECT 1 FROM password_resets pr
      WHERE pr.owner_id = o.id AND pr.purpose = 'verify'
  );
