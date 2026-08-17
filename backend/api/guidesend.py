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


# ── Désignation HUMAINE d'un séjour pour les journaux (audit UX, principe 0.4) ─
# « Aucune surface lue par un humain ne parle en identifiants techniques » : le
# journal du J-7 disait « séjour 3ccdc040… » (« une référence pour un
# programmeur », André). Il dit désormais « séjour Tracy Russel · 08–22.08 ·
# Villa Ballarin ». Toutes les données sont déjà dans la ligne candidate jointe.

def _fmt_stay_dates(start: _dt.date, end: _dt.date) -> str:
    """Dates d'un séjour en format compact : « 08–22.08 » (même mois) ou
    « 28.08–03.09 » (mois différents)."""
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day:02d}–{end.day:02d}.{start.month:02d}"
    return f"{start.day:02d}.{start.month:02d}–{end.day:02d}.{end.month:02d}"


def booking_label(row: dict) -> str:
    """Désignation lisible d'un séjour pour les logs et le rapport du script J-7 :
    « séjour <nom ou « sans nom »> · <dates> · <logement> ». Jamais l'UUID."""
    name = (row.get("guest_name") or "").strip() or "sans nom"
    dates = _fmt_stay_dates(row["starts_on"], row["ends_on"])
    return f"séjour {name} · {dates} · {row['property_name']}"


# ── Construction de l'email de séjour (partagée manuel ↔ automatique) ────────

def offered_langs(conn, prop: dict) -> list[str]:
    """Langues réellement offertes par CE guide : langue source + langues publiées
    du logement, bornées au REGISTRE (invariant 15). Le registre fait foi."""
    registry = repo.published_language_codes(conn)
    default = (prop.get("default_lang") or "fr")
    prop_langs = [l for l in (prop.get("published_langs") or []) if l in registry]
    return [default] + [l for l in prop_langs if l != default]


def stay_natural_lang(conn, prop: dict, booking: dict) -> str:
    """Langue « naturelle » du lien de séjour ET de l'email — **le point de décision
    de la précédence de langue §3.5 (acté V2-36)**.

    Trois branches, dans l'ordre :
      1. `guest_lang` **renseignée et offerte** par le guide (publiée + registre,
         invariant 15) → elle gagne **partout** : email envoyé dans cette langue, lien
         `/b/` ouvert dans cette langue (côté page, `app.js initLang` lit
         `data-guest-lang` et fige la fiche — la devinette `navigator.language` ne s'y
         superpose plus). C'est la fiche qui fait foi.
      2. `guest_lang` **vide** → repli **langue du logement** (`default_lang`) pour
         l'email (comportement du 16/08, Tracy Taylor : sans langue, l'email partait —
         et part toujours — en français ; désormais documenté et **compensé** par la
         relance « langue non précisée », `care.select_lang_reminders`). Côté lien
         `/b/` nu, la devinette M-09 reste active (le serveur ne fige rien).
      3. `guest_lang` renseignée mais **non offerte** → repli langue du logement aussi
         (on ne promet pas une langue que le guide ne sait pas générer, invariant 15) —
         sans relance : le locataire A précisé une langue, l'écart n'est pas un oubli.

    Orthogonal à tout cela : un **clic explicite** du visiteur sur le sélecteur de
    langue de la page gagne et est retenu (`?lang=`, clé `casaguide:lang:b` — §3.5
    amendement 2). L'envoi email n'entre pas en jeu à ce moment (la page est déjà chez
    le locataire)."""
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
    lang_reminders: int = 0      # relances « langue non précisée » NEUVES (V2-36)
    failures: list[str] = field(default_factory=list)  # booking_id en échec SMTP
    failure_labels: list[str] = field(default_factory=list)  # même échecs, lisibles


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
    # Fenêtre de sélection élargie d'UN jour : la relance « langue non précisée »
    # (V2-36 pièce 1) prévient la VEILLE de l'entrée dans la fenêtre d'envoi (arrivée =
    # today + horizon + 1). `select_auto_sends` re-borne les envois à `horizon` (un
    # séjour à today+horizon+1 est relancé mais jamais envoyé).
    rows = repo.list_auto_send_candidates(
        conn, today=today,
        horizon_end=today + _dt.timedelta(days=care.AUTO_SEND_HORIZON_DAYS + 1))
    plan = care.select_auto_sends(rows, today=today)
    report = AutoSendReport(candidates=len(rows), reminders=len(plan.to_remind))

    # ── Pièce 1 : relances « langue non précisée » AVANT la passe d'envoi. ──────
    # Idempotence par le registre `guide_reminders` : une relance par séjour et par
    # motif, jamais une par jour (un run silencieux si la relance a déjà été émise).
    for r in care.select_lang_reminders(rows, today=today):
        pid, bid = str(r["property_id"]), str(r["id"])
        days = (r["starts_on"] - today).days
        when = "demain" if days > care.AUTO_SEND_HORIZON_DAYS else "aujourd'hui"
        if dry_run:
            log.info("[dry-run] relance : langue non précisée · %s · envoi prévu %s.",
                     booking_label(r), when)
            report.lang_reminders += 1
            continue
        if repo.record_reminder(conn, property_id=pid, booking_id=bid,
                                code="lang_missing"):
            conn.commit()
            report.lang_reminders += 1
            log.info("· relance : langue non précisée · %s · envoi prévu %s.",
                     booking_label(r), when)

    for r in plan.to_remind:
        log.info("· relance : %s — dans la fenêtre, email manquant.",
                 booking_label(r))

    for booking in plan.to_send:
        pid = str(booking["property_id"])
        bid = str(booking["id"])
        recipient = care.effective_email(booking)   # garanti non nul par le moteur
        # Cci propriétaire (V2-36 pièce 2) : l'email auto part aussi en copie cachée à
        # l'adresse du compte → il sait que c'est parti et peut vérifier le contenu.
        owner_bcc = booking.get("owner_email") or None
        if dry_run:
            log.info("[dry-run] envoi : %s → %s%s", booking_label(booking), recipient,
                     " (Cci propriétaire)" if owner_bcc else "")
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
            mailer.send(recipient, email, bcc=owner_bcc)
        except Exception:  # noqa: BLE001 — best-effort : jamais bloquant (V2-16)
            conn.rollback()   # défait l'éventuel stay_token créé (regénéré demain)
            log.warning("Envoi automatique du guide échoué (%s) — non tracé, "
                        "ré-essai au prochain passage.", booking_label(booking),
                        exc_info=True)
            report.failures.append(bid)
            report.failure_labels.append(booking_label(booking))
            continue
        # Trace APRÈS envoi réussi (verrou d'idempotence) + commit explicite (V2-16b).
        repo.record_guide_send(conn, property_id=pid, booking_id=bid, kind="stay",
                               lang=lang, recipient=recipient, origin="auto")
        conn.commit()
        report.sent += 1
        log.info("· envoyé : %s → %s (%s)%s", booking_label(booking), recipient, lang,
                 " (Cci propriétaire)" if owner_bcc else "")
    return report
