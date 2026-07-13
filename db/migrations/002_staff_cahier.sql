-- Migration 002 — Cahier de préparation pour l'équipe d'entretien (M-13)
-- Ajoute la distinction guest/staff sur les sections et un second lien secret
-- (staff_token) sur les logements, distinct du lien voyageur (guide_token).
--
-- À exécuter APRÈS db/seed.sql (idempotent : IF NOT EXISTS). Sur une base
-- fraîche, schema.sql crée déjà ces colonnes → cette migration est un no-op.

-- Public cible d'une section : 'guest' (guide voyageur) | 'staff' (cahier équipe).
ALTER TABLE section_templates
    ADD COLUMN IF NOT EXISTS audience TEXT NOT NULL DEFAULT 'guest';

-- Second lien secret (128 bits), distinct de guide_token. Le DEFAULT volatile
-- est évalué par ligne → chaque logement existant reçoit un token unique.
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS staff_token TEXT NOT NULL
        DEFAULT encode(gen_random_bytes(16), 'hex');

-- Unicité du staff_token (créée à part car ADD COLUMN ... UNIQUE n'est pas
-- combinable avec IF NOT EXISTS ; l'index est lui-même idempotent).
CREATE UNIQUE INDEX IF NOT EXISTS uq_properties_staff_token
    ON properties (staff_token);
