"""Envoi du guide de séjour — voie unique partagée (V2-23d).

Volet 1 a introduit l'envoi manuel depuis la fenêtre du back-office
(`routers/send.py`). Volet 2 ajoute l'envoi **automatique à J-7** (timer
quotidien, `ops/send_guides.py`). Les deux construisent le **même** email de
séjour : pour éviter toute dérive (langue, vignette, lien, overlay i18n), la
construction vit ici, en un seul endroit (`build_stay_email`).

Ce module contient aussi le **cœur testable** de l'automatisation J-7
(`run_auto_send`) : la sélection PURE est dans `care.select_auto_sends`, et ici on
l'exécute contre la base (email + trace `guide_sends` origin='auto' APRÈS envoi
réussi, commit explicite, un échec SMTP n'arrête jamais la boucle). Le script
`ops/send_guides.py` n'est qu'un mince habillage CLI, comme `sync_calendars.py`
enveloppe `calendars.sync_all`.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

from . import care, emails, i18n, repo

log = logging.getLogger("casaguide.guidesend")


# ── Construction de l'email de séjour (partagée manuel ↔ automatique) ────────

def offered_langs(conn, prop: dict) -> list[str]:
    """Langues réellement offertes par CE guide : langue source + langues publiées
    du logement, bornées au REGISTRE (invariant 15). Le registre fait foi."""
    registry = repo.published_language_codes(conn)
    default = (prop.get("default_lang") or "fr")
    prop_langs = [l for l in (prop.get("published_langs") or []) if l in registry]
    return [default] + [l for l in prop_langs if l != default]


def stay_natural_lang(conn, prop: dict, booking: dict) -> str:
    """Langue « naturelle » du lien de séjour : `guest_lang` si offerte par le
    guide, sinon la langue source du logement (invariant 15)."""
    gl = (booking.get("guest_lang") or "").lower()
    return gl if gl in offered_langs(conn, prop) else (prop.get("default_lang") or "fr")


def target_image_url(conn, prop: dict, base: str, api_base: str) -> str:
    """Vignette de la cible pour l'email : la **photo de couverture** désignée
    d'abord (V2-30, si servable), sinon la première photo du logement, sinon
    l'image de marque générée. URL absolue servie via le préfixe de la cible
    (`/b/…` ou `/v/…`) — jamais un `/g/{guide_token}` (le token éternel ne fuite
    pas)."""
    media = repo.guide_media(conn, str(prop["id"]))
    cover_id = prop.get("cover_media_id")
    if cover_id:
        for m in media:
            if str(m["id"]) == str(cover_id) and m["kind"] == "photo":
                return f"{base}{api_base}/media/{m['id']}"
    for m in media:
        if m["kind"] == "photo":
            return f"{base}{api_base}/media/{m['id']}"
    return f"{base}{api_base}/og-image.png"


def link(base: str, api_base: str, lang: str, natural: str) -> str:
    """Lien de la cible dans la langue choisie : `?lang=` seulement si elle diffère
    de la langue par défaut de la page (sinon le lien nu suffit — §3.5)."""
    if lang == natural:
        return f"{base}{api_base}"
    return f"{base}{api_base}?lang={lang}"


def build_localized(conn, lang: str, build):
    """Construit un email en posant l'overlay i18n de la langue effective (langues
    supplémentaires nl/de/it/sq → `ui_translations`, clés `email.*`) le temps de la
    construction. FR/EN/ES → overlay vide, rendu depuis le code."""
    tok = i18n.set_overlay(repo.ui_translations(conn, lang))
    try:
        return build()
    finally:
        i18n.reset_overlay(tok)


def build_stay_email(conn, prop: dict, booking: dict, *, token: str, base: str,
                     lang: str) -> emails.Email:
    """Email HTML « guide de séjour » prêt à envoyer, dans `lang` (déjà résolue par
    l'appelant). Voie unique du manuel (routers/send.py) et de l'auto (run_auto_send)."""
    api_base = f"/b/{token}"
    natural = stay_natural_lang(conn, prop, booking)
    url = link(base, api_base, lang, natural)
    # Accueil au PRÉNOM (copie actée « Bonjour {prénom}, »).
    first_name = (booking.get("guest_name") or "").strip().split()[:1]
    return build_localized(conn, lang, lambda: emails.guide_stay_email(
        property_name=prop["name"],
        guest_name=(first_name[0] if first_name else None),
        start=booking["starts_on"].strftime("%d/%m"),
        end=booking["ends_on"].strftime("%d/%m"),
        url=url, image_url=target_image_url(conn, prop, base, api_base), lang=lang))


# ── Automatisation J-7 : exécution contre la base (cœur testable) ────────────

@dataclass
class AutoSendReport:
    """Bilan d'un passage du planificateur J-7."""
    candidates: int = 0          # séjours dans la fenêtre inspectés
    sent: int = 0                # emails envoyés + tracés
    reminders: int = 0           # éligibles sans email (relance calendrier)
    failures: list[str] = field(default_factory=list)  # booking_id en échec SMTP


def _prop_of(row: dict) -> dict:
    """Reconstruit le `dict` logement minimal attendu par la construction d'email
    à partir d'une ligne de `repo.list_auto_send_candidates` (contexte joint)."""
    return {"id": row["property_id"], "name": row["property_name"],
            "default_lang": row["default_lang"],
            "published_langs": row["published_langs"],
            "cover_media_id": row["cover_media_id"]}


def run_auto_send(conn, mailer, *, base_url: str, today: _dt.date,
                  dry_run: bool = False) -> AutoSendReport:
    """Envoie automatiquement le guide aux séjours à J-7 (V2-23d volet 2).

    Sélection PURE (`care.select_auto_sends`) puis, pour chaque séjour retenu :
    on assure le `stay_token`, on construit le même email que le canal manuel, on
    envoie, et **APRÈS un envoi réussi** on trace `guide_sends` origin='auto' avec
    un `conn.commit()` explicite (le registre est le verrou d'idempotence : un
    re-run le même jour ne renvoie rien). Un échec SMTP est journalisé, **non
    tracé** (ré-essai au passage du lendemain, le séjour est encore dans la
    fenêtre) et **n'arrête pas la boucle**. `dry_run` : planifie sans envoyer.

    `today` est toujours passé (jamais d'horloge implicite — testable)."""
    base = base_url.rstrip("/")
    rows = repo.list_auto_send_candidates(
        conn, today=today,
        horizon_end=today + _dt.timedelta(days=care.AUTO_SEND_HORIZON_DAYS))
    plan = care.select_auto_sends(rows, today=today)
    report = AutoSendReport(candidates=len(rows), reminders=len(plan.to_remind))

    for r in plan.to_remind:
        log.info("· relance : séjour %s dans la fenêtre, email manquant (%s).",
                 r["id"], r["property_name"])

    for booking in plan.to_send:
        pid = str(booking["property_id"])
        bid = str(booking["id"])
        recipient = care.effective_email(booking)   # garanti non nul par le moteur
        if dry_run:
            log.info("[dry-run] envoi séjour %s → %s (%s)", bid, recipient,
                     booking["property_name"])
            report.sent += 1
            continue
        try:
            token = repo.ensure_stay_token(conn, pid, bid)
            if not token:                            # course/suppression concurrente
                conn.rollback()
                continue
            prop = _prop_of(booking)
            lang = stay_natural_lang(conn, prop, booking)
            email = build_stay_email(conn, prop, booking, token=token, base=base,
                                     lang=lang)
            mailer.send(recipient, email)
        except Exception:  # noqa: BLE001 — best-effort : jamais bloquant (V2-16)
            conn.rollback()   # défait l'éventuel stay_token créé (regénéré demain)
            log.warning("Envoi automatique du guide (séjour %s) échoué — non tracé, "
                        "ré-essai au prochain passage.", bid, exc_info=True)
            report.failures.append(bid)
            continue
        # Trace APRÈS envoi réussi (verrou d'idempotence) + commit explicite (V2-16b).
        repo.record_guide_send(conn, property_id=pid, booking_id=bid, kind="stay",
                               lang=lang, recipient=recipient, origin="auto")
        conn.commit()
        report.sent += 1
        log.info("· envoyé : séjour %s → %s (%s, %s)", bid, recipient,
                 booking["property_name"], lang)
    return report
