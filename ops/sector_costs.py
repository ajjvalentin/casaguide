#!/usr/bin/env python3
"""Coût RÉEL de la sélection éditoriale, PAR SECTEUR — EN LECTURE SEULE (V2-81).

V2-81 calibre la cible de la passe `reputed_sorties` sur la densité du secteur : un
secteur dense (Seminyak) demande 30 adresses au lieu de 12, donc une collecte initiale
plus chère ; la mémoire de secteur (V2-78) doit l'amortir dès le second guide. Ce script
le VÉRIFIE sur les deux sources de vérité :

  · le MARQUEUR de secteur (`area_facts` `reputed_sorties`) : densité mesurée, cible
    demandée, lieux trouvés/mémorisés, coût de la collecte qui l'a posé ;
  · `api_costs` : tout ce qui a été réellement FACTURÉ pour cette passe, par secteur
    (pays + commune des logements), et le nombre de guides qui en ont profité.

Coût par guide = facturé / guides du secteur. **Aucune écriture.**

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/sector_costs.py
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


def sector_rows(conn) -> list[dict]:
    """Une ligne par secteur (pays, commune) ayant facturé ou mémorisé la passe."""
    return conn.execute(
        """WITH billed AS (
               SELECT p.country_code, p.city,
                      round(sum(c.cost_cts)::numeric, 2) AS billed_cts,
                      count(DISTINCT c.property_id)      AS billed_guides
                 FROM api_costs c JOIN properties p ON p.id = c.property_id
                WHERE c.operation = 'reputed_sorties'
                GROUP BY p.country_code, p.city),
           guides AS (
               SELECT country_code, city, count(*) AS guides
                 FROM properties WHERE guest_guide GROUP BY country_code, city),
           marker AS (
               SELECT country_code, admin_area AS city, content, fetched_at
                 FROM area_facts WHERE fact_type = 'reputed_sorties')
           SELECT coalesce(b.country_code, m.country_code) AS country_code,
                  coalesce(b.city, m.city)                 AS city,
                  m.content->>'density'   AS density,
                  m.content->>'target'    AS target,
                  m.content->>'discovered' AS discovered,
                  m.content->>'persisted' AS persisted,
                  m.content->>'cost_cts'  AS collect_cts,
                  coalesce(b.billed_cts, 0)    AS billed_cts,
                  coalesce(b.billed_guides, 0) AS billed_guides,
                  coalesce(g.guides, 0)        AS guides,
                  m.fetched_at
             FROM billed b
             FULL JOIN marker m ON m.country_code = b.country_code AND m.city = b.city
             LEFT JOIN guides g ON g.country_code = coalesce(b.country_code, m.country_code)
                               AND g.city = coalesce(b.city, m.city)
            ORDER BY billed_cts DESC NULLS LAST""").fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=None, help="backend/.env à charger")
    parser.add_argument("--dsn", default=None, help="DSN PostgreSQL (défaut : CASAGUIDE_DB).")
    args = parser.parse_args()
    opsenv.load_env(args.env_file)
    dsn = args.dsn or os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = sector_rows(conn)
    print(f"{'secteur':28} {'dens.':>5} {'cible':>5} {'trouv.':>6} {'mém.':>5} "
          f"{'collecte':>9} {'facturé':>8} {'guides':>6} {'ct/guide':>8}")
    for r in rows:
        guides = r["guides"] or 0
        per = (float(r["billed_cts"]) / guides) if guides else None
        print(f"{(r['country_code'] + ' ' + (r['city'] or '?'))[:28]:28} "
              f"{r['density'] or '—':>5} {r['target'] or '—':>5} "
              f"{r['discovered'] or '—':>6} {r['persisted'] or '—':>5} "
              f"{r['collect_cts'] or '—':>9} {float(r['billed_cts']):>8.2f} "
              f"{guides:>6} {('%.2f' % per) if per is not None else '—':>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
