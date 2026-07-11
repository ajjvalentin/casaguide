"""Termine l'étape Claude d'un enrichissement dont la moisson Overpass a déjà
réussi (POI en base) : area_facts (urgences, tri, bruit) + descriptions des
POI éditoriaux. N'appelle ni Nominatim, ni Overpass, ni OSRM.

Usage (ANTHROPIC_API_KEY doit être exportée dans CE terminal) :
    python scripts/claude_finish.py 4bf92306
"""
from __future__ import annotations

import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrich import claude_enrich, db  # noqa: E402
from enrich.settings import settings  # noqa: E402


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "4bf92306"
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("❌ Exporter d'abord : export ANTHROPIC_API_KEY=sk-ant-…")
    ai = anthropic.Anthropic()
    total_cts = 0.0

    with db.connect() as conn:
        prop = conn.execute(
            "SELECT id, city, country_code FROM properties WHERE id::text LIKE %s",
            (prefix + "%",)).fetchone()
        if not prop:
            sys.exit(f"❌ Aucun logement dont l'id commence par {prefix!r}")
        pid = str(prop["id"])
        print(f"🏠 Logement {pid[:8]}… ({prop['city']}, {prop['country_code']})")

        # 1. Données locales (mutualisées pays+commune)
        if db.area_facts_fresh(conn, prop["country_code"], prop["city"]):
            print("✅ area_facts déjà présents et récents")
        else:
            facts, meta = claude_enrich.fetch_area_facts(
                prop["city"], prop["country_code"], ai)
            db.upsert_area_facts(conn, prop["country_code"], prop["city"],
                                 facts, source=settings.anthropic_model)
            db.record_cost(conn, pid, None, "anthropic", "area_facts",
                           meta["units"], meta["cost_cts"])
            total_cts += meta["cost_cts"]
            print(f"✅ area_facts générés ({meta['units']} tokens)")

        # 2. Descriptions des POI éditoriaux sans description
        rows = conn.execute(
            """SELECT source_ref, name, category_code AS category FROM pois
               WHERE property_id = %s AND source_ref IS NOT NULL
                 AND description_md IS NULL AND status = 'suggested'
                 AND category_code = ANY(%s)""",
            (pid, list(settings.describe_categories))).fetchall()
        if rows:
            descs, meta = claude_enrich.describe_pois(
                [dict(r) for r in rows], prop["city"], prop["country_code"], ai)
            n = 0
            for ref, text in descs.items():
                conn.execute(
                    """UPDATE pois SET description_md = %s
                       WHERE property_id = %s AND source_ref = %s
                         AND status = 'suggested'""",
                    (text, pid, ref))
                n += 1
            db.record_cost(conn, pid, None, "anthropic", "describe_pois",
                           meta["units"], meta["cost_cts"])
            total_cts += meta["cost_cts"]
            print(f"✅ {n} descriptions rédigées ({meta['units']} tokens)")
        else:
            print("✅ Aucun POI éditorial en attente de description")

        conn.commit()
    print(f"\n💶 Coût de l'opération : {total_cts:.2f} centime(s)")


if __name__ == "__main__":
    main()
