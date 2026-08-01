#!/usr/bin/env python3
"""Réimport d'un fichier de relecture i18n (V2-21a, volet 2).

Lit un CSV produit par `ops/i18n_export.py` (colonnes cle, contexte, source_fr,
proposition, correction) et écrit les libellés traduits en base
(`ui_translations`, UNE seule source de vérité). Pour chaque ligne :

  texte_final = correction (si remplie) sinon proposition

- **Idempotent** : réimporter le même fichier ne change rien (upsert).
- **Refuse les clés inconnues** (absentes de `i18n/inventory.json`) avec un
  rapport clair — jamais d'écriture d'une clé qui n'existe pas dans le produit.
- Une ligne sans texte final (correction ET proposition vides) est **ignorée**.

La langue est déduite du nom de fichier `ui_<lang>.csv` (ou forcée par `--lang`)
et doit exister au registre (`languages`).

Lancement :

    python ops/i18n_import.py i18n/review/ui_de.csv
    python ops/i18n_import.py i18n/review/ui_de.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))                    # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))    # api.*
import opsenv  # noqa: E402
from i18n_inventory import INVENTORY_PATH, load_inventory  # noqa: E402

log = logging.getLogger("casaguide.i18n_import")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def lang_from_filename(path: Path) -> str | None:
    """Déduit le code langue de `ui_<lang>.csv` (repli : None)."""
    m = re.match(r"^ui_([a-z]{2,3})$", path.stem)
    return m.group(1) if m else None


def read_csv(path: Path) -> list[dict]:
    """Lit le CSV de relecture (tolère le BOM utf-8-sig d'Excel)."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def plan_import(rows: list[dict], inventory: dict) -> tuple[dict[str, str], list[str], int]:
    """Calcule (à écrire {clé: texte}, clés inconnues, lignes vides ignorées) à
    partir des lignes CSV. Fonction PURE — cœur testable sans base.

    texte_final = correction.strip() sinon proposition.strip(). Clé absente de
    l'inventaire → inconnue (jamais écrite). Texte final vide → ignorée."""
    to_write: dict[str, str] = {}
    unknown: list[str] = []
    empty = 0
    for row in rows:
        key = (row.get("cle") or "").strip()
        if not key:
            continue
        if key not in inventory:
            unknown.append(key)
            continue
        final = (row.get("correction") or "").strip() or (row.get("proposition") or "").strip()
        if not final:
            empty += 1
            continue
        to_write[key] = final
    return to_write, unknown, empty


def import_csv(conn, csv_path: Path, lang: str, inventory: dict,
               dry_run: bool = False) -> dict:
    """Applique l'import. Renvoie un rapport {written, unknown, empty, ...}."""
    from api import repo
    rows = read_csv(csv_path)
    to_write, unknown, empty = plan_import(rows, inventory)
    if not dry_run:
        for key, text in to_write.items():
            repo.upsert_ui_translation(conn, lang, key, text)
    return {"written": len(to_write), "unknown": unknown, "empty": empty,
            "rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Réimporte un CSV de relecture i18n (V2-21a).")
    parser.add_argument("csv", help="chemin du fichier CSV (ui_<lang>.csv).")
    parser.add_argument("--lang", default=None,
                        help="code langue (défaut : déduit du nom de fichier).")
    parser.add_argument("--dsn", default=None, help="DSN (défaut CASAGUIDE_DB).")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut backend/.env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="analyse sans écrire.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        log.error("✗ fichier introuvable : %s", csv_path)
        return 2
    lang = args.lang or lang_from_filename(csv_path)
    if not lang:
        log.error("✗ langue indéterminée : nommer le fichier ui_<lang>.csv ou "
                  "passer --lang.")
        return 2

    inventory = load_inventory()
    if not inventory:
        log.error("✗ inventaire absent (%s) : lancer d'abord "
                  "ops/i18n_inventory.py.", INVENTORY_PATH)
        return 2

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        if conn.execute("SELECT 1 FROM languages WHERE code = %s",
                        (lang,)).fetchone() is None:
            log.error("✗ langue « %s » absente du registre (table languages).", lang)
            return 2
        report = import_csv(conn, csv_path, lang, inventory, dry_run=args.dry_run)
        if not args.dry_run:
            conn.commit()

    prefix = "[dry-run] " if args.dry_run else ""
    log.info("%s%d ligne(s) lue(s) ; %d libellé(s) importé(s) en « %s » ; "
             "%d ligne(s) vide(s) ignorée(s).",
             prefix, report["rows"], report["written"], lang, report["empty"])
    if report["unknown"]:
        log.warning("⚠ %d clé(s) INCONNUE(S) refusée(s) (absentes de "
                    "l'inventaire) : %s", len(report["unknown"]),
                    ", ".join(report["unknown"][:20])
                    + (" …" if len(report["unknown"]) > 20 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
