-- V2-42 — titre de rubrique personnalisable par logement.
--
-- La rubrique B_pool s'intitule « Piscine / jacuzzi / barbecue » au seed : un titre
-- figé qui promet des équipements absents (Ballarin a un barbecue mais pas de
-- jacuzzi). Générique : le propriétaire peut renommer N'IMPORTE QUELLE rubrique ; le
-- voyageur lit ce titre dans sa langue ; vide = comportement actuel au mot près.
--
-- Deux colonnes : la SOURCE (`property_sections.title_override`, saisie par le
-- propriétaire dans la langue du logement) et sa TRADUCTION par langue
-- (`section_translations.title_override`, écrite par le pipeline de traduction, motif
-- de `body_md`). Au rendu : traduit → sinon source → sinon nom du modèle du seed.
--
-- Idempotente (IF NOT EXISTS), DEFAULT NULL, aucun backfill : NULL = nom du modèle.
ALTER TABLE property_sections   ADD COLUMN IF NOT EXISTS title_override TEXT;
ALTER TABLE section_translations ADD COLUMN IF NOT EXISTS title_override TEXT;
