-- V2-58 — Vitrine holaguia.com : guide de DÉMONSTRATION vivant.
--
-- La page d'accueil montre un VRAI guide cliquable (le différenciateur du créneau).
-- Ce guide est une fiche « guide voyageur » (guest_guide) DÉDIÉE, marquée `demo` :
-- exclue du cache anti-abus (une commande réelle près de la démo ne doit JAMAIS
-- resservir la démo), régénérable par un script idempotent, jamais un guide client.
--
-- Idempotente. Aucun backfill (DEFAULT FALSE).
ALTER TABLE properties ADD COLUMN IF NOT EXISTS demo BOOLEAN NOT NULL DEFAULT FALSE;
