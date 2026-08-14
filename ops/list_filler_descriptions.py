#!/usr/bin/env python3
"""Recense — EN LECTURE SEULE — les descriptions de POI « de remplissage » (V2-35).

Le remplissage est une invention polie : une phrase creuse qu'un modèle produit
quand il ignore ce qu'est le lieu (constat 14/08, « La Marquesa » : « Site à visiter
à Orihuela, accessible aux vacanciers intéressés par la culture locale »). Depuis
V2-35 le prompt l'interdit et la réception le rejette pour les POI NEUFS, mais les
descriptions DÉJÀ en base ne sont pas touchées (le COALESCE de l'upsert les conserve).

Ce script les liste, par logement, à partir des MÊMES marqueurs que l'enrichissement
(`enrich.claude_enrich.FILLER_MARKERS` / `is_filler_description`) : **André décide**
quoi purger, à la main ou plus tard. **Aucune écriture** (connexion en lecture ;
ni UPDATE ni DELETE).

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/list_filler_descriptions.py
    …/python …/list_filler_descriptions.py --property-id <uuid>   # un seul logement

Connexion : DSN dans `CASAGUIDE_DB` (défaut `postgresql:///casaguide`, comme
`deploy.sh`). Sort en code 0 même si rien n'est trouvé (relançable).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                       # ops/ (import opsenv)
sys.path.insert(0, str(_HERE.parent / "backend"))    # backend/ (import enrich.*)
import opsenv  # noqa: E402
from enrich.claude_enrich import is_filler_description  # noqa: E402


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def find_filler_descriptions(conn, property_id: str | None = None) -> list[dict]:
    """Regroupe par logement les POI dont la description est du REMPLISSAGE. Chaque
    entrée : {property_id, property_name, items: [{poi_id, name, category, description}]}.
    LECTURE SEULE (un unique SELECT) — testable sans effet de bord."""
    rows = conn.execute(
        """SELECT p.id AS poi_id, p.name AS poi_name, p.description_md,
                  p.category_code, pr.id AS property_id, pr.name AS property_name
           FROM pois p JOIN properties pr ON pr.id = p.property_id
           WHERE p.description_md IS NOT NULL AND p.description_md <> ''
             AND (%(pid)s::uuid IS NULL OR pr.id = %(pid)s::uuid)
           ORDER BY pr.name, p.name""",
        {"pid": property_id},
    ).fetchall()
    by_prop: dict[str, dict] = {}
    for r in rows:
        if not is_filler_description(r["description_md"]):
            continue
        grp = by_prop.setdefault(str(r["property_id"]), {
            "property_id": str(r["property_id"]),
            "property_name": r["property_name"], "items": []})
        grp["items"].append({"poi_id": str(r["poi_id"]), "name": r["poi_name"],
                             "category": r["category_code"],
                             "description": r["description_md"]})
    return list(by_prop.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recense (lecture seule) les descriptions de POI de remplissage.")
    parser.add_argument("--property-id", default=None,
                        help="limiter à un logement (UUID) ; défaut : tous.")
    parser.add_argument("--dsn", default=None,
                        help="DSN PostgreSQL (défaut : CASAGUIDE_DB).")
    parser.add_argument("--env-file",
                        help="chemin d'un .env à charger (défaut : backend/.env).")
    args = parser.parse_args(argv)

    loaded = opsenv.load_env(args.env_file)
    if loaded:
        print(f"· configuration chargée depuis {loaded}")

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        print(f"✗ connexion à la base impossible : {exc}", file=sys.stderr)
        return 1

    with conn:
        groups = find_filler_descriptions(conn, args.property_id)

    total = sum(len(g["items"]) for g in groups)
    if not groups:
        print("✓ aucune description de remplissage détectée. Rien à purger.")
        return 0

    print(f"⚠ {total} description(s) de remplissage sur {len(groups)} logement(s) — "
          "à examiner (le script n'écrit RIEN ; purge manuelle par André) :\n")
    for g in groups:
        print(f"■ {g['property_name']} ({g['property_id']}) — {len(g['items'])} :")
        for it in g["items"]:
            print(f"    • {it['name']} [{it['category']}]")
            print(f"        « {it['description']} »")
        print()
    print("Purge (à la main, POI par POI) — exemple SQL à adapter :")
    print("    UPDATE pois SET description_md = NULL WHERE id = '<poi_id>';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
