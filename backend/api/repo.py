"""Accès aux données de l'API (psycopg 3).

Toutes les fonctions portant sur un logement prennent `owner_id` et filtrent
dessus : c'est ici que se joue l'isolation multi-tenant (§7 du CdC). Les
routers ne construisent jamais de SQL eux-mêmes.

Les distances des POI sont lues telles quelles depuis la base (pré-calculées
par le pipeline) : aucun calcul géographique ni appel externe côté lecture
(invariant 4).
"""
from __future__ import annotations

import json
from typing import Any

# Colonnes publiques d'un logement (jamais de secrets ici)
_PROP_COLS = """
    id, name, address_line1, address_line2, postal_code, city, region,
    country_code, ST_Y(geom) AS lat, ST_X(geom) AS lon,
    geocode_source, geocode_accuracy, guide_token, staff_token, access_mode, status,
    default_lang, published_langs, contact_name, contact_phone,
    contact_whatsapp, contact_email, contact_backup, tourism_license,
    created_at, updated_at
"""


# ── Comptes ──────────────────────────────────────────────────────────────────

def get_owner_by_email(conn, email: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM owners WHERE lower(email) = lower(%s)", (email,)
    ).fetchone()


def get_owner(conn, owner_id: str) -> dict | None:
    return conn.execute(
        """SELECT o.id, o.email, o.full_name, o.company_name, o.phone, o.locale,
                  o.is_active, o.email_verified, o.password_hash,
                  (SELECT plan_id FROM subscriptions s WHERE s.owner_id = o.id
                   ORDER BY created_at DESC LIMIT 1) AS plan_id
           FROM owners o WHERE o.id = %s""",
        (owner_id,),
    ).fetchone()


def create_owner(conn, *, email: str, password_hash: str, full_name: str,
                 company_name: str | None, phone: str | None,
                 locale: str) -> dict:
    return conn.execute(
        """INSERT INTO owners (email, password_hash, full_name, company_name,
                               phone, locale)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (email, password_hash, full_name, company_name, phone, locale),
    ).fetchone()


def create_subscription(conn, owner_id: str, plan_id: str) -> None:
    conn.execute(
        """INSERT INTO subscriptions (owner_id, plan_id, status)
           VALUES (%s, %s, 'trialing')""",
        (owner_id, plan_id),
    )


def set_owner_password(conn, owner_id: str, password_hash: str) -> None:
    """Remplace le hash de mot de passe (réinitialisation, V2-08)."""
    conn.execute(
        "UPDATE owners SET password_hash = %s, updated_at = now() WHERE id = %s",
        (password_hash, owner_id),
    )


def set_owner_email_verified(conn, owner_id: str) -> None:
    """Marque l'email du propriétaire comme vérifié (V2-08)."""
    conn.execute(
        "UPDATE owners SET email_verified = TRUE, updated_at = now() WHERE id = %s",
        (owner_id,),
    )


# ── Jetons transactionnels : réinitialisation / vérification (V2-08) ─────────

def create_auth_token(conn, owner_id: str, token_hash: str, purpose: str,
                      expires_at) -> None:
    """Enregistre l'empreinte d'un jeton à usage unique (jamais le jeton en clair)."""
    conn.execute(
        """INSERT INTO password_resets (owner_id, token_hash, purpose, expires_at)
           VALUES (%s, %s, %s, %s)""",
        (owner_id, token_hash, purpose, expires_at),
    )


def get_auth_token(conn, token_hash: str, purpose: str) -> dict | None:
    """Ligne du jeton pour cette empreinte + usage (incl. used_at / expires_at).
    Le contrôle d'expiration / usage unique est fait par l'appelant."""
    return conn.execute(
        """SELECT id, owner_id, purpose, expires_at, used_at
           FROM password_resets WHERE token_hash = %s AND purpose = %s""",
        (token_hash, purpose),
    ).fetchone()


def mark_auth_token_used(conn, token_id: str) -> None:
    conn.execute(
        "UPDATE password_resets SET used_at = now() WHERE id = %s AND used_at IS NULL",
        (token_id,),
    )


def invalidate_owner_tokens(conn, owner_id: str, purpose: str) -> None:
    """Consomme tous les jetons encore valides du propriétaire pour cet usage
    (après une réinitialisation réussie : plus aucun ancien lien n'est utilisable)."""
    conn.execute(
        """UPDATE password_resets SET used_at = now()
           WHERE owner_id = %s AND purpose = %s AND used_at IS NULL""",
        (owner_id, purpose),
    )


def recent_auth_token(conn, owner_id: str, purpose: str, since) -> bool:
    """Vrai si un jeton de cet usage a été créé depuis `since` (cadence anti-spam)."""
    row = conn.execute(
        """SELECT 1 FROM password_resets
           WHERE owner_id = %s AND purpose = %s AND created_at >= %s LIMIT 1""",
        (owner_id, purpose, since),
    ).fetchone()
    return row is not None


def get_owner_plan(conn, owner_id: str) -> dict | None:
    """Plan courant du propriétaire (quotas, limites)."""
    return conn.execute(
        """SELECT p.* FROM plans p
           JOIN subscriptions s ON s.plan_id = p.id
           WHERE s.owner_id = %s
           ORDER BY s.created_at DESC LIMIT 1""",
        (owner_id,),
    ).fetchone()


# ── Logements ────────────────────────────────────────────────────────────────

def list_properties(conn, owner_id: str) -> list[dict]:
    return conn.execute(
        f"SELECT {_PROP_COLS} FROM properties WHERE owner_id = %s "
        "ORDER BY created_at",
        (owner_id,),
    ).fetchall()


def count_properties(conn, owner_id: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM properties WHERE owner_id = %s", (owner_id,)
    ).fetchone()["n"]


def get_owned_property(conn, owner_id: str, property_id: str) -> dict | None:
    """Charge un logement en vérifiant l'appartenance. None si absent ou étranger."""
    return conn.execute(
        f"SELECT {_PROP_COLS} FROM properties WHERE id = %s AND owner_id = %s",
        (property_id, owner_id),
    ).fetchone()


def create_property(conn, owner_id: str, data: dict) -> dict:
    cols = ["owner_id", "name", "address_line1", "address_line2", "postal_code",
            "city", "region", "country_code", "default_lang", "contact_name",
            "contact_phone", "contact_whatsapp", "contact_email",
            "contact_backup", "tourism_license"]
    values = [owner_id] + [data.get(c) for c in cols[1:]]
    placeholders = ", ".join(["%s"] * len(cols))
    row = conn.execute(
        f"INSERT INTO properties ({', '.join(cols)}) VALUES ({placeholders}) "
        f"RETURNING {_PROP_COLS}",
        values,
    ).fetchone()
    return row


# Champs simples modifiables via PATCH (hors lat/lon traités à part)
_UPDATABLE = (
    "name", "address_line1", "address_line2", "postal_code", "city", "region",
    "country_code", "default_lang", "access_mode", "status", "contact_name",
    "contact_phone", "contact_whatsapp", "contact_email", "contact_backup",
    "tourism_license",
)


def update_property(conn, owner_id: str, property_id: str,
                    fields: dict) -> dict | None:
    sets, params = [], []
    for key in _UPDATABLE:
        if key in fields:
            sets.append(f"{key} = %s")
            params.append(fields[key])
    # Placement manuel du point (le propriétaire corrige le géocodage)
    if fields.get("lat") is not None and fields.get("lon") is not None:
        sets.append("geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)")
        params.extend([fields["lon"], fields["lat"]])
        sets.append("geocode_source = 'manual'")
        sets.append("geocode_accuracy = 'rooftop'")
    if not sets:
        return get_owned_property(conn, owner_id, property_id)
    params.extend([property_id, owner_id])
    return conn.execute(
        f"UPDATE properties SET {', '.join(sets)} "
        f"WHERE id = %s AND owner_id = %s RETURNING {_PROP_COLS}",
        params,
    ).fetchone()


def set_geocode(conn, owner_id: str, property_id: str, *, lat: float,
                lon: float, accuracy: str, source: str = "nominatim") -> dict | None:
    """Repositionne le logement depuis un (re)géocodage explicite (M-24).

    À la différence du placement manuel (`update_property`), `geocode_source`
    n'est PAS 'manual' : c'est une position issue de l'adresse, que le
    propriétaire pourra encore ajuster à la main ensuite. N'est jamais appelé
    automatiquement (invariant : une position 'manual' n'est écrasée qu'à la
    demande explicite du propriétaire)."""
    return conn.execute(
        f"""UPDATE properties
            SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                geocode_source = %s, geocode_accuracy = %s
            WHERE id = %s AND owner_id = %s RETURNING {_PROP_COLS}""",
        (lon, lat, source, accuracy, property_id, owner_id),
    ).fetchone()


def delete_property(conn, owner_id: str, property_id: str) -> bool:
    row = conn.execute(
        "DELETE FROM properties WHERE id = %s AND owner_id = %s RETURNING id",
        (property_id, owner_id),
    ).fetchone()
    return row is not None


# ── Données sensibles (chiffrées) ────────────────────────────────────────────

def upsert_secrets(conn, property_id: str, *, wifi_ssid: str | None,
                   wifi_pass_enc: bytes | None, wifi_networks_enc: bytes | None,
                   keybox_code_enc: bytes | None,
                   keybox_notes: str | None) -> None:
    """Écrit les secrets. `wifi_networks_enc` porte la liste multi-réseaux (M-15) ;
    `wifi_ssid`/`wifi_pass_enc` restent en **miroir du réseau n°1** (rétrocompat)."""
    conn.execute(
        """INSERT INTO property_secrets (property_id, wifi_ssid, wifi_pass_enc,
                                         wifi_networks_enc,
                                         keybox_code_enc, keybox_notes, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (property_id) DO UPDATE SET
               wifi_ssid = EXCLUDED.wifi_ssid,
               wifi_pass_enc = EXCLUDED.wifi_pass_enc,
               wifi_networks_enc = EXCLUDED.wifi_networks_enc,
               keybox_code_enc = EXCLUDED.keybox_code_enc,
               keybox_notes = EXCLUDED.keybox_notes,
               updated_at = now()""",
        (property_id, wifi_ssid, wifi_pass_enc, wifi_networks_enc,
         keybox_code_enc, keybox_notes),
    )


def get_secrets(conn, property_id: str) -> dict | None:
    return conn.execute(
        "SELECT wifi_ssid, wifi_pass_enc, wifi_networks_enc, "
        "keybox_code_enc, keybox_notes "
        "FROM property_secrets WHERE property_id = %s",
        (property_id,),
    ).fetchone()


# ── Sections ─────────────────────────────────────────────────────────────────

def list_sections_with_templates(conn, property_id: str) -> list[dict]:
    """Catalogue complet des sections + contenu déjà saisi pour ce logement."""
    return conn.execute(
        """SELECT t.code, t.chapter, t.sort_order, t.icon, t.name_i18n,
                  t.description_i18n, t.field_schema, t.ai_enrichable, t.is_sensitive,
                  t.audience,
                  ps.id AS section_id, ps.content, ps.body_md, ps.is_visible,
                  ps.completed
           FROM section_templates t
           LEFT JOIN property_sections ps
             ON ps.template_code = t.code AND ps.property_id = %s
           ORDER BY t.sort_order""",
        (property_id,),
    ).fetchall()


def upsert_section(conn, property_id: str, template_code: str, *,
                   content: dict, body_md: str | None, is_visible: bool,
                   completed: bool) -> dict:
    row = conn.execute(
        """INSERT INTO property_sections (property_id, template_code, content,
                                          body_md, is_visible, completed, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (property_id, template_code) DO UPDATE SET
               content = EXCLUDED.content, body_md = EXCLUDED.body_md,
               is_visible = EXCLUDED.is_visible, completed = EXCLUDED.completed,
               updated_at = now()
           RETURNING id, template_code, is_visible, completed""",
        (property_id, template_code, json.dumps(content), body_md, is_visible,
         completed),
    ).fetchone()
    # Le contenu source a changé → ses traductions deviennent périmées (M-09).
    # La re-traduction (à la publication ou via le bouton) ne retraitera que
    # le périmé (ciblage, §9).
    mark_section_translations_stale(conn, str(row["id"]))
    return row


def mark_section_translations_stale(conn, section_id: str) -> None:
    conn.execute(
        "UPDATE section_translations SET is_stale = TRUE WHERE section_id = %s",
        (section_id,),
    )


def section_template_exists(conn, template_code: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM section_templates WHERE code = %s", (template_code,)
    ).fetchone() is not None


def get_section_id(conn, property_id: str, template_code: str) -> str | None:
    """Identifiant de la section instanciée pour ce logement (None si non créée)."""
    row = conn.execute(
        "SELECT id FROM property_sections WHERE property_id = %s AND template_code = %s",
        (property_id, template_code),
    ).fetchone()
    return str(row["id"]) if row else None


def ensure_section(conn, property_id: str, template_code: str) -> str:
    """Renvoie l'id de la section, en la créant (vide) au besoin — nécessaire pour
    y rattacher un média avant toute saisie de contenu."""
    existing = get_section_id(conn, property_id, template_code)
    if existing:
        return existing
    row = conn.execute(
        """INSERT INTO property_sections (property_id, template_code)
           VALUES (%s, %s) RETURNING id""",
        (property_id, template_code),
    ).fetchone()
    return str(row["id"])


# ── Médias (photos / PDF par section, M-12) ──────────────────────────────────

# Vue commune : le média + le code de section (NULL si rattaché au logement).
_MEDIA_COLS = """
    m.id, m.section_id, t.code AS section_code, m.kind, m.storage_key,
    m.caption, m.sort_order, m.created_at
"""


def create_media(conn, property_id: str, section_id: str | None, kind: str,
                 storage_key: str, caption: str | None) -> dict:
    """Insère un média en fin de liste (sort_order = max+1 dans son groupe)."""
    return conn.execute(
        f"""INSERT INTO media (property_id, section_id, kind, storage_key,
                               caption, sort_order)
            VALUES (%s, %s, %s, %s, %s,
                COALESCE((SELECT max(sort_order) + 1 FROM media
                          WHERE property_id = %s
                            AND section_id IS NOT DISTINCT FROM %s), 0))
            RETURNING id, section_id, kind, storage_key, caption, sort_order,
                      created_at""",
        (property_id, section_id, kind, storage_key, caption,
         property_id, section_id),
    ).fetchone()


def list_media(conn, property_id: str, section_id: str | None = None,
               all_sections: bool = True) -> list[dict]:
    """Médias d'un logement (côté propriétaire), triés par section puis ordre.

    `all_sections=True` : tous les médias. Sinon, uniquement ceux du groupe
    `section_id` donné (None = médias rattachés au logement, sans section)."""
    q = (f"SELECT {_MEDIA_COLS} FROM media m "
         "LEFT JOIN property_sections ps ON ps.id = m.section_id "
         "LEFT JOIN section_templates t ON t.code = ps.template_code "
         "WHERE m.property_id = %s")
    params: list[Any] = [property_id]
    if not all_sections:
        q += " AND m.section_id IS NOT DISTINCT FROM %s"
        params.append(section_id)
    q += " ORDER BY t.sort_order NULLS FIRST, m.sort_order, m.created_at"
    return conn.execute(q, params).fetchall()


def get_media_full(conn, property_id: str, media_id: str) -> dict | None:
    """Média complet (avec section_code) pour ce logement, ou None."""
    return conn.execute(
        f"SELECT {_MEDIA_COLS} FROM media m "
        "LEFT JOIN property_sections ps ON ps.id = m.section_id "
        "LEFT JOIN section_templates t ON t.code = ps.template_code "
        "WHERE m.id = %s AND m.property_id = %s",
        (media_id, property_id),
    ).fetchone()


def get_media(conn, property_id: str, media_id: str) -> dict | None:
    """Métadonnées minimales (clé de stockage) pour servir/supprimer un média."""
    return conn.execute(
        "SELECT id, kind, storage_key FROM media WHERE id = %s AND property_id = %s",
        (media_id, property_id),
    ).fetchone()


def update_media_caption(conn, property_id: str, media_id: str,
                         caption: str | None) -> dict | None:
    return conn.execute(
        "UPDATE media SET caption = %s WHERE id = %s AND property_id = %s "
        "RETURNING id",
        (caption, media_id, property_id),
    ).fetchone()


def delete_media(conn, property_id: str, media_id: str) -> dict | None:
    """Supprime la ligne et renvoie la clé de stockage (pour effacer le fichier)."""
    return conn.execute(
        "DELETE FROM media WHERE id = %s AND property_id = %s "
        "RETURNING id, storage_key",
        (media_id, property_id),
    ).fetchone()


def reorder_media(conn, property_id: str, ordered_ids: list[str]) -> int:
    """Réordonne les médias selon la liste d'identifiants (isolation par logement)."""
    n = 0
    for i, mid in enumerate(ordered_ids):
        row = conn.execute(
            "UPDATE media SET sort_order = %s WHERE id = %s AND property_id = %s "
            "RETURNING id",
            (i, mid, property_id),
        ).fetchone()
        if row:
            n += 1
    return n


# ── Catégories POI (catalogue pour l'ajout manuel, M-22) ─────────────────────

def list_categories(conn) -> list[dict]:
    """Catalogue des catégories POI (code, chapitre, libellés, couleur, icône)
    pour le sélecteur de l'ajout manuel. Ordonné par chapitre puis code."""
    return conn.execute(
        "SELECT code, chapter, name_i18n, map_color, icon "
        "FROM poi_categories ORDER BY chapter, code"
    ).fetchall()


# ── POI ──────────────────────────────────────────────────────────────────────

# Projection commune de l'écran de validation (libellé/icône/couleur de chapitre
# depuis le seed) : partagée par la liste et la relecture d'un POI unique.
_POI_SELECT = (
    "SELECT p.id, p.category_code, c.chapter, c.name_i18n AS category_name, "
    "c.icon AS category_icon, c.map_color, "
    "p.name, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon, "
    "p.address, p.phone, p.website, p.opening_hours, p.cuisine, p.description_md, "
    "p.owner_comment, p.price_level, "
    "p.dist_walk_m, p.walk_min, p.dist_drive_m, p.drive_min, "
    "p.source, p.source_ref, p.status, p.fetched_at "
    "FROM pois p JOIN poi_categories c ON c.code = p.category_code "
    "WHERE p.property_id = %s")


def list_pois(conn, property_id: str, status: str | None) -> list[dict]:
    """POI du logement pour l'écran de validation (§5.1 étape 5).

    Jointure sur `poi_categories` pour porter le libellé, l'icône et la couleur
    de chapitre du seed : l'écran de validation regroupe et colore les POI comme
    le guide voyageur, sans second appel. Données owner-side (aucun secret)."""
    q = _POI_SELECT
    params: list[Any] = [property_id]
    if status:
        q += " AND p.status = %s"
        params.append(status)
    q += " ORDER BY p.category_code, p.dist_walk_m NULLS LAST, p.name"
    return conn.execute(q, params).fetchall()


def get_poi_full(conn, property_id: str, poi_id: str) -> dict | None:
    """Relit un POI unique dans la même projection que `list_pois` (pour renvoyer
    le POI fraîchement créé au front, prêt à afficher, M-22)."""
    return conn.execute(_POI_SELECT + " AND p.id = %s",
                        [property_id, poi_id]).fetchone()


def poi_category_exists(conn, code: str) -> bool:
    """Vrai si `code` est une catégorie POI connue (validation avant création)."""
    return conn.execute("SELECT 1 FROM poi_categories WHERE code = %s",
                        (code,)).fetchone() is not None


def create_manual_poi(conn, property_id: str, data: dict) -> dict | None:
    """Crée un POI saisi par le propriétaire (M-22) : source='owner',
    status='approved' (arbitrage explicite → jamais écrasé, invariant 1),
    sans `source_ref` (hors upsert d'enrichissement). Les distances déjà
    calculées sont passées dans `data` (dist_*_m / *_min)."""
    row = conn.execute(
        """INSERT INTO pois (property_id, category_code, name, geom, address,
               phone, website, opening_hours, cuisine, description_md, owner_comment,
               dist_walk_m, walk_min, dist_drive_m, drive_min,
               source, status, fetched_at)
           VALUES (%(pid)s, %(category_code)s, %(name)s,
               ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
               %(address)s, %(phone)s, %(website)s, %(opening_hours)s,
               %(cuisine)s, %(description_md)s, %(owner_comment)s,
               %(dist_walk_m)s, %(walk_min)s, %(dist_drive_m)s, %(drive_min)s,
               'owner', 'approved', now())
           RETURNING id""",
        {"pid": property_id, **data},
    ).fetchone()
    return get_poi_full(conn, property_id, str(row["id"])) if row else None


def property_stats(conn, property_id: str) -> dict:
    """Indicateurs affichés dans « Mes logements » et l'éditeur : complétude des
    sections (sur le catalogue complet) et décompte des POI par statut.

    La complétude rapporte les sections marquées « complétées » par le
    propriétaire au nombre total de sections pré-définies (§4). Elle ne concerne
    que le **guide voyageur** (audience='guest') : le cahier de l'équipe
    d'entretien (M-13) a son propre indicateur et ne dilue pas ce pourcentage."""
    sec = conn.execute(
        """SELECT (SELECT count(*) FROM section_templates WHERE audience = 'guest') AS total,
                  count(*) FILTER (WHERE ps.completed AND COALESCE(t.audience, 'guest') = 'guest') AS done,
                  count(*) FILTER (WHERE ps.is_visible AND COALESCE(t.audience, 'guest') = 'guest') AS visible
           FROM property_sections ps
           LEFT JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s""",
        (property_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM pois WHERE property_id = %s "
        "GROUP BY status",
        (property_id,),
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    total = sec["total"] or 0
    done = sec["done"] or 0
    return {
        "sections_total": total,
        "sections_done": done,
        "sections_visible": sec["visible"] or 0,
        "completion_pct": round(done / total * 100) if total else 0,
        "pois_total": sum(by_status.values()),
        "pois_suggested": by_status.get("suggested", 0),
        "pois_approved": by_status.get("approved", 0),
        "pois_edited": by_status.get("edited", 0),
        "pois_rejected": by_status.get("rejected", 0),
    }


def list_poi_positions(conn, property_id: str) -> list[dict]:
    """Coordonnées des POI (hors géométrie nulle) pour recalcul des distances (§5.1)."""
    return conn.execute(
        "SELECT id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM pois "
        "WHERE property_id = %s AND geom IS NOT NULL",
        (property_id,),
    ).fetchall()


def update_poi_distances(conn, poi_id: str, *, dist_walk_m: int | None,
                         walk_min: int | None, dist_drive_m: int | None,
                         drive_min: int | None) -> None:
    """Met à jour uniquement les distances/temps d'un POI (jamais son statut ni
    son contenu arbitré par le propriétaire)."""
    conn.execute(
        "UPDATE pois SET dist_walk_m = %s, walk_min = %s, dist_drive_m = %s, "
        "drive_min = %s WHERE id = %s",
        (dist_walk_m, walk_min, dist_drive_m, drive_min, poi_id),
    )


def get_poi(conn, property_id: str, poi_id: str) -> dict | None:
    return conn.execute(
        "SELECT id, status FROM pois WHERE id = %s AND property_id = %s",
        (poi_id, property_id),
    ).fetchone()


def set_poi_status(conn, property_id: str, poi_id: str, status: str) -> dict | None:
    return conn.execute(
        "UPDATE pois SET status = %s WHERE id = %s AND property_id = %s "
        "RETURNING id, status",
        (status, poi_id, property_id),
    ).fetchone()


_POI_EDITABLE = ("name", "address", "phone", "website", "opening_hours",
                 "cuisine", "description_md", "owner_comment")


def edit_poi(conn, property_id: str, poi_id: str, fields: dict) -> dict | None:
    """Applique les champs édités et force le statut 'edited' (choix propriétaire)."""
    sets, params = ["status = 'edited'"], []
    for key in _POI_EDITABLE:
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = %s")
            params.append(fields[key])
    params.extend([poi_id, property_id])
    row = conn.execute(
        f"UPDATE pois SET {', '.join(sets)} "
        "WHERE id = %s AND property_id = %s "
        "RETURNING id, status",
        params,
    ).fetchone()
    # Le texte éditorial du POI a changé → ses traductions sont périmées (M-09).
    if row:
        conn.execute(
            "UPDATE poi_translations SET is_stale = TRUE WHERE poi_id = %s",
            (poi_id,),
        )
    return row


# ── Jobs d'enrichissement ────────────────────────────────────────────────────

def create_pending_job(conn, property_id: str, trigger: str) -> str:
    """Crée un job 'pending' pour renvoyer un identifiant immédiat à l'API.
    Le pipeline (tâche de fond) le passera en 'running' puis 'done'/'failed'."""
    row = conn.execute(
        """INSERT INTO enrichment_jobs (property_id, trigger, status)
           VALUES (%s, %s, 'pending') RETURNING id""",
        (property_id, trigger),
    ).fetchone()
    return str(row["id"])


def count_jobs_current_month(conn, property_id: str) -> int:
    """Enrichissements décomptés du quota mensuel (§5.2).

    Les jobs en échec ne comptent pas (`status <> 'failed'`) : une tentative
    qui n'a rien produit — clé IA invalide, serveurs OSM indisponibles… — ne
    doit pas consommer le quota du propriétaire (M-01). Les jobs de traduction
    (trigger='translate', M-09) ne sont pas des enrichissements : hors quota."""
    return conn.execute(
        """SELECT count(*) AS n FROM enrichment_jobs
           WHERE property_id = %s
             AND created_at >= date_trunc('month', now())
             AND status <> 'failed'
             AND trigger <> 'translate'""",
        (property_id,),
    ).fetchone()["n"]


def fail_orphan_running_jobs(conn) -> int:
    """Requalifie en 'failed' les jobs restés 'running' : leur BackgroundTask ne
    survit pas à un redémarrage d'uvicorn (M-01). Appelé au démarrage de l'API.
    Retourne le nombre de jobs requalifiés."""
    rows = conn.execute(
        """UPDATE enrichment_jobs
           SET status = 'failed', error = 'interrompu par redémarrage',
               finished_at = now()
           WHERE status = 'running'
           RETURNING id""",
    ).fetchall()
    return len(rows)


def list_jobs(conn, property_id: str) -> list[dict]:
    return conn.execute(
        """SELECT id, trigger, status, steps, error, created_at, started_at,
                  finished_at
           FROM enrichment_jobs WHERE property_id = %s
           ORDER BY created_at DESC LIMIT 50""",
        (property_id,),
    ).fetchall()


def get_job(conn, property_id: str, job_id: str) -> dict | None:
    return conn.execute(
        """SELECT id, trigger, status, steps, error, created_at, started_at,
                  finished_at
           FROM enrichment_jobs WHERE id = %s AND property_id = %s""",
        (job_id, property_id),
    ).fetchone()


# ── Guide public (lecture seule, aucune donnée sensible) ─────────────────────

def get_published_property_by_token(conn, token: str) -> dict | None:
    """Logement publié désigné par son token secret. None si brouillon/archivé
    ou token inconnu (on ne révèle pas l'existence d'un guide non publié)."""
    return conn.execute(
        """SELECT id, name, address_line1, address_line2, postal_code,
                  city, region, country_code,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon,
                  default_lang, published_langs, access_mode,
                  contact_name, contact_phone, contact_whatsapp, contact_email,
                  contact_backup, tourism_license
           FROM properties
           WHERE guide_token = %s AND status = 'published'""",
        (token,),
    ).fetchone()


def guide_sections(conn, property_id: str) -> list[dict]:
    """Sections **voyageur** visibles d'un guide (audience='guest'), avec les
    métadonnées de leur template. Les sections 'staff' (cahier de l'équipe
    d'entretien, M-13) ne sortent JAMAIS ici (invariant 7)."""
    return conn.execute(
        """SELECT t.code, t.chapter, t.sort_order, t.icon, t.name_i18n,
                  t.field_schema, t.is_sensitive, ps.content, ps.body_md
           FROM property_sections ps
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s AND ps.is_visible = TRUE
             AND t.audience = 'guest'
           ORDER BY t.sort_order""",
        (property_id,),
    ).fetchall()


def guide_pois(conn, property_id: str) -> list[dict]:
    """POI approuvés/édités uniquement (jamais 'suggested' ni 'rejected'),
    avec la catégorie (icône/couleur du seed). Distances déjà en base."""
    return conn.execute(
        """SELECT p.id, p.category_code, c.chapter, c.name_i18n AS category_name,
                  c.icon AS category_icon, c.map_color,
                  p.name, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon,
                  p.address, p.phone, p.website, p.opening_hours, p.cuisine,
                  p.description_md, p.owner_comment, p.price_level,
                  p.dist_walk_m, p.walk_min, p.dist_drive_m, p.drive_min,
                  p.status
           FROM pois p
           JOIN poi_categories c ON c.code = p.category_code
           WHERE p.property_id = %s AND p.status IN ('approved', 'edited')
           ORDER BY p.category_code,
                    (p.owner_comment IS NOT NULL AND p.owner_comment <> '') DESC,
                    p.dist_walk_m NULLS LAST, p.name""",
        (property_id,),
    ).fetchall()


def guide_media(conn, property_id: str) -> list[dict]:
    """Médias servis dans le guide public (M-12) : uniquement ceux d'une section
    **visible**, plus ceux rattachés au logement (section_id NULL). Un média
    d'une section masquée n'est jamais listé (invariant de visibilité). Un média
    d'une section 'staff' (M-13) n'est jamais listé côté voyageur (invariant 7)."""
    return conn.execute(
        """SELECT m.id, m.kind, m.caption, m.sort_order, t.code AS section_code
           FROM media m
           LEFT JOIN property_sections ps ON ps.id = m.section_id
           LEFT JOIN section_templates t ON t.code = ps.template_code
           WHERE m.property_id = %s
             AND (m.section_id IS NULL
                  OR (ps.is_visible = TRUE AND t.audience = 'guest'))
           ORDER BY t.sort_order NULLS FIRST, m.sort_order, m.created_at""",
        (property_id,),
    ).fetchall()


def get_public_media(conn, token: str, media_id: str) -> dict | None:
    """Média d'un guide **publié**, servi seulement si sa section est visible (ou
    s'il est rattaché au logement). None sinon : token inconnu, guide non publié,
    ou section masquée — on ne révèle rien (invariants 4/5, §8). Un média de
    section 'staff' (M-13) n'est jamais servi sur /g (invariant 7)."""
    return conn.execute(
        """SELECT m.kind, m.storage_key
           FROM media m
           JOIN properties pr ON pr.id = m.property_id
           LEFT JOIN property_sections ps ON ps.id = m.section_id
           LEFT JOIN section_templates t ON t.code = ps.template_code
           WHERE m.id = %s AND pr.guide_token = %s AND pr.status = 'published'
             AND (m.section_id IS NULL
                  OR (ps.is_visible = TRUE AND t.audience = 'guest'))""",
        (media_id, token),
    ).fetchone()


def get_published_secrets_by_token(conn, token: str) -> dict | None:
    """Secrets chiffrés d'un guide **publié** en mode d'accès 'link' (MVP, §8).

    Le lien secret (token ≥ 128 bits) tenant lieu de clé d'accès, le voyageur qui
    le possède peut voir le wifi et le code de la boîte à clés. Renvoie None si le
    token est inconnu, le guide non publié, ou le mode d'accès n'est pas 'link'
    (les modes 'pin'/'stay_dates' de la V2 exigeront la saisie d'un code)."""
    return conn.execute(
        """SELECT s.wifi_ssid, s.wifi_pass_enc, s.wifi_networks_enc,
                  s.keybox_code_enc, s.keybox_notes
           FROM properties pr
           JOIN property_secrets s ON s.property_id = pr.id
           WHERE pr.guide_token = %s AND pr.status = 'published'
             AND pr.access_mode = 'link'""",
        (token,),
    ).fetchone()


def guide_area_facts(conn, country_code: str, city: str | None) -> dict:
    """Faits locaux (urgences, tri, bruit). Priorité à la commune, repli national."""
    rows = conn.execute(
        """SELECT admin_area, fact_type, content FROM area_facts
           WHERE country_code = %s
             AND (admin_area = %s OR admin_area IS NULL)""",
        (country_code, city),
    ).fetchall()
    facts: dict[str, Any] = {}
    # D'abord le national, puis la commune écrase (priorité au plus précis)
    for r in sorted(rows, key=lambda r: r["admin_area"] is not None):
        facts[r["fact_type"]] = r["content"]
    return facts


# ── Traductions servies au voyageur (M-09, §9) ───────────────────────────────
# Lectures seules, sans secret. Le contenu source (français) reste la source de
# vérité : ces traductions ne sont overlayées qu'à la demande (?lang=xx). Un
# repli sur le français est toujours possible (jamais de trou, §9).

def guide_section_translations(conn, property_id: str, lang: str) -> dict:
    """Traductions **fraîches** des sections voyageur pour `lang`, indexées par
    code de section. Chaque valeur : {content, body_md}. Uniquement
    audience='guest'.

    Les traductions périmées (`is_stale`) sont ignorées : la section retombe
    alors sur le français (repli élégant, jamais d'info traduite obsolète, §9).
    La re-traduction (publication / bouton) les rafraîchit."""
    rows = conn.execute(
        """SELECT t.code, st.content, st.body_md
           FROM section_translations st
           JOIN property_sections ps ON ps.id = st.section_id
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s AND st.lang = %s AND t.audience = 'guest'
             AND st.is_stale = FALSE""",
        (property_id, lang),
    ).fetchall()
    return {r["code"]: {"content": r["content"], "body_md": r["body_md"]}
            for r in rows}


def guide_poi_translations(conn, property_id: str, lang: str) -> dict:
    """Traductions **fraîches** des POI retenus pour `lang`, indexées par id de
    POI. Les traductions périmées sont ignorées (repli sur le français, §9)."""
    rows = conn.execute(
        """SELECT pt.poi_id, pt.description_md, pt.owner_comment
           FROM poi_translations pt
           JOIN pois p ON p.id = pt.poi_id
           WHERE p.property_id = %s AND pt.lang = %s
             AND p.status IN ('approved', 'edited') AND pt.is_stale = FALSE""",
        (property_id, lang),
    ).fetchall()
    return {str(r["poi_id"]): {"description_md": r["description_md"],
                               "owner_comment": r["owner_comment"]}
            for r in rows}


def translation_status(conn, property_id: str, langs: list[str]) -> dict:
    """État des traductions pour le bouton « Mettre à jour les traductions » de
    l'éditeur : par langue, nombre d'éléments (sections + POI) à jour et périmés,
    et total d'éléments porteurs de texte.

    « Périmé » agrège le manquant (jamais traduit) et le périmé (source modifiée) :
    ce sont les éléments que la prochaine traduction (re)traitera."""
    # Éléments source porteurs de texte (sections guest avec contenu, POI retenus).
    sec_ids = [str(r["section_id"]) for r in conn.execute(
        """SELECT ps.id AS section_id FROM property_sections ps
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s AND t.audience = 'guest'
             AND (ps.body_md IS NOT NULL OR ps.content <> '{}'::jsonb)""",
        (property_id,)).fetchall()]
    poi_ids = [str(r["id"]) for r in conn.execute(
        """SELECT id FROM pois WHERE property_id = %s
             AND status IN ('approved', 'edited')
             AND (description_md IS NOT NULL OR owner_comment IS NOT NULL)""",
        (property_id,)).fetchall()]
    total = len(sec_ids) + len(poi_ids)

    per_lang: dict[str, dict] = {}
    for lang in langs:
        fresh = conn.execute(
            """SELECT count(*) AS n FROM section_translations st
               JOIN property_sections ps ON ps.id = st.section_id
               JOIN section_templates t ON t.code = ps.template_code
               WHERE ps.property_id = %s AND st.lang = %s
                 AND t.audience = 'guest' AND st.is_stale = FALSE""",
            (property_id, lang)).fetchone()["n"]
        fresh += conn.execute(
            """SELECT count(*) AS n FROM poi_translations pt
               JOIN pois p ON p.id = pt.poi_id
               WHERE p.property_id = %s AND pt.lang = %s
                 AND p.status IN ('approved', 'edited') AND pt.is_stale = FALSE""",
            (property_id, lang)).fetchone()["n"]
        fresh = min(fresh, total)
        per_lang[lang] = {"fresh": fresh, "stale": total - fresh}
    outdated = sum(v["stale"] for v in per_lang.values())
    return {"langs": per_lang, "total": total, "outdated": outdated,
            "up_to_date": outdated == 0}


# ── Cahier de préparation « équipe d'entretien » (/s/{staff_token}, M-13) ─────
# Ce cahier est **volontairement accessible même en brouillon** : l'équipe
# d'entretien prépare le logement AVANT la publication du guide voyageur. Le
# staff_token (≥ 128 bits, distinct du guide_token) tient lieu de clé d'accès.
# Aucune de ces requêtes ne remonte jamais les secrets, les POI ni les sections
# 'guest' (invariant 7).

def get_property_by_staff_token(conn, token: str) -> dict | None:
    """Logement désigné par son staff_token (tout statut, y compris 'draft').
    None si le token est inconnu — on ne révèle pas l'existence d'un logement."""
    return conn.execute(
        """SELECT id, name, city, region, country_code, status
           FROM properties
           WHERE staff_token = %s""",
        (token,),
    ).fetchone()


def staff_sections(conn, property_id: str) -> list[dict]:
    """Sections **équipe d'entretien** visibles (audience='staff'). Jamais les
    sections 'guest' (invariant 7)."""
    return conn.execute(
        """SELECT t.code, t.chapter, t.sort_order, t.icon, t.name_i18n,
                  t.field_schema, t.is_sensitive, ps.content, ps.body_md
           FROM property_sections ps
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = %s AND ps.is_visible = TRUE
             AND t.audience = 'staff'
           ORDER BY t.sort_order""",
        (property_id,),
    ).fetchall()


def staff_media(conn, property_id: str) -> list[dict]:
    """Médias des sections 'staff' visibles (panier de bienvenue illustré…).
    N'inclut jamais les médias 'guest' ni ceux au niveau logement."""
    return conn.execute(
        """SELECT m.id, m.kind, m.caption, m.sort_order, t.code AS section_code
           FROM media m
           JOIN property_sections ps ON ps.id = m.section_id
           JOIN section_templates t ON t.code = ps.template_code
           WHERE m.property_id = %s AND ps.is_visible = TRUE
             AND t.audience = 'staff'
           ORDER BY t.sort_order, m.sort_order, m.created_at""",
        (property_id,),
    ).fetchall()


def get_staff_media(conn, token: str, media_id: str) -> dict | None:
    """Média d'un cahier 'staff' servi via /s/{staff_token} (tout statut). Servi
    seulement si sa section est 'staff' et visible ; None sinon (on ne révèle
    rien : token inconnu, média 'guest', section masquée)."""
    return conn.execute(
        """SELECT m.kind, m.storage_key
           FROM media m
           JOIN properties pr ON pr.id = m.property_id
           JOIN property_sections ps ON ps.id = m.section_id
           JOIN section_templates t ON t.code = ps.template_code
           WHERE m.id = %s AND pr.staff_token = %s
             AND ps.is_visible = TRUE AND t.audience = 'staff'""",
        (media_id, token),
    ).fetchone()
