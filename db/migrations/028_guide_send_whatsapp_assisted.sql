-- Migration 028 — WhatsApp assisté : nouvelle origine d'envoi du guide (V2-32 volet 1)
--
-- Le J-7 assisté WhatsApp (V2-32) présente au propriétaire les guides prêts à
-- envoyer par WhatsApp — un appui par locataire. « Marquer envoyé » pose une ligne
-- `guide_sends` d'origine 'whatsapp_assisted' (wa.me n'offre aucune confirmation
-- technique : le geste est déclaratif). Le REGISTRE reste le verrou d'idempotence
-- (invariant 18, note V2-32) : une ligne kind='stay', quelle que soit son origine,
-- empêche tout autre envoi (l'email automatique du lendemain compris).
--
-- La contrainte de domaine de `origin` (posée en 025 : 'manual' | 'auto') doit
-- désormais accueillir 'whatsapp_assisted'. On la DROP puis re-ADD (idempotent).
-- Aucun backfill : les lignes existantes sont toutes 'manual'/'auto', déjà valides
-- sous la nouvelle contrainte (élargissement pur du domaine).
--
-- Idempotente (DROP IF EXISTS + re-ADD conditionnel), sûre au rejeu. Testée contre
-- l'état antérieur réel (base portant la contrainte 'manual'/'auto' de la 025).

ALTER TABLE guide_sends DROP CONSTRAINT IF EXISTS guide_sends_origin_check;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'guide_sends_origin_check'
  ) THEN
    ALTER TABLE guide_sends
      ADD CONSTRAINT guide_sends_origin_check
      CHECK (origin IN ('manual', 'auto', 'whatsapp_assisted'));
  END IF;
END $$;
