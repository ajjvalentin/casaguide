-- Migration 005 — Jetons d'authentification transactionnels (V2-08)
-- Table à usage unique et durée limitée servant à la fois à la réinitialisation
-- de mot de passe (purpose='reset') et à la vérification d'email (purpose='verify').
--
-- Le jeton n'est JAMAIS stocké en clair : seule son empreinte SHA-256 (hex) est
-- en base (token_hash). Idempotent (IF NOT EXISTS) → rejouable à chaque déploiement.

CREATE TABLE IF NOT EXISTS password_resets (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id    UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,                  -- SHA-256 hex du jeton brut
    purpose     TEXT NOT NULL DEFAULT 'reset',  -- 'reset' | 'verify'
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,                    -- NULL tant que non consommé
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recherche par empreinte (consommation d'un lien) — unicité de l'empreinte.
CREATE UNIQUE INDEX IF NOT EXISTS uq_password_resets_token_hash
    ON password_resets (token_hash);

-- Cadence par compte (« pas plus d'une demande / 2 min ») et purge éventuelle.
CREATE INDEX IF NOT EXISTS ix_password_resets_owner_created
    ON password_resets (owner_id, purpose, created_at DESC);
