-- V2-07 volet 2 — Provenance de la COMPLÉTION automatique des fiches de service.
--
-- La complétion (téléphone/site/horaires par Claude + recherche web, avec preuve)
-- ne change NI le `source` NI le `status` du POI (ce n'est pas une édition
-- propriétaire ; le POI reste 'osm'/'approved'…). Mais la provenance doit rester
-- traçable : `completion_meta` (JSONB) porte, PAR CHAMP complété, l'URL de preuve
-- et la date de vérification, plus un marqueur `_checked_on` (dernière tentative,
-- même infructueuse → cadence de re-vérification maîtrisée, jamais de re-appel en
-- boucle). Forme :
--   {"phone":       {"source_url": "https://…", "verified_on": "2026-08-12"},
--    "opening_hours":{"source_url": "https://…", "verified_on": "2026-08-12"},
--    "_checked_on":  "2026-08-12"}
--
-- Idempotente (IF NOT EXISTS) ; DEFAULT NULL → aucun backfill, les POI existants
-- gardent NULL (= jamais complétés). Testée contre l'état antérieur réel (une base
-- de pois sans la colonne : l'ajout est non destructif, la valeur reste NULL).

ALTER TABLE pois ADD COLUMN IF NOT EXISTS completion_meta JSONB;
