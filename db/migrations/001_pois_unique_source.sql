-- Migration 001 — Idempotence des enrichissements
-- Découverte lors du développement du pipeline : l'upsert des POI exige une
-- contrainte d'unicité (property_id, source, source_ref). Index partiel car
-- les POI ajoutés à la main par le propriétaire n'ont pas de source_ref.

CREATE UNIQUE INDEX IF NOT EXISTS uq_pois_property_source_ref
    ON pois (property_id, source, source_ref)
    WHERE source_ref IS NOT NULL;
