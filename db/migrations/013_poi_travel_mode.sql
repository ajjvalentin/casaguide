-- Migration 013 — Mode de trajet préféré par catégorie de POI (V2-24)
--
-- Le guide choisissait jusqu'ici à pied / en voiture selon la seule distance
-- (à pied si ≤ 30 min, sinon voiture). Absurde pour les catégories « véhicule » :
-- on va à la station-service EN voiture même à 400 m. On ajoute donc un mode de
-- trajet préféré PAR CATÉGORIE :
--
--   poi_categories.travel_mode :
--     'driving' — toujours en voiture, avec la distance (fuel, charging_station) ;
--     'walking' — à pied tant que raisonnable (beach) ;
--     NULL      — comportement historique (auto selon distance).
--
-- Le mode ne modifie JAMAIS les distances stockées (walk_min / drive_min sont
-- toutes deux calculées par l'enrichissement) : il ne change que l'affichage.
-- Une catégorie 'driving' dont le temps voiture manquerait (POI ancien) retombe
-- proprement sur le temps à pied ; le prochain enrichissement (ou
-- `POST /api/properties/{id}/recompute-distances`) comble le trou.
--
-- Idempotent (IF NOT EXISTS + UPDATE ciblés) et sûr au rejeu.

ALTER TABLE poi_categories
    ADD COLUMN IF NOT EXISTS travel_mode TEXT;

-- Contrainte de domaine ajoutée une seule fois (pas de IF NOT EXISTS pour les
-- CHECK avant PostgreSQL 17 → on la (re)crée de façon idempotente).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'poi_categories_travel_mode_check'
  ) THEN
    ALTER TABLE poi_categories
      ADD CONSTRAINT poi_categories_travel_mode_check
      CHECK (travel_mode IN ('driving', 'walking'));
  END IF;
END $$;

UPDATE poi_categories SET travel_mode = 'driving'
  WHERE code IN ('fuel', 'charging_station');
UPDATE poi_categories SET travel_mode = 'walking'
  WHERE code = 'beach';
