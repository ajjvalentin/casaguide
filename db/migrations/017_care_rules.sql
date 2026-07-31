-- Migration 017 — Règles d'entretien, catalogue de demandes (V2-23b, volet 1, §1.1/§1.2)
--
-- Le cahier d'équipe répond à trois questions : depuis quand la maison est libre,
-- pour quand elle doit être prête, et QUOI faire. Le « quoi » se calcule (jamais
-- ne se stocke : voir api/care.py) à partir de deux entrées vivant en base :
--
--   properties.care_rules JSONB — les règles d'entretien du logement (jamais en
--     dur — invariant 8) : jour à partir duquel les draps sont changés, ménage en
--     cours de séjour, welcome pack, tranches d'âge des enfants et l'équipement
--     qu'elles SUGGÈRENT, effort de rotation en HOMMES-HEURES. Le propriétaire
--     doit pouvoir tout ajuster → aucune valeur figée dans le code. Le défaut
--     applicatif (api/care.default_care_rules) est posé à la CRÉATION du logement.
--
--   property_request_types — catalogue de demandes particulières par logement
--     (lit bébé, chaise haute, parasol, lit d'appoint…), personnalisable. Amorcé
--     à la création du logement.
--
--   booking_requests — instances de demande rattachées à un séjour, d'origine
--     'owner' (le propriétaire prépare) ou 'guest' (demande du voyageur, volet 3).
--
-- Idempotent (IF NOT EXISTS / gardes), sûr au rejeu.

-- ── 1. Règles d'entretien par logement ───────────────────────────────────────
ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS care_rules JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ── 2. Catalogue de demandes particulières (par logement) ────────────────────
CREATE TABLE IF NOT EXISTS property_request_types (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,                    -- 'lit_bebe', 'chaise_haute'…
    label       TEXT NOT NULL,                    -- libellé affiché (FR)
    sort_order  INT  NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,    -- désactivé ≠ supprimé (garde l'historique)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_request_types_property
    ON property_request_types(property_id);
-- Un même code n'existe qu'une fois par logement (amorçage idempotent).
CREATE UNIQUE INDEX IF NOT EXISTS idx_request_types_property_code
    ON property_request_types(property_id, code);

-- ── 3. Demandes rattachées à un séjour ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS booking_requests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    booking_id      UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    request_type_id UUID REFERENCES property_request_types(id) ON DELETE SET NULL,
    label           TEXT,                          -- copie du libellé (survit à la suppression du type)
    quantity        INT NOT NULL DEFAULT 1,
    note            TEXT,
    origin          TEXT NOT NULL DEFAULT 'owner'
                    CHECK (origin IN ('owner', 'guest')),   -- 'guest' = volet 3
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'declined')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_booking_requests_booking
    ON booking_requests(booking_id);
