-- Migration 019 — Registre des langues du produit (V2-21a, volet 1)
--
-- Jusqu'ici, la liste des langues offertes par le produit était CODÉE EN DUR à
-- une dizaine d'endroits (sélecteur du guide, menu de partage, modale séjour,
-- cibles de traduction, libellés…). Ajouter une langue relevait donc du
-- développement. On introduit un REGISTRE unique en base : `languages`.
--
--   status : 'draft'      — langue préparée mais invisible pour l'utilisateur ;
--            'in_review'   — en relecture (toujours invisible) ;
--            'published'   — offerte au produit (sélecteurs, partage, détection).
--
-- Principe directeur (invariant 8 étendu aux langues) : le produit n'offre
-- JAMAIS que les langues `published`. Plus aucune liste de langues en dur : ce
-- registre fait foi partout (filtre status='published', tri sort_order).
--
-- `register_note` porte la décision de REGISTRE (vouvoiement/tutoiement) tranchée
-- une fois par langue et imposée au modèle à chaque génération — cohérence
-- terminologique > qualité ponctuelle. Ajustable en base, jamais en dur.
--
-- Idempotent (CREATE TABLE IF NOT EXISTS + seed ON CONFLICT). Le seed ne touche
-- JAMAIS `status` (c'est un état d'EXPLOITATION, pas une donnée de seed : un
-- rejeu ne doit ni dépublier ni republier une langue). Backfill : aucun logement
-- ni aucun `published_langs` ne bouge — le registre est une NOUVELLE table.

CREATE TABLE IF NOT EXISTS languages (
    code        TEXT PRIMARY KEY,          -- 'fr', 'en', 'es', 'nl', 'de', 'it', 'sq'
    name_native TEXT NOT NULL,             -- 'Français', 'Nederlands', 'Shqip'…
    status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','in_review','published')),
    sort_order  INT  NOT NULL DEFAULT 0,
    register_note TEXT                     -- consigne de registre imposée au modèle
);

-- Seed idempotent. `fr`/`en`/`es` = état actuel du produit (déjà offerts) →
-- insérés `published`. `nl`/`de`/`it`/`sq` = préparés en `draft` (générés puis
-- relus en missions V2-21b…n, publiés une à une). Le DO UPDATE ne rafraîchit
-- QUE name_native/sort_order/register_note — jamais `status`.
INSERT INTO languages (code, name_native, status, sort_order, register_note) VALUES
    ('fr', 'Français',   'published', 10, NULL),
    ('en', 'English',    'published', 20, NULL),
    ('es', 'Español',    'published', 30, NULL),
    ('nl', 'Nederlands', 'draft',     40, 'Vouvoiement (u) — le guide s''adresse poliment au voyageur.'),
    ('de', 'Deutsch',    'draft',     50, 'Vouvoiement (Sie) — le guide s''adresse poliment au voyageur.'),
    ('it', 'Italiano',   'draft',     60, 'Voi — le guide s''adresse au groupe de voyageurs.'),
    ('sq', 'Shqip',      'draft',     70, 'Forme de politesse (ju) — le guide s''adresse poliment au voyageur.')
ON CONFLICT (code) DO UPDATE SET
    name_native   = EXCLUDED.name_native,
    sort_order    = EXCLUDED.sort_order,
    register_note = EXCLUDED.register_note;
