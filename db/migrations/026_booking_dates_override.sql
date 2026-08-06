-- Migration 026 — Dates ajustées à la main protégées de la synchro (V2-23g)
--
-- Constat terrain (mordu deux fois par André en 24 h, cas Tracy Russel) : les
-- dates d'un séjour importé « appartiennent au flux » (l'upsert de synchro ne
-- rafraîchit QUE les dates — invariant 13). Une arrivée avancée à la main au
-- 07.08 revenait donc au 08.08 à la synchro suivante, silencieusement, toutes
-- les 4 h. Les HEURES ont déjà leur grammaire « (ajustée) » ; les DATES doivent
-- l'avoir aussi.
--
-- Trois colonnes, aucune n'existant avant cette migration :
--
--   1. bookings.dates_overridden — le propriétaire a pris possession des dates de
--      CE séjour importé. Tant qu'il vaut TRUE, la synchro ne rafraîchit plus
--      starts_on/ends_on (mais continue de mémoriser ce que le flux annonce, cf.
--      2/3, pour le SIGNAL DE DIVERGENCE). DEFAULT FALSE = comportement actuel à
--      l'identique → aucun backfill (les séjours existants restent pilotés par le
--      flux tant que personne n'a touché leurs dates).
--
--   2/3. bookings.feed_starts_on / feed_ends_on — les DERNIÈRES dates vues dans le
--      flux, mémorisées à CHAQUE synchro (que le marqueur soit posé ou non). On ne
--      peut pas les recalculer au rendu : le rendu (GET /calendar) n'a aucun accès
--      réseau au flux (l'événement iCal n'existe qu'au moment de la synchro). Il
--      faut donc les stocker pour comparer « dates saisies » vs « dates du flux »
--      et n'afficher le signal que si le flux DIVERGE réellement. NULL tant qu'une
--      synchro n'a pas eu lieu après cette migration (pas de divergence signalée
--      sans date de flux mémorisée — repli propre, zéro backfill).
--
-- Idempotente (IF NOT EXISTS), sûre au rejeu, testée contre l'état antérieur réel
-- (base SANS ces colonnes) : les DEFAULT s'appliquent aux lignes existantes à
-- l'ajout de la colonne. Aucun changement de contrainte CHECK ici.

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS dates_overridden BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS feed_starts_on   DATE,
    ADD COLUMN IF NOT EXISTS feed_ends_on     DATE;
