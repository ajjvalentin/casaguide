-- V2-91 : provenance et verrou PAR CHAMP d'une fiche POI (fondation des mises à jour
-- automatisées, V2-92/93/94).
--
--   human_fields : colonnes dont la valeur COURANTE a été saisie par un humain. Noms
--                  admis = `enrich.locks.LOCKABLE_FIELDS` (éditables + geom).
--   status_by    : qui a posé le statut — 'owner' | 'judge' | NULL (= suggested,
--                  personne n'a statué).
--   status_at    : date de la dernière décision de statut.
--
-- AUCUN comportement d'écriture automatique ne change ici : `upsert_pois` garde sa
-- garde `status = 'suggested'` (invariant 1). V2-93 branchera `system_may_write`.
--
-- Idempotente : ADD COLUMN IF NOT EXISTS ; contraintes DROP → UPDATE → ADD (leçon
-- 015) ; la reprise ne touche QUE les lignes jamais renseignées (status_at NULL ET
-- human_fields vide) — un rejeu après usage ne réécrit aucune provenance posée par le
-- code.

BEGIN;

ALTER TABLE pois ADD COLUMN IF NOT EXISTS human_fields TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE pois ADD COLUMN IF NOT EXISTS status_by    TEXT;
ALTER TABLE pois ADD COLUMN IF NOT EXISTS status_at    TIMESTAMPTZ;

-- DROP (avant toute écriture de valeurs)
ALTER TABLE pois DROP CONSTRAINT IF EXISTS pois_status_by_check;
ALTER TABLE pois DROP CONSTRAINT IF EXISTS pois_human_fields_check;

-- UPDATE — reprise de l'existant (brief V2-91 §2, règle prudente appliquée telle
-- quelle). L'historique ne dit pas QUELS champs d'une fiche `edited` ont été touchés :
-- tous ses champs éditables non NULL sont réputés humains (la fiche reste figée comme
-- aujourd'hui). Le trigger trg_pois_updated réécrirait updated_at : on le neutralise le
-- temps de la reprise pour que status_at = updated_at d'AVANT, et que updated_at reste
-- la date de la dernière modification réelle.
ALTER TABLE pois DISABLE TRIGGER trg_pois_updated;

WITH todo AS (
    SELECT p.id, p.source, p.status, p.updated_at, pr.guest_guide,
           array_remove(ARRAY[
               CASE WHEN p.category_code  IS NOT NULL THEN 'category_code'  END,
               CASE WHEN p.name           IS NOT NULL THEN 'name'           END,
               CASE WHEN p.address        IS NOT NULL THEN 'address'        END,
               CASE WHEN p.locality       IS NOT NULL THEN 'locality'       END,
               CASE WHEN p.phone          IS NOT NULL THEN 'phone'          END,
               CASE WHEN p.website        IS NOT NULL THEN 'website'        END,
               CASE WHEN p.opening_hours  IS NOT NULL THEN 'opening_hours'  END,
               CASE WHEN p.cuisine        IS NOT NULL THEN 'cuisine'        END,
               CASE WHEN p.weekday        IS NOT NULL THEN 'weekday'        END,
               CASE WHEN p.weekday_note   IS NOT NULL THEN 'weekday_note'   END,
               CASE WHEN p.description_md IS NOT NULL THEN 'description_md' END,
               CASE WHEN p.owner_comment  IS NOT NULL THEN 'owner_comment'  END
           ]::text[], NULL) AS filled
    FROM pois p JOIN properties pr ON pr.id = p.property_id
    WHERE p.status_at IS NULL AND p.human_fields = '{}'
)
UPDATE pois SET
    human_fields = CASE
        WHEN todo.source = 'owner'   THEN todo.filled || ARRAY['geom']
        WHEN todo.status = 'edited'  THEN todo.filled
        ELSE '{}'::text[] END,
    -- Une fiche `suggested` n'a pas de statuant, même saisie par le propriétaire
    -- (annulation M-23 vers 'suggested') : NULL = personne n'a statué.
    status_by = CASE
        WHEN todo.status = 'suggested'                 THEN NULL
        WHEN todo.source = 'owner'                     THEN 'owner'
        WHEN todo.status = 'edited'                    THEN 'owner'
        WHEN todo.guest_guide                          THEN 'judge'
        ELSE 'owner' END,
    status_at = CASE WHEN todo.status = 'suggested' THEN NULL
                     ELSE todo.updated_at END
FROM todo
WHERE pois.id = todo.id
  AND (todo.source = 'owner' OR todo.status <> 'suggested');

ALTER TABLE pois ENABLE TRIGGER trg_pois_updated;

-- ADD
ALTER TABLE pois ADD CONSTRAINT pois_status_by_check
    CHECK (status_by IN ('owner', 'judge'));
ALTER TABLE pois ADD CONSTRAINT pois_human_fields_check
    CHECK (human_fields <@ ARRAY['category_code', 'name', 'address', 'locality',
                                 'phone', 'website', 'opening_hours', 'cuisine',
                                 'weekday', 'weekday_note', 'description_md',
                                 'owner_comment', 'geom']::text[]);

COMMIT;
