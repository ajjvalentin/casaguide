-- Migration 010 — Traçage des emails de relance d'essai (V2-18a, volet 2)
--
-- Les emails de relance J-7 et J-2 (`ops/send_trial_reminders.py`, timer systemd
-- quotidien) doivent être IDEMPOTENTS : une double exécution du job (ou deux
-- passages le même jour) ne renvoie jamais deux fois la même relance. On stampe
-- donc l'envoi dans une colonne dédiée par fenêtre (7 j / 2 j) sur l'abonnement.
--
-- Une relance n'est envoyée que si `status='trialing'`, `trial_ends_at` est dans
-- la fenêtre (≤ N jours restants) ET la colonne correspondante est NULL ; on la
-- stampe seulement APRÈS un envoi réussi (un échec SMTP → réessai le lendemain,
-- best-effort, leçon V2-16). Sortir de l'essai (paiement) purge `trial_ends_at`
-- → l'abonnement quitte naturellement la sélection.
--
-- Idempotent (IF NOT EXISTS), sûr au rejeu : les colonnes restent NULL sur les
-- comptes existants (jamais expédié rétroactivement).

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS reminder_7d_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reminder_2d_sent_at TIMESTAMPTZ;
