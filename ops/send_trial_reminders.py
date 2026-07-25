#!/usr/bin/env python3
"""Relances de fin d'essai J-7 / J-2 (V2-18a, volet 2).

Sélectionne les essais (`status='trialing'`) dont l'échéance `trial_ends_at`
tombe dans ≤ 7 jours ou ≤ 2 jours et qui n'ont pas encore reçu la relance de
cette fenêtre, puis envoie un email sobre (gabarit `api.emails.trial_reminder_email`)
via le Mailer configuré (SMTP Infomaniak ou repli ConsoleMailer en dev).

Idempotent : chaque fenêtre est stampée (`subscriptions.reminder_7d_sent_at` /
`reminder_2d_sent_at`) **après un envoi réussi** — deux exécutions le même jour
ne renvoient jamais deux fois la même relance. Un échec SMTP est best-effort
(journalisé, non stampé → réessai le lendemain) et ne bloque JAMAIS le reste
(leçon V2-16). L'expiration des droits ne dépend pas de ces emails : elle est
calculée à la lecture (invariant V2-18a).

Lancement (timer systemd quotidien, ou à la main sur le serveur) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/send_trial_reminders.py

Charge `backend/.env` lui-même (exécution hors EnvironmentFile systemd, OPS-1) ;
DSN dans `CASAGUIDE_DB` (défaut socket local). `--dry-run` n'envoie ni ne stampe.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))          # ops/ (opsenv)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # api.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.trial_reminders")

# Fenêtres de relance (jours restants avant échéance) — ordre décroissant.
WINDOWS = (7, 2)


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def run_reminders(conn, mailer, *, base_url: str, windows=WINDOWS,
                  now=None, dry_run: bool = False) -> dict[int, int]:
    """Cœur testable : pour chaque fenêtre, sélectionne les essais dus, envoie la
    relance (best-effort) et stampe l'envoi réussi. Renvoie {fenêtre: nb_envoyés}.

    Un échec d'envoi est avalé (journalisé) et n'est PAS stampé (réessai plus
    tard). `now` injectable (tests). `dry_run` : n'envoie ni ne stampe."""
    from api import emails, repo  # import tardif : env chargé avant api.config

    dashboard_url = f"{base_url.rstrip('/')}/#/abonnement"
    sent: dict[int, int] = {}
    for days in windows:
        rows = repo.trials_due_for_reminder(conn, days, now=now)
        count = 0
        for row in rows:
            if dry_run:
                log.info("[dry-run] relance J-%d → %s", days, row["email"])
                count += 1
                continue
            email = emails.trial_reminder_email(days, dashboard_url,
                                                row.get("full_name"))
            try:
                mailer.send(row["email"], email)
            except Exception:  # noqa: BLE001 — best-effort : jamais bloquant
                log.warning("Relance J-%d vers %s échouée (non stampée, réessai "
                            "au prochain passage).", days, row["email"],
                            exc_info=True)
                continue
            repo.mark_trial_reminder_sent(conn, row["subscription_id"], days,
                                          now=now)
            conn.commit()  # stampe au fil de l'eau : un plantage tardif ne
            #                 re-spamme pas les relances déjà envoyées.
            count += 1
        sent[days] = count
        log.info("Fenêtre J-%d : %d relance(s) envoyée(s).", days, count)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Envoie les relances de fin d'essai J-7 / J-2 (V2-18a).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                             "postgresql:///casaguide).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="liste les relances sans envoyer ni stamper.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)
    elif args.env_file:
        log.warning("⚠ --env-file introuvable : %s (repli environnement).",
                    args.env_file)

    # Imports api APRÈS chargement du .env (api.config lit l'env à l'import).
    from api.config import settings
    from api.deps import build_mailer

    base_url = settings.public_base_url or "https://holaguia.com"
    mailer = build_mailer()

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        try:
            sent = run_reminders(conn, mailer, base_url=base_url,
                                 dry_run=args.dry_run)
        except psycopg.errors.UndefinedColumn:
            log.error("✗ colonnes de relance absentes : appliquer la migration "
                      "010 (deploy.sh) avant de lancer les relances.")
            return 2
    total = sum(sent.values())
    log.info("Terminé : %d relance(s) au total%s.", total,
             " (dry-run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
