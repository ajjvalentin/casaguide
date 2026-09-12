-- V2-54 Mission A — Offre « Guide Voyageur » : entité guest, owner système, anti-abus.
--
-- Un vacancier achète (2,90 € one-shot) un guide touristique auto-généré à l'adresse
-- de son lieu de vacances : instantané FIGÉ (jamais ré-enrichi, jamais expiré), servi
-- comme n'importe quel guide `/g/` MAIS amputé (Autour de vous + Urgences seulement),
-- sans donnée propriétaire. On RÉUTILISE l'entité `property` (rendu/SSR/i18n/cartes/PWA
-- inchangés) — un simple drapeau distingue ces fiches.
--
-- Idempotente (IF NOT EXISTS + ON CONFLICT). Aucun backfill : DEFAULT FALSE couvre
-- l'existant (aucune fiche existante n'est un guide voyageur).

-- 1. Le drapeau qui distingue une fiche « guide voyageur » d'une fiche propriétaire.
--    Ces fiches sont exclues des listings/quotas propriétaires (elles appartiennent
--    à l'owner système ci-dessous) mais restent visibles de l'audit géocodage/ops.
ALTER TABLE properties ADD COLUMN IF NOT EXISTS guest_guide BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. Owner SYSTÈME propriétaire de toutes les fiches guest (properties.owner_id est
--    NOT NULL). Résolu par e-mail côté application (jamais d'UUID en dur, invariant 8).
--    Domaine `.internal` non routable : ce compte n'a pas de mot de passe et ne se
--    connecte jamais au back-office.
INSERT INTO owners (email, full_name, email_verified, is_active)
VALUES ('guest-guides@holaguia.internal', 'Guides Voyageur', true, true)
ON CONFLICT (email) DO NOTHING;

-- 3. Journal des générations payantes (anti-abus). Alimenté par la génération
--    (Mission B collecte l'e-mail au checkout, l'IP au tunnel). Sert aux limites
--    par e-mail et par IP (fenêtre glissante). property_id NULL possible (génération
--    refusée avant création). ON DELETE SET NULL : purger un guide ne perd pas la trace.
CREATE TABLE IF NOT EXISTS guest_guide_generations (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email        TEXT,
    ip           TEXT,
    property_id  UUID REFERENCES properties(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_guest_gen_email ON guest_guide_generations(email, created_at);
CREATE INDEX IF NOT EXISTS idx_guest_gen_ip    ON guest_guide_generations(ip, created_at);

-- 4. Le cache de proximité (resservir un guide voisin de < 100 m et < 30 j) s'appuie
--    sur l'index GIST existant idx_properties_geom (ST_DWithin sur geom::geography) —
--    aucun index supplémentaire nécessaire.
