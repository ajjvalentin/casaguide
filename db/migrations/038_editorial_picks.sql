-- V2-56c — L'échantillonnage éditorial devient CUMULATIF : mémoire de secteur.
--
-- Constat : deux runs, deux jurys (Casa Manolo au 1er, absent du 2e ; Brown's dans
-- aucun) — la découverte web ÉCHANTILLONNE, elle n'inventorie pas. Cette table
-- MÉMORISE, par secteur (pays+commune), les picks éditoriaux « sorties » déjà
-- POSITIONNÉS (V2-56b : appariés/géocodés proprement). Chaque run upsert ses élus ;
-- la fusion consomme l'UNION des mémorisés récents (< 90 j) et des frais → la
-- connaissance du secteur s'accumule, chaque guide vendu enrichit le suivant.
--
-- `name_norm`/`city_norm` sont normalisés côté application (enrich.dedup._norm) et
-- portent la clé d'unicité (idempotence de l'upsert par secteur+catégorie+nom).
-- Idempotente. Aucun backfill (mémoire vierge, se remplit aux runs suivants).
CREATE TABLE IF NOT EXISTS editorial_picks (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_code  CHAR(2) NOT NULL,
    city          TEXT NOT NULL,               -- commune telle que saisie (affichage)
    city_norm     TEXT NOT NULL,               -- clé de secteur normalisée
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL,               -- clé de dédup normalisée
    category      TEXT NOT NULL,               -- restaurant | bar | cafe
    reason        TEXT,                         -- la phrase de réputation (langue source)
    source_url    TEXT,
    verified_on   TEXT,
    geom          GEOMETRY(Point, 4326) NOT NULL,  -- position FIABLE (jamais un centroïde)
    phone         TEXT,
    website       TEXT,
    locality      TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_editorial_picks
    ON editorial_picks(country_code, city_norm, category, name_norm);
CREATE INDEX IF NOT EXISTS idx_editorial_picks_sector
    ON editorial_picks(country_code, city_norm, last_seen);
