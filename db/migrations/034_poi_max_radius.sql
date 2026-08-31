-- V2-44 volet 1 — Collecte adaptée à la ruralité : rayon MAXIMAL par catégorie.
--
-- Constat (benchmark Op de Boerderie, Noordgouwe NL, Zélande rurale) : les rayons
-- par catégorie sont calibrés sur un littoral DENSE (supermarché 3 km, boulangerie
-- 2 km, plage 10 km) → en zone rurale ils ne trouvent RIEN, alors que Zierikzee à
-- 10 min a Albert Heijn/Jumbo/Lidl, pharmacies, vingt restaurants, et Renesse ses
-- plages à 17 min.
--
-- `default_radius_m` devient le rayon de PRÉFÉRENCE ; `max_radius_m` est le rayon
-- jusqu'où on accepte de compléter quand la préférence ne garantit pas un minimum
-- de résultats (overpass._select_adaptive, MIN_RESULTS). En zone dense, la
-- préférence est déjà pleine → aucune escalade, comportement inchangé (test
-- byte-identique). NULL = pas d'escalade (rayon fixe, ex. parking, arrêt de bus,
-- aéroport — capé aux plus proches, pas élargi).
--
-- Idempotente (IF NOT EXISTS + UPDATE rejouable). Les valeurs font AUTORITÉ dans le
-- seed (poi_categories) ; ce backfill couvre les bases déjà en place.
ALTER TABLE poi_categories ADD COLUMN IF NOT EXISTS max_radius_m INT;

-- Catégories de PROXIMITÉ élargies à 25 km (commerces, santé, services, sorties,
-- loisirs, transports terrestres) : trouver au moins MIN_RESULTS lieux même en
-- rural, sans jamais rien changer en zone dense.
UPDATE poi_categories SET max_radius_m = 25000 WHERE code IN (
  'supermarket','market','bakery','atm','post_office','mall','laundry',
  'pharmacy','doctor','police','veterinary','taxi','rental',
  'restaurant','bar','cafe','beach','sight','family_activity','sport',
  'bus_station','train_station','fuel','charging_station');

-- Rayon FIXE (pas d'escalade) : hyper-local (parking, arrêt de bus), déjà large
-- (hôpital 25 km), aéroport (capé aux plus proches, jamais élargi), ou catégories
-- Claude (baby-sitting, livraison — hors Overpass). max_radius_m = default_radius_m.
UPDATE poi_categories SET max_radius_m = default_radius_m WHERE code IN (
  'parking','bus_stop','hospital','airport','babysitter','food_delivery');
