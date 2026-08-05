-- V2-30 — Photo de couverture du logement.
--
-- La vignette des emails et l'og-image des liens partagés prennent aujourd'hui la
-- PREMIÈRE photo du guide (niveau logement, puis 1re section visible). Utile au
-- locataire dans sa section (« la boîte à clés vue de la rue »), désastreux comme
-- image de vente. On désigne désormais UNE photo de couverture par logement.
--
-- `cover_media_id` référence un média de CE logement. `ON DELETE SET NULL` : si la
-- photo est supprimée, la couverture redevient NULL (repli gracieux sur la première
-- photo puis l'image générée — le contrat est le repli à chaque étage).
--
-- Aucun backfill : NULL = comportement actuel à l'identique. Idempotente (rejeu sûr).
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS cover_media_id UUID REFERENCES media(id) ON DELETE SET NULL;
