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

from . import repo

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
                         use_claude: bool = True, do_translate: bool = True) -> dict:
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
