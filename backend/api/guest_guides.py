"""Orchestrateur de l'offre « Guide Voyageur » (V2-54, Mission A).

Socle backend d'un guide touristique auto-généré à l'adresse d'un lieu de vacances :
cache de proximité → géocodage (garde mismatch V2-46) → création de la fiche guest →
enrichissement complet (le pipeline juge et publie en fin de course) → traduction
7 langues (best-effort). Fonction PURE d'orchestration réutilisée par le script de
recette `ops/make_guest_guide.py` (Mission A) ET, plus tard, par le webhook Stripe
(Mission B) — l'entrée de génération est ici, une seule fois.

Ne fait AUCUN appel réseau lui-même : géocodage/enrichissement/traduction délèguent aux
modules `enrich.*`. Le paiement, la collecte d'e-mail et le tunnel sont hors périmètre.
"""
from __future__ import annotations

import logging

from enrich import db, geocode, pipeline, translate
from enrich.settings import settings

from . import emails, repo
from .config import settings as api_settings

log = logging.getLogger("casaguide.guest_guides")


class GuestGuideError(Exception):
    """Génération refusée (motif métier, message FR prêt à afficher)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GuestGuideMismatch(GuestGuideError):
    """Le point géocodé ne correspond pas à la commune/CP saisis (V2-46) : le
    vacancier doit ajuster le point sur la carte du tunnel avant de générer."""

    def __init__(self, message: str, mismatch=None):
        super().__init__("geocode_mismatch", message)
        self.mismatch = mismatch


def _check_abuse(conn, email: str | None, ip: str | None) -> None:
    """Applique les limites anti-abus par e-mail et par IP (0 = illimité)."""
    lim_e = settings.guest_max_per_email_per_day
    if email and lim_e and repo.count_guest_generations(conn, email=email) >= lim_e:
        raise GuestGuideError(
            "rate_limited_email",
            "Trop de guides générés avec cet e-mail aujourd'hui. Réessayez demain.")
    lim_ip = settings.guest_max_per_ip_per_day
    if ip and lim_ip and repo.count_guest_generations(conn, ip=ip) >= lim_ip:
        raise GuestGuideError(
            "rate_limited_ip",
            "Trop de guides générés depuis cette connexion aujourd'hui. "
            "Réessayez demain.")


def generate_guest_guide(*, city: str, country_code: str,
                         lat: float | None = None, lon: float | None = None,
                         address: str | None = None, postal_code: str | None = None,
                         region: str | None = None, name: str | None = None,
                         email: str | None = None, ip: str | None = None,
                         use_claude: bool = True, do_translate: bool = True,
                         enforce_limits: bool = True) -> dict:
    """Génère (ou ressert depuis le cache) un guide voyageur. Renvoie
    `{"property": <row publiée>, "cached": bool, "summary": <résumé pipeline|None>}`.

    Étapes : (0) anti-abus ; (1) position — `lat`/`lon` fournis (point ajusté dans le
    tunnel) OU géocodage de l'adresse, **refus propre sur mismatch** (V2-46) ;
    (2) **cache** — un guide guest publié à < `guest_cache_radius_m` et < `guest_cache_
    max_age_days` est resservi (marge pure) ; (3) création de la fiche guest + commit ;
    (4) `pipeline.run_with_retries` (juge auto + publication en interne) ; (5)
    `translate.run` best-effort → 7 langues. Lève `GuestGuideError`/`GuestGuideMismatch`
    sur refus métier."""
    with db.connect() as conn:
        # Un ACHAT payé génère toujours (le paiement EST le gate) → `enforce_limits`
        # False côté fulfillment ; les limites e-mail/IP protègent l'entrée gratuite.
        if enforce_limits:
            _check_abuse(conn, email, ip)

        # (1) Position.
        if lat is None or lon is None:
            geo = geocode.geocode(street=address, city=city, postalcode=postal_code,
                                  country_code=country_code)
            if geo["accuracy"] == "mismatch":
                mm = geo.get("mismatch")
                msg = (mm.message_fr() if mm is not None
                       else "commune/code postal incohérents avec la saisie")
                raise GuestGuideMismatch(msg, mismatch=mm)
            lat, lon, accuracy = geo["lat"], geo["lon"], geo["accuracy"]
        else:
            accuracy = "manual"

        # (2) Cache de proximité (resservir un guide voisin récent).
        cached = repo.find_recent_guest_guide_near(
            conn, lat, lon, settings.guest_cache_radius_m,
            settings.guest_cache_max_age_days)
        if cached is not None:
            repo.record_guest_generation(conn, email, ip, str(cached["id"]))
            conn.commit()
            log.info("Guide voyageur resservi depuis le cache : %s", cached["id"])
            return {"property": cached, "cached": True, "summary": None}

        # (3) Création de la fiche guest (position déjà connue).
        prop = repo.create_guest_property(
            conn, name=name or f"Guide — {city}", city=city,
            country_code=country_code, lat=lat, lon=lon, address_line1=address,
            postal_code=postal_code, region=region, geocode_accuracy=accuracy)
        property_id = str(prop["id"])
        repo.record_guest_generation(conn, email, ip, property_id)
        conn.commit()

    # (4) Enrichissement complet (le pipeline juge et publie en fin de course).
    summary = pipeline.run_with_retries(
        property_id, use_claude=use_claude, trigger="guest")

    # (5) Traduction 7 langues (best-effort : un guide FR utile vaut mieux qu'un échec).
    if do_translate:
        try:
            translate.run(property_id)
        except Exception as exc:  # noqa: BLE001 — jamais bloquant
            log.warning("Traduction du guide voyageur %s non résolue : %s",
                        property_id, exc)

    with db.connect() as conn:
        published = repo.get_published_property_by_id(conn, property_id)
    return {"property": published, "cached": False, "summary": summary}


# ── Paiement one-shot Stripe (V2-54 Mission B) ───────────────────────────────
# Le WEBHOOK est la seule source de vérité (doctrine V2-27) : la génération ne
# démarre qu'à `checkout.session.completed` PAYÉ. La génération elle-même est lourde
# (géocodage + moisson + IA + traduction, plusieurs minutes) → JAMAIS dans le webhook
# (Stripe re-rejoue au-delà de quelques secondes) : le webhook accuse vite, la
# génération part en tâche de fond, idempotente par verrou sur la commande.

def is_guest_checkout(event: dict) -> bool:
    """Vrai si l'événement est la fin d'un Checkout de guide voyageur (mode payment,
    metadata kind='guest_guide') — à router hors du chemin abonnements."""
    obj = (event.get("data") or {}).get("object") or {}
    return (obj.get("object") == "checkout.session"
            and (obj.get("metadata") or {}).get("kind") == "guest_guide")


def on_guest_checkout_paid(conn, event: dict, background, mailer,
                           *, base_url: str) -> str:
    """`checkout.session.completed` d'un guide voyageur : confirme le paiement et
    ENQUEUE la génération (jamais inline). Idempotent : un rejeu ne relance pas une
    génération faite (le verrou de `fulfill_order_bg` tranche). Commit AVANT la tâche
    de fond (V2-16b : la commande doit être visible de sa connexion séparée)."""
    obj = (event.get("data") or {}).get("object") or {}
    if obj.get("payment_status") != "paid":
        return "guest_unpaid"          # paiement non confirmé → aucune génération
    session_id = obj.get("id")
    order = repo.get_guest_order_by_session(conn, session_id)
    if order is None:                  # repli : order_id porté par les metadata
        oid = (obj.get("metadata") or {}).get("order_id")
        order = repo.get_guest_order(conn, oid) if oid else None
    if order is None:
        log.warning("Checkout guest inconnu (session=%s)", session_id)
        return "guest_unknown"
    if order["status"] == "done":
        return "guest_already_done"
    repo.mark_guest_order_paid(conn, str(order["id"]))
    conn.commit()                      # visible pour la tâche de fond (V2-16b)
    background.add_task(fulfill_order_bg, str(order["id"]), mailer, base_url)
    return "guest_enqueued"


def fulfill_order_bg(order_id: str, mailer, base_url: str) -> None:
    """Tâche de fond : génère le guide d'une commande PAYÉE puis livre le lien par
    e-mail ; sur échec, e-mail de reprise (ajuster le point, sans re-paiement).
    Idempotente par verrou atomique `paid|failed → generating`."""
    with db.connect() as conn:
        if not repo.lock_guest_order_for_generation(conn, order_id):
            conn.commit()
            return                     # déjà en cours / déjà faite ailleurs
        order = repo.get_guest_order(conn, order_id)
        conn.commit()
    if order is None:
        return
    try:
        res = generate_guest_guide(
            city=order["city"], country_code=order["country_code"],
            lat=order["lat"], lon=order["lon"], address=order["address_line1"],
            postal_code=order["postal_code"], region=order["region"],
            email=order["email"], ip=order["ip"],
            use_claude=True, do_translate=True, enforce_limits=False)
    except Exception as exc:  # noqa: BLE001 — mismatch/pipeline : jamais de re-paiement
        log.warning("Génération guide voyageur (commande %s) échouée : %s",
                    order_id, exc)
        with db.connect() as conn:
            repo.fail_guest_order(conn, order_id, f"{type(exc).__name__}: {exc}")
            conn.commit()
        retry_url = f"{base_url.rstrip('/')}/#/voyageur/reprise/{order['token']}"
        _send_bg_safe(mailer, order["email"],
                      emails.guide_retry_email(retry_url=retry_url,
                                               lang=order["lang"]))
        return
    token = (res.get("property") or {}).get("guide_token")
    pid = (res.get("property") or {}).get("id")
    with db.connect() as conn:
        repo.complete_guest_order(conn, order_id, property_id=str(pid),
                                  guide_token=token)
        conn.commit()
    url = f"{base_url.rstrip('/')}/g/{token}"
    _send_bg_safe(mailer, order["email"],
                  emails.guide_purchase_email(url=url, lang=order["lang"]))


def retry_paid_order(order: dict, background, mailer, *, base_url: str,
                     lat: float | None = None, lon: float | None = None,
                     address: str | None = None) -> None:
    """Reprise d'une commande PAYÉE non aboutie (§3) : ajuste le point si fourni puis
    ré-enqueue la génération. Jamais de re-paiement. À appeler sur une commande
    'paid'/'failed' uniquement (garanti par le routeur)."""
    with db.connect() as conn:
        if lat is not None and lon is not None:
            repo.update_guest_order_point(conn, str(order["id"]), lat=lat, lon=lon,
                                          address_line1=address)
        conn.commit()
    background.add_task(fulfill_order_bg, str(order["id"]), mailer, base_url)


def resend_guide(conn, email: str, *, mailer, base_url: str) -> None:
    """Renvoie le dernier guide livré pour un e-mail (endpoint « renvoyer mon
    guide »). Silencieux si rien à renvoyer ou si la cadence anti-abus n'est pas
    respectée (l'endpoint répond 200 constant, anti-énumération)."""
    order = repo.latest_delivered_order_for_email(conn, email)
    if order is None or not order.get("guide_token"):
        return
    if not repo.guest_resend_allowed(conn, str(order["id"]),
                                     api_settings.guest_resend_min_interval_s):
        return
    repo.touch_guest_order_delivered(conn, str(order["id"]))
    conn.commit()
    url = f"{base_url.rstrip('/')}/g/{order['guide_token']}"
    _send_bg_safe(mailer, email,
                  emails.guide_purchase_email(url=url, lang=order["lang"],
                                              resend=True))


def _send_bg_safe(mailer, to: str, email) -> None:
    """Envoi best-effort (jamais bloquant) — même doctrine que auth._send_email_bg."""
    try:
        mailer.send(to, email)
    except Exception:  # noqa: BLE001
        log.warning("Envoi e-mail guide voyageur vers %s échoué (ignoré).", to,
                    exc_info=True)
