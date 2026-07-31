#!/usr/bin/env python3
"""Rattrapage des règles d'entretien & du catalogue de demandes (V2-23b, volet 1).

Les migrations 016/017 ajoutent les COLONNES/TABLES mais ne peuvent pas amorcer
un CONTENU applicatif : `properties.care_rules` naît à `{}` et un logement créé
avant le volet 1 n'a aucun `property_request_type`. Ce script pose, sur les
logements **déjà existants**, exactement ce que `repo.create_property` pose à la
création — en réutilisant les MÊMES fonctions (`care.default_care_rules`,
`care.DEFAULT_REQUEST_TYPES`, `repo.seed_request_types`), **jamais** une copie du
JSON dans le SQL (invariant 8 : la vérité vit dans le code, pas dans une valeur
figée).

Leçon de l'incident 015 (voir CLAUDE.md) : **tout état amorcé à la création exige
un backfill** pour les lignes antérieures — une migration SQL seule ne suffit pas.

Idempotent : ne (ré)écrit `care_rules` que sur les logements où il vaut encore
`{}` (jamais d'écrasement d'un réglage saisi) ; `seed_request_types` saute les
codes déjà présents. Ré-exécutable à volonté.

Lancement (à la main sur le serveur, après `deploy.sh`) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/backfill_care.py

Charge `backend/.env` lui-même (hors EnvironmentFile systemd, OPS-1) ; DSN dans
`CASAGUIDE_DB` (défaut socket local). `--dry-run` compte sans écrire.
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

log = logging.getLogger("casaguide.backfill_care")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Amorce care_rules + catalogue de demandes sur les logements "
                    "existants (V2-23b, volet 1).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                             "postgresql:///casaguide).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="compte ce qui serait amorcé, sans écrire.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)
    elif args.env_file:
        log.warning("⚠ --env-file introuvable : %s (repli environnement).",
                    args.env_file)

    # Imports api APRÈS chargement du .env (api.config lit l'env à l'import).
    from api import care, repo

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    rules_seeded = types_inserted = touched = 0
    with conn:
        try:
            props = repo.list_properties_care(conn)
        except psycopg.errors.UndefinedColumn:
            log.error("✗ colonne properties.care_rules absente : appliquer la "
                      "migration 017 (deploy.sh) avant le rattrapage.")
            return 2

        for prop in props:
            pid = str(prop["id"])
            changed = False
            # care_rules == {} → jamais amorcé (on ne touche jamais un réglage saisi).
            if not prop["care_rules"]:
                if args.dry_run:
                    log.info("[dry-run] logement %s : care_rules à amorcer", pid)
                else:
                    repo.set_care_rules(conn, pid, care.default_care_rules())
                rules_seeded += 1
                changed = True
            # Catalogue : idempotent (ON CONFLICT), on peut toujours l'appeler.
            if args.dry_run:
                # Compte sans écrire : combien de codes par défaut manquent.
                existing = {t["code"] for t in repo.list_request_types(conn, pid)}
                missing = sum(1 for t in care.DEFAULT_REQUEST_TYPES
                              if t["code"] not in existing)
                if missing:
                    log.info("[dry-run] logement %s : %d type(s) à amorcer",
                             pid, missing)
                types_inserted += missing
                changed = changed or bool(missing)
            else:
                n = repo.seed_request_types(conn, pid, care.DEFAULT_REQUEST_TYPES)
                types_inserted += n
                changed = changed or bool(n)
            touched += int(changed)

        if not args.dry_run:
            conn.commit()

    prefix = "[dry-run] " if args.dry_run else ""
    log.info("%sTerminé : %d logement(s) concerné(s) ; care_rules amorcés sur %d ; "
             "%d type(s) de demande insérés.",
             prefix, touched, rules_seeded, types_inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
