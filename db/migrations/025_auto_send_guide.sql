-- Migration 025 — Envoi automatique du guide à J-7 (V2-23d, volet 2)
--
-- Volet 1 a posé le canal d'envoi manuel (`routers/send.py`) et sa trace
-- (`guide_sends`). Volet 2 automatise l'envoi 7 jours avant l'arrivée, via un
-- timer quotidien. Deux ajouts :
--
--   1. properties.auto_send_guide — interrupteur PAR LOGEMENT, défaut ACTIVÉ.
--      Le DEFAULT TRUE couvre l'existant (pas de backfill séparé) : tous les
--      logements déjà en base optent pour l'envoi automatique, cohérent avec la
--      décision « défaut activé » (André, 05/08).
--
--   2. guide_sends.origin — 'manual' (envoi depuis la fenêtre du back-office) ou
--      'auto' (envoi par le planificateur J-7). Le DEFAULT 'manual' backfille
--      trivialement les lignes existantes (toutes issues du canal manuel du
--      volet 1). Le REGISTRE des envois reste le verrou d'idempotence : une ligne
--      kind='stay' (peu importe l'origine) empêche tout renvoi automatique.
--
-- Idempotente (IF NOT EXISTS + CHECK conditionnel), sûre au rejeu. Testée contre
-- l'état antérieur réel (base SANS ces colonnes) : le DEFAULT s'applique aux
-- lignes existantes à l'ajout de la colonne.

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS auto_send_guide BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE guide_sends
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual';

-- Contrainte de domaine ajoutée une seule fois (pas de IF NOT EXISTS pour les
-- CHECK avant PostgreSQL 17 → on la (re)crée de façon idempotente).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'guide_sends_origin_check'
  ) THEN
    ALTER TABLE guide_sends
      ADD CONSTRAINT guide_sends_origin_check
      CHECK (origin IN ('manual', 'auto'));
  END IF;
END $$;
