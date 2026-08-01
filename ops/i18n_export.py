#!/usr/bin/env python3
"""Export d'un fichier de relecture i18n pour une langue (V2-21a, volet 2).

Produit un CSV lisible par un bénévole non technique (Excel/Numbers) :

    cle, contexte, source_fr, proposition, correction

- `source_fr`   : le texte français d'origine (référence du relecteur) ;
- `proposition` : la traduction déjà en base (`ui_translations`) — vide tant
  qu'aucune génération/import n'a eu lieu (volet 3 la remplit) ;
- `correction`  : colonne VIDE que le relecteur remplit ; au réimport, si elle
  est remplie elle remplace la proposition (`ops/i18n_import.py`).

Fichier écrit dans `i18n/review/ui_<lang>.csv`. La langue doit exister au
registre (`languages`). L'inventaire (`i18n/inventory.json`) doit avoir été
généré au préalable (`ops/i18n_inventory.py`).

Lancement :

    python ops/i18n_export.py --lang de
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))                    # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))    # api.*
import opsenv  # noqa: E402
from i18n_inventory import INVENTORY_PATH, REPO_ROOT, load_inventory  # noqa: E402

log = logging.getLogger("casaguide.i18n_export")

REVIEW_DIR = REPO_ROOT / "i18n" / "review"
CSV_COLUMNS = ["cle", "contexte", "source_fr", "proposition", "correction"]


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def lang_exists(conn, lang: str) -> bool:
    return conn.execute("SELECT 1 FROM languages WHERE code = %s",
                        (lang,)).fetchone() is not None


def export_rows(inventory: dict, proposals: dict[str, str]) -> list[dict]:
    """Construit les lignes du CSV (triées par clé) depuis l'inventaire et les
    propositions déjà en base. Fonction pure — testable sans écrire de fichier."""
    rows = []
    for key in sorted(inventory):
        entry = inventory[key]
        rows.append({
            "cle": key,
            "contexte": entry.get("context", ""),
            "source_fr": entry.get("fr", ""),
            "proposition": proposals.get(key, ""),
            "correction": "",
        })
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig : Excel (Windows) reconnaît alors l'UTF-8 sans réglage.
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def export_lang(conn, lang: str, inventory: dict, out_dir: Path = REVIEW_DIR) -> Path:
    """Écrit le CSV de relecture pour `lang` et renvoie son chemin."""
    from api import repo
    proposals = repo.ui_translations(conn, lang)
    rows = export_rows(inventory, proposals)
    out_path = out_dir / f"ui_{lang}.csv"
    write_csv(rows, out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exporte un CSV de relecture i18n pour une langue (V2-21a).")
    parser.add_argument("--lang", required=True, help="code langue (ex. de, nl).")
    parser.add_argument("--dsn", default=None, help="DSN (défaut CASAGUIDE_DB).")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut backend/.env).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)

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
        if not lang_exists(conn, args.lang):
            log.error("✗ langue « %s » absente du registre (table languages). "
                      "L'ajouter d'abord.", args.lang)
            return 2
        out_path = export_lang(conn, args.lang, inventory)

    log.info("✓ %d libellé(s) exporté(s) → %s", len(inventory),
             out_path.relative_to(REPO_ROOT))
    log.info("  Le relecteur remplit la colonne « correction » puis renvoie le "
             "fichier (réimport : ops/i18n_import.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
