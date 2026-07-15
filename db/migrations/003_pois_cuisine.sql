-- Migration 003 — Type de cuisine des restaurants (M-16)
-- Ajoute la colonne `cuisine` sur les POI : récoltée depuis le tag OSM
-- `cuisine` (normalisée : premier terme, minuscules), éditable par le
-- propriétaire, et utilisée pour le filtre par cuisine du guide voyageur.
--
-- Idempotent (IF NOT EXISTS). Sur une base fraîche, schema.sql crée déjà la
-- colonne → cette migration est un no-op.

ALTER TABLE pois
    ADD COLUMN IF NOT EXISTS cuisine TEXT;   -- ex. 'italian', 'pizza', 'seafood'
