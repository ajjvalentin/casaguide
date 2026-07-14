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
                  region, country_code, default_lang,
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


def job_mark_running(conn, job_id: str) -> None:
    """Passe un job pré-créé (status 'pending' par l'API) en 'running'."""
    conn.execute(
        "UPDATE enrichment_jobs SET status = 'running', started_at = now() "
        "WHERE id = %s",
        (job_id,),
    )


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


# ── Traductions du guide voyageur (M-09, §9) ─────────────────────────────────
# Lectures/écritures utilisées par le pipeline de traduction (tâche de fond,
# connexion propre). Ne concernent QUE les sections voyageur (audience='guest')
# et les POI retenus (approved/edited) — jamais les secrets ni le cahier staff.

def translatable_sections(conn, property_id: str) -> list[dict]:
    """Sections voyageur instanciées d'un logement (avec leur field_schema et
    leur contenu source) candidates à la traduction."""
    return conn.execute(
        """SELECT ps.id AS section_id, t.field_schema, ps.content, ps.body_md
           FROM property_sections ps
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s AND t.audience = 'guest'
           ORDER BY t.sort_order""",
        (property_id,),
    ).fetchall()


def translatable_pois(conn, property_id: str) -> list[dict]:
    """POI retenus (approved/edited) porteurs de texte éditorial à traduire."""
    return conn.execute(
        """SELECT id, description_md, owner_comment FROM pois
           WHERE property_id = %s AND status IN ('approved', 'edited')""",
        (property_id,),
    ).fetchall()


def get_section_translation(conn, section_id: str, lang: str) -> dict | None:
    return conn.execute(
        "SELECT is_stale FROM section_translations "
        "WHERE section_id = %s AND lang = %s",
        (section_id, lang),
    ).fetchone()


def get_poi_translation(conn, poi_id: str, lang: str) -> dict | None:
    return conn.execute(
        "SELECT is_stale FROM poi_translations WHERE poi_id = %s AND lang = %s",
        (poi_id, lang),
    ).fetchone()


def upsert_section_translation(conn, section_id: str, lang: str,
                               content: dict, body_md: str | None) -> None:
    """Écrit une traduction de section (is_stale=FALSE : fraîche par définition)."""
    conn.execute(
        """INSERT INTO section_translations (section_id, lang, content, body_md,
                                             is_stale, updated_at)
           VALUES (%s, %s, %s, %s, FALSE, now())
           ON CONFLICT (section_id, lang) DO UPDATE SET
               content = EXCLUDED.content, body_md = EXCLUDED.body_md,
               is_stale = FALSE, updated_at = now()""",
        (section_id, lang, json.dumps(content), body_md),
    )


def upsert_poi_translation(conn, poi_id: str, lang: str,
                           description_md: str | None,
                           owner_comment: str | None) -> None:
    conn.execute(
        """INSERT INTO poi_translations (poi_id, lang, description_md,
                                         owner_comment, is_stale)
           VALUES (%s, %s, %s, %s, FALSE)
           ON CONFLICT (poi_id, lang) DO UPDATE SET
               description_md = EXCLUDED.description_md,
               owner_comment = EXCLUDED.owner_comment, is_stale = FALSE""",
        (poi_id, lang, description_md, owner_comment),
    )


def delete_section_translation(conn, section_id: str, lang: str) -> None:
    conn.execute("DELETE FROM section_translations "
                 "WHERE section_id = %s AND lang = %s", (section_id, lang))


def delete_poi_translation(conn, poi_id: str, lang: str) -> None:
    conn.execute("DELETE FROM poi_translations WHERE poi_id = %s AND lang = %s",
                 (poi_id, lang))


def set_published_langs(conn, property_id: str, langs: list[str]) -> None:
    """Publie la liste des langues traduites disponibles (pilote le sélecteur du
    guide). N'inclut jamais la langue source (déduite au rendu)."""
    conn.execute(
        "UPDATE properties SET published_langs = %s WHERE id = %s",
        (list(langs), property_id),
    )
