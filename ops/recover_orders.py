#!/usr/bin/env python3
"""Chien de garde des commandes voyageur (V2-64) — backstop périodique.

Constat terrain (achat réel, Remaufens, 14/09) : une tâche de fond de génération
morte silencieusement (vraisemblablement tuée par un redémarrage de déploiement)
laisse la commande figée en `generating` à vie — client sur une roue éternelle,
aucun e-mail. Un client PAYANT ne doit JAMAIS rester sans rien.

Le démarrage de l'app reprend déjà toute commande orpheline (`api.main` lifespan,
seuil 0 — après un redémarrage aucune tâche n'a survécu). Ce script est le **backstop
périodique** pour le cas où l'app reste debout mais une tâche meurt seule : il reprend
toute commande PAYÉE bloquée (`paid`/`generating`) dont `updated_at` dépasse le seuil
(`CASAGUIDE_GUEST_RECOVER_STALE_S`, 45 min par défaut — bien au-delà de toute étape de
génération grâce au battement de cœur) et **relance sa génération** (ici, en synchrone :
le oneshot exécute lui-même le pipeline). Le verrou atomique de génération garantit
qu'une même commande n'est jamais générée deux fois (app + ce script).

Mince habillage CLI du cœur testable `api.guest_guides.recover_stuck_orders` (patron
`send_guides.py` → `guidesend.run_auto_send`).

Lancement (timer systemd toutes les ~5 min, ou à la main sur le serveur) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/recover_orders.py

Charge `backend/.env` lui-même (exécution hors EnvironmentFile systemd, OPS-1) ;
DSN dans `CASAGUIDE_DB` (défaut socket local). `--dry-run` : liste sans relancer.
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

log = logging.getLogger("casaguide.recover_orders")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reprend les commandes voyageur payées bloquées et relance "
                    "leur génération (V2-64).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                             "postgresql:///casaguide).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    parser.add_argument("--stale-seconds", type=int, default=None,
                        help="ancienneté d'updated_at au-delà de laquelle une "
                             "commande est orpheline (défaut : "
                             "CASAGUIDE_GUEST_RECOVER_STALE_S ou 2700).")
    parser.add_argument("--dry-run", action="store_true",
                        help="liste les commandes reprenables sans relancer.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)

    # Imports api APRÈS chargement du .env (api.config lit l'env à l'import).
    from api import guest_guides, repo
    from api.config import settings
    from api.deps import build_mailer

    stale_s = (args.stale_seconds if args.stale_seconds is not None
               else int(os.getenv("CASAGUIDE_GUEST_RECOVER_STALE_S", "2700")))
    base_url = settings.public_base_url or "https://holaguia.com"
    mailer = build_mailer()

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        if args.dry_run:
            rows = conn.execute(
                "SELECT id, email, city, status, updated_at FROM guest_guide_orders "
                "WHERE status IN ('paid','generating') "
                "AND updated_at < now() - make_interval(secs => %s) "
                "ORDER BY updated_at", (stale_s,)).fetchall()
            for r in rows:
                log.info("reprenable : %s — %s (%s, MAJ %s)",
                         r["id"], r["city"], r["status"], r["updated_at"])
            log.info("Terminé (dry-run) : %d commande(s) reprenable(s) "
                     "(seuil %d s).", len(rows), stale_s)
            return 0
        try:
            n = guest_guides.recover_stuck_orders(
                conn, mailer=mailer, base_url=base_url, older_than_s=stale_s,
                spawn=lambda fn: fn())          # oneshot : exécution synchrone
        except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
            log.error("✗ table guest_guide_orders absente : appliquer les migrations "
                      "(deploy.sh) avant de lancer la reprise.")
            return 2

    log.info("Terminé : %d commande(s) orpheline(s) reprise(s) (seuil %d s).",
             n, stale_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
