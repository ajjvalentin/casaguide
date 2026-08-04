-- Migration 022 — Traçage des envois du guide par le backend (V2-23d, volet 1)
--
-- La fenêtre « Envoyer le guide » gagne un canal « Envoyer par Holaguia » :
-- l'email HTML part du serveur (mailer transactionnel V2-08), synchrone, et le
-- propriétaire attend la confirmation. On garde une trace de chaque envoi réussi
-- dans `guide_sends` :
--   · pour AFFICHER le dernier envoi dans la fenêtre (« envoyé le 02/08 à 21h »)
--     et éviter le double envoi involontaire ;
--   · comme MÉMOIRE dont l'automatisation J-7 (volet 2, hors périmètre) aura
--     besoin (« déjà envoyé ? »).
--
-- `booking_id` est NULL pour un envoi de VITRINE (kind='showcase'), renseigné
-- pour un envoi de SÉJOUR (kind='stay'). `recipient` = l'adresse réellement
-- servie. Une ligne n'est écrite qu'APRÈS un envoi SMTP réussi (jamais sur
-- échec) — c'est un journal d'envois effectifs, pas de tentatives.
--
-- Aucun backfill : rien n'est amorcé à la création d'un logement (contrairement
-- à care_rules) — la table naît vide et se remplit au fil des envois.
--
-- Idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Sûr au rejeu.

CREATE TABLE IF NOT EXISTS guide_sends (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    booking_id  UUID REFERENCES bookings(id) ON DELETE SET NULL,  -- NULL pour la vitrine
    kind        TEXT NOT NULL CHECK (kind IN ('stay', 'showcase')),
    lang        TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- « Dernier envoi » par cible (logement / séjour) : lecture triée par date.
CREATE INDEX IF NOT EXISTS idx_guide_sends_property
    ON guide_sends (property_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_guide_sends_booking
    ON guide_sends (booking_id, sent_at DESC);
