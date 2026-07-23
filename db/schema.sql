-- ============================================================================
-- CasaGuide — Schéma PostgreSQL (v1.0)
-- Aligné sur le cahier des charges v1.0 (§4, §5, §7, §8)
-- Prérequis : PostgreSQL 15+, extensions postgis et pgcrypto
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;      -- géométrie, distances, index spatiaux
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- chiffrement des données sensibles
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- génération d'UUID

-- ============================================================================
-- 1. COMPTES ET ABONNEMENTS (multi-tenant)
-- ============================================================================

CREATE TABLE owners (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT,                          -- NULL si OAuth uniquement
    oauth_provider  TEXT,                          -- 'google' | NULL
    oauth_sub       TEXT,                          -- identifiant OAuth
    full_name       TEXT NOT NULL,
    company_name    TEXT,                          -- conciergeries
    phone           TEXT,
    locale          TEXT NOT NULL DEFAULT 'fr',    -- langue du back-office
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plans (
    id              TEXT PRIMARY KEY,              -- 'free' | 'solo' | 'pro'
    name            TEXT NOT NULL,
    max_properties  INT,                           -- NULL = illimité
    enrich_quota    INT NOT NULL,                  -- enrichissements IA / mois / logement
    price_month_cts INT NOT NULL,                  -- prix en centimes
    features        JSONB NOT NULL DEFAULT '{}',   -- flags : multilingue, stats, marque blanche…
    stripe_price_id TEXT                            -- Price Stripe synchronisé (V2-05b, NULL pour 'free')
);

CREATE TABLE subscriptions (
    id                     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id               UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    plan_id                TEXT NOT NULL REFERENCES plans(id),
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    status                 TEXT NOT NULL DEFAULT 'trialing',
                           -- trialing | active | past_due | canceled
    current_period_end     TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_subscriptions_owner ON subscriptions(owner_id);

-- Journal d'idempotence des webhooks Stripe (V2-05b) : un event.id déjà présent
-- est ignoré silencieusement (Stripe rejoue ses événements). Le webhook est la
-- seule source de vérité du status/plan_id/current_period_end des abonnements.
CREATE TABLE stripe_events (
    id           TEXT PRIMARY KEY,                   -- event.id Stripe (evt_...)
    type         TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- ============================================================================
-- 2. LOGEMENTS
-- ============================================================================

CREATE TABLE properties (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id         UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,                -- nom interne ("Villa Mar Azul")
    -- Adresse structurée
    address_line1    TEXT NOT NULL,
    address_line2    TEXT,
    postal_code      TEXT,
    city             TEXT NOT NULL,
    region           TEXT,
    country_code     CHAR(2) NOT NULL,             -- ISO 3166-1 ('ES', 'FR'…)
    -- Géolocalisation (source du calcul des distances)
    geom             GEOMETRY(Point, 4326),        -- rempli par le géocodage
    geocode_source   TEXT,                         -- 'nominatim' | 'google' | 'manual'
    geocode_accuracy TEXT,                         -- 'rooftop' | 'street' | 'city'
    -- Publication
    guide_token      TEXT NOT NULL UNIQUE
                     DEFAULT encode(gen_random_bytes(16), 'hex'), -- lien secret 128 bits
    -- Second lien secret, distinct du lien voyageur : cahier de préparation de
    -- l'équipe d'entretien (§M-13). Même mécanique que guide_token (128 bits).
    staff_token      TEXT NOT NULL UNIQUE
                     DEFAULT encode(gen_random_bytes(16), 'hex'),
    access_mode      TEXT NOT NULL DEFAULT 'link',
                     -- 'link' (MVP) | 'pin' | 'stay_dates' (V2)
    access_pin_hash  TEXT,                         -- si access_mode = 'pin'
    status           TEXT NOT NULL DEFAULT 'draft',-- draft | published | archived
    default_lang     TEXT NOT NULL DEFAULT 'fr',   -- langue source du contenu
    published_langs  TEXT[] NOT NULL DEFAULT '{}', -- langues traduites publiées
    -- Contact voyageur (§4.D)
    contact_name     TEXT,
    contact_phone    TEXT,
    contact_whatsapp TEXT,
    contact_email    TEXT,
    contact_backup   TEXT,                         -- contact de secours (voisin…)
    -- Réglementaire (§4.I)
    tourism_license  TEXT,                         -- n° VT/VUT en Espagne
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_properties_owner ON properties(owner_id);
CREATE INDEX idx_properties_geom  ON properties USING GIST(geom);

-- Données sensibles chiffrées au niveau applicatif (§8) : le backend chiffre
-- (AES-GCM, clé hors base) avant insertion ; la base ne voit que du bytea.
CREATE TABLE property_secrets (
    property_id  UUID PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
    wifi_ssid    TEXT,                             -- legacy : miroir du réseau n°1 (M-15)
    wifi_pass_enc BYTEA,                           -- legacy : chiffré, miroir du réseau n°1
    wifi_networks_enc BYTEA,                       -- liste [{label,ssid,pass}] chiffrée (M-15)
    keybox_code_enc BYTEA,                         -- chiffré
    keybox_notes TEXT,                             -- emplacement, instructions
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Co-gestionnaires (V2) — prévu dès maintenant pour éviter une migration lourde
CREATE TABLE property_members (
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    owner_id    UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'editor',    -- editor | viewer
    PRIMARY KEY (property_id, owner_id)
);

-- ============================================================================
-- 3. STRUCTURE DU GUIDE — sections pré-définies (§4)
-- ============================================================================

-- Catalogue des sections (la checklist du cahier des charges).
-- Alimenté par un seed : A_checkin, A_checkout, A_keybox, B_wifi, C_trash,
-- D_emergency, D_hospital, E_taxi, E_food_delivery, F_restaurants, G_market…
CREATE TABLE section_templates (
    code         TEXT PRIMARY KEY,                 -- 'A_checkin', 'C_trash'…
    chapter      TEXT NOT NULL,                    -- 'A'..'I' (§4 du CdC)
    sort_order   INT  NOT NULL,
    icon         TEXT,
    name_i18n    JSONB NOT NULL,                   -- {"fr": "Arrivée", "en": "Check-in"…}
    description_i18n JSONB NOT NULL DEFAULT '{}',  -- aide à la saisie pour le propriétaire
    field_schema JSONB NOT NULL DEFAULT '{}',      -- schéma des champs structurés attendus
    ai_enrichable BOOLEAN NOT NULL DEFAULT FALSE,  -- section pré-remplissable par l'IA
    is_sensitive  BOOLEAN NOT NULL DEFAULT FALSE,  -- masquée tant que non authentifié (V2)
    -- Public cible de la section (M-13) : le guide voyageur ('guest') ou le
    -- cahier de préparation de l'équipe d'entretien ('staff'). Une section 'staff'
    -- ne sort JAMAIS sur /g ; une section 'guest' ne sort JAMAIS sur /s.
    audience      TEXT NOT NULL DEFAULT 'guest'    -- 'guest' | 'staff'
);

-- Instance d'une section pour un logement donné
CREATE TABLE property_sections (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id   UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    template_code TEXT REFERENCES section_templates(code),
                  -- NULL = section personnalisée créée par le propriétaire
    custom_title  TEXT,                            -- pour les sections personnalisées
    sort_order    INT NOT NULL DEFAULT 0,
    is_visible    BOOLEAN NOT NULL DEFAULT TRUE,   -- masquer une section non pertinente
    -- Contenu dans la langue source du logement :
    content       JSONB NOT NULL DEFAULT '{}',     -- champs structurés (selon field_schema)
    body_md       TEXT,                            -- texte libre (Markdown)
    completed     BOOLEAN NOT NULL DEFAULT FALSE,  -- pour l'indicateur de complétude
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (property_id, template_code)
);
CREATE INDEX idx_psections_property ON property_sections(property_id);

-- Traductions stockées (§9) — régénérées à chaque modification de la source
CREATE TABLE section_translations (
    section_id  UUID NOT NULL REFERENCES property_sections(id) ON DELETE CASCADE,
    lang        TEXT NOT NULL,                     -- 'en', 'es', 'de', 'nl'…
    content     JSONB NOT NULL DEFAULT '{}',
    body_md     TEXT,
    is_stale    BOOLEAN NOT NULL DEFAULT FALSE,    -- source modifiée depuis la traduction
    reviewed_by_owner BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (section_id, lang)
);

-- Photos et documents (façade, boîte à clés, notices PDF…)
CREATE TABLE media (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    section_id  UUID REFERENCES property_sections(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'photo',     -- photo | pdf
    storage_key TEXT NOT NULL,                     -- clé S3
    caption     TEXT,
    sort_order  INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_media_property ON media(property_id);

-- ============================================================================
-- 4. POINTS D'INTÉRÊT (POI) — enrichissement et carte (§5, §6)
-- ============================================================================

CREATE TABLE poi_categories (
    code       TEXT PRIMARY KEY,     -- 'hospital','pharmacy','police','supermarket',
                                     -- 'market','mall','restaurant','bar','beach',
                                     -- 'taxi','babysitter','food_delivery','bus_stop',
                                     -- 'atm','bakery','laundry','activity','airport'…
    chapter    TEXT NOT NULL,        -- rattachement au chapitre du guide ('C','D','E','F','G','H')
    name_i18n  JSONB NOT NULL,
    icon       TEXT,
    map_color  TEXT,
    default_radius_m INT NOT NULL    -- rayon de recherche par défaut (pharmacie 2000, hôpital 25000…)
);

CREATE TABLE pois (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id    UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    category_code  TEXT NOT NULL REFERENCES poi_categories(code),
    name           TEXT NOT NULL,
    geom           GEOMETRY(Point, 4326) NOT NULL,
    address        TEXT,
    phone          TEXT,
    website        TEXT,
    opening_hours  TEXT,                           -- format OSM ou texte libre
    price_level    SMALLINT,                       -- 1-4, optionnel
    cuisine        TEXT,                            -- type de cuisine (tag OSM normalisé, M-16)
    description_md TEXT,                           -- rédigé par l'IA ou le propriétaire
    owner_comment  TEXT,                           -- le "coup de cœur" personnel (§4.F)
    -- Distances pré-calculées (cache — aucun appel API côté voyageur)
    dist_walk_m    INT, walk_min  INT,
    dist_drive_m   INT, drive_min INT,
    -- Traçabilité (§5.2)
    source         TEXT NOT NULL,                  -- 'osm' | 'google' | 'claude' | 'owner'
    source_ref     TEXT,                           -- osm node id, google place id…
    fetched_at     TIMESTAMPTZ,
    -- Workflow de validation (§5.1 étape 5)
    status         TEXT NOT NULL DEFAULT 'suggested',
                   -- suggested | approved | edited | rejected
    sort_order     INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_pois_property        ON pois(property_id);
CREATE INDEX idx_pois_property_status ON pois(property_id, status);
CREATE INDEX idx_pois_geom            ON pois USING GIST(geom);

-- Traductions des descriptions de POI
CREATE TABLE poi_translations (
    poi_id     UUID NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    description_md TEXT,
    owner_comment  TEXT,
    is_stale   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (poi_id, lang)
);

-- Données pays/commune générées par l'IA (numéros d'urgence, règles de tri,
-- réglementation bruit) — mutualisables entre logements d'une même zone
CREATE TABLE area_facts (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_code CHAR(2) NOT NULL,
    admin_area   TEXT,                             -- commune / province ; NULL = national
    fact_type    TEXT NOT NULL,                    -- 'emergency_numbers' | 'waste_rules' | 'noise_rules'
    content      JSONB NOT NULL,
    source       TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (country_code, admin_area, fact_type)
);

-- ============================================================================
-- 5. PIPELINE D'ENRICHISSEMENT — suivi et coûts (§5)
-- ============================================================================

CREATE TABLE enrichment_jobs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id  UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    trigger      TEXT NOT NULL DEFAULT 'manual',   -- manual | initial | refresh
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    steps        JSONB NOT NULL DEFAULT '{}',      -- état par étape (geocode, overpass, claude, osrm)
    error        TEXT,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_enrich_property ON enrichment_jobs(property_id);

-- Comptabilité des coûts API par logement (pilotage de la marge, §5.2)
CREATE TABLE api_costs (
    id          BIGSERIAL PRIMARY KEY,
    property_id UUID REFERENCES properties(id) ON DELETE SET NULL,
    job_id      UUID REFERENCES enrichment_jobs(id) ON DELETE SET NULL,
    provider    TEXT NOT NULL,                     -- 'anthropic' | 'google' | 'deepl'…
    operation   TEXT NOT NULL,                     -- 'enrich' | 'translate' | 'geocode'
    units       INT NOT NULL DEFAULT 1,            -- tokens, requêtes…
    cost_cts    NUMERIC(10,4) NOT NULL DEFAULT 0,  -- coût en centimes
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 6. CONSULTATION VOYAGEUR — statistiques anonymes (§3.1, §8 RGPD)
-- ============================================================================

CREATE TABLE guide_views (
    id           BIGSERIAL PRIMARY KEY,
    property_id  UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    section_code TEXT,                             -- NULL = page d'accueil / carte
    lang         TEXT,
    visitor_hash TEXT,                             -- hash journalier anonymisé (pas d'IP stockée)
    viewed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_views_property_date ON guide_views(property_id, viewed_at);

-- Signalement d'information obsolète par un voyageur (V2)
CREATE TABLE issue_reports (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    section_id  UUID REFERENCES property_sections(id) ON DELETE SET NULL,
    poi_id      UUID REFERENCES pois(id) ON DELETE SET NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',      -- open | resolved | dismissed
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 7. UTILITAIRES
-- ============================================================================

-- Mise à jour automatique de updated_at
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['owners','subscriptions','properties','property_sections','pois']
  LOOP
    EXECUTE format(
      'CREATE TRIGGER trg_%s_updated BEFORE UPDATE ON %I
       FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t, t);
  END LOOP;
END $$;

-- Exemple de requête clé (guide voyageur) : POI approuvés triés par distance
-- SELECT p.*, ST_Distance(p.geom::geography, pr.geom::geography) AS dist_m
-- FROM pois p JOIN properties pr ON pr.id = p.property_id
-- WHERE p.property_id = :pid AND p.status IN ('approved','edited')
-- ORDER BY p.category_code, dist_m;
