#!/usr/bin/env python3
"""Synchronisation périodique des flux iCal du calendrier des séjours (V2-23a).

Parcourt **tous** les flux `property_calendars` de tous les logements, fetch +
parse + upsert par UID (idempotent), passe 'cancelled' les séjours disparus.
L'échec d'un flux (plateforme lente, lien invalide) est enregistré sur le flux
(`last_sync_status='error'`, `sync_error`) et **jamais bloquant** pour les autres
(invariant 3 de la mission) : chaque flux est committé indépendamment.

Lancement (timer systemd toutes les 4 h, ou à la main sur le serveur) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/sync_calendars.py

Charge `backend/.env` lui-même (exécution hors EnvironmentFile systemd, OPS-1) ;
DSN dans `CASAGUIDE_DB` (défaut socket local). `--dry-run` liste les flux sans
fetch ni écriture. Nécessite `CASAGUIDE_SECRET_KEY` (les URL iCal sont chiffrées).
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

log = logging.getLogger("casaguide.sync_calendars")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronise les flux iCal du calendrier des séjours (V2-23a).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                             "postgresql:///casaguide).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="liste les flux sans fetch ni écriture.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)
    elif args.env_file:
        log.warning("⚠ --env-file introuvable : %s (repli environnement).",
                    args.env_file)

    # Imports api APRÈS chargement du .env (api.config / api.crypto lisent l'env).
    from api import calendars, crypto, repo

    if not crypto.is_configured():
        log.error("✗ CASAGUIDE_SECRET_KEY absente : impossible de déchiffrer les "
                  "URL iCal. Renseignez-la dans backend/.env.")
        return 3

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        try:
            cals = repo.list_all_calendars(conn)
        except psycopg.errors.UndefinedTable:
            log.error("✗ table property_calendars absente : appliquer la migration "
                      "014 (deploy.sh) avant de synchroniser.")
            return 2
        if args.dry_run:
            for cal in cals:
                log.info("[dry-run] flux %s (%s) — dernier statut : %s",
                         cal["id"], cal["platform"], cal["last_sync_status"])
            log.info("Terminé (dry-run) : %d flux.", len(cals))
            return 0
        report = calendars.sync_all(conn)

    log.info("Terminé : %d flux (%d ok, %d en erreur) ; %d créés, %d mis à jour, "
             "%d annulés.", report.calendars, report.ok, report.errors,
             report.created, report.updated, report.cancelled)
    if report.failures:
        log.warning("Flux en erreur : %s", ", ".join(report.failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
