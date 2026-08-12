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
                                 phone, website, opening_hours, cuisine, description_md,
                                 dist_walk_m, walk_min, dist_drive_m, drive_min,
                                 source, source_ref, fetched_at, status)
               VALUES (%(pid)s, %(cat)s, %(name)s,
                       ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                       %(address)s, %(phone)s, %(website)s, %(opening_hours)s,
                       %(cuisine)s, %(description_md)s,
                       %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
                       %(source)s, %(source_ref)s, now(), 'suggested')
               ON CONFLICT (property_id, source, source_ref)
               WHERE source_ref IS NOT NULL
               DO UPDATE SET
                   name = EXCLUDED.name, geom = EXCLUDED.geom,
                   address = EXCLUDED.address, phone = EXCLUDED.phone,
                   website = EXCLUDED.website, opening_hours = EXCLUDED.opening_hours,
                   cuisine = COALESCE(EXCLUDED.cuisine, pois.cuisine),
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
                "cuisine": p.get("cuisine"),
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
             AND fact_type IN ('emergency_numbers', 'waste_rules', 'noise_rules')
             AND fetched_at > now() - make_interval(days => %s)""",
        (country_code, admin_area, max_age_days),
    ).fetchone()
    return row["n"] >= 3


def get_area_fact(conn, country_code: str, admin_area: str | None,
                  fact_type: str) -> dict | None:
    """Contenu d'un `area_fact` précis pour (pays, commune) — lecture pour la
    matérialisation (V2-07 volet 3 : les marchés sont matérialisés par logement
    depuis le fait mutualisé de leur commune). None si absent."""
    row = conn.execute(
        """SELECT content FROM area_facts
           WHERE country_code = %s AND admin_area IS NOT DISTINCT FROM %s
             AND fact_type = %s""",
        (country_code, admin_area, fact_type),
    ).fetchone()
    return row["content"] if row else None


def area_fact_fresh(conn, country_code: str, admin_area: str | None,
                    fact_type: str, max_age_days: int) -> bool:
    """True si CE fact_type existe déjà et est récent pour (pays, commune) — cadence
    de rafraîchissement PROPRE, indépendante des 3 area_facts historiques (V2-07 :
    la livraison de repas a sa propre fenêtre de validité). Mutualisation : deux
    logements d'une même commune partagent le résultat, aucun nouvel appel dans la
    fenêtre. Une ligne existante (même à liste vide) suffit à couper l'appel."""
    row = conn.execute(
        """SELECT 1 FROM area_facts
           WHERE country_code = %s AND admin_area IS NOT DISTINCT FROM %s
             AND fact_type = %s
             AND fetched_at > now() - make_interval(days => %s)""",
        (country_code, admin_area, fact_type, max_age_days),
    ).fetchone()
    return row is not None


# ── Complétion des fiches de service (V2-07 volet 2) ─────────────────────────

def category_label_fr(conn, category: str) -> str:
    """Libellé français d'une catégorie (pour le prompt de complétion)."""
    row = conn.execute(
        "SELECT name_i18n FROM poi_categories WHERE code = %s", (category,)
    ).fetchone()
    if not row:
        return category
    n = row["name_i18n"] or {}
    return n.get("fr") or n.get("en") or category


def pois_needing_completion(conn, property_id: str, category: str,
                            fields: tuple[str, ...],
                            max_age_days: int) -> list[dict]:
    """POI RETENUS (approved/edited) d'une catégorie auxquels il MANQUE au moins un
    champ du périmètre (`fields` ⊆ {phone, website, opening_hours}), et non revérifiés
    récemment (marqueur `completion_meta->>'_checked_on'`, cadence propre → jamais de
    re-appel en boucle). Chaque ligne porte `missing` = les champs NULL du périmètre.

    `fields` est CODE-CONTRÔLÉ (constantes du périmètre) → interpolé sans risque.
    Ne remonte JAMAIS un POI 'suggested'/'rejected' : la complétion ne vise que ce
    que le propriétaire a retenu (invariant du volet : compléter, jamais écraser)."""
    safe = [f for f in fields if f in ("phone", "website", "opening_hours")]
    if not safe:
        return []
    null_clause = " OR ".join(f"{f} IS NULL" for f in safe)
    rows = conn.execute(
        f"""SELECT id, name, address, phone, website, opening_hours
            FROM pois
            WHERE property_id = %s AND category_code = %s
              AND status IN ('approved', 'edited')
              AND ({null_clause})
              AND (completion_meta->>'_checked_on' IS NULL
                   OR (completion_meta->>'_checked_on')::date
                        < (now()::date - make_interval(days => %s)))""",
        (property_id, category, max_age_days),
    ).fetchall()
    out = []
    for r in rows:
        missing = [f for f in safe if r[f] is None]
        if missing:
            out.append({"id": str(r["id"]), "name": r["name"],
                        "address": r["address"], "missing": missing})
    return out


def apply_poi_completion(conn, poi_id: str, fields: dict, source_url: str,
                         verified_on: str, checked_on: str) -> int:
    """COMPLÈTE un POI retenu : ne remplit que les champs NULL (COALESCE — jamais
    d'écrasement d'une saisie propriétaire), sans toucher au `status` ni au `source`
    (ce n'est pas une édition propriétaire). La provenance par champ (source_url +
    date) et le marqueur `_checked_on` vont dans `completion_meta`. Retourne le
    nombre de POI modifiés (0 si aucun champ n'était NULL — course bénigne).

    Garde-fou : n'agit QUE sur les POI approved/edited ; `fields` est restreint aux
    trois colonnes du périmètre."""
    cols = {f: v for f, v in fields.items()
            if f in ("phone", "website", "opening_hours")}
    meta = {f: {"source_url": source_url, "verified_on": verified_on} for f in cols}
    meta["_checked_on"] = checked_on
    set_parts = [f"{f} = COALESCE({f}, %({f})s)" for f in cols]
    set_parts.append("completion_meta = COALESCE(completion_meta, '{}'::jsonb) || %(meta)s")
    set_parts.append("updated_at = now()")
    params = dict(cols)
    params["meta"] = json.dumps(meta)
    params["pid"] = poi_id
    cur = conn.execute(
        f"UPDATE pois SET {', '.join(set_parts)} "
        "WHERE id = %(pid)s AND status IN ('approved', 'edited')",
        params,
    )
    return cur.rowcount


def mark_pois_checked(conn, poi_ids: list[str], checked_on: str) -> None:
    """Marque des POI retenus « revérifiés le {checked_on} » sans rien remplir
    (champs restés introuvables) → pas de re-appel avant l'échéance suivante."""
    if not poi_ids:
        return
    conn.execute(
        """UPDATE pois
           SET completion_meta = COALESCE(completion_meta, '{}'::jsonb) || %s
           WHERE id = ANY(%s) AND status IN ('approved', 'edited')""",
        (json.dumps({"_checked_on": checked_on}), list(poi_ids)),
    )


def insert_service_poi(conn, property_id: str, category: str, name: str,
                       lat: float, lon: float, *, phone: str | None,
                       website: str | None, source_ref: str,
                       completion_meta: dict | None = None) -> int:
    """Crée un POI de SERVICE issu de Claude+web (baby-sitting, V2-07 volet 2) :
    `source='claude'`, `status='suggested'` (validation propriétaire comme tout le
    pipeline). Idempotent par (property_id, source, source_ref) → un ré-enrichissement
    ne duplique pas ; ne réécrit QUE si la fiche est encore 'suggested' (invariant 1).
    La position vaut celle du logement (service TÉLÉPHONIQUE, pas une destination) —
    le propriétaire peut la préciser. Retourne 1 si inséré, 0 si conflit ignoré."""
    cur = conn.execute(
        """INSERT INTO pois (property_id, category_code, name, geom, phone, website,
                             completion_meta, source, source_ref, fetched_at, status)
           VALUES (%(pid)s, %(cat)s, %(name)s,
                   ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                   %(phone)s, %(website)s, %(meta)s, 'claude', %(ref)s, now(), 'suggested')
           ON CONFLICT (property_id, source, source_ref) WHERE source_ref IS NOT NULL
           DO UPDATE SET name = EXCLUDED.name, phone = EXCLUDED.phone,
                         website = EXCLUDED.website,
                         completion_meta = EXCLUDED.completion_meta, fetched_at = now()
           WHERE pois.status = 'suggested'""",
        {"pid": property_id, "cat": category, "name": name, "lat": lat, "lon": lon,
         "phone": phone, "website": website,
         "meta": json.dumps(completion_meta) if completion_meta else None,
         "ref": source_ref},
    )
    return cur.rowcount


def existing_market_pois(conn, property_id: str) -> list[dict]:
    """POI `market` du logement — TOUS statuts (V2-07 volet 3, déduplication) :
    un marché déjà présent (`edited` du propriétaire) n'est jamais recréé, un
    `rejected` ne ressuscite jamais. Position en lat/lon pour le rapprochement."""
    return conn.execute(
        """SELECT name, weekday, ST_Y(geom) AS lat, ST_X(geom) AS lon, status
           FROM pois WHERE property_id = %s AND category_code = 'market'""",
        (property_id,),
    ).fetchall()


def insert_market_poi(conn, property_id: str, market: dict) -> int:
    """Crée un POI `market` issu de Claude+web (V2-07 volet 3) : `source='claude'`,
    `status='suggested'` (validation propriétaire), avec `weekday`/`weekday_note`
    (V2-33), position RÉELLE et distances pré-calculées, preuve en `completion_meta`.
    Idempotent par (property_id, source, source_ref) ; ne réécrit QUE si encore
    'suggested' (invariant 1). Retourne 1 si inséré, 0 si conflit ignoré."""
    cur = conn.execute(
        """INSERT INTO pois (property_id, category_code, name, geom, address,
                             weekday, weekday_note, dist_walk_m, walk_min,
                             dist_drive_m, drive_min, completion_meta,
                             source, source_ref, fetched_at, status)
           VALUES (%(pid)s, 'market', %(name)s,
                   ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                   %(address)s, %(weekday)s, %(weekday_note)s,
                   %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
                   %(meta)s, 'claude', %(ref)s, now(), 'suggested')
           ON CONFLICT (property_id, source, source_ref) WHERE source_ref IS NOT NULL
           DO UPDATE SET name = EXCLUDED.name, geom = EXCLUDED.geom,
                         address = EXCLUDED.address, weekday = EXCLUDED.weekday,
                         weekday_note = EXCLUDED.weekday_note,
                         dist_walk_m = EXCLUDED.dist_walk_m, walk_min = EXCLUDED.walk_min,
                         dist_drive_m = EXCLUDED.dist_drive_m, drive_min = EXCLUDED.drive_min,
                         completion_meta = EXCLUDED.completion_meta, fetched_at = now()
           WHERE pois.status = 'suggested'""",
        {"pid": property_id, "name": market["name"],
         "lat": market["lat"], "lon": market["lon"],
         "address": market.get("address"), "weekday": market["weekday"],
         "weekday_note": market.get("weekday_note"),
         "dist_walk_m": market.get("dist_walk_m"), "walk_min": market.get("walk_min"),
         "dist_drive_m": market.get("dist_drive_m"), "drive_min": market.get("drive_min"),
         "meta": json.dumps(market["completion_meta"]) if market.get("completion_meta")
                 else None,
         "ref": market["source_ref"]},
    )
    return cur.rowcount


def poi_source_ref_exists(conn, property_id: str, source_ref: str) -> bool:
    """True si un POI de ce (logement, source_ref) existe déjà (TOUS statuts) →
    idempotence AVANT géocodage (on ne re-géocode pas un marché déjà matérialisé)."""
    return conn.execute(
        "SELECT 1 FROM pois WHERE property_id = %s AND source = 'claude' "
        "AND source_ref = %s LIMIT 1", (property_id, source_ref),
    ).fetchone() is not None


def recent_operation(conn, property_id: str, operation: str,
                     max_age_days: int) -> bool:
    """True si une opération `operation` (api_costs) a été enregistrée pour ce
    logement dans la fenêtre — sert de mémoire « on a déjà cherché » (baby-sitting :
    un vide est un résultat valide qu'on ne re-cherche pas à chaque run)."""
    row = conn.execute(
        """SELECT 1 FROM api_costs
           WHERE property_id = %s AND operation = %s
             AND created_at > now() - make_interval(days => %s)
           LIMIT 1""",
        (property_id, operation, max_age_days),
    ).fetchone()
    return row is not None


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


def published_language_codes(conn) -> list[str]:
    """Codes des langues PUBLIÉES du registre (`languages`, V2-21a), ordonnés par
    `sort_order`. Source unique des cibles de traduction quand aucune n'est
    imposée par l'appelant (chemin CLI) — plus de liste MVP en dur."""
    rows = conn.execute(
        "SELECT code FROM languages WHERE status = 'published' ORDER BY sort_order, code"
    ).fetchall()
    return [r["code"] for r in rows]


def set_published_langs(conn, property_id: str, langs: list[str]) -> None:
    """Publie la liste des langues traduites disponibles (pilote le sélecteur du
    guide). N'inclut jamais la langue source (déduite au rendu)."""
    conn.execute(
        "UPDATE properties SET published_langs = %s WHERE id = %s",
        (list(langs), property_id),
    )
