-- Migration 014 — Calendrier des locations (V2-23a)
--
-- Chaque logement gagne un calendrier des séjours : importés des plateformes
-- (Airbnb / Vrbo-Abritel / Booking via un flux iCal) et/ou saisis directement.
-- Ce volet 1 pose le modèle ; l'import iCal (parser, upsert par UID, timer) et la
-- vue « Séjours » s'appuient dessus.
--
--   properties.default_checkin_time / default_checkout_time
--     Heures standard du logement (défauts sobres 15:00 / 10:00, éditables dans
--     la fiche Informations). NULL sur un séjour = « heures standard ».
--
--   property_calendars — les flux iCal du logement (MULTI-flux : un logement peut
--     être en ligne sur Airbnb ET Abritel). `ical_url_enc` est un SECRET (l'URL
--     iCal donne accès au calendrier complet du bien) : chiffrée AES au niveau
--     applicatif, même régime que les mots de passe wifi (invariant 1 de la
--     mission, invariant 5 du CdC). La base ne voit que du bytea.
--
--   bookings — les séjours. Idempotence des imports par (calendar_id,
--     external_uid) : re-synchroniser N fois = zéro doublon. Un événement disparu
--     du flux passe `cancelled` (conservé, jamais supprimé — invariant maison).
--     Les heures/nom/contact/notes saisis à la main ne sont jamais écrasés par une
--     sync. `starts_on`/`ends_on` suivent la sémantique iCal en intervalle
--     semi-ouvert [arrivée, départ) : `ends_on` = jour du départ (= DTEND, exclusif
--     dans le standard iCal pour les événements « journée entière »). Deux séjours
--     qui se touchent (départ = arrivée le même jour) ne se chevauchent donc PAS :
--     c'est une rotation.
--
-- Idempotent (IF NOT EXISTS partout) et sûr au rejeu.

-- ── Heures standard du logement ──────────────────────────────────────────────
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS default_checkin_time  TIME NOT NULL DEFAULT '15:00',
    ADD COLUMN IF NOT EXISTS default_checkout_time TIME NOT NULL DEFAULT '10:00';

-- ── Flux de calendrier (iCal) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS property_calendars (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id      UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    platform         TEXT NOT NULL DEFAULT 'other'
                     CHECK (platform IN ('airbnb', 'vrbo', 'booking', 'other')),
    ical_url_enc     BYTEA NOT NULL,               -- URL iCal chiffrée AES (secret)
    last_sync_at     TIMESTAMPTZ,
    last_sync_status TEXT,                          -- 'ok' | 'error' | NULL (jamais synchro)
    sync_error       TEXT,                          -- message court en cas d'échec
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calendars_property ON property_calendars(property_id);

-- ── Séjours ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bookings (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id   UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    -- Flux d'origine (NULL = saisie directe). ON DELETE SET NULL : supprimer un
    -- flux ne supprime jamais les séjours (ils sont d'abord passés 'cancelled').
    calendar_id   UUID REFERENCES property_calendars(id) ON DELETE SET NULL,
    starts_on     DATE NOT NULL,                    -- jour d'arrivée
    ends_on       DATE NOT NULL,                    -- jour de départ (intervalle [arrivée, départ))
    checkin_time  TIME,                             -- NULL = heure standard du logement
    checkout_time TIME,                             -- NULL = heure standard du logement
    source        TEXT NOT NULL DEFAULT 'direct'
                  CHECK (source IN ('airbnb', 'vrbo', 'booking', 'direct', 'other')),
    external_uid  TEXT,                             -- UID iCal (NULL si saisie directe)
    guest_name    TEXT,
    guest_contact TEXT,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'confirmed'
                  CHECK (status IN ('confirmed', 'blocked', 'cancelled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bookings_property ON bookings(property_id);
CREATE INDEX IF NOT EXISTS idx_bookings_property_dates
    ON bookings(property_id, starts_on, ends_on);

-- Idempotence des imports : un UID iCal est unique PAR flux (deux flux différents
-- peuvent légitimement porter le même UID). Index partiel : les saisies directes
-- (calendar_id NULL ou external_uid NULL) ne sont pas contraintes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_calendar_uid
    ON bookings(calendar_id, external_uid)
    WHERE calendar_id IS NOT NULL AND external_uid IS NOT NULL;
