-- V2-54 Mission B — Commandes de l'offre « Guide Voyageur » (paiement one-shot Stripe).
--
-- Un vacancier paie 2,90 € (Checkout Stripe, mode `payment`) sans compte. Le WEBHOOK
-- est la SEULE source de vérité (doctrine V2-27) : la génération ne démarre qu'à
-- l'événement de paiement confirmé. Cette table porte l'intention d'achat (adresse/
-- point + e-mail) entre la création du Checkout et le webhook, puis l'état de la
-- livraison. Le `token` sert de clé au parcours sans compte : suivi de livraison,
-- reprise après échec, renvoi du guide par e-mail — jamais le `guide_token` du guide.
--
-- Idempotente (IF NOT EXISTS). Aucun backfill.
CREATE TABLE IF NOT EXISTS guest_guide_orders (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Clé publique du parcours sans compte (suivi/reprise/renvoi). 128 bits hex,
    -- même fabrique que guide_token — ne révèle jamais le guide_token du guide.
    token             TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(16), 'hex'),
    stripe_session_id TEXT UNIQUE,               -- session Checkout (rapproche le webhook)
    email             TEXT NOT NULL,             -- collecté au Checkout (livraison)
    lang              TEXT NOT NULL DEFAULT 'fr', -- langue de l'e-mail de livraison
    ip                TEXT,                       -- anti-abus (fenêtre par IP)
    -- Adresse / point demandés (le point est ajusté sur la carte du tunnel).
    city              TEXT NOT NULL,
    country_code      CHAR(2) NOT NULL,
    address_line1     TEXT,
    postal_code       TEXT,
    region            TEXT,
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    -- Cycle de vie : pending (Checkout ouvert) → paid (webhook) → generating →
    -- done (guide publié + e-mail envoyé) | failed (e-mail de reprise, pas de re-paiement).
    status            TEXT NOT NULL DEFAULT 'pending',
    property_id       UUID REFERENCES properties(id) ON DELETE SET NULL,
    guide_token       TEXT,                       -- token du guide livré (raccourci de lecture)
    error             TEXT,                       -- motif d'échec (diagnostic, jamais au client)
    paid_at           TIMESTAMPTZ,
    delivered_at      TIMESTAMPTZ,                -- dernier envoi (livraison OU renvoi) → cadence anti-abus
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_guest_orders_email ON guest_guide_orders(email, created_at);
