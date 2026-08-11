-- Migration 029 — Jour du marché (V2-33 volet 1)
--
-- Les marchés locaux (catégorie `market`) portaient jusqu'ici leur jour DANS le
-- nom (« Samedi · Mercadillo de Zoco ») — compromis d'un jour, illisible dans un
-- guide anglais et impossible à trier « par jour ». On sort le jour du nom vers
-- une colonne dédiée :
--
--   · pois.weekday       SMALLINT NULL, 1..7 (1 = lundi, ISO-8601) ;
--   · pois.weekday_note  TEXT NULL, précision libre (« été seulement »,
--                        « fin juin → début sept », « soir »…).
--
-- Le badge du jour est ensuite TRADUIT au rendu (Intl.DateTimeFormat côté client,
-- Babel/CLDR côté serveur) — aucune clé i18n, 7 langues gratuites.
--
-- Idempotente (IF NOT EXISTS + CHECK conditionnel). Sur une base fraîche,
-- schema.sql crée déjà les colonnes → les ALTER sont des no-op.

ALTER TABLE pois ADD COLUMN IF NOT EXISTS weekday      SMALLINT;
ALTER TABLE pois ADD COLUMN IF NOT EXISTS weekday_note TEXT;

-- Contrainte de domaine (pas de IF NOT EXISTS pour les CHECK avant PG 17 → on la
-- (re)crée de façon idempotente, même motif que la migration 028).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'pois_weekday_check'
  ) THEN
    ALTER TABLE pois
      ADD CONSTRAINT pois_weekday_check CHECK (weekday BETWEEN 1 AND 7);
  END IF;
END $$;

-- ── Backfill ciblé : les marchés dont le nom commence par un jour français ────
--
-- « Samedi · … » → weekday=6, préfixe retiré ; « Mercredi soir (été) · … » →
-- weekday=3, weekday_note='soir (été)'. C'est la migration des 7 marchés de
-- Ballarin, écrite en général (tout nom de la catégorie `market` préfixé d'un jour
-- FR suivi de « · »). IDEMPOTENTE : on ne touche qu'un séjour à `weekday IS NULL`
-- dont le nom matche encore le motif — un nom déjà nettoyé n'a plus de préfixe et
-- porte déjà son `weekday`, donc le rejeu est un no-op.
--
-- Restreint à la catégorie `market` : le compromis « jour dans le nom » n'a existé
-- que là ; on évite d'amputer un restaurant qui s'appellerait « Lundi au Soleil ».
DO $$
DECLARE
  days text[] := ARRAY['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche'];
  n   int;
BEGIN
  FOR n IN 1..7 LOOP
    UPDATE pois SET
      weekday      = n,
      -- Note = le texte entre le jour et le « · » (souvent vide → NULL).
      weekday_note = NULLIF(btrim(
        substring(name from '(?i)^' || days[n] || '\s*([^·]*?)\s*·')), ''),
      -- Nom nettoyé = tout ce qui suit le premier « · ».
      name         = btrim(substring(name from '·\s*(.*)$'))
    WHERE category_code = 'market'
      AND weekday IS NULL
      -- Jour EN DÉBUT DE NOM (frontière : espace ou « · ») ET un « · » présent
      -- (sans « · », il n'y a pas de préfixe à retirer → on ne touche pas).
      AND name ~* ('^' || days[n] || '(\s|·)[^·]*·');
  END LOOP;
END $$;
