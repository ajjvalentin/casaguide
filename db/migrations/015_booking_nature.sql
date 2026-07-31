-- Migration 015 — Nature du séjour, bagages, rattachement de bloc miroir (V2-23b, volet 0)
--
-- Constat terrain (31/07) : le champ `bookings.status` portait DEUX choses à la
-- fois — le cycle de vie (annulé ou non) ET la sémantique (réservation / bloc). On
-- les sépare. La bonne question opérationnelle n'est pas « est-ce loué » mais
-- « est-ce occupé » : l'axe commercial et l'axe préparation sont indépendants et ne
-- doivent pas être portés par le même champ.
--
--   bookings.nature — la SÉMANTIQUE du séjour (5 valeurs). C'est elle qui pilote la
--     préparation par l'équipe (invariant maison : « la nature pilote la
--     préparation, jamais le statut »). « occupé » = nature IN
--     ('reservation','private') : quelqu'un dort ici → il y a une préparation.
--
--   bookings.status — désormais un CYCLE DE VIE pur : 'active' | 'cancelled'.
--     La synchro iCal passe un séjour disparu en 'cancelled' (conservé, jamais
--     supprimé) et le réactive s'il réapparaît.
--
--   bookings.luggage_drop_time / luggage_until_time — dépôt de bagages avant
--     l'entrée / consigne après le départ (affichés seulement si renseignés).
--
--   bookings.linked_booking_id — rattachement d'un « bloc miroir » : une location
--     directe hors plateforme est ensuite bloquée sur Abritel pour éviter la double
--     vente → deux lignes pour un même séjour. Le bloc importé est RATTACHÉ au
--     séjour direct (jamais supprimé : la synchro le recréerait) et disparaît de la
--     liste et du planning (une seule arrivée pour l'équipe).
--
-- Le moteur de synchronisation ne doit JAMAIS écraser une nature saisie à la main
-- (prolongement de l'invariant 13). Idempotent (IF NOT EXISTS / gardes) et sûr au
-- rejeu.

-- ── 1. Nature (sémantique) ───────────────────────────────────────────────────
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS nature TEXT NOT NULL DEFAULT 'unqualified'
        CHECK (nature IN ('reservation', 'private', 'works', 'unavailable', 'unqualified'));

-- ── 2. Backfill de la nature depuis l'ancien statut ──────────────────────────
-- Ne s'exécute QUE tant que les valeurs héritées ('confirmed'/'blocked') sont
-- encore présentes : après l'étape 3 le statut vaut 'active'/'cancelled', ces
-- UPDATE ne matchent plus rien → rejeu sans effet, jamais d'écrasement d'une
-- nature saisie à la main (celle-ci arrive forcément APRÈS la migration).
UPDATE bookings SET nature = 'reservation' WHERE status = 'confirmed';
UPDATE bookings SET nature = 'unqualified' WHERE status = 'blocked';

-- ── 3. Statut ramené à un cycle de vie pur ───────────────────────────────────
-- ORDRE CRITIQUE : la contrainte héritée de la 014 n'autorise que
-- 'confirmed'|'blocked'|'cancelled'. Elle doit être retirée AVANT de migrer les
-- valeurs, sinon l'UPDATE est refusé par Postgres sur une base existante (constat
-- prod 31/07 : la migration échouait systématiquement ici ; la suite de tests ne
-- l'avait pas vu car elle construit sa base depuis schema.sql, où la contrainte
-- est déjà la nouvelle — une migration se teste contre l'ÉTAT ANTÉRIEUR réel).
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_status_check;
UPDATE bookings SET status = 'active' WHERE status IN ('confirmed', 'blocked');
ALTER TABLE bookings ADD CONSTRAINT bookings_status_check
    CHECK (status IN ('active', 'cancelled'));
ALTER TABLE bookings ALTER COLUMN status SET DEFAULT 'active';

-- ── 4. Bagages & rattachement ────────────────────────────────────────────────
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS luggage_drop_time  TIME,   -- dépôt de bagages avant l'entrée
    ADD COLUMN IF NOT EXISTS luggage_until_time TIME,   -- consigne de bagages après le départ
    ADD COLUMN IF NOT EXISTS linked_booking_id  UUID REFERENCES bookings(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_bookings_linked ON bookings(linked_booking_id)
    WHERE linked_booking_id IS NOT NULL;
