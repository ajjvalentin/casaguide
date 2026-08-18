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
    default_checkin_time, default_checkout_time, care_rules, cover_media_id,
    auto_send_guide, created_at, updated_at
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


def create_subscription(conn, owner_id: str, plan_id: str,
                        status: str = "active", trial_ends_at=None) -> None:
    """Attribue un abonnement au propriétaire (V2-05a, V2-18a).

    L'inscription pose désormais un ESSAI (`plan_id='trial'`, `status='trialing'`,
    `trial_ends_at` = échéance) — cf. `routers/auth.register`. `trial_ends_at`
    reste NULL hors essai (rattrapage 'free', comptes importés). En V2-05b le
    webhook Stripe est la seule source de vérité du `status` des plans payants."""
    conn.execute(
        """INSERT INTO subscriptions (owner_id, plan_id, status, trial_ends_at)
           VALUES (%s, %s, %s, %s)""",
        (owner_id, plan_id, status, trial_ends_at),
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


def get_plan_by_id(conn, plan_id: str) -> dict | None:
    """Définition d'un plan par son identifiant ('free' | 'solo' | 'pro')."""
    return conn.execute(
        "SELECT * FROM plans WHERE id = %s", (plan_id,)
    ).fetchone()


def list_plans(conn) -> list[dict]:
    """Catalogue des plans (par prix croissant, puis id pour départager les deux
    plans à 0 : 'free' et 'trial') — inscription & page abonnement."""
    return conn.execute(
        "SELECT * FROM plans ORDER BY price_month_cts, id"
    ).fetchall()


def get_plan_by_guide_token(conn, token: str) -> dict | None:
    """Plan du propriétaire d'un guide, désigné par son `guide_token` public
    (V2-05a). Sert au rendu du guide voyageur (watermark du plan gratuit) sans
    exposer l'`owner_id`. None si token inconnu."""
    return conn.execute(
        """SELECT p.* FROM plans p
           JOIN subscriptions s ON s.plan_id = p.id
           JOIN properties pr ON pr.owner_id = s.owner_id
           WHERE pr.guide_token = %s
           ORDER BY s.created_at DESC LIMIT 1""",
        (token,),
    ).fetchone()


# ── Facturation Stripe (V2-05b) ──────────────────────────────────────────────

def get_plan_by_stripe_price_id(conn, price_id: str) -> dict | None:
    """Plan correspondant à un Price Stripe (résolution webhook price→plan). None
    si aucun plan ne porte ce `stripe_price_id` (prix inconnu / non synchronisé)."""
    return conn.execute(
        "SELECT * FROM plans WHERE stripe_price_id = %s", (price_id,)
    ).fetchone()


def set_plan_stripe_price_id(conn, plan_id: str, price_id: str) -> None:
    """Enregistre le Price Stripe d'un plan (écrit par `ops/stripe_sync_products.py`)."""
    conn.execute(
        "UPDATE plans SET stripe_price_id = %s WHERE id = %s", (price_id, plan_id))


def get_plan_by_addon_stripe_price_id(conn, price_id: str) -> dict | None:
    """Plan dont l'add-on « logement supplémentaire » porte ce Price Stripe (V2-18b,
    résolution webhook de la quantité d'add-on). None si aucun plan ne le porte."""
    return conn.execute(
        "SELECT * FROM plans WHERE addon_stripe_price_id = %s", (price_id,)
    ).fetchone()


def get_subscription_by_customer_id(conn, customer_id: str) -> dict | None:
    """Abonnement rattaché à un Customer Stripe (résolution owner côté webhook).
    None si aucun abonnement ne porte ce `stripe_customer_id`."""
    return conn.execute(
        """SELECT id, owner_id, plan_id, status, stripe_customer_id,
                  stripe_subscription_id, current_period_end, scheduled_plan_id
           FROM subscriptions WHERE stripe_customer_id = %s
           ORDER BY created_at DESC LIMIT 1""",
        (customer_id,),
    ).fetchone()


def _latest_subscription_id(conn, owner_id: str):
    """Id de la ligne d'abonnement courante (la plus récente) d'un propriétaire."""
    row = conn.execute(
        """SELECT id FROM subscriptions WHERE owner_id = %s
           ORDER BY created_at DESC LIMIT 1""",
        (owner_id,),
    ).fetchone()
    return row["id"] if row else None


def set_subscription_customer(conn, owner_id: str, customer_id: str) -> None:
    """Rattache un Customer Stripe à l'abonnement courant du propriétaire (posé au
    moment du Checkout, AVANT tout webhook → la résolution owner par customer_id
    fonctionne quel que soit l'ordre d'arrivée des événements)."""
    sub_id = _latest_subscription_id(conn, owner_id)
    if sub_id is not None:
        conn.execute(
            """UPDATE subscriptions SET stripe_customer_id = %s, updated_at = now()
               WHERE id = %s""",
            (customer_id, sub_id))


def update_subscription_from_stripe(conn, owner_id: str, *, plan_id: str,
                                    status: str,
                                    stripe_subscription_id: str | None,
                                    current_period_end,
                                    addon_qty: int = 0) -> None:
    """Applique l'état Stripe à l'abonnement courant (SEULE écriture d'autorité,
    depuis le handler de webhook). Ne supprime jamais de données : un retour à
    'free' ne fait que rebasculer `plan_id` (invariant downgrade V2-05a) et
    remettre `addon_qty` à 0.

    `addon_qty` (V2-18b) est la quantité de logements supplémentaires lue dans les
    items de l'abonnement Stripe : le webhook en est la SEULE source (invariant 1).

    Purge `trial_ends_at` (→ NULL) : toute décision Stripe (souscription payante,
    annulation) fait SORTIR du régime d'essai (V2-18a) — l'essai ne s'applique
    qu'à un abonnement 'trialing' non piloté par Stripe."""
    sub_id = _latest_subscription_id(conn, owner_id)
    if sub_id is None:  # filet : compte sans abonnement → on en crée un
        conn.execute(
            """INSERT INTO subscriptions
                   (owner_id, plan_id, status, stripe_subscription_id,
                    current_period_end, addon_qty, trial_ends_at)
               VALUES (%s, %s, %s, %s, %s, %s, NULL)""",
            (owner_id, plan_id, status, stripe_subscription_id,
             current_period_end, addon_qty))
        return
    conn.execute(
        """UPDATE subscriptions
           SET plan_id = %s, status = %s, stripe_subscription_id = %s,
               current_period_end = %s, addon_qty = %s, trial_ends_at = NULL,
               updated_at = now()
           WHERE id = %s""",
        (plan_id, status, stripe_subscription_id, current_period_end,
         addon_qty, sub_id))


def set_subscription_status(conn, owner_id: str, status: str) -> None:
    """Met à jour le seul `status` de l'abonnement courant (échec de paiement →
    'past_due'). Ne touche ni au plan ni au reste : l'accès n'est retiré qu'à
    l'annulation effective (subscription.deleted → retour à 'free')."""
    sub_id = _latest_subscription_id(conn, owner_id)
    if sub_id is not None:
        conn.execute(
            "UPDATE subscriptions SET status = %s, updated_at = now() WHERE id = %s",
            (status, sub_id))


# ── Changement d'offre programmé à l'échéance (V2-18e) ───────────────────────

def set_scheduled_change(conn, owner_id: str, plan_id: str, effective_at) -> None:
    """Mémorise un downgrade programmé (offre cible + date d'effet) sur
    l'abonnement courant. Écrit EXCLUSIVEMENT par le webhook Stripe (événements
    `subscription_schedule.*`, invariant 12) — purement informatif (bandeau
    back-office). N'affecte JAMAIS `plan_id` ni l'accès (qui reste celui de
    l'offre en cours jusqu'à l'échéance)."""
    sub_id = _latest_subscription_id(conn, owner_id)
    if sub_id is not None:
        conn.execute(
            """UPDATE subscriptions
               SET scheduled_plan_id = %s, scheduled_change_at = %s,
                   updated_at = now()
               WHERE id = %s""",
            (plan_id, effective_at, sub_id))


def clear_scheduled_change(conn, owner_id: str) -> None:
    """Efface un downgrade programmé (annulation, prise d'effet, ou annulation
    Stripe). Écrit EXCLUSIVEMENT par le webhook (invariant 12)."""
    sub_id = _latest_subscription_id(conn, owner_id)
    if sub_id is not None:
        conn.execute(
            """UPDATE subscriptions
               SET scheduled_plan_id = NULL, scheduled_change_at = NULL,
                   updated_at = now()
               WHERE id = %s""",
            (sub_id,))


# ── Idempotence des webhooks Stripe (V2-05b) ─────────────────────────────────

def stripe_event_begin(conn, event_id: str, event_type: str) -> bool:
    """Réserve le traitement d'un événement webhook. Renvoie True si l'événement
    est nouveau (à traiter), False s'il a déjà été reçu (rejeu Stripe → ignorer).

    L'INSERT ... ON CONFLICT DO NOTHING est atomique : deux livraisons
    concurrentes du même event ne peuvent pas être traitées deux fois."""
    row = conn.execute(
        """INSERT INTO stripe_events (id, type) VALUES (%s, %s)
           ON CONFLICT (id) DO NOTHING
           RETURNING id""",
        (event_id, event_type),
    ).fetchone()
    return row is not None


def stripe_event_mark_processed(conn, event_id: str) -> None:
    """Horodate la fin de traitement d'un événement (`processed_at`)."""
    conn.execute(
        "UPDATE stripe_events SET processed_at = now() WHERE id = %s", (event_id,))


# ── Relances de fin d'essai (V2-18a, ops/send_trial_reminders.py) ────────────

# Fenêtres de relance : jours restants → colonne d'idempotence dédiée.
_REMINDER_COLUMNS = {7: "reminder_7d_sent_at", 2: "reminder_2d_sent_at"}


def trials_due_for_reminder(conn, days: int, *, now=None) -> list[dict]:
    """Essais en cours dont l'échéance tombe dans ≤ `days` jours et dont la
    relance de cette fenêtre n'a pas encore été expédiée (idempotence). `now`
    injectable (tests). Ne remonte que les essais non expirés (relancer un essai
    déjà échu n'a pas de sens : l'accès est déjà en lecture seule)."""
    col = _REMINDER_COLUMNS[days]   # KeyError si fenêtre non prévue (garde-fou)
    return conn.execute(
        f"""SELECT s.id AS subscription_id, s.trial_ends_at,
                   o.email, o.full_name
            FROM subscriptions s JOIN owners o ON o.id = s.owner_id
            WHERE s.status = 'trialing'
              AND s.trial_ends_at IS NOT NULL
              AND s.trial_ends_at > COALESCE(%(now)s, now())
              AND s.trial_ends_at <= COALESCE(%(now)s, now())
                                     + make_interval(days => %(days)s)
              AND s.{col} IS NULL
            ORDER BY s.trial_ends_at""",
        {"now": now, "days": days},
    ).fetchall()


def mark_trial_reminder_sent(conn, subscription_id: str, days: int, *,
                             now=None) -> None:
    """Stampe l'envoi de la relance de la fenêtre `days` (après un envoi RÉUSSI)
    → jamais renvoyée. `now` injectable (tests)."""
    col = _REMINDER_COLUMNS[days]
    conn.execute(
        f"UPDATE subscriptions SET {col} = COALESCE(%s, now()) WHERE id = %s",
        (now, subscription_id))


# ── Registre des langues (V2-21a) ────────────────────────────────────────────
# Source UNIQUE des langues offertes par le produit : le registre `languages`.
# Le produit n'offre JAMAIS que les langues `status='published'` (invariant 8
# étendu aux langues) — plus aucune liste de langues en dur. Une langue passée
# en 'draft'/'in_review' disparaît partout sans redéploiement.

def published_languages(conn) -> list[dict]:
    """Langues publiées du registre, ordonnées : [{code, name_native}].
    Filtre `status='published'`, tri `sort_order`. C'est LA vérité des langues
    offertes (sélecteur du guide, partage, détection, modale séjour, cibles de
    traduction)."""
    rows = conn.execute(
        "SELECT code, name_native FROM languages "
        "WHERE status = 'published' ORDER BY sort_order, code"
    ).fetchall()
    return [{"code": r["code"], "name_native": r["name_native"]} for r in rows]


def published_language_codes(conn) -> list[str]:
    """Codes des langues publiées, ordonnés (`sort_order`). Raccourci de
    `published_languages` quand seuls les codes importent (cibles de traduction,
    filtrage d'un `published_langs` de logement contre le registre)."""
    return [l["code"] for l in published_languages(conn)]


def ui_translations(conn, lang: str) -> dict[str, str]:
    """Libellés STATIQUES traduits pour `lang` (V2-21a, volet 2) : carte
    {clé_inventaire: texte} depuis `ui_translations`. Vide pour FR/EN/ES (jamais
    importées → le SSR retombe sur le code). Superposée au rendu du guide."""
    rows = conn.execute(
        "SELECT key, text FROM ui_translations WHERE lang = %s", (lang,)
    ).fetchall()
    return {r["key"]: r["text"] for r in rows}


def upsert_ui_translation(conn, lang: str, key: str, text: str) -> None:
    """Écrit/écrase un libellé statique traduit (réimport de relecture). Idempotent
    (ON CONFLICT sur la clé composite)."""
    conn.execute(
        "INSERT INTO ui_translations (lang, key, text) VALUES (%s, %s, %s) "
        "ON CONFLICT (lang, key) DO UPDATE SET text = EXCLUDED.text, "
        "updated_at = now()",
        (lang, key, text))


def published_langs(conn, property_id: str) -> list[str]:
    """Langues cibles déjà publiées pour un logement (`properties.published_langs`,
    hors langue source). Liste vide si le logement n'a jamais été traduit."""
    row = conn.execute(
        "SELECT published_langs FROM properties WHERE id = %s", (property_id,)
    ).fetchone()
    return list(row["published_langs"]) if row and row["published_langs"] else []


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
    """Crée un logement et **amorce** ses règles d'entretien (`care_rules`, §1.1)
    et son catalogue de demandes (`property_request_types`, §1.2) avec les défauts
    applicatifs (`api/care`) — jamais figés en base, ajustables ensuite."""
    from . import care  # import local : évite un cycle au chargement du module
    cols = ["owner_id", "name", "address_line1", "address_line2", "postal_code",
            "city", "region", "country_code", "default_lang", "contact_name",
            "contact_phone", "contact_whatsapp", "contact_email",
            "contact_backup", "tourism_license", "care_rules"]
    care_rules = data.get("care_rules") or care.default_care_rules()
    scalar = [data.get(c) for c in cols[1:-1]]
    values = [owner_id] + scalar + [json.dumps(care_rules)]
    placeholders = ", ".join(["%s"] * len(cols))
    row = conn.execute(
        f"INSERT INTO properties ({', '.join(cols)}) VALUES ({placeholders}) "
        f"RETURNING {_PROP_COLS}",
        values,
    ).fetchone()
    seed_request_types(conn, str(row["id"]), care.DEFAULT_REQUEST_TYPES)
    return row


# Champs simples modifiables via PATCH (hors lat/lon traités à part)
_UPDATABLE = (
    "name", "address_line1", "address_line2", "postal_code", "city", "region",
    "country_code", "default_lang", "access_mode", "status", "contact_name",
    "contact_phone", "contact_whatsapp", "contact_email", "contact_backup",
    "tourism_license", "default_checkin_time", "default_checkout_time",
    "auto_send_guide",
)


def update_property(conn, owner_id: str, property_id: str,
                    fields: dict) -> dict | None:
    sets, params = [], []
    for key in _UPDATABLE:
        if key in fields:
            sets.append(f"{key} = %s")
            params.append(fields[key])
    # Règles d'entretien (JSONB, §1.1) — sérialisées, jamais un objet brut.
    if "care_rules" in fields and fields["care_rules"] is not None:
        sets.append("care_rules = %s")
        params.append(json.dumps(fields["care_rules"]))
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


def set_cover_media(conn, owner_id: str, property_id: str,
                    media_id: str | None) -> dict | None:
    """Désigne (ou retire, `media_id=None`) la photo de couverture du logement
    (V2-30). L'appartenance du média est vérifiée en amont (router) ; ici on borne
    encore par `owner_id` (isolation multi-tenant). Retirer la couverture ne
    supprime jamais le média — seule la référence redevient NULL."""
    return conn.execute(
        f"UPDATE properties SET cover_media_id = %s "
        f"WHERE id = %s AND owner_id = %s RETURNING {_PROP_COLS}",
        (media_id, property_id, owner_id),
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


def section_template_audience(conn, template_code: str) -> str | None:
    """Audience d'un gabarit de section ('guest' | 'staff'), ou None si inconnu.
    Sert au gating du cahier équipe réservé à l'offre Pro (V2-18b)."""
    row = conn.execute(
        "SELECT audience FROM section_templates WHERE code = %s", (template_code,)
    ).fetchone()
    return row["audience"] if row else None


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
    "c.icon AS category_icon, c.map_color, c.travel_mode, "
    "p.name, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon, "
    "p.address, p.locality, p.phone, p.website, p.opening_hours, p.cuisine, "
    "p.weekday, p.weekday_note, p.description_md, "
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
               locality, phone, website, opening_hours, cuisine, weekday, weekday_note,
               description_md, owner_comment,
               dist_walk_m, walk_min, dist_drive_m, drive_min,
               source, status, fetched_at)
           VALUES (%(pid)s, %(category_code)s, %(name)s,
               ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
               %(address)s, %(locality)s, %(phone)s, %(website)s, %(opening_hours)s,
               %(cuisine)s, %(weekday)s, %(weekday_note)s,
               %(description_md)s, %(owner_comment)s,
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


def journey_facts(conn, property_ids: list[str]) -> dict[str, dict]:
    """Rassemble les FAITS de substance nécessaires au fil des étapes (V2-31,
    volet 2) pour un lot de logements — une requête agrégée par famille de faits,
    jamais N+1. Rien n'est déchiffré : les secrets sont testés par IS NOT NULL
    (invariant 5). Le jugement (garni ? étape faite ?) revient au module pur
    `api.journey`. Renvoie un dict par property_id (str)."""
    if not property_ids:
        return {}
    facts = {pid: {"sections": [], "keybox_present": False, "wifi_present": False,
                   "poi_counts": {}, "sends": 0} for pid in property_ids}
    # Sections voyageur (contenu brut — la substance est jugée par le module pur).
    for r in conn.execute(
        """SELECT ps.property_id, ps.template_code AS code, ps.content, ps.body_md
           FROM property_sections ps
           JOIN section_templates t ON t.code = ps.template_code
           WHERE ps.property_id = ANY(%s::uuid[]) AND t.audience = 'guest'""",
        (property_ids,),
    ).fetchall():
        facts[str(r["property_id"])]["sections"].append(
            {"code": r["code"], "content": r["content"], "body_md": r["body_md"]})
    # Présence des secrets (jamais déchiffrés — wifi multi-réseaux OU legacy).
    for r in conn.execute(
        """SELECT property_id,
                  (keybox_code_enc IS NOT NULL) AS kb,
                  (wifi_networks_enc IS NOT NULL OR wifi_ssid IS NOT NULL) AS wf
           FROM property_secrets WHERE property_id = ANY(%s::uuid[])""",
        (property_ids,),
    ).fetchall():
        f = facts[str(r["property_id"])]
        f["keybox_present"] = bool(r["kb"])
        f["wifi_present"] = bool(r["wf"])
    # POI par statut (l'enrichissement a-t-il produit des lieux ? reste-t-il à valider ?).
    for r in conn.execute(
        """SELECT property_id, status, count(*) AS n FROM pois
           WHERE property_id = ANY(%s::uuid[]) GROUP BY property_id, status""",
        (property_ids,),
    ).fetchall():
        facts[str(r["property_id"])]["poi_counts"][r["status"]] = r["n"]
    # Envois du guide (tout kind/origin — l'étape 7 est franchie dès le premier).
    for r in conn.execute(
        """SELECT property_id, count(*) AS n FROM guide_sends
           WHERE property_id = ANY(%s::uuid[]) GROUP BY property_id""",
        (property_ids,),
    ).fetchall():
        facts[str(r["property_id"])]["sends"] = r["n"]
    return facts


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


_POI_EDITABLE = ("category_code", "name", "address", "locality", "phone", "website",
                 "opening_hours", "cuisine", "weekday", "weekday_note",
                 "description_md", "owner_comment")


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


def count_owner_jobs_current_month(conn, owner_id: str) -> int:
    """Total des enrichissements du mois calendaire pour TOUS les logements du
    propriétaire (jauge « Mon abonnement », V2-05a). Même règle que le quota :
    hors jobs `failed` et hors traductions."""
    return conn.execute(
        """SELECT count(*) AS n FROM enrichment_jobs j
           JOIN properties p ON p.id = j.property_id
           WHERE p.owner_id = %s
             AND j.created_at >= date_trunc('month', now())
             AND j.status <> 'failed'
             AND j.trigger <> 'translate'""",
        (owner_id,),
    ).fetchone()["n"]


def max_published_langs_count(conn, owner_id: str) -> int:
    """Plus grand nombre de langues cibles publiées parmi les logements du
    propriétaire (jauge langues, V2-05a). 0 si aucun logement traduit."""
    row = conn.execute(
        """SELECT coalesce(max(cardinality(published_langs)), 0) AS n
           FROM properties WHERE owner_id = %s""",
        (owner_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


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
                  contact_backup, tourism_license, cover_media_id
           FROM properties
           WHERE guide_token = %s AND status = 'published'""",
        (token,),
    ).fetchone()


def get_published_property_by_id(conn, property_id: str) -> dict | None:
    """Logement publié désigné par son id (V2-23c, lien de séjour `/b/`). Mêmes
    colonnes publiques que `get_published_property_by_token`, **plus le
    `guide_token`** : le lien de séjour résout le logement côté serveur depuis le
    séjour du token, puis rend le guide via le `guide_token` (fetches internes de
    l'app : secrets/data/média). None si brouillon/archivé ou id inconnu."""
    return conn.execute(
        """SELECT id, name, address_line1, address_line2, postal_code,
                  city, region, country_code,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon,
                  default_lang, published_langs, access_mode, guide_token,
                  contact_name, contact_phone, contact_whatsapp, contact_email,
                  contact_backup, tourism_license, cover_media_id
           FROM properties
           WHERE id = %s AND status = 'published'""",
        (property_id,),
    ).fetchone()


def get_property_by_showcase_token(conn, token: str) -> dict | None:
    """Logement publié désigné par son **lien vitrine** (V2-23c). Mêmes colonnes
    publiques que `get_published_property_by_token` (jamais de secret). None si le
    token est inconnu ou le logement non publié — on ne révèle rien. Le préfixe
    d'URL est distinct (`/v/…`) : un token vitrine ne peut jamais être confondu
    avec un guide réel (ni côté code, ni dans les journaux)."""
    return conn.execute(
        """SELECT id, owner_id, name, address_line1, address_line2, postal_code,
                  city, region, country_code,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon,
                  default_lang, published_langs, access_mode,
                  contact_name, contact_phone, contact_whatsapp, contact_email,
                  contact_backup, tourism_license, cover_media_id
           FROM properties
           WHERE showcase_token = %s AND status = 'published'""",
        (token,),
    ).fetchone()


def get_booking_by_stay_token(conn, token: str) -> dict | None:
    """Séjour désigné par son **lien de séjour** (V2-23c), quel que soit son
    statut/nature (le router arbitre : logement du guide, actif, non expiré). None
    si le token est inconnu — le lien meurt avec le séjour (annulation/suppression
    → 404, on ne révèle rien). `guest_lang` pilote la langue par défaut du rendu ;
    `guest_name`/dates l'accueil personnalisé."""
    return conn.execute(
        """SELECT id, property_id, guest_name, guest_lang,
                  starts_on, ends_on, status, keybox_code_enc
           FROM bookings
           WHERE stay_token = %s""",
        (token,),
    ).fetchone()


def get_booking_keybox_enc(conn, property_id: str, booking_id: str) -> dict | None:
    """Surcharge chiffrée du code de boîte à clés d'un séjour (V2-23c volet 2),
    pour l'édition par le propriétaire. **Isolée de `_BOOKING_COLS`** à dessein :
    la surcharge ne doit JAMAIS fuir dans `BookingOut` / `GET /calendar` / une
    liste (même régime que `property_secrets`). None si le séjour n'appartient pas
    au logement (garde-fou multi-tenant)."""
    return conn.execute(
        "SELECT keybox_code_enc FROM bookings WHERE id = %s AND property_id = %s",
        (booking_id, property_id),
    ).fetchone()


def ensure_stay_token(conn, property_id: str, booking_id: str) -> str | None:
    """Renvoie le `stay_token` du séjour, le générant au **premier usage** (fenêtre
    d'envoi, §3.1). None si le séjour n'appartient pas au logement (garde-fou
    multi-tenant).

    Fabrique : **hex 128 bits `encode(gen_random_bytes(16),'hex')`** — la MÊME que
    `guide_token`/`staff_token` depuis le premier jour (§1.1, corrigé 02/08). La
    garde SQL `WHERE … AND stay_token IS NULL` rend la génération **idempotente et
    atomique** : deux clics simultanés ne créent qu'un token (la garde tranche côté
    base — le second UPDATE ne voit plus NULL et ne touche rien), puis on **relit**
    le token effectif. Un séjour garde son token une fois créé."""
    conn.execute(
        """UPDATE bookings SET stay_token = encode(gen_random_bytes(16), 'hex')
           WHERE id = %s AND property_id = %s AND stay_token IS NULL""",
        (booking_id, property_id))
    row = conn.execute(
        "SELECT stay_token FROM bookings WHERE id = %s AND property_id = %s",
        (booking_id, property_id)).fetchone()
    return row["stay_token"] if row else None


def ensure_showcase_token(conn, owner_id: str, property_id: str) -> str | None:
    """Renvoie le `showcase_token` du logement, le générant au **premier usage**
    (même fabrique hex 128 bits et même garde SQL idempotente/atomique que
    `ensure_stay_token`). Filtre par propriétaire (garde-fou multi-tenant)."""
    conn.execute(
        """UPDATE properties SET showcase_token = encode(gen_random_bytes(16), 'hex')
           WHERE id = %s AND owner_id = %s AND showcase_token IS NULL""",
        (property_id, owner_id))
    row = conn.execute(
        "SELECT showcase_token FROM properties WHERE id = %s AND owner_id = %s",
        (property_id, owner_id)).fetchone()
    return row["showcase_token"] if row else None


def record_guide_send(conn, *, property_id: str, booking_id: str | None,
                      kind: str, lang: str, recipient: str,
                      origin: str = "manual") -> dict:
    """Journalise un envoi du guide RÉUSSI (V2-23d) et renvoie la ligne créée.
    Écrit uniquement APRÈS un envoi SMTP réussi (jamais sur échec) — mémoire du
    dernier envoi (fenêtre) et **verrou d'idempotence** de l'automatisation J-7
    (volet 2). `origin` : 'manual' (fenêtre du back-office) ou 'auto' (timer J-7)."""
    return conn.execute(
        """INSERT INTO guide_sends
                       (property_id, booking_id, kind, lang, recipient, origin)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id, property_id, booking_id, kind, lang, recipient, origin,
                     sent_at""",
        (property_id, booking_id, kind, lang, recipient, origin)).fetchone()


def record_reminder(conn, *, property_id: str, booking_id: str, code: str) -> bool:
    """Journalise une relance du planificateur d'envoi (V2-36 pièce 1) de façon
    IDEMPOTENTE : une par séjour et par motif (contrainte UNIQUE (booking_id, code)).
    Renvoie True si la relance est NEUVE (à journaliser et à compter dans le bilan du
    run), False si elle avait déjà été émise (« pas une relance par jour » — le run
    reste alors silencieux pour ce séjour). Même modèle de verrou que `guide_sends`."""
    row = conn.execute(
        """INSERT INTO guide_reminders (property_id, booking_id, code)
           VALUES (%s, %s, %s)
           ON CONFLICT (booking_id, code) DO NOTHING
           RETURNING id""",
        (property_id, booking_id, code)).fetchone()
    return row is not None


def record_help_search(conn, *, owner_id: str, query: str,
                       results_count: int) -> None:
    """Journalise une recherche d'aide (V2-31 volet 3a). Best-effort côté API :
    l'appelant avale toute exception (un échec de journal ne casse jamais la
    recherche, purement front). Le taux de zéro-résultat (`results_count = 0`) est
    la métrique de santé de l'index."""
    conn.execute(
        """INSERT INTO help_searches (owner_id, query, results_count)
           VALUES (%s, %s, %s)""",
        (owner_id, query, results_count))


def list_auto_send_candidates(conn, *, today, horizon_end) -> list[dict]:
    """Séjours candidats à l'envoi automatique du guide à J-7 (V2-23d volet 2),
    tous logements confondus (rattrapage ops — jamais de filtre propriétaire).

    Borné à la **fenêtre d'arrivée** `[today, horizon_end]` (efficience ; le moteur
    pur `care.select_auto_sends` re-vérifie la fenêtre et tous les autres critères).
    Chaque ligne porte les colonnes de séjour PLUS le contexte du logement dont le
    moteur et la construction de l'email ont besoin : `auto_send_guide`, `published`
    (booléen), `property_name`, `default_lang`, `published_langs`, `cover_media_id`,
    `owner_email` (adresse du compte propriétaire → **Cci** de l'envoi automatique,
    V2-36 pièce 2), et `already_sent` (une ligne `guide_sends` kind='stay' existe
    déjà pour ce séjour — le **registre est le verrou d'idempotence**). La surcharge
    de code de boîte à clés n'est JAMAIS sélectionnée (absente de `_BOOKING_COLS`,
    invariant V2-23c volet 2)."""
    cols = ", ".join(f"b.{c.strip()}" for c in _BOOKING_COLS.split(","))
    return conn.execute(
        f"""SELECT {cols},
                   p.auto_send_guide,
                   (p.status = 'published')        AS published,
                   p.name                          AS property_name,
                   p.default_lang,
                   p.published_langs,
                   p.cover_media_id,
                   o.email                         AS owner_email,
                   EXISTS (SELECT 1 FROM guide_sends gs
                           WHERE gs.booking_id = b.id AND gs.kind = 'stay')
                                                   AS already_sent
              FROM bookings b
              JOIN properties p ON p.id = b.property_id
              JOIN owners o     ON o.id = p.owner_id
             WHERE b.starts_on >= %s AND b.starts_on <= %s
             ORDER BY b.starts_on, b.id""",
        (today, horizon_end)).fetchall()


def stay_sent_booking_ids(conn, property_id: str) -> set[str]:
    """Ensemble des id de séjours d'un logement pour lesquels une ligne `guide_sends`
    kind='stay' existe (tout origin : manuel, auto, WhatsApp assisté). Le registre
    est le verrou d'idempotence (V2-32) : sert à la vue du calendrier pour retirer
    ces séjours de la file WhatsApp ET éteindre le motif de relance email manquant."""
    rows = conn.execute(
        """SELECT DISTINCT booking_id FROM guide_sends
           WHERE property_id = %s AND kind = 'stay' AND booking_id IS NOT NULL""",
        (property_id,)).fetchall()
    return {str(r["booking_id"]) for r in rows}


def last_guide_send(conn, property_id: str, *, kind: str,
                    booking_id: str | None) -> dict | None:
    """Dernier envoi du guide pour une cible : un séjour donné (kind='stay' +
    booking_id) ou la vitrine du logement (kind='showcase'). None si jamais
    envoyé. Sert à afficher « envoyé le … » dans la fenêtre d'envoi."""
    if kind == "stay":
        row = conn.execute(
            """SELECT kind, lang, recipient, sent_at FROM guide_sends
               WHERE property_id = %s AND kind = 'stay' AND booking_id = %s
               ORDER BY sent_at DESC LIMIT 1""",
            (property_id, booking_id)).fetchone()
    else:
        row = conn.execute(
            """SELECT kind, lang, recipient, sent_at FROM guide_sends
               WHERE property_id = %s AND kind = 'showcase'
               ORDER BY sent_at DESC LIMIT 1""",
            (property_id,)).fetchone()
    return row


def guide_sections(conn, property_id: str) -> list[dict]:
    """Sections **voyageur** visibles d'un guide (audience='guest'), avec les
    métadonnées de leur template. Les sections 'staff' (cahier de l'équipe
    d'entretien, M-13) ne sortent JAMAIS ici (invariant 7).

    Invariant « sections vierges » (V2-07 volet 1bis) : une section qui **déclare
    un fait de zone** (`field_schema.area_facts`, ex. `food_delivery`, `waste_rules`,
    `noise_rules`) doit pouvoir porter son encart **même si le propriétaire ne l'a
    jamais enregistrée** — sans ligne `property_sections`, l'encart resterait
    invisible (bug prouvé en prod 11/08). On part donc des **templates guest** en
    LEFT JOIN : une section sans ligne (`virtual=TRUE`) n'est incluse que si elle
    déclare un fait de zone (jamais une coquille pour les 40+ autres sections). Le
    tri des vraies coquilles vides (fait absent/vide) se fait au rendu
    (`guide_page._prune_virtual_sections`), qui connaît la vacuité par type de fait.

    Un `is_visible = FALSE` **explicite** masque toujours (choix du propriétaire, en
    miroir de l'invariant de visibilité des médias) — même si un fait est présent."""
    return conn.execute(
        """SELECT t.code, t.chapter, t.sort_order, t.icon, t.name_i18n,
                  t.field_schema, t.is_sensitive, ps.content, ps.body_md,
                  (ps.id IS NULL) AS virtual
           FROM section_templates t
           LEFT JOIN property_sections ps
             ON ps.template_code = t.code AND ps.property_id = %s
           WHERE t.audience = 'guest'
             AND COALESCE(ps.is_visible, TRUE) = TRUE
             AND (ps.id IS NOT NULL OR jsonb_exists(t.field_schema, 'area_facts'))
           ORDER BY t.sort_order""",
        (property_id,),
    ).fetchall()


def guide_pois(conn, property_id: str) -> list[dict]:
    """POI approuvés/édités uniquement (jamais 'suggested' ni 'rejected'),
    avec la catégorie (icône/couleur du seed). Distances déjà en base."""
    return conn.execute(
        """SELECT p.id, p.category_code, c.chapter, c.name_i18n AS category_name,
                  c.icon AS category_icon, c.map_color, c.travel_mode,
                  p.name, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon,
                  p.address, p.locality, p.phone, p.website, p.opening_hours, p.cuisine,
                  p.weekday, p.weekday_note,
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


def get_showcase_media(conn, showcase_token: str, media_id: str) -> dict | None:
    """Média d'un logement **publié** servi via son lien vitrine (V2-23c). Mêmes
    garanties de visibilité que `get_public_media` (section visible & 'guest', ou
    média de niveau logement), mais résolu par `showcase_token` : le vrai
    `guide_token` ne transite jamais par la page vitrine (progrès de sécurité)."""
    return conn.execute(
        """SELECT m.kind, m.storage_key
           FROM media m
           JOIN properties pr ON pr.id = m.property_id
           LEFT JOIN property_sections ps ON ps.id = m.section_id
           LEFT JOIN section_templates t ON t.code = ps.template_code
           WHERE m.id = %s AND pr.showcase_token = %s AND pr.status = 'published'
             AND (m.section_id IS NULL
                  OR (ps.is_visible = TRUE AND t.audience = 'guest'))""",
        (media_id, showcase_token),
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
    et total d'éléments porteurs de texte. **Lecture seule** : ne nettoie rien.

    « Périmé » agrège le manquant (jamais traduit) et le périmé (source modifiée) :
    ce sont les éléments que la prochaine traduction (re)traitera.

    **Parité stricte du prédicat (V2-41).** Les fraîches se comptent sur EXACTEMENT
    le même prédicat que le total — le parent doit encore être un porteur de texte
    (section guest à contenu, POI retenu à texte). Sans ce re-filtrage, une ligne
    `*_translations` **orpheline** (le parent a perdu son texte à une purge, mais la
    ligne fraîche subsiste) était comptée fraîche → le badge disait « à jour » en
    masquant une vraie périmée (bug prouvé le 18/08 : 42 orphelines sur Ballarin).
    Le plafond `min(fresh, total)` qui cachait l'écart est **supprimé** : avec le
    prédicat aligné, `fresh ≤ total` par construction (PK `(parent, lang)` → au plus
    une ligne par parent) ; si `fresh > total` survenait, ce serait un bug à VOIR."""
    # `text_carrier` : prédicat commun total ⇄ fraîches (la seule source de vérité).
    _SEC_HAS_TEXT = "(ps.body_md IS NOT NULL OR ps.content <> '{}'::jsonb)"
    _POI_HAS_TEXT = "(p.description_md IS NOT NULL OR p.owner_comment IS NOT NULL)"

    # Éléments source porteurs de texte (sections guest avec contenu, POI retenus).
    sec_total = conn.execute(
        f"""SELECT count(*) AS n FROM property_sections ps
            JOIN section_templates t ON t.code = ps.template_code
            WHERE ps.property_id = %s AND t.audience = 'guest' AND {_SEC_HAS_TEXT}""",
        (property_id,)).fetchone()["n"]
    poi_total = conn.execute(
        f"""SELECT count(*) AS n FROM pois p WHERE p.property_id = %s
              AND p.status IN ('approved', 'edited') AND {_POI_HAS_TEXT}""",
        (property_id,)).fetchone()["n"]
    total = sec_total + poi_total

    per_lang: dict[str, dict] = {}
    for lang in langs:
        # Fraîches : MÊME jointure + MÊME prédicat de porteur de texte que le total,
        # plus `is_stale = FALSE`. Une ligne dont le parent n'a plus de texte (orpheline)
        # ne compte donc RIEN — ni au total, ni aux fraîches.
        fresh = conn.execute(
            f"""SELECT count(*) AS n FROM section_translations st
                JOIN property_sections ps ON ps.id = st.section_id
                JOIN section_templates t ON t.code = ps.template_code
                WHERE ps.property_id = %s AND st.lang = %s
                  AND t.audience = 'guest' AND {_SEC_HAS_TEXT}
                  AND st.is_stale = FALSE""",
            (property_id, lang)).fetchone()["n"]
        fresh += conn.execute(
            f"""SELECT count(*) AS n FROM poi_translations pt
                JOIN pois p ON p.id = pt.poi_id
                WHERE p.property_id = %s AND pt.lang = %s
                  AND p.status IN ('approved', 'edited') AND {_POI_HAS_TEXT}
                  AND pt.is_stale = FALSE""",
            (property_id, lang)).fetchone()["n"]
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
        """SELECT id, owner_id, name, city, region, country_code, status,
                  care_rules, default_checkin_time, default_checkout_time
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


# ── Calendrier des séjours (V2-23a) ──────────────────────────────────────────
#
# `property_calendars.ical_url_enc` est un secret (chiffré AES par le router) :
# le repo ne le déchiffre jamais ; il le transporte tel quel (bytea) et c'est
# l'appelant autorisé qui déchiffre pour synchroniser ou masquer l'affichage.

# Colonnes d'un flux hors URL chiffrée (jamais l'URL en clair dans une réponse).
_CAL_COLS = ("id, property_id, platform, last_sync_at, last_sync_status, "
             "sync_error, created_at, updated_at")


def list_calendars(conn, property_id: str) -> list[dict]:
    """Flux d'un logement (sans l'URL chiffrée — affichage masqué côté router)."""
    return conn.execute(
        f"SELECT {_CAL_COLS} FROM property_calendars WHERE property_id = %s "
        "ORDER BY created_at",
        (property_id,),
    ).fetchall()


def list_calendars_with_url(conn, property_id: str) -> list[dict]:
    """Flux d'un logement, **URL chiffrée comprise** (synchro « maintenant » de
    tous les flux du logement). Jamais renvoyé tel quel dans une réponse API."""
    return conn.execute(
        f"SELECT {_CAL_COLS}, ical_url_enc FROM property_calendars "
        "WHERE property_id = %s ORDER BY created_at",
        (property_id,),
    ).fetchall()


def get_calendar(conn, property_id: str, calendar_id: str) -> dict | None:
    """Un flux du logement, **URL chiffrée comprise** (pour la synchro)."""
    return conn.execute(
        f"SELECT {_CAL_COLS}, ical_url_enc FROM property_calendars "
        "WHERE id = %s AND property_id = %s",
        (calendar_id, property_id),
    ).fetchone()


def list_all_calendars(conn) -> list[dict]:
    """Tous les flux de tous les logements (synchro périodique ops). URL chiffrée
    incluse — le script la déchiffre pour fetcher, jamais journalisée en clair."""
    return conn.execute(
        f"SELECT {_CAL_COLS}, ical_url_enc FROM property_calendars "
        "ORDER BY property_id, created_at"
    ).fetchall()


def create_calendar(conn, property_id: str, *, platform: str,
                    ical_url_enc: bytes) -> dict:
    return conn.execute(
        f"""INSERT INTO property_calendars (property_id, platform, ical_url_enc)
            VALUES (%s, %s, %s) RETURNING {_CAL_COLS}""",
        (property_id, platform, ical_url_enc),
    ).fetchone()


def recent_calendar_sync_seconds(conn, property_id: str) -> float | None:
    """Nombre de secondes depuis la synchro la plus récente d'un flux du logement,
    ou None si aucun flux n'a jamais été synchronisé (rate-limit du bouton
    « Synchroniser maintenant »)."""
    row = conn.execute(
        """SELECT EXTRACT(EPOCH FROM (now() - max(last_sync_at))) AS s
           FROM property_calendars
           WHERE property_id = %s AND last_sync_at IS NOT NULL""",
        (property_id,),
    ).fetchone()
    return float(row["s"]) if row and row["s"] is not None else None


def update_calendar_sync(conn, calendar_id: str, *, status: str,
                         error: str | None) -> None:
    """Horodate le résultat d'une synchro (last_sync_at = now, statut, message)."""
    conn.execute(
        """UPDATE property_calendars
           SET last_sync_at = now(), last_sync_status = %s, sync_error = %s,
               updated_at = now()
           WHERE id = %s""",
        (status, error, calendar_id),
    )


def delete_calendar(conn, property_id: str, calendar_id: str) -> bool:
    """Supprime un flux. Ses séjours ne sont **jamais** supprimés : ils passent
    d'abord 'cancelled' (conservés, grisés — invariant maison), puis la ligne de
    flux est retirée (leur `calendar_id` devient NULL via ON DELETE SET NULL)."""
    row = conn.execute(
        "SELECT id FROM property_calendars WHERE id = %s AND property_id = %s",
        (calendar_id, property_id),
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE bookings SET status = 'cancelled', updated_at = now() "
        "WHERE calendar_id = %s AND status <> 'cancelled'",
        (calendar_id,),
    )
    conn.execute(
        "DELETE FROM property_calendars WHERE id = %s AND property_id = %s",
        (calendar_id, property_id),
    )
    return True


# ── Séjours ──────────────────────────────────────────────────────────────────

_BOOKING_COLS = ("id, property_id, calendar_id, starts_on, ends_on, "
                 "checkin_time, checkout_time, luggage_drop_time, "
                 "luggage_until_time, source, external_uid, "
                 "guest_name, guest_contact, guest_phone, guest_email, guest_lang, "
                 "notes, nature, status, "
                 "guest_count, children_ages, "
                 "linked_booking_id, dates_overridden, feed_starts_on, feed_ends_on, "
                 "created_at, updated_at")

# Champs d'un séjour modifiables à la main (jamais écrasés par une synchro). `nature`
# et `linked_booking_id` en font partie : la synchro ne touche QUE les dates (et
# réactive un 'cancelled' réapparu), jamais la sémantique saisie (invariant 13).
# `dates_overridden` (V2-23g) rejoint la liste dans le MÊME esprit : le propriétaire
# qui déplace les dates d'un import en prend possession → le flux ne les rafraîchit
# plus. Le router le pose automatiquement (l'intention EST la modification).
_BOOKING_UPDATABLE = ("starts_on", "ends_on", "checkin_time", "checkout_time",
                      "luggage_drop_time", "luggage_until_time",
                      "guest_name", "guest_contact", "guest_phone", "guest_email",
                      "guest_lang", "notes", "nature", "status",
                      "source", "linked_booking_id", "guest_count", "children_ages",
                      "keybox_code_enc", "dates_overridden")


def list_bookings(conn, property_id: str) -> list[dict]:
    """Tous les séjours du logement (annulés compris — le router les replie).
    Triés par arrivée puis départ (ordre chronologique de la vue « Séjours »)."""
    return conn.execute(
        f"SELECT {_BOOKING_COLS} FROM bookings WHERE property_id = %s "
        "ORDER BY starts_on, ends_on",
        (property_id,),
    ).fetchall()


def get_booking(conn, property_id: str, booking_id: str) -> dict | None:
    return conn.execute(
        f"SELECT {_BOOKING_COLS} FROM bookings "
        "WHERE id = %s AND property_id = %s",
        (booking_id, property_id),
    ).fetchone()


def create_booking(conn, property_id: str, data: dict) -> dict:
    """Saisie directe d'un séjour (jamais rattachée à un flux : calendar_id NULL,
    external_uid NULL). Les heures NULL signifient « heures standard du logement ».
    `status` reste au défaut 'active' (cycle de vie) ; c'est `nature` qui porte la
    sémantique (réservation, privé, travaux…)."""
    cols = ("starts_on", "ends_on", "checkin_time", "checkout_time",
            "luggage_drop_time", "luggage_until_time", "source",
            "guest_name", "guest_contact", "guest_phone", "guest_email",
            "guest_lang", "notes", "nature",
            "guest_count", "children_ages", "keybox_code_enc")
    values = [property_id] + [data.get(c) for c in cols]
    placeholders = ", ".join(["%s"] * (len(cols) + 1))
    return conn.execute(
        f"INSERT INTO bookings (property_id, {', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING {_BOOKING_COLS}",
        values,
    ).fetchone()


def update_booking(conn, property_id: str, booking_id: str,
                   fields: dict) -> dict | None:
    """Complète/édite un séjour (nom, contact, heures, notes, nature, dates,
    bagages, rattachement). Sert notamment à **qualifier** un import 'unqualified'
    en 'reservation'/'private'/… (V2-23b, complétion). None si le séjour
    n'appartient pas au logement."""
    sets, params = [], []
    for key in _BOOKING_UPDATABLE:
        if key in fields:
            sets.append(f"{key} = %s")
            params.append(fields[key])
    if not sets:
        return get_booking(conn, property_id, booking_id)
    sets.append("updated_at = now()")
    params.extend([booking_id, property_id])
    return conn.execute(
        f"UPDATE bookings SET {', '.join(sets)} "
        f"WHERE id = %s AND property_id = %s RETURNING {_BOOKING_COLS}",
        params,
    ).fetchone()


def absorb_block_into_master(conn, property_id: str, block_id: str,
                             master_id: str) -> None:
    """Rattachement d'un bloc miroir (§0.5) : le maître ABSORBE la substance du
    bloc avant qu'il ne disparaisse de la vue (V2-23f). Sans quoi les demandes du
    bloc (booking_requests) et ses coordonnées s'évanouiraient silencieusement —
    perte de données réelle constatée en prod (cas Tracy Russel, 05/08).

    DEUX transferts, dans la MÊME transaction que la pose de `linked_booking_id`
    (le routeur les enchaîne) :

    1. **Demandes** : TOUTES (pending ET accepted) migrent vers le maître — les
       acceptées nourrissent `plan_interventions`, le planning doit rester vrai.
       Un transfert, jamais une création/destruction.
    2. **Champs de fiche** : repris SEULEMENT si le maître est vide/NULL et le bloc
       renseigné (COALESCE + NULLIF sur les vides) — une valeur du maître n'est
       JAMAIS écrasée (esprit invariant 13). `keybox_code_enc` se copie tel quel
       (bytea chiffré, aucun déchiffrement — invariant 5).

    **Asymétrie assumée** : le DÉTACHEMENT (`linked_booking_id`→NULL) ne rejoue
    RIEN à l'envers — le transfert est un acte, pas un miroir. Demandes et champs
    migrés restent sur le maître (pas de migration inverse).

    Idempotent : re-rattacher le même bloc ne double rien — les demandes ont déjà
    migré (le WHERE ne matche plus), et les champs du maître, désormais garnis,
    sont conservés par COALESCE."""
    conn.execute(
        "UPDATE booking_requests SET booking_id = %s WHERE booking_id = %s",
        (master_id, block_id))
    conn.execute(
        """UPDATE bookings AS m SET
             guest_phone       = COALESCE(NULLIF(m.guest_phone, ''), b.guest_phone),
             guest_email       = COALESCE(NULLIF(m.guest_email, ''), b.guest_email),
             guest_lang        = COALESCE(NULLIF(m.guest_lang, ''), b.guest_lang),
             notes             = COALESCE(NULLIF(m.notes, ''), b.notes),
             guest_count       = COALESCE(m.guest_count, b.guest_count),
             children_ages     = COALESCE(NULLIF(m.children_ages, '{}'::int[]),
                                          b.children_ages),
             keybox_code_enc   = COALESCE(m.keybox_code_enc, b.keybox_code_enc),
             luggage_drop_time = COALESCE(m.luggage_drop_time, b.luggage_drop_time),
             updated_at        = now()
           FROM bookings AS b
           WHERE m.id = %s AND m.property_id = %s
             AND b.id = %s AND b.property_id = %s""",
        (master_id, property_id, block_id, property_id))


def inherit_booking_fiche(conn, property_id: str, source_id: str,
                          target_id: str) -> None:
    """Succession d'identifiants (V2-23h) : le NOUVEAU séjour (`target`) hérite de la
    fiche de son prédécesseur ANNULÉ (`source`), réémis sous un nouvel uid par la
    plateforme. Réutilise la mécanique d'absorption V2-23f (mêmes champs, guest_name
    EN PLUS ; copie seulement vers le vide du target ; demandes migrées ;
    keybox_code_enc tel quel — bytea chiffré, aucun déchiffrement, invariant 5).

    DEUX différences GRAVÉES avec le rattachement d'un bloc miroir (V2-23f) :

    (a) **Pas de `linked_booking_id`** — la source reste 'cancelled' et invisible ;
        ce n'est PAS un bloc miroir (le double d'un séjour présent) mais une
        SUCCESSION (l'uid a changé, l'ancien est mort).
    (b) **Le registre `guide_sends` n'est JAMAIS hérité** — c'est le cœur et c'est
        voulu (invariant maison) : le target est vierge d'envoi, donc la fenêtre
        d'envoi et le J-7 renverront un lien VIVANT au NOUVEAU token. L'incident du
        06/08 : le lien /b/ de 9 h 00 était mort chez la cliente parce que
        l'ancien séjour était annulé.

    Idempotent (patron V2-23f) : la copie ne remplit que du vide (COALESCE + NULLIF),
    et les demandes migrées ne matchent plus la 2ᵉ fois."""
    conn.execute(
        "UPDATE booking_requests SET booking_id = %s "
        "WHERE booking_id = %s AND status IN ('pending', 'accepted')",
        (target_id, source_id))
    conn.execute(
        """UPDATE bookings AS m SET
             guest_name        = COALESCE(NULLIF(m.guest_name, ''), s.guest_name),
             guest_phone       = COALESCE(NULLIF(m.guest_phone, ''), s.guest_phone),
             guest_email       = COALESCE(NULLIF(m.guest_email, ''), s.guest_email),
             guest_lang        = COALESCE(NULLIF(m.guest_lang, ''), s.guest_lang),
             notes             = COALESCE(NULLIF(m.notes, ''), s.notes),
             guest_count       = COALESCE(m.guest_count, s.guest_count),
             children_ages     = COALESCE(NULLIF(m.children_ages, '{}'::int[]),
                                          s.children_ages),
             keybox_code_enc   = COALESCE(m.keybox_code_enc, s.keybox_code_enc),
             luggage_drop_time = COALESCE(m.luggage_drop_time, s.luggage_drop_time),
             updated_at        = now()
           FROM bookings AS s
           WHERE m.id = %s AND m.property_id = %s
             AND s.id = %s AND s.property_id = %s""",
        (target_id, property_id, source_id, property_id))


def delete_booking(conn, property_id: str, booking_id: str) -> str | None:
    """Retire un séjour. Une **saisie directe** (aucun flux) est réellement
    supprimée ; un séjour **importé** est conservé et passé 'cancelled' (il
    reviendrait de toute façon à la prochaine synchro — invariant maison).
    Renvoie 'deleted' | 'cancelled', ou None si le séjour est introuvable."""
    row = conn.execute(
        "SELECT calendar_id, external_uid FROM bookings "
        "WHERE id = %s AND property_id = %s",
        (booking_id, property_id),
    ).fetchone()
    if not row:
        return None
    is_direct = row["calendar_id"] is None and row["external_uid"] is None
    if is_direct:
        conn.execute("DELETE FROM bookings WHERE id = %s", (booking_id,))
        return "deleted"
    conn.execute(
        "UPDATE bookings SET status = 'cancelled', updated_at = now() "
        "WHERE id = %s", (booking_id,))
    return "cancelled"


# ── Upsert de synchronisation (idempotent par UID iCal) ──────────────────────

def upsert_imported_booking(conn, *, property_id: str, calendar_id: str,
                            source: str, external_uid: str,
                            starts_on, ends_on, nature: str) -> bool:
    """Insère ou met à jour un séjour importé, clé (calendar_id, external_uid).

    Idempotent (invariant 2 de la mission) : re-synchroniser N fois ne crée aucun
    doublon. Sur conflit, seules les **dates** sont rafraîchies — les champs
    saisis à la main (nom, contact, heures, notes, **nature**, rattachement) ne
    sont **jamais** écrasés (invariant 13). La `nature` déduite du flux ne sert
    donc qu'à la **création** ; une qualification manuelle ('unqualified' →
    'private'…) survit à toutes les synchros suivantes.

    **Dates ajustées à la main (V2-23g)** : si `dates_overridden` est posé, le flux
    ne rafraîchit **plus** starts_on/ends_on (le propriétaire en a pris possession —
    le flux les écrasait silencieusement, cas Tracy). starts_on/ends_on restent
    alors la source de vérité de TOUT l'aval (rotations, planning, envoi J-7 qui lit
    starts_on). Mais on continue de **mémoriser** `feed_starts_on`/`feed_ends_on` à
    chaque passage : c'est la seule trace de ce que le flux annonce désormais, d'où
    se déduit le **signal de divergence** (« le flux indique autre chose »). Sans
    marqueur, les dates du flux s'appliquent comme avant et feed_* = starts/ends.

    Le `status` (cycle de vie) : un import est créé 'active' ; un séjour disparu
    passé 'cancelled' (`cancel_missing_bookings`) et qui **réapparaît** dans le
    flux est **réactivé** ('active'). Un séjour 'active' n'est jamais retouché. La
    protection des dates survit à ce cycle (marqueur et feed_* intacts).

    Renvoie True si un séjour a été **créé**, False s'il a été mis à jour."""
    row = conn.execute(
        f"""INSERT INTO bookings (property_id, calendar_id, source, external_uid,
                                  starts_on, ends_on, feed_starts_on, feed_ends_on,
                                  nature, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            ON CONFLICT (calendar_id, external_uid)
              WHERE calendar_id IS NOT NULL AND external_uid IS NOT NULL
            DO UPDATE SET
                starts_on = CASE WHEN bookings.dates_overridden
                                 THEN bookings.starts_on ELSE EXCLUDED.starts_on END,
                ends_on   = CASE WHEN bookings.dates_overridden
                                 THEN bookings.ends_on ELSE EXCLUDED.ends_on END,
                feed_starts_on = EXCLUDED.feed_starts_on,
                feed_ends_on   = EXCLUDED.feed_ends_on,
                status    = CASE WHEN bookings.status = 'cancelled'
                                 THEN 'active' ELSE bookings.status END,
                updated_at = now()
            RETURNING (xmax = 0) AS inserted""",
        (property_id, calendar_id, source, external_uid, starts_on, ends_on,
         starts_on, ends_on, nature),
    ).fetchone()
    return bool(row["inserted"])


def reset_booking_to_feed_dates(conn, property_id: str,
                                booking_id: str) -> dict | None:
    """« Reprendre les dates du flux » (V2-23g) : réaligne les dates saisies sur les
    **dernières dates du flux** mémorisées et **rend la main à la synchro**
    (`dates_overridden` → FALSE). Le chemin INVERSE de la pose du marqueur — isolé
    à dessein du PATCH ordinaire, qui reposerait aussitôt le marqueur puisque
    l'écriture de dates VAUT prise de possession.

    Réservé aux séjours **importés** (`external_uid IS NOT NULL`) : une saisie
    directe n'a pas de flux à reprendre → None (404 côté router). Si aucune date de
    flux n'est mémorisée (jamais synchronisé depuis la migration 026), les dates
    restent inchangées (COALESCE), seul le marqueur retombe."""
    return conn.execute(
        f"""UPDATE bookings SET
              starts_on = COALESCE(feed_starts_on, starts_on),
              ends_on   = COALESCE(feed_ends_on, ends_on),
              dates_overridden = FALSE,
              updated_at = now()
            WHERE id = %s AND property_id = %s AND external_uid IS NOT NULL
            RETURNING {_BOOKING_COLS}""",
        (booking_id, property_id),
    ).fetchone()


def cancel_missing_bookings(conn, calendar_id: str,
                            seen_uids: list[str]) -> int:
    """Passe 'cancelled' les séjours de ce flux dont l'UID n'est **plus** présent
    dans la dernière synchro (annulation côté plateforme). Conservés, jamais
    supprimés. Renvoie le nombre de séjours annulés."""
    row = conn.execute(
        """UPDATE bookings SET status = 'cancelled', updated_at = now()
           WHERE calendar_id = %s AND status <> 'cancelled'
             AND NOT (external_uid = ANY(%s))
           RETURNING id""",
        (calendar_id, seen_uids),
    ).fetchall()
    return len(row)


# ── Catalogue de demandes particulières (V2-23b, §1.2) ───────────────────────

_REQ_TYPE_COLS = ("id, property_id, code, label, sort_order, is_active, created_at")


def seed_request_types(conn, property_id: str, types: list[dict]) -> int:
    """Amorce le catalogue d'un logement (à sa création **ou** en rattrapage).
    Idempotent : un code déjà présent n'est pas dupliqué (ON CONFLICT sur
    (property_id, code)). Renvoie le nombre de types réellement **insérés**."""
    inserted = 0
    for i, t in enumerate(types):
        row = conn.execute(
            """INSERT INTO property_request_types
                   (property_id, code, label, sort_order)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (property_id, code) DO NOTHING
               RETURNING id""",
            (property_id, t["code"], t["label"], t.get("sort_order", i))).fetchone()
        inserted += int(row is not None)
    return inserted


def list_properties_care(conn) -> list[dict]:
    """(id, care_rules) de **tous** les logements (rattrapage ops, tous
    propriétaires confondus). `care_rules = {}` = jamais amorcé."""
    return conn.execute(
        "SELECT id, care_rules FROM properties ORDER BY created_at").fetchall()


def set_care_rules(conn, property_id: str, care_rules: dict) -> None:
    """Pose les règles d'entretien d'un logement (rattrapage ops). Sérialise le
    JSONB — jamais un objet brut, jamais un littéral SQL recopié (invariant 8 :
    la vérité est `care.default_care_rules`, pas une copie figée)."""
    conn.execute("UPDATE properties SET care_rules = %s WHERE id = %s",
                 (json.dumps(care_rules), property_id))


def list_request_types(conn, property_id: str,
                       *, active_only: bool = False) -> list[dict]:
    sql = (f"SELECT {_REQ_TYPE_COLS} FROM property_request_types "
           "WHERE property_id = %s")
    if active_only:
        sql += " AND is_active"
    sql += " ORDER BY sort_order, label"
    return conn.execute(sql, (property_id,)).fetchall()


def create_request_type(conn, property_id: str, *, code: str, label: str,
                        sort_order: int = 0) -> dict:
    return conn.execute(
        f"""INSERT INTO property_request_types (property_id, code, label, sort_order)
            VALUES (%s, %s, %s, %s) RETURNING {_REQ_TYPE_COLS}""",
        (property_id, code, label, sort_order),
    ).fetchone()


def update_request_type(conn, property_id: str, type_id: str,
                        fields: dict) -> dict | None:
    allowed = ("label", "sort_order", "is_active")
    sets = [f"{k} = %s" for k in allowed if k in fields]
    if not sets:
        return conn.execute(
            f"SELECT {_REQ_TYPE_COLS} FROM property_request_types "
            "WHERE id = %s AND property_id = %s", (type_id, property_id)).fetchone()
    params = [fields[k] for k in allowed if k in fields] + [type_id, property_id]
    return conn.execute(
        f"UPDATE property_request_types SET {', '.join(sets)} "
        f"WHERE id = %s AND property_id = %s RETURNING {_REQ_TYPE_COLS}",
        params,
    ).fetchone()


def get_request_type(conn, property_id: str, type_id: str) -> dict | None:
    return conn.execute(
        f"SELECT {_REQ_TYPE_COLS} FROM property_request_types "
        "WHERE id = %s AND property_id = %s", (type_id, property_id)).fetchone()


def get_request_type_by_code(conn, property_id: str, code: str) -> dict | None:
    return conn.execute(
        f"SELECT {_REQ_TYPE_COLS} FROM property_request_types "
        "WHERE property_id = %s AND code = %s", (property_id, code)).fetchone()


# ── Demandes rattachées à un séjour (V2-23b, §1.2 ; guest au volet 3) ─────────

_REQ_COLS = ("id, booking_id, request_type_id, label, quantity, note, origin, "
             "status, created_at, updated_at")


def list_booking_requests(conn, booking_id: str) -> list[dict]:
    return conn.execute(
        f"SELECT {_REQ_COLS} FROM booking_requests WHERE booking_id = %s "
        "ORDER BY created_at", (booking_id,)).fetchall()


def list_requests_for_property(conn, property_id: str) -> list[dict]:
    """Toutes les demandes des séjours d'un logement (une charge pour la vue)."""
    return conn.execute(
        f"""SELECT r.id, r.booking_id, r.request_type_id, r.label, r.quantity,
                   r.note, r.origin, r.status, r.created_at, r.updated_at
            FROM booking_requests r
            JOIN bookings b ON b.id = r.booking_id
            WHERE b.property_id = %s ORDER BY r.created_at""",
        (property_id,)).fetchall()


def create_booking_request(conn, booking_id: str, *, request_type_id: str | None,
                           label: str | None, quantity: int, note: str | None,
                           origin: str, status: str) -> dict:
    return conn.execute(
        f"""INSERT INTO booking_requests
                (booking_id, request_type_id, label, quantity, note, origin, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_REQ_COLS}""",
        (booking_id, request_type_id, label, quantity, note, origin, status),
    ).fetchone()


def update_booking_request(conn, request_id: str, fields: dict) -> dict | None:
    allowed = ("quantity", "note", "status")
    sets = [f"{k} = %s" for k in allowed if k in fields]
    if not sets:
        return conn.execute(
            f"SELECT {_REQ_COLS} FROM booking_requests WHERE id = %s",
            (request_id,)).fetchone()
    sets.append("updated_at = now()")
    params = [fields[k] for k in allowed if k in fields] + [request_id]
    return conn.execute(
        f"UPDATE booking_requests SET {', '.join(sets)} WHERE id = %s "
        f"RETURNING {_REQ_COLS}", params).fetchone()


def get_booking_request(conn, property_id: str, request_id: str) -> dict | None:
    """Une demande, garantie appartenir à un séjour du logement (isolation §7)."""
    return conn.execute(
        f"""SELECT {', '.join('r.' + c for c in _REQ_COLS.split(', '))}
            FROM booking_requests r JOIN bookings b ON b.id = r.booking_id
            WHERE r.id = %s AND b.property_id = %s""",
        (request_id, property_id)).fetchone()


def delete_booking_request(conn, property_id: str, request_id: str) -> bool:
    row = conn.execute(
        """DELETE FROM booking_requests r USING bookings b
           WHERE r.booking_id = b.id AND r.id = %s AND b.property_id = %s
           RETURNING r.id""", (request_id, property_id)).fetchone()
    return row is not None


# ── Demande du voyageur (V2-23b, volet 3, §3.1) ──────────────────────────────

def current_or_next_booking_by_guide_token(conn, token: str, today) -> dict | None:
    """Séjour **occupé** auquel rattacher une demande du voyageur (§3.1).

    Le lien du guide est propre au logement, pas au séjour : on rattache au séjour
    EN COURS à la date du jour, à défaut au **suivant**. Occupé = nature
    reservation/private, actif, non rattaché (miroir de `care._is_occupied`). Le
    logement doit être **publié** (le guide n'existe que publié). None si aucun
    séjour occupé n'est en cours ni à venir (ou token inconnu/brouillon).

    Ordre : les séjours qui recouvrent aujourd'hui (`starts_on <= today`) d'abord,
    puis le plus proche à venir ; on écarte les séjours déjà partis (`ends_on <
    today`)."""
    return conn.execute(
        """SELECT b.id, b.property_id, b.guest_name, b.starts_on, b.ends_on
             FROM bookings b
             JOIN properties p ON p.id = b.property_id
            WHERE p.guide_token = %s AND p.status = 'published'
              AND b.status = 'active'
              AND b.nature IN ('reservation', 'private')
              AND b.linked_booking_id IS NULL
              AND b.ends_on >= %s
            ORDER BY (b.starts_on <= %s) DESC, b.starts_on
            LIMIT 1""",
        (token, today, today)).fetchone()


def requestable_section_label(conn, token: str, code: str) -> str | None:
    """Libellé FR de la demande d'une section **requestable** visible d'un guide
    publié, ou None si la section n'existe pas / n'est pas visible / n'est pas
    requestable (`field_schema.request.label`). Garde-fou : le libellé stocké dans
    la demande vient TOUJOURS du template (jamais d'une valeur libre du voyageur),
    et la section doit être réellement offerte sur ce guide (audience guest,
    visible). Le staff n'a jamais de bouton de demande (invariant 7)."""
    row = conn.execute(
        """SELECT t.field_schema -> 'request' ->> 'label' AS label
             FROM property_sections ps
             JOIN section_templates t ON t.code = ps.template_code
             JOIN properties p ON p.id = ps.property_id
            WHERE p.guide_token = %s AND p.status = 'published'
              AND ps.template_code = %s AND ps.is_visible = TRUE
              AND t.audience = 'guest'
              AND t.field_schema ? 'request'""",
        (token, code)).fetchone()
    return row["label"] if row else None


def seconds_since_last_guest_request(conn, property_id: str) -> float | None:
    """Ancienneté (en secondes) de la **dernière** demande d'un voyageur pour ce
    logement, ou None s'il n'y en a jamais eu. Sert au rate-limit anti-abus (le
    voyageur n'est pas authentifié → cadence par guide, §3.1)."""
    row = conn.execute(
        """SELECT EXTRACT(EPOCH FROM (now() - max(r.created_at))) AS secs
             FROM booking_requests r
             JOIN bookings b ON b.id = r.booking_id
            WHERE b.property_id = %s AND r.origin = 'guest'""",
        (property_id,)).fetchone()
    return float(row["secs"]) if row and row["secs"] is not None else None


def get_owner_by_property(conn, property_id: str) -> dict | None:
    """Propriétaire (email, nom) d'un logement — pour la notification de demande."""
    return conn.execute(
        """SELECT o.id, o.email, o.full_name
             FROM owners o JOIN properties p ON p.owner_id = o.id
            WHERE p.id = %s""", (property_id,)).fetchone()
