-- V2-71 : sous-type d'un lieu de sport/loisir (discipline OSM `sport`, sinon type
-- `leisure` : sports_centre, pitch, swimming_pool, stadium…). Affiché en puce dans le
-- guide (comme les cuisines des restaurants) et transmis à la passe de description IA
-- pour nommer la discipline. Idempotent, DEFAULT NULL, aucun backfill (rempli au
-- prochain (ré)enrichissement).
ALTER TABLE pois ADD COLUMN IF NOT EXISTS subtype TEXT;
