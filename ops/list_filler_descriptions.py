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
import re
import sys
import unicodedata
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


def _norm(s: str) -> str:
    return (unicodedata.normalize("NFKD", s or "")
            .encode("ascii", "ignore").decode().lower())


def _address_locality(address: str | None) -> str | None:
    """Ville portée par l'adresse OSM (V2-37) : dernier segment après une virgule
    (« 5 Route X, Vétroz » → « Vétroz ») ; sinon une adresse sans virgule ET sans
    chiffre est prise pour une ville seule (« Vétroz »). None si indécidable."""
    if not address:
        return None
    if "," in address:
        return address.rsplit(",", 1)[-1].strip() or None
    a = address.strip()
    return a if a and not any(ch.isdigit() for ch in a) else None


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


def find_suspect_communes(conn, property_id: str | None = None) -> list[dict]:
    """Regroupe par logement les POI dont la description AFFIRME la commune du LOGEMENT
    alors que l'adresse OSM porte une AUTRE ville (V2-37, bonus). Le motif-filler ne
    voit pas ce défaut (« restaurant à Ardon » pour un lieu de Vétroz) — c'est le seul
    moyen d'attraper l'existant. Heuristique prudente (l'humain tranche) : la ville du
    logement apparaît comme MOT dans la description, PAS dans le nom du POI, et la ville
    de l'adresse en DIFFÈRE. LECTURE SEULE."""
    rows = conn.execute(
        """SELECT p.id AS poi_id, p.name AS poi_name, p.description_md, p.address,
                  pr.id AS property_id, pr.name AS property_name, pr.city AS property_city
           FROM pois p JOIN properties pr ON pr.id = p.property_id
           WHERE p.description_md IS NOT NULL AND p.description_md <> ''
             AND p.address IS NOT NULL AND p.address <> ''
             AND (%(pid)s::uuid IS NULL OR pr.id = %(pid)s::uuid)
           ORDER BY pr.name, p.name""",
        {"pid": property_id},
    ).fetchall()
    by_prop: dict[str, dict] = {}
    for r in rows:
        city = (r["property_city"] or "").strip()
        addr_city = _address_locality(r["address"])
        if not city or not addr_city or _norm(addr_city) == _norm(city):
            continue                                  # même commune (ou indécidable) → OK
        word = re.compile(rf"\b{re.escape(_norm(city))}\b")
        if not word.search(_norm(r["description_md"])):
            continue                                  # la description ne cite pas la ville logement
        if word.search(_norm(r["poi_name"])):
            continue                                  # ville dans le NOM → légitime, pas suspect
        grp = by_prop.setdefault(str(r["property_id"]), {
            "property_id": str(r["property_id"]),
            "property_name": r["property_name"],
            "property_city": city, "items": []})
        grp["items"].append({"poi_id": str(r["poi_id"]), "name": r["poi_name"],
                             "address_city": addr_city, "description": r["description_md"]})
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
        suspects = find_suspect_communes(conn, args.property_id)

    total = sum(len(g["items"]) for g in groups)
    if not groups:
        print("✓ aucune description de remplissage détectée.")
    else:
        print(f"⚠ {total} description(s) de remplissage sur {len(groups)} logement(s) — "
              "à examiner (le script n'écrit RIEN ; purge manuelle par André) :\n")
        for g in groups:
            print(f"■ {g['property_name']} ({g['property_id']}) — {len(g['items'])} :")
            for it in g["items"]:
                print(f"    • {it['name']} [{it['category']}]")
                print(f"        « {it['description']} »")
            print()

    # Bonus V2-37 : communes suspectes (le motif-filler ne les voit pas).
    sus_total = sum(len(g["items"]) for g in suspects)
    if suspects:
        print(f"⚑ {sus_total} description(s) affirmant une COMMUNE SUSPECTE (le lieu "
              "semble d'une autre commune que celle citée) — à vérifier :\n")
        for g in suspects:
            print(f"■ {g['property_name']} ({g['property_id']}) — logement à "
                  f"{g['property_city']} :")
            for it in g["items"]:
                print(f"    • {it['name']} — adresse à « {it['address_city']} » :")
                print(f"        « {it['description']} »")
            print()

    if not groups and not suspects:
        print("✓ rien à signaler.")
        return 0
    print("Purge / correction (à la main, POI par POI) — exemple SQL à adapter :")
    print("    UPDATE pois SET description_md = NULL WHERE id = '<poi_id>';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
