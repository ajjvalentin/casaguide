-- V2-66b cas (a) : nom LOCAL (écriture d'origine) d'un pick éditorial (V2-56).
-- Les picks « réputés » viennent d'une recherche web à noms latinisés (« Kyubey ») et
-- ne passaient pas par le mapping OSM/Overture qui renseigne `_name_local` → à Tokyo,
-- restaurants/bars/cafés éditoriaux sans nom japonais ni bouton 🔊. On mémorise le nom
-- local (issu de la fiche OSM appariée, sinon fourni par la recherche web) pour le
-- reporter en `completion_meta._name_local` à la matérialisation en POI.
-- Idempotent.
ALTER TABLE editorial_picks ADD COLUMN IF NOT EXISTS name_local TEXT;
