#!/usr/bin/env python3
"""Envoi automatique du guide à J-7 (V2-23d, volet 2).

Sept jours avant l'arrivée, le guide du séjour part tout seul par email — l'hôte
le reçoit au petit-déjeuner. Ce script est le mince habillage CLI du cœur testable
`api.guidesend.run_auto_send` (patron `sync_calendars.py` → `calendars.sync_all`).

Sélection PURE (`api.care.select_auto_sends`) : logement `auto_send_guide` vrai et
publié, séjour `reservation`/`private` actif non rattaché, arrivée dans la fenêtre
`[today, today+7]` (rattrapage : un timer en panne deux jours ne saute personne),
et **pas déjà envoyé** — une ligne `guide_sends` kind='stay' (manuelle OU auto) est
le verrou d'idempotence. Un séjour éligible SANS email n'est pas envoyé mais relancé
côté calendrier (pastille `missing_info`, esprit §0.6) — jamais d'email de relance.

La trace `guide_sends` origin='auto' est écrite **APRÈS un envoi réussi** avec un
`conn.commit()` explicite ; un échec SMTP est journalisé, non tracé (ré-essai au
passage du lendemain) et n'arrête jamais la boucle (best-effort, leçon V2-16).

Lancement (timer systemd quotidien 09:00 Europe/Madrid, ou à la main sur le
serveur) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/send_guides.py

Charge `backend/.env` lui-même (exécution hors EnvironmentFile systemd, OPS-1) ;
DSN dans `CASAGUIDE_DB` (défaut socket local). `--dry-run` : plan sans envoi.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))          # ops/ (opsenv)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # api.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.send_guides")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Envoie automatiquement le guide aux séjours à J-7 (V2-23d).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                             "postgresql:///casaguide).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="planifie les envois sans envoyer ni tracer.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)
    elif args.env_file:
        log.warning("⚠ --env-file introuvable : %s (repli environnement).",
                    args.env_file)

    # Imports api APRÈS chargement du .env (api.config lit l'env à l'import).
    from api import guidesend
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
            report = guidesend.run_auto_send(conn, mailer, base_url=base_url,
                                             today=_dt.date.today(),
                                             dry_run=args.dry_run)
        except psycopg.errors.UndefinedColumn:
            log.error("✗ colonnes d'envoi automatique absentes : appliquer la "
                      "migration 025 (deploy.sh) avant de lancer les envois.")
            return 2

    log.info("Terminé%s : %d candidat(s) dans la fenêtre → %d envoyé(s), "
             "%d relance(s) (email manquant), %d échec(s).",
             " (dry-run)" if args.dry_run else "", report.candidates,
             report.sent, report.reminders, len(report.failures))
    if report.failures:
        # Rapport lisible par un humain (audit UX, principe 0.4) : jamais l'UUID.
        log.warning("Séjours en échec (ré-essai demain) : %s",
                    " ; ".join(report.failure_labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
