-- V2-93 : revérification planifiée des lieux auprès de leur source d'origine.
--
--   last_checked_at    : date du dernier contrôle par identifiant auprès de la source
--                        (`fetched_at` reste la date de MOISSON).
--   source_state       : ce que la source dit du lieu au dernier contrôle —
--                        'ok' | 'closed' (OSM `is_closed`) | 'missing' (l'élément
--                        n'existe plus) | NULL (jamais contrôlé).
--   source_state_since : depuis quand la source est dans cet état (ne bouge pas tant
--                        que l'état reste le même).
--
-- Ces trois colonnes ne sont PAS suivies par le journal (`trg_pois_journal` ne trace
-- que `locks.LOCKABLE_FIELDS`) : un contrôle quotidien sans changement n'écrit rien.
-- Constater « fermé » ou « disparu » ne retire rien du guide (le retrait passe par le
-- juge et le propriétaire, V2-94).
--
-- Idempotente : ADD COLUMN IF NOT EXISTS ; contrainte DROP → ADD ; index IF NOT EXISTS ;
-- déclencheur DROP IF EXISTS → CREATE.
-- Aucune reprise de l'existant (NULL = jamais contrôlé : la première nuit contrôle tout).

BEGIN;

ALTER TABLE pois ADD COLUMN IF NOT EXISTS last_checked_at    TIMESTAMPTZ;
ALTER TABLE pois ADD COLUMN IF NOT EXISTS source_state       TEXT;
ALTER TABLE pois ADD COLUMN IF NOT EXISTS source_state_since TIMESTAMPTZ;

ALTER TABLE pois DROP CONSTRAINT IF EXISTS pois_source_state_check;
ALTER TABLE pois ADD CONSTRAINT pois_source_state_check
    CHECK (source_state IN ('ok', 'closed', 'missing'));

-- Sélection nocturne : fiches retenues revérifiables, les plus anciennement contrôlées.
CREATE INDEX IF NOT EXISTS idx_pois_refresh_due
    ON pois (property_id, last_checked_at)
    WHERE status IN ('approved', 'edited') AND source IN ('osm', 'overture')
      AND source_ref IS NOT NULL;

-- `updated_at` reste la date de la dernière modification RÉELLE : une écriture dont
-- SEULES les colonnes de contrôle changent de valeur (revérification sans changement)
-- ne le bouge pas. Toute autre écriture le pose, comme avant (y compris une écriture
-- qui ne change aucune valeur).
DROP TRIGGER IF EXISTS trg_pois_updated ON pois;
CREATE TRIGGER trg_pois_updated BEFORE UPDATE ON pois
    FOR EACH ROW
    WHEN ((to_jsonb(OLD) - ARRAY['last_checked_at', 'source_state', 'source_state_since'])
              IS DISTINCT FROM
          (to_jsonb(NEW) - ARRAY['last_checked_at', 'source_state', 'source_state_since'])
          OR (OLD.last_checked_at, OLD.source_state, OLD.source_state_since)
              IS NOT DISTINCT FROM
             (NEW.last_checked_at, NEW.source_state, NEW.source_state_since))
    EXECUTE FUNCTION set_updated_at();

COMMIT;
