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
                  region, country_code, default_lang, guest_guide,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon, geocode_source
           FROM properties WHERE id = %s""",
        (property_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"Logement introuvable : {property_id}")
    return row


def load_categories(conn) -> list[dict]:
    # `max_radius_m` (V2-44) : rayon maximal de complétion en rural. NULL = pas
    # d'escalade (l'appelant retombe sur `default_radius_m`).
    return conn.execute(
        "SELECT code, default_radius_m, max_radius_m FROM poi_categories ORDER BY code"
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
    """Insère/actualise les POI suggérés. Ne touche JAMAIS le contenu d'une fiche
    déjà arbitrée (invariant 1) — À UNE EXCEPTION, gravée et étroite (V2-38bis) : la
    seule colonne `locality` est complétée quel que soit le statut.

    **Pourquoi cette exception ne viole pas l'invariant 1.** Le guide n'affiche que
    les fiches RETENUES (approved/edited) ; or l'upsert ne mettait à jour que les
    `suggested`, si bien qu'AUCUNE localité (V2-38) n'atteignait jamais le guide
    (re-run Ardon 18/08 : 0 localité affichée). `locality` est une **métadonnée**
    (jamais du contenu rédigé), issue de la **même source OSM** (même `source_ref`) :
    la compléter ne « réenrichit » pas la fiche, ça remplit un trou. La forme
    `COALESCE(pois.locality, EXCLUDED.locality)` pour les fiches retenues **interdit
    par construction tout écrasement** — une localité saisie ou éditée est conservée,
    seul un NULL est comblé. **Tous les autres champs** (nom, description, téléphone,
    catégorie, coup de cœur, distances, `fetched_at`…) restent STRICTEMENT hors
    upsert pour une fiche retenue (self-assignment `= pois.x`).

    Structure : plus de `WHERE pois.status = 'suggested'` global (il empêchait la
    branche retenue) ; chaque champ est gardé par un `CASE` sur le statut — seule
    `locality` a une branche « retenue » non triviale (fill-NULL-only)."""
    n = 0
    for p in pois:
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 locality, phone, website, opening_hours, cuisine, subtype,
                                 description_md, owner_comment, completion_meta,
                                 dist_walk_m, walk_min, dist_drive_m, drive_min,
                                 source, source_ref, fetched_at, status)
               VALUES (%(pid)s, %(cat)s, %(name)s,
                       ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                       %(address)s, %(locality)s, %(phone)s, %(website)s,
                       %(opening_hours)s, %(cuisine)s, %(subtype)s, %(description_md)s,
                       %(owner_comment)s, %(meta)s,
                       %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
                       %(source)s, %(source_ref)s, now(), 'suggested')
               ON CONFLICT (property_id, source, source_ref)
               WHERE source_ref IS NOT NULL
               DO UPDATE SET
                   -- SEULE exception à l'invariant 1 (V2-38bis) : la localité est
                   -- complétée quel que soit le statut. Fiche retenue → fill-NULL-only
                   -- (COALESCE(pois.locality, …) : jamais d'écrasement) ; suggested →
                   -- rafraîchie par OSM sans jamais s'effacer (COALESCE(EXCLUDED, …)).
                   locality = CASE
                       WHEN pois.status = 'suggested'
                           THEN COALESCE(EXCLUDED.locality, pois.locality)
                       ELSE COALESCE(pois.locality, EXCLUDED.locality)
                   END,
                   -- Tout le reste : rafraîchi UNIQUEMENT pour les suggested
                   -- (invariant 1) ; self-assignment (no-op) pour approved/edited.
                   name = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.name ELSE pois.name END,
                   geom = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.geom ELSE pois.geom END,
                   address = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.address ELSE pois.address END,
                   phone = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.phone ELSE pois.phone END,
                   website = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.website ELSE pois.website END,
                   opening_hours = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.opening_hours ELSE pois.opening_hours END,
                   cuisine = CASE WHEN pois.status = 'suggested' THEN COALESCE(EXCLUDED.cuisine, pois.cuisine) ELSE pois.cuisine END,
                   subtype = CASE WHEN pois.status = 'suggested' THEN COALESCE(EXCLUDED.subtype, pois.subtype) ELSE pois.subtype END,
                   description_md = CASE WHEN pois.status = 'suggested' THEN COALESCE(EXCLUDED.description_md, pois.description_md) ELSE pois.description_md END,
                   -- Coup de cœur / raison éditoriale (V2-56) : porté par l'enrichissement
                   -- pour les picks « réputés » (source='web'). Complété sans s'effacer
                   -- pour un suggested ; intouché pour une fiche arbitrée (invariant 1 —
                   -- un coup de cœur SAISI par le propriétaire survit).
                   owner_comment = CASE WHEN pois.status = 'suggested' THEN COALESCE(EXCLUDED.owner_comment, pois.owner_comment) ELSE pois.owner_comment END,
                   -- completion_meta : FUSION (jamais un remplacement) pour préserver
                   -- ce que les étapes suivantes y accumulent (`_judge`, `_web`,
                   -- `_nearest_of_network`, complétions). Un suggested prend les clés
                   -- de la moisson (EXCLUDED gagne). Une fiche RETENUE ne se fait
                   -- COMPLÉTER (jamais écraser) que les clés ABSENTES (pois gagne) —
                   -- même exception étroite que `locality` (V2-38bis) : le nom LOCAL
                   -- V2-66 (`_name_local`, métadonnée OSM) doit atteindre le guide, qui
                   -- n'affiche que les fiches retenues, sans jamais toucher au contenu.
                   -- `NULLIF(…, '{}')` : rien des deux côtés → reste NULL (aucune
                   -- pollution de la colonne, comportement historique préservé).
                   completion_meta = CASE
                       WHEN pois.status = 'suggested'
                           THEN NULLIF(COALESCE(pois.completion_meta, '{}'::jsonb)
                                || COALESCE(EXCLUDED.completion_meta, '{}'::jsonb),
                                '{}'::jsonb)
                       ELSE NULLIF(COALESCE(EXCLUDED.completion_meta, '{}'::jsonb)
                                || COALESCE(pois.completion_meta, '{}'::jsonb),
                                '{}'::jsonb)
                   END,
                   dist_walk_m = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.dist_walk_m ELSE pois.dist_walk_m END,
                   walk_min = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.walk_min ELSE pois.walk_min END,
                   dist_drive_m = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.dist_drive_m ELSE pois.dist_drive_m END,
                   drive_min = CASE WHEN pois.status = 'suggested' THEN EXCLUDED.drive_min ELSE pois.drive_min END,
                   fetched_at = CASE WHEN pois.status = 'suggested' THEN now() ELSE pois.fetched_at END""",
            {
                "pid": property_id, "cat": category,
                "name": p["name"], "lat": p["lat"], "lon": p["lon"],
                "address": p.get("address"), "locality": p.get("locality"),
                "phone": p.get("phone"),
                "website": p.get("website"), "opening_hours": p.get("opening_hours"),
                "cuisine": p.get("cuisine"), "subtype": p.get("subtype"),
                "description_md": p.get("description_md"),
                "owner_comment": p.get("owner_comment"),
                "dist_walk_m": p.get("dist_walk_m"), "walk_min": p.get("walk_min"),
                "dist_drive_m": p.get("dist_drive_m"), "drive_min": p.get("drive_min"),
                "meta": json.dumps(p["completion_meta"]) if p.get("completion_meta")
                        else None,
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


# ── Juge IA & auto-publication (offre « Guide Voyageur », V2-54) ──────────────

def load_pois_for_judge(conn, property_id: str) -> list[dict]:
    """POI encore à arbitrer (`status='suggested'`) d'un logement, avec les champs
    dont le prompt du juge a besoin. Seuls les 'suggested' sont chargés : les fiches
    déjà arbitrées (jamais le cas d'un guide guest neuf) ne sont pas re-jugées."""
    return conn.execute(
        """SELECT id::text AS id, name, category_code, address, locality,
                  walk_min, drive_min, source, description_md
           FROM pois
           WHERE property_id = %s AND status = 'suggested'
           ORDER BY category_code, name""", (property_id,)).fetchall()


def apply_judge_verdict(conn, poi_id: str, status: str, judge_meta: dict) -> int:
    """Arbitre un POI `suggested` d'après le verdict du juge (offre Guide Voyageur) :
    fixe `status` ('approved' ou 'rejected') et TRACE le motif dans
    `completion_meta._judge` (verdict, confiance, motif, seuil) — patron V2-07, aucun
    nouveau champ de schéma. N'agit QUE sur un POI encore `suggested` (idempotent, ne
    touche jamais une fiche déjà arbitrée)."""
    cur = conn.execute(
        """UPDATE pois
           SET status = %s,
               completion_meta = COALESCE(completion_meta, '{}'::jsonb) || %s,
               updated_at = now()
           WHERE id = %s AND status = 'suggested'""",
        (status, json.dumps({"_judge": judge_meta}), poi_id),
    )
    return cur.rowcount


def publish_property(conn, property_id: str) -> None:
    """Publie un logement (`status='published'`) — auto-publication en fin de pipeline
    pour un guide voyageur. `published_langs` est rempli séparément par la traduction."""
    conn.execute(
        "UPDATE properties SET status = 'published', updated_at = now() WHERE id = %s",
        (property_id,),
    )


# ── Mémoire de secteur des picks éditoriaux « sorties » (V2-56c) ──────────────

def upsert_editorial_pick(conn, *, country_code: str, city: str, city_norm: str,
                          name: str, name_norm: str, category: str,
                          reason: str | None, source_url: str | None,
                          verified_on: str | None, lat: float, lon: float,
                          phone: str | None, website: str | None,
                          locality: str | None, name_local: str | None = None) -> None:
    """Mémorise un pick éditorial POSITIONNÉ pour son secteur (pays+commune). Idempotent
    par (secteur, catégorie, nom normalisé) : un re-run rafraîchit position/contacts/
    raison (les plus récents gagnent) et `last_seen`. La connaissance du secteur
    s'accumule (V2-56c) — Casa Manolo ne disparaît plus."""
    conn.execute(
        """INSERT INTO editorial_picks
             (country_code, city, city_norm, name, name_norm, category, reason,
              source_url, verified_on, geom, phone, website, locality, name_local)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                   ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s)
           ON CONFLICT (country_code, city_norm, category, name_norm)
           DO UPDATE SET
               reason      = COALESCE(EXCLUDED.reason, editorial_picks.reason),
               source_url  = COALESCE(EXCLUDED.source_url, editorial_picks.source_url),
               verified_on = COALESCE(EXCLUDED.verified_on, editorial_picks.verified_on),
               geom        = EXCLUDED.geom,
               phone       = COALESCE(EXCLUDED.phone, editorial_picks.phone),
               website     = COALESCE(EXCLUDED.website, editorial_picks.website),
               locality    = COALESCE(EXCLUDED.locality, editorial_picks.locality),
               name_local  = COALESCE(EXCLUDED.name_local, editorial_picks.name_local),
               last_seen   = now()""",
        (country_code.upper(), city, city_norm, name, name_norm, category, reason,
         source_url, verified_on, lon, lat, phone, website, locality, name_local),
    )


def sector_editorial_picks(conn, country_code: str, city_norm: str, category: str,
                           max_age_days: int) -> list[dict]:
    """Picks éditoriaux mémorisés du secteur pour une catégorie, récents (< max_age) —
    l'UNION que la fusion consomme (V2-56c). Déjà positionnés (geom fiable)."""
    return conn.execute(
        """SELECT name, category, reason, source_url, verified_on,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon, phone, website, locality,
                  name_local
           FROM editorial_picks
           WHERE country_code = %s AND city_norm = %s AND category = %s
             AND last_seen > now() - make_interval(days => %s)
           ORDER BY last_seen DESC""",
        (country_code.upper(), city_norm, category, max_age_days),
    ).fetchall()


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


def existing_pois_for_dedup(conn, property_id: str, category: str) -> list[dict]:
    """POI déjà ARBITRÉS (approved/edited/rejected) d'une catégorie, pour le
    dédoublonnage à la suggestion (V2-40). Les `suggested` sont exclus : ils
    n'existent pas encore, ou seront ré-upsertés à l'identique par `source_ref`.
    Position en lat/lon + statut (la règle diffère selon retenue/rejetée) + le
    `source_ref` (le dédoublonnage ne retire QUE les doublons sous un AUTRE
    source_ref : la MÊME fiche re-moissonnée passe par l'upsert, idempotent et
    respectueux du statut — invariant 1, V2-38bis pour la localité)."""
    return conn.execute(
        """SELECT name, ST_Y(geom) AS lat, ST_X(geom) AS lon, status, source_ref
           FROM pois
           WHERE property_id = %s AND category_code = %s
             AND status IN ('approved', 'edited', 'rejected')""",
        (property_id, category),
    ).fetchall()


def existing_suggested_pois(conn, property_id: str, category: str) -> list[dict]:
    """POI encore SUGGESTED d'une catégorie, avec assez de champs pour comparer leur
    « renseignement » (V2-44 volet 2, réconciliation OSM/web). Sert à éviter un DOUBLON
    INTER-RUN : quand la fusion OSM/web fait basculer le source_ref gagnant d'un run à
    l'autre, une même place pourrait laisser deux fiches suggested sous des source_ref
    différents. On ne renvoie QUE du suggested (jamais l'arbitré — invariant 1)."""
    return conn.execute(
        """SELECT id, name, ST_Y(geom) AS lat, ST_X(geom) AS lon, source, source_ref,
                  phone, website, opening_hours, cuisine, locality, walk_min, drive_min
           FROM pois
           WHERE property_id = %s AND category_code = %s AND status = 'suggested'""",
        (property_id, category),
    ).fetchall()


def delete_pois(conn, ids: list[str]) -> int:
    """Supprime des POI par id (V2-44 volet 2 : fiches suggested rendues obsolètes par
    la réconciliation OSM/web). Ne supprime QUE ce que l'appelant a filtré (jamais de
    fiche arbitrée). Retourne le nombre supprimé."""
    if not ids:
        return 0
    cur = conn.execute("DELETE FROM pois WHERE id = ANY(%s)", (list(ids),))
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


def insert_local_commerce_poi(conn, property_id: str, poi: dict) -> int:
    """Crée un POI de COMMERCE/SERVICE de village issu de Claude+web (V2-74) : catégorie
    ESSENTIELLE réelle (pharmacy/supermarket/bakery/doctor/post_office/tobacco — V2-77),
    `status='suggested'` (validation propriétaire), position RÉELLE (adresse géocodée) +
    distances pré-calculées + `locality` (commune, honnêteté de la distance V2-38) + preuve
    en `completion_meta`. Idempotent par (property_id, source, source_ref) ; ne réécrit QUE si
    encore 'suggested' (invariant 1). Retourne 1 si inséré, 0 si conflit ignoré."""
    cur = conn.execute(
        """INSERT INTO pois (property_id, category_code, name, geom, address, locality,
                             phone, website, dist_walk_m, walk_min,
                             dist_drive_m, drive_min,
                             completion_meta, subtype, source, source_ref,
                             fetched_at, status)
           VALUES (%(pid)s, %(cat)s, %(name)s,
                   ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                   %(address)s, %(locality)s, %(phone)s, %(website)s,
                   %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
                   %(meta)s, %(subtype)s, 'claude', %(ref)s, now(),
                   'suggested')
           ON CONFLICT (property_id, source, source_ref) WHERE source_ref IS NOT NULL
           DO UPDATE SET name = EXCLUDED.name, geom = EXCLUDED.geom,
                         address = EXCLUDED.address, locality = EXCLUDED.locality,
                         phone = EXCLUDED.phone,
                         website = COALESCE(EXCLUDED.website, pois.website),
                         dist_walk_m = EXCLUDED.dist_walk_m, walk_min = EXCLUDED.walk_min,
                         dist_drive_m = EXCLUDED.dist_drive_m, drive_min = EXCLUDED.drive_min,
                         completion_meta = EXCLUDED.completion_meta,
                         subtype = COALESCE(EXCLUDED.subtype, pois.subtype),
                         fetched_at = now()
           WHERE pois.status = 'suggested'""",
        {"pid": property_id, "cat": poi["category"], "name": poi["name"],
         "lat": poi["lat"], "lon": poi["lon"], "address": poi.get("address"),
         "locality": poi.get("locality"), "phone": poi.get("phone"),
         "website": poi.get("website"),
         "dist_walk_m": poi.get("dist_walk_m"), "walk_min": poi.get("walk_min"),
         "dist_drive_m": poi.get("dist_drive_m"), "drive_min": poi.get("drive_min"),
         "meta": json.dumps(poi["completion_meta"]) if poi.get("completion_meta") else None,
         "subtype": poi.get("subtype"), "ref": poi["source_ref"]},
    )
    return cur.rowcount



def set_poi_subtype_by_name(conn, property_id: str, category: str, name: str,
                            subtype: str, meta_key: str, meta: dict,
                            fill: dict | None = None) -> int:
    """Pose le SOUS-TYPE d'un POI déjà moissonné, identifié par (logement, catégorie, nom) —
    V2-77b : c'est ainsi que la passe web marque « cet établissement-ci EST un estanco » ou
    « ce bar-ci sert la chicha », sans créer de doublon (cascade V2-73g : on s'accroche au
    lieu déjà connu plutôt que d'en poser un second).

    **Exception étroite à l'invariant 1, du même régime EXACT que `locality` (V2-38bis)** :
    le sous-type est une MÉTADONNÉE, pas du contenu rédigé, et il est **complété si NULL**
    quel que soit le statut — `COALESCE(subtype, %(subtype)s)` rend l'écrasement impossible
    par construction (un sous-type tagué par OSM ou saisi par le propriétaire survit
    intact). Sans cela, un guide déjà arbitré — le cas d'Adeje, dont les POI sont
    `approved` — n'afficherait JAMAIS la puce, puisque le guide ne sert que les fiches
    retenues. La preuve est FUSIONNÉE dans `completion_meta` (jamais remplacée). Retourne
    le nombre de fiches touchées (0 si le nom ne correspond à aucune).

    `fill` (V2-77e) : coordonnées publiées par le lieu (`phone`, `website`) complétées au
    MÊME régime fill-NULL-only — un bar qu'OSM connaît sans téléphone gagne celui que
    l'établissement publie, sans qu'une valeur existante soit jamais touchée."""
    cur = conn.execute(
        """UPDATE pois
              SET subtype = COALESCE(subtype, %(subtype)s),
                  phone = COALESCE(phone, %(phone)s),
                  website = COALESCE(website, %(website)s),
                  completion_meta = COALESCE(completion_meta, '{}'::jsonb)
                                    || jsonb_build_object(%(key)s::text, %(meta)s::jsonb)
            WHERE property_id = %(pid)s AND category_code = %(cat)s AND name = %(name)s""",
        {"pid": property_id, "cat": category, "name": name, "subtype": subtype,
         "phone": (fill or {}).get("phone"), "website": (fill or {}).get("website"),
         "key": meta_key, "meta": json.dumps(meta)},
    )
    return cur.rowcount


def categories_with_pois(conn, property_id: str) -> set[str]:
    """Catégories réellement GARNIES pour ce logement, tous statuts (V2-77f).

    Le récapitulatif « catégories sans résultat » se calculait à la fin de la MOISSON, soit
    400 lignes avant les passes web (marchés, commerces de village, estancos, chicha) : une
    rubrique remplie par le web était donc annoncée VIDE. Défaut trompeur — il a fait
    chercher un problème inexistant sur le guide d'Adeje, dont la rubrique `shisha`
    contenait deux POI approuvés. La base est la seule source de vérité après coup."""
    rows = conn.execute(
        "SELECT DISTINCT category_code FROM pois WHERE property_id = %s",
        (property_id,)).fetchall()
    return {r["category_code"] for r in rows}

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


def record_costs(conn, property_id: str, job_id: str, provider: str,
                 operation: str, attempts: list[dict]) -> None:
    """Une ligne `api_costs` PAR essai d'appel (V2-07 volet 3bis) : un appel
    web_search régénéré après JSON invalide a été PAYÉ deux fois — l'argent est
    dépensé à la réponse, pas au succès. `attempts` = [{units, cost_cts}, …]."""
    for c in attempts or []:
        record_cost(conn, property_id, job_id, provider, operation,
                    c["units"], c["cost_cts"])


# ── Traductions du guide voyageur (M-09, §9) ─────────────────────────────────
# Lectures/écritures utilisées par le pipeline de traduction (tâche de fond,
# connexion propre). Ne concernent QUE les sections voyageur (audience='guest')
# et les POI retenus (approved/edited) — jamais les secrets ni le cahier staff.

def translatable_sections(conn, property_id: str) -> list[dict]:
    """Sections voyageur instanciées d'un logement (avec leur field_schema et
    leur contenu source) candidates à la traduction."""
    return conn.execute(
        """SELECT ps.id AS section_id, t.field_schema, ps.content, ps.body_md,
                  ps.title_override
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
                               content: dict, body_md: str | None,
                               title_override: str | None = None) -> None:
    """Écrit une traduction de section (is_stale=FALSE : fraîche par définition).
    `title_override` (V2-42) : titre de rubrique traduit, motif de `body_md`."""
    conn.execute(
        """INSERT INTO section_translations (section_id, lang, content, body_md,
                                             title_override, is_stale, updated_at)
           VALUES (%s, %s, %s, %s, %s, FALSE, now())
           ON CONFLICT (section_id, lang) DO UPDATE SET
               content = EXCLUDED.content, body_md = EXCLUDED.body_md,
               title_override = EXCLUDED.title_override,
               is_stale = FALSE, updated_at = now()""",
        (section_id, lang, json.dumps(content), body_md, title_override),
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
