"""Parsing des flux iCal du calendrier des séjours (V2-23a).

Module **pur** (aucun réseau) : il transforme le texte iCal d'un flux plateforme
(Airbnb / Vrbo-Abritel / Booking) en une liste d'événements normalisés, prêts à
l'upsert par UID (`api.calendars`). Le fetch réseau vit dans `api.calendars`.

Sémantique de dates — invariant maison : on modélise un séjour comme un intervalle
**semi-ouvert** `[arrivée, départ)`. C'est exactement la convention iCal des
événements « journée entière » (`VALUE=DATE`), dont le `DTEND` est **exclusif** :
`DTSTART;VALUE=DATE:20260812` / `DTEND;VALUE=DATE:20260831` = arrivée le 12,
départ le 31 (nuits 12→30). Deux séjours qui se touchent (départ = arrivée le même
jour) ne se chevauchent donc pas : c'est une rotation.

Robustesse (invariant 3 de la mission) : un flux malformé lève `ICalError` (jamais
une exception brute qui fuit vers l'appelant) ; un flux valide mais vide renvoie
une liste vide (un calendrier neuf est légitime, pas une erreur).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass

from icalendar import Calendar

# Marqueurs de blocage manuel côté plateforme (fermeture, indisponibilité) — par
# opposition à une vraie réservation. Recherche en minuscules dans le SUMMARY.
_BLOCKED_HINTS = (
    "not available", "unavailable", "not-available", "blocked", "block",
    "closed", "indispo", "non disponible", "no disponible", "gesperrt",
)


class ICalError(Exception):
    """Flux iCal illisible (réponse non-iCal, syntaxe cassée…)."""


@dataclass(frozen=True)
class ICalEvent:
    """Un VEVENT normalisé, prêt pour l'upsert par UID."""
    uid: str
    summary: str
    starts_on: _dt.date          # arrivée
    ends_on: _dt.date            # départ (exclusif : dernière nuit = ends_on - 1)
    status: str                  # 'confirmed' | 'blocked'


def _as_date(value) -> _dt.date | None:
    """Ramène un DTSTART/DTEND (date entière OU datetime, avec ou sans fuseau) à
    une date. Les datetimes sont ramenés à leur jour (l'heure de plateforme n'est
    pas fiable ; les heures effectives d'un séjour sont saisies à la main)."""
    if value is None:
        return None
    dt = getattr(value, "dt", value)   # icalendar enveloppe dans .dt
    if isinstance(dt, _dt.datetime):
        return dt.date()
    if isinstance(dt, _dt.date):
        return dt
    return None


def _status_from_summary(summary: str) -> str:
    low = summary.lower()
    return "blocked" if any(h in low for h in _BLOCKED_HINTS) else "confirmed"


def _stable_uid(summary: str, start: _dt.date, end: _dt.date) -> str:
    """UID de repli déterministe pour un VEVENT sans UID (rare — les plateformes
    en fournissent toujours un). Stable → l'idempotence de l'upsert tient quand
    même (même événement = même clé de synchro en synchro)."""
    seed = f"{start.isoformat()}|{end.isoformat()}|{summary}".encode("utf-8")
    return "gen-" + hashlib.sha1(seed).hexdigest()[:24]


def parse_events(text: str | bytes) -> list[ICalEvent]:
    """Parse le texte iCal et renvoie ses VEVENT normalisés.

    Chaque événement gagne : `starts_on` (DTSTART ramené au jour), `ends_on`
    (DTEND exclusif ramené au jour ; à défaut, journée entière → +1 jour,
    datetime → au moins une nuit), un `status` déduit du SUMMARY, un `uid`
    (celui du flux, sinon un repli déterministe). Les événements sans DTSTART
    exploitable sont ignorés (impossible à placer).
    """
    if isinstance(text, str):
        text = text.encode("utf-8")
    try:
        cal = Calendar.from_ical(text)
    except (ValueError, TypeError) as exc:
        raise ICalError(f"flux iCal illisible : {exc}") from exc

    events: list[ICalEvent] = []
    for comp in cal.walk("VEVENT"):
        start = _as_date(comp.get("DTSTART"))
        if start is None:
            continue  # un séjour sans arrivée n'a pas de sens : ignoré
        end = _as_date(comp.get("DTEND"))
        if end is None:
            # Ni DTEND ni DURATION exploitable → journée entière = une nuit.
            end = start + _dt.timedelta(days=1)
        elif end <= start:
            # Datetime dégénéré (même jour) → au moins une nuit, jamais à l'envers.
            end = start + _dt.timedelta(days=1)

        summary = str(comp.get("SUMMARY") or "").strip()
        raw_uid = comp.get("UID")
        uid = str(raw_uid).strip() if raw_uid else ""
        if not uid:
            uid = _stable_uid(summary, start, end)

        events.append(ICalEvent(
            uid=uid, summary=summary, starts_on=start, ends_on=end,
            status=_status_from_summary(summary)))
    return events
