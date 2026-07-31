-- Migration 016 — Nombre de voyageurs par séjour (V2-23b, volet 1, §1.0)
--
-- La checklist d'équipe n'est pas une liste, c'est une liste QUANTIFIÉE :
-- préparer draps et welcome pack pour 8 personnes n'a rien à voir avec 2. Le flux
-- iCal ne transporte pas le nombre de voyageurs → il est renseigné à la main par
-- le propriétaire (comme les coordonnées), et un séjour sans `guest_count` rejoint
-- la relance active du §0.6 (signalé dans le calendrier).
--
--   bookings.guest_count   — total de voyageurs, enfants compris. NULL = non
--     renseigné (le moteur d'interventions prend alors l'hypothèse PESSIMISTE :
--     pleine occupation — jamais un trou silencieux).
--
--   bookings.children_ages — âges À L'ARRIVÉE des enfants, ex. '{1,3,14}'. Le
--     libellé « âge à l'arrivée » évite toute mécanique de date de naissance
--     (une résa posée 8 mois à l'avance verrait sinon « 1 an » devenir faux). Le
--     NOMBRE d'enfants se déduit de la longueur du tableau : pas de colonne
--     redondante, pas d'incohérence possible. Les tranches d'âge et l'équipement
--     qu'elles SUGGÈRENT vivent dans properties.care_rules (invariant 8), jamais
--     en dur — voir migration 017.
--
-- RGPD : les âges d'enfants suivent le régime des coordonnées — visibles par
-- l'équipe pour les séjours en cours et à venir uniquement (V2-25).
--
-- Idempotent (IF NOT EXISTS), sûr au rejeu.

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS guest_count   INT,     -- total, enfants compris ; NULL = non renseigné
    ADD COLUMN IF NOT EXISTS children_ages INT[];   -- âges À L'ARRIVÉE, ex. '{1,3,14}'
