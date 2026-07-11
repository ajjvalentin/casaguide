"""Orchestrateur du pipeline d'enrichissement (§5.1 du CdC).

Usage :
    python -m enrich.pipeline --property-id <uuid> [--no-claude]
                              [--categories restaurant,hospital,...]

Étapes (chacune tracée dans enrichment_jobs.steps) :
    1. geocode   : adresse -> lat/lon (sauté si le logement a déjà un geom)
    2. overpass  : POI par catégorie dans le rayon du seed
    3. distances : temps à pied / en voiture (OSRM, fallback estimation)
    4. claude    : area_facts (urgences, tri, bruit) + descriptions éditoriales
    5. save      : upserts en base, statut 'suggested' -> validation propriétaire
"""
from __future__ import annotations

import argparse
import os
import sys

import anthropic
import httpx

from . import claude_enrich, db, distance, geocode, overpass
from .settings import settings


def run(property_id: str, *, use_claude: bool = True, trigger: str = "manual",
        only_categories: set[str] | None = None,
        job_id: str | None = None,
        http_client: httpx.Client | None = None,
        anthropic_client: anthropic.Anthropic | None = None) -> dict:
    """Exécute le pipeline pour un logement. Retourne un résumé.

    Si `job_id` est fourni (job 'pending' pré-créé par l'API pour renvoyer un
    identifiant immédiat), il est réutilisé ; sinon un nouveau job est créé.
    """
    summary: dict = {"pois": 0, "categories": {}, "area_facts": False, "cost_cts": 0.0}

    with db.connect() as conn:
        prop = db.load_property(conn, property_id)
        if job_id is None:
            job_id = db.job_start(conn, property_id, trigger)
        else:
            db.job_mark_running(conn, job_id)
        conn.commit()

        try:
            # ── 1. Géocodage ────────────────────────────────────────────────
            if prop["lat"] is None:
                geo = geocode.geocode(
                    country_code=prop["country_code"], client=http_client,
                    street=prop["address_line1"], postalcode=prop["postal_code"],
                    city=prop["city"])
                db.save_geocode(conn, property_id, geo["lat"], geo["lon"],
                                geo["source"], geo["accuracy"])
                prop["lat"], prop["lon"] = geo["lat"], geo["lon"]
                db.job_step(conn, job_id, "geocode",
                            {"ok": True, "accuracy": geo["accuracy"]})
            else:
                db.job_step(conn, job_id, "geocode", {"ok": True, "skipped": True})
            conn.commit()  # progression visible en temps réel
            origin = (prop["lat"], prop["lon"])

            # ── 2 + 3. POI Overpass puis distances ─────────────────────────
            # Overpass : une requête par palier de rayon (union de sélecteurs),
            # résultats re-ventilés par catégorie via leurs tags (perf, M-01).
            categories = db.load_categories(conn)
            wanted = [c for c in categories
                      if (not only_categories or c["code"] in only_categories)
                      and c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES]
            grouped, failed_categories = overpass.fetch_grouped(
                wanted, origin[0], origin[1], client=http_client)

            all_editorial: list[dict] = []
            for cat in wanted:
                code = cat["code"]
                pois = grouped.get(code) or []
                if not pois:
                    continue
                try:
                    distance.compute_distances(origin, pois, client=http_client)
                except Exception as exc:
                    # Un échec de distances ne doit pas faire perdre la catégorie :
                    # on la trace et on continue (ré-enrichissable plus tard).
                    failed_categories[code] = f"{type(exc).__name__}: {exc}"[:120]
                    continue
                for p in pois:
                    p["category"] = code
                if code in settings.describe_categories:
                    all_editorial.extend(pois)
                n = db.upsert_pois(conn, property_id, code, pois)
                summary["categories"][code] = n
                summary["pois"] += n
                conn.commit()  # les POI de cette catégorie sont acquis
                db.job_step(conn, job_id, "overpass",
                            {"ok": False, "in_progress": code,
                             "pois": summary["pois"], "failed": failed_categories})
                conn.commit()
            summary["failed_categories"] = failed_categories
            db.job_step(conn, job_id, "overpass",
                        {"ok": not failed_categories or summary["pois"] > 0,
                         "pois": summary["pois"],
                         "failed": failed_categories})
            db.job_step(conn, job_id, "distances", {"ok": True})
            conn.commit()

            # ── 4. Enrichissement Claude ────────────────────────────────────
            if use_claude:
                ai = anthropic_client or anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"])

                # 4a. Données locales mutualisées (pays + commune)
                if not db.area_facts_fresh(conn, prop["country_code"], prop["city"]):
                    facts, meta = claude_enrich.fetch_area_facts(
                        prop["city"], prop["country_code"], ai)
                    db.upsert_area_facts(conn, prop["country_code"], prop["city"],
                                         facts, source=settings.anthropic_model)
                    db.record_cost(conn, property_id, job_id, "anthropic",
                                   "area_facts", meta["units"], meta["cost_cts"])
                    summary["cost_cts"] += meta["cost_cts"]
                summary["area_facts"] = True

                # 4b. Descriptions courtes des POI éditoriaux
                if all_editorial:
                    descs, meta = claude_enrich.describe_pois(
                        all_editorial, prop["city"], prop["country_code"], ai)
                    for p in all_editorial:
                        if p["source_ref"] in descs:
                            p["description_md"] = descs[p["source_ref"]]
                    # ré-upsert : seule la description change
                    for code in {p["category"] for p in all_editorial}:
                        db.upsert_pois(conn, property_id, code,
                                       [p for p in all_editorial
                                        if p["category"] == code])
                    db.record_cost(conn, property_id, job_id, "anthropic",
                                   "describe_pois", meta["units"], meta["cost_cts"])
                    summary["cost_cts"] += meta["cost_cts"]
                db.job_step(conn, job_id, "claude",
                            {"ok": True, "cost_cts": summary["cost_cts"]})
            else:
                db.job_step(conn, job_id, "claude", {"ok": True, "skipped": True})

            db.job_finish(conn, job_id, "done")
            conn.commit()
        except Exception as exc:  # échec -> job 'failed', rien de corrompu
            conn.rollback()
            db.job_finish(conn, job_id, "failed", error=f"{type(exc).__name__}: {exc}")
            conn.commit()
            raise

    summary["job_id"] = job_id
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline d'enrichissement CasaGuide")
    parser.add_argument("--property-id", required=True)
    parser.add_argument("--no-claude", action="store_true",
                        help="sauter l'étape IA (test des étapes géo)")
    parser.add_argument("--categories", default=None,
                        help="liste de catégories séparées par des virgules")
    parser.add_argument("--trigger", default="manual",
                        choices=["manual", "initial", "refresh"])
    args = parser.parse_args()

    cats = set(args.categories.split(",")) if args.categories else None
    result = run(args.property_id, use_claude=not args.no_claude,
                 trigger=args.trigger, only_categories=cats)
    print(f"Job {result['job_id']} terminé : {result['pois']} POI suggérés, "
          f"coût IA {result['cost_cts']:.2f} ct")
    for cat, n in sorted(result["categories"].items()):
        print(f"  {cat:<16} {n}")


if __name__ == "__main__":
    sys.exit(main())
