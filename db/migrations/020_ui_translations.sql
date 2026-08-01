-- Migration 020 — Traductions des libellés STATIQUES du produit (V2-21a, volet 2)
--
-- Volet 1 a posé le registre des langues (`languages`). Volet 2 outille la
-- traduction des LIBELLÉS STATIQUES du guide voyageur (interface `_UI`, noms de
-- chapitres, catégories de POI, sections du seed, cuisines…) — par opposition au
-- CONTENU des guides (sections remplies, POI), déjà couvert par le pipeline de
-- traduction par logement (M-09).
--
--   ui_translations(lang, key, text) — UNE seule source de vérité des libellés
--     traduits, par (langue, clé stable). Les libellés FR/EN/ES restent portés
--     par le CODE (guide_page._UI…) et le SEED (name_i18n) : cette table ne porte
--     QUE les langues supplémentaires (nl/de/it/sq…), importées depuis la
--     relecture (ops/i18n_import.py). Le SSR les superpose au rendu pour toute
--     langue publiée ; jamais d'écrasement des sources FR/EN/ES.
--
-- Rien à backfiller : table NOUVELLE, vide au départ (aucune langue supplémentaire
-- n'est encore publiée). Idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS ui_translations (
    lang        TEXT NOT NULL REFERENCES languages(code) ON DELETE CASCADE,
    key         TEXT NOT NULL,          -- clé stable de l'inventaire (i18n/inventory.json)
    text        TEXT NOT NULL,          -- libellé traduit
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lang, key)
);

CREATE INDEX IF NOT EXISTS idx_ui_translations_lang ON ui_translations(lang);
