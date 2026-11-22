-- V2-92 : journal daté des modifications du contenu des guides + retour arrière.
--
-- Le journal est écrit par un DÉCLENCHEUR sur `pois`, pas par le code applicatif :
-- une quinzaine de chemins écrivent cette table (recensés en V2-91), le déclencheur
-- garantit par construction qu'aucun n'échappe. L'auteur, le lot et le job sont
-- transmis par des variables de TRANSACTION (`set_config(…, true)`, jamais de
-- session — une valeur de session fuirait d'une requête à la suivante) :
--   holaguia.actor      system (défaut) | judge | owner | support
--   holaguia.batch_id   une exécution = un lot, annulable d'un bloc
--   holaguia.job_id     job d'enrichissement
--   holaguia.reverts_id posé par enrich/journal.py le temps d'une annulation
-- Posées par `enrich.journal` (declare / acting / bind).
--
-- Ne journalise que ce qui touche le guide visible (fiches retenues) : une fiche
-- `suggested` rafraîchie à chaque moisson n'écrit RIEN. Pas de reprise de l'existant :
-- le journal commence à cette migration. Idempotente (IF NOT EXISTS / OR REPLACE /
-- DROP TRIGGER IF EXISTS).

BEGIN;

CREATE TABLE IF NOT EXISTS content_changes (
    id          BIGSERIAL PRIMARY KEY,
    -- Suppression des données d'un logement = suppression de son journal.
    property_id UUID REFERENCES properties(id) ON DELETE CASCADE,
    entity      TEXT NOT NULL DEFAULT 'poi'
                CHECK (entity IN ('poi', 'area_fact')),   -- area_fact : V2-93
    entity_id   UUID NOT NULL,                  -- SANS FK : survit à la suppression de la fiche
    action      TEXT NOT NULL
                CHECK (action IN ('create', 'update', 'status', 'delete', 'revert')),
    field       TEXT,                           -- colonne (update, status, revert d'un champ)
    -- Valeur avant / après. create/delete : instantané complet de la fiche ;
    -- status : {status, status_by, status_at} ; geom : {"lat":…, "lon":…}.
    old_value   JSONB,
    new_value   JSONB,
    actor       TEXT NOT NULL
                CHECK (actor IN ('system', 'judge', 'owner', 'support')),
    source      TEXT,                           -- repris de la fiche au moment de l'écriture
    source_ref  TEXT,
    job_id      UUID REFERENCES enrichment_jobs(id) ON DELETE SET NULL,
    batch_id    UUID,
    reverts_id  BIGINT REFERENCES content_changes(id),   -- ligne `revert` : la modification annulée
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_content_changes_property ON content_changes(property_id, created_at);
CREATE INDEX IF NOT EXISTS idx_content_changes_entity   ON content_changes(entity_id);
CREATE INDEX IF NOT EXISTS idx_content_changes_batch    ON content_changes(batch_id);
-- Une modification ne s'annule qu'UNE fois (garantie en base, même sous concurrence).
CREATE UNIQUE INDEX IF NOT EXISTS uq_content_changes_reverts
    ON content_changes(reverts_id) WHERE reverts_id IS NOT NULL;

-- Instantané d'une fiche : toutes ses colonnes, `geom` en {"lat","lon"}. Sert au
-- journal (create/delete) ET aux comparaisons du retour arrière (même représentation).
CREATE OR REPLACE FUNCTION journal_poi_snapshot(p pois) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT (to_jsonb(p) - 'geom')
           || jsonb_build_object('geom', CASE WHEN p.geom IS NULL THEN NULL
                   ELSE jsonb_build_object('lat', ST_Y(p.geom), 'lon', ST_X(p.geom)) END)
$$;

CREATE OR REPLACE FUNCTION journal_pois() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- Colonnes suivies = enrich.locks.LOCKABLE_FIELDS (garde par test) ; `status` à part.
    tracked CONSTANT text[] := ARRAY['category_code', 'name', 'address', 'locality',
                                     'phone', 'website', 'opening_hours', 'cuisine',
                                     'weekday', 'weekday_note', 'description_md',
                                     'owner_comment', 'geom'];
    retained CONSTANT text[] := ARRAY['approved', 'edited'];
    v_actor  text   := COALESCE(NULLIF(current_setting('holaguia.actor', true), ''), 'system');
    v_batch  uuid   := NULLIF(current_setting('holaguia.batch_id', true), '')::uuid;
    v_job    uuid   := NULLIF(current_setting('holaguia.job_id', true), '')::uuid;
    v_rev    bigint := NULLIF(current_setting('holaguia.reverts_id', true), '')::bigint;
    o jsonb;
    n jsonb;
    f text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Création : seulement si la fiche naît retenue (lieu saisi à la main), ou
        -- recréation par une annulation de suppression.
        IF v_rev IS NULL AND NOT (NEW.status = ANY (retained)) THEN
            RETURN NULL;
        END IF;
        INSERT INTO content_changes (property_id, entity, entity_id, action, field,
                                     old_value, new_value, actor, source, source_ref,
                                     job_id, batch_id, reverts_id)
        VALUES (NEW.property_id, 'poi', NEW.id,
                CASE WHEN v_rev IS NULL THEN 'create' ELSE 'revert' END, NULL,
                NULL, journal_poi_snapshot(NEW), v_actor, NEW.source, NEW.source_ref,
                v_job, v_batch, v_rev);
        RETURN NULL;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF v_rev IS NULL AND NOT (OLD.status = ANY (retained)) THEN
            RETURN NULL;
        END IF;
        -- Suppression EN CASCADE du logement : son journal part avec lui, on n'en
        -- écrit pas un nouveau (la clé étrangère le refuserait d'ailleurs).
        IF NOT EXISTS (SELECT 1 FROM properties WHERE id = OLD.property_id) THEN
            RETURN NULL;
        END IF;
        INSERT INTO content_changes (property_id, entity, entity_id, action, field,
                                     old_value, new_value, actor, source, source_ref,
                                     job_id, batch_id, reverts_id)
        VALUES (OLD.property_id, 'poi', OLD.id,
                CASE WHEN v_rev IS NULL THEN 'delete' ELSE 'revert' END, NULL,
                journal_poi_snapshot(OLD), NULL, v_actor, OLD.source, OLD.source_ref,
                v_job, v_batch, v_rev);
        RETURN NULL;
    END IF;

    -- UPDATE — changement de statut : toujours.
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO content_changes (property_id, entity, entity_id, action, field,
                                     old_value, new_value, actor, source, source_ref,
                                     job_id, batch_id, reverts_id)
        VALUES (NEW.property_id, 'poi', NEW.id,
                CASE WHEN v_rev IS NULL THEN 'status' ELSE 'revert' END, 'status',
                jsonb_build_object('status', OLD.status, 'status_by', OLD.status_by,
                                   'status_at', OLD.status_at),
                jsonb_build_object('status', NEW.status, 'status_by', NEW.status_by,
                                   'status_at', NEW.status_at),
                v_actor, NEW.source, NEW.source_ref, v_job, v_batch, v_rev);
    END IF;

    -- Champs suivis : seulement si la fiche est retenue avant OU après l'écriture
    -- (une annulation est toujours tracée), une ligne par champ qui change vraiment.
    IF v_rev IS NULL AND NOT (OLD.status = ANY (retained))
                     AND NOT (NEW.status = ANY (retained)) THEN
        RETURN NULL;
    END IF;
    o := journal_poi_snapshot(OLD);
    n := journal_poi_snapshot(NEW);
    FOREACH f IN ARRAY tracked LOOP
        IF (o -> f) IS DISTINCT FROM (n -> f) THEN
            INSERT INTO content_changes (property_id, entity, entity_id, action, field,
                                         old_value, new_value, actor, source, source_ref,
                                         job_id, batch_id, reverts_id)
            VALUES (NEW.property_id, 'poi', NEW.id,
                    CASE WHEN v_rev IS NULL THEN 'update' ELSE 'revert' END, f,
                    o -> f, n -> f, v_actor, NEW.source, NEW.source_ref,
                    v_job, v_batch, v_rev);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_pois_journal ON pois;
CREATE TRIGGER trg_pois_journal
    AFTER INSERT OR UPDATE OR DELETE ON pois
    FOR EACH ROW EXECUTE FUNCTION journal_pois();

COMMIT;
