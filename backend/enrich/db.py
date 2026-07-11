"""Accès PostgreSQL du pipeline (psycopg 3).

Toutes les écritures sont idempotentes :
  - pois       : ON CONFLICT (property_id, source, source_ref) -> mise à jour
                 (nécessite la migration 001, index unique partiel)
  - area_facts : ON CONFLICT (country_code, admin_area, fact_type) -> mise à jour
Un POI déjà 'approved'/'edited'/'rejected' par le propriétaire n'est jamais
écrasé par un nouvel enrichissement (respect du workflow §5.1 étape 5).
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .settings import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.db_dsn, row_factory=dict_row)


# ── Lectures ─────────────────────────────────────────────────────────────────

def load_property(conn, property_id: str) -> dict:
    row = conn.execute(
        """SELECT id, name, address_line1, address_line2, postal_code, city,
                  region, country_code,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon, geocode_source
           FROM properties WHERE id = %s""",
        (property_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"Logement introuvable : {property_id}")
    return row


def load_categories(conn) -> list[dict]:
    return conn.execute(
        "SELECT code, default_radius_m FROM poi_categories ORDER BY code"
    ).fetchall()


# ── Écritures ────────────────────────────────────────────────────────────────

def save_geocode(conn, property_id: str, lat: float, lon: float,
                 source: str, accuracy: str) -> None:
    conn.execute(
        """UPDATE properties
           SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
               geocode_source = %s, geocode_accuracy = %s
           WHERE id = %s""",
        (lon, lat, source, accuracy, property_id),
    )


def upsert_pois(conn, property_id: str, category: str, pois: list[dict]) -> int:
    """Insère/actualise les POI suggérés. Ne touche jamais aux POI déjà arbitrés."""
    n = 0
    for p in pois:
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 phone, website, opening_hours, description_md,
                                 dist_walk_m, walk_min, dist_drive_m, drive_min,
                                 source, source_ref, fetched_at, status)
               VALUES (%(pid)s, %(cat)s, %(name)s,
                       ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                       %(address)s, %(phone)s, %(website)s, %(opening_hours)s,
                       %(description_md)s,
                       %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
                       %(source)s, %(source_ref)s, now(), 'suggested')
               ON CONFLICT (property_id, source, source_ref)
               WHERE source_ref IS NOT NULL
               DO UPDATE SET
                   name = EXCLUDED.name, geom = EXCLUDED.geom,
                   address = EXCLUDED.address, phone = EXCLUDED.phone,
                   website = EXCLUDED.website, opening_hours = EXCLUDED.opening_hours,
                   description_md = COALESCE(EXCLUDED.description_md, pois.description_md),
                   dist_walk_m = EXCLUDED.dist_walk_m, walk_min = EXCLUDED.walk_min,
                   dist_drive_m = EXCLUDED.dist_drive_m, drive_min = EXCLUDED.drive_min,
                   fetched_at = now()
               WHERE pois.status = 'suggested'""",
            {
                "pid": property_id, "cat": category,
                "name": p["name"], "lat": p["lat"], "lon": p["lon"],
                "address": p.get("address"), "phone": p.get("phone"),
                "website": p.get("website"), "opening_hours": p.get("opening_hours"),
                "description_md": p.get("description_md"),
                "dist_walk_m": p.get("dist_walk_m"), "walk_min": p.get("walk_min"),
                "dist_drive_m": p.get("dist_drive_m"), "drive_min": p.get("drive_min"),
                "source": p["source"], "source_ref": p["source_ref"],
            },
        )
        n += 1
    return n


def upsert_area_facts(conn, country_code: str, admin_area: str | None,
                      facts: dict[str, Any], source: str) -> None:
    for fact_type, content in facts.items():
        conn.execute(
            """INSERT INTO area_facts (country_code, admin_area, fact_type,
                                       content, source, fetched_at)
               VALUES (%s, %s, %s, %s, %s, now())
               ON CONFLICT (country_code, admin_area, fact_type)
               DO UPDATE SET content = EXCLUDED.content,
                             source = EXCLUDED.source, fetched_at = now()""",
            (country_code, admin_area, fact_type, json.dumps(content), source),
        )


def area_facts_fresh(conn, country_code: str, admin_area: str | None,
                     max_age_days: int = 180) -> bool:
    """True si les 3 area_facts existent déjà et sont récents (mutualisation)."""
    row = conn.execute(
        """SELECT count(*) AS n FROM area_facts
           WHERE country_code = %s AND admin_area IS NOT DISTINCT FROM %s
             AND fetched_at > now() - make_interval(days => %s)""",
        (country_code, admin_area, max_age_days),
    ).fetchone()
    return row["n"] >= 3


# ── Suivi de job et coûts (§5.2) ─────────────────────────────────────────────

def job_start(conn, property_id: str, trigger: str) -> str:
    row = conn.execute(
        """INSERT INTO enrichment_jobs (property_id, trigger, status, started_at)
           VALUES (%s, %s, 'running', now()) RETURNING id""",
        (property_id, trigger),
    ).fetchone()
    return str(row["id"])


def job_step(conn, job_id: str, step: str, state: dict) -> None:
    conn.execute(
        "UPDATE enrichment_jobs SET steps = steps || %s WHERE id = %s",
        (json.dumps({step: state}), job_id),
    )


def job_finish(conn, job_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        """UPDATE enrichment_jobs
           SET status = %s, error = %s, finished_at = now() WHERE id = %s""",
        (status, error, job_id),
    )


def record_cost(conn, property_id: str, job_id: str, provider: str,
                operation: str, units: int, cost_cts: float) -> None:
    conn.execute(
        """INSERT INTO api_costs (property_id, job_id, provider, operation,
                                  units, cost_cts)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (property_id, job_id, provider, operation, units, cost_cts),
    )
