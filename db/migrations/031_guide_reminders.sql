-- V2-36 pièce 1 — registre des relances du planificateur d'envoi (idempotence).
--
-- La relance « langue non précisée » (comme, demain, tout autre motif de relance du
-- moteur d'envoi quotidien) doit partir UNE FOIS par séjour et par motif — jamais
-- une par jour. On la journalise donc dans un registre, sur le MÊME modèle de verrou
-- que `guide_sends` (V2-23d) : une ligne = la relance a déjà été émise. La pastille
-- calendrier, elle, reste calculée AU RENDU (état, pas événement — `care.missing_info`)
-- et n'a pas besoin de ce registre : elle disparaît d'elle-même quand la donnée est
-- complétée ou le guide envoyé.
--
-- Idempotente (IF NOT EXISTS) ; aucun backfill : un registre vide = personne encore
-- relancé, l'état réel au 17/08.
CREATE TABLE IF NOT EXISTS guide_reminders (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    booking_id  UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,            -- motif de relance (ex. 'lang_missing')
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (booking_id, code)             -- « une relance par séjour et par motif »
);
