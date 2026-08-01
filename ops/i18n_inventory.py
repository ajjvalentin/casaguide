#!/usr/bin/env python3
"""Inventaire des libellés STATIQUES du guide voyageur (V2-21a, volet 2).

GÉNÉRÉ PAR SCRIPT depuis les sources vivantes (code `api.guide_page` + seed en
base), **jamais maintenu à la main** — sinon il divergerait dès la mission
suivante. Écrit `i18n/inventory.json` : pour chaque libellé, une clé stable, un
contexte (FR, aide au relecteur) et les textes source FR/EN/ES existants.

Rejouable. Il **signale les clés nouvelles/disparues** par rapport à l'inventaire
précédent : c'est l'outil de non-régression i18n des futures missions (une clé
ajoutée au code/seed doit apparaître ici ; une clé retirée doit disparaître).

Périmètre = la surface MULTILINGUE (guide voyageur SSR) : libellés d'interface
`_UI`, noms de chapitres, catégories de POI, sections du seed, cuisines. HORS
périmètre (surfaces restées FR : back-office, cahier /s/, emails owner) — cf.
docs/i18n.md.

Lancement :

    python ops/i18n_inventory.py            # (re)génère i18n/inventory.json
    python ops/i18n_inventory.py --check    # échoue si l'inventaire a changé (CI)

Charge `backend/.env` lui-même (OPS-1) ; DSN dans `CASAGUIDE_DB`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))                    # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))    # api.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.i18n_inventory")

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "i18n" / "inventory.json"


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def load_inventory(path: Path = INVENTORY_PATH) -> dict[str, dict[str, str]]:
    """Charge l'inventaire existant (dict clé→entrée), ou {} s'il n'existe pas."""
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def diff_keys(previous: dict, current: dict) -> tuple[list[str], list[str]]:
    """(clés ajoutées, clés disparues) entre deux inventaires."""
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    return added, removed


def write_inventory(inventory: dict, path: Path = INVENTORY_PATH) -> None:
    """Écrit l'inventaire en JSON stable (clés triées, UTF-8 lisible)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def build(conn) -> dict[str, dict[str, str]]:
    """Recense l'inventaire depuis les sources vivantes (délègue à api.i18n)."""
    from api import i18n
    return i18n.collect_inventory(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Génère i18n/inventory.json depuis les sources (V2-21a).")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB).")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut backend/.env).")
    parser.add_argument("--check", action="store_true",
                        help="n'écrit pas ; échoue (code 3) si l'inventaire "
                             "diffère de i18n/inventory.json (gate CI).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        current = build(conn)
    previous = load_inventory()
    added, removed = diff_keys(previous, current)

    log.info("Inventaire : %d libellés (%d chapitre(s)+section(s)+catégorie(s) "
             "de seed, reste de code).", len(current),
             sum(1 for k in current if k.startswith(("section.", "poi_category.",
                                                     "chapter."))))
    if added:
        log.info("  + %d clé(s) NOUVELLE(S) : %s", len(added),
                 ", ".join(added[:20]) + (" …" if len(added) > 20 else ""))
    if removed:
        log.info("  − %d clé(s) DISPARUE(S) : %s", len(removed),
                 ", ".join(removed[:20]) + (" …" if len(removed) > 20 else ""))
    if not added and not removed:
        log.info("  = aucun changement de clés depuis l'inventaire précédent.")

    changed = current != previous
    if args.check:
        if changed:
            log.error("✗ --check : l'inventaire a changé — relancer sans --check "
                      "puis committer i18n/inventory.json.")
            return 3
        log.info("✓ --check : inventaire à jour.")
        return 0

    write_inventory(current)
    log.info("✓ écrit %s", INVENTORY_PATH.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
