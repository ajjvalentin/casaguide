#!/usr/bin/env python3
"""Annulation de modifications du journal (V2-92) — geste de l'EXPLOITANT.

Annule une modification (`--change ID`) ou tout un lot (`--batch UUID`, une
exécution du pipeline = un lot ; son identifiant figure dans la ligne
« ▶ Enrichissement … lot <uuid> » et dans `content_changes.batch_id`).

L'annulation est écrite avec l'auteur **`support`** : une intervention lancée à la
main par l'exploitant apparaît comme telle dans le journal, jamais comme un acte du
système. À n'utiliser qu'à la demande expresse d'un propriétaire, ou pour défaire
une écriture automatique fautive.

Règles (enrich/journal.py) : seules les modifications d'auteur `system`/`judge`
s'annulent ; rien n'est restauré si la valeur a changé depuis ou si le
propriétaire a repris la main sur le champ (conflit nommé) ; une modification ne
s'annule qu'une fois.

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --batch <uuid> --dry-run
    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --change 1234

`--dry-run` joue EXACTEMENT la même logique puis annule la transaction (ROLLBACK) :
le plan affiché est celui qui serait appliqué, sans aucune écriture.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))          # ops/ (opsenv)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # enrich.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.revert_changes")

_LABELS = {"reverted": "annulée", "conflict": "CONFLIT", "refused": "refusée"}


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def _line(r, dry_run: bool) -> str:
    label = ("serait annulée" if dry_run and r.outcome == "reverted"
             else _LABELS.get(r.outcome, r.outcome))
    what = f"{r.action or '?'}" + (f" {r.field}" if r.field else "")
    tail = f" — {r.reason}" if r.reason else ""
    rid = f" (ligne revert {r.revert_id})" if r.revert_id and not dry_run else ""
    return f"  #{r.change_id:<8} {what:<24} fiche {r.entity_id or '?'} : {label}{tail}{rid}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Annule une modification ou un lot du journal (V2-92), auteur 'support'.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--batch", help="UUID du lot à annuler (ordre inverse).")
    target.add_argument("--change", type=int, help="id de la ligne content_changes.")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche le plan sans rien écrire (ROLLBACK).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou postgresql:///casaguide).")
    parser.add_argument("--env-file", help="chemin d'un .env à charger (défaut : backend/.env).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)

    from enrich import journal  # noqa: PLC0415 — après le .env

    if args.batch:
        try:
            uuid.UUID(args.batch)
        except ValueError:
            log.error("✗ --batch n'est pas un UUID : %s", args.batch)
            return 2

    try:
        conn = psycopg.connect(args.dsn or _default_dsn(), row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        if args.batch:
            results = journal.revert_batch(conn, args.batch, actor="support")
            log.info("Lot %s — %d modification(s)%s :", args.batch, len(results),
                     " [dry-run]" if args.dry_run else "")
        else:
            results = [journal.revert_change(conn, args.change, actor="support")]
            log.info("Modification #%d%s :", args.change,
                     " [dry-run]" if args.dry_run else "")
        for r in results:
            log.info(_line(r, args.dry_run))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    n_ok = sum(r.outcome == "reverted" for r in results)
    n_conf = sum(r.outcome == "conflict" for r in results)
    n_ref = sum(r.outcome == "refused" for r in results)
    verb = "à annuler" if args.dry_run else "annulée(s)"
    log.info("Terminé%s : %d %s, %d en conflit, %d refusée(s).",
             " (dry-run, rien n'est écrit)" if args.dry_run else "",
             n_ok, verb, n_conf, n_ref)
    return 0 if (n_conf == 0 and n_ref == 0) else 4


if __name__ == "__main__":
    raise SystemExit(main())
