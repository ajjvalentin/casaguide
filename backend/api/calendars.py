"""Moteur de synchronisation des flux iCal du calendrier des séjours (V2-23a).

Nouveau flux réseau **sortant** (serveur → plateformes), le premier régulier hors
OSM / Claude / Stripe / SMTP. Règles (invariant 3 de la mission) : timeout court,
User-Agent propre, suivi des redirections, et **jamais bloquant** — l'échec d'un
flux enregistre son erreur (`last_sync_status='error'`, `sync_error`) sans jamais
faire remonter d'exception ni empêcher la synchro des autres flux.

Chaîne : `fetch_ical(url)` (réseau) → `ical.parse_events(text)` (pur) → upsert par
UID (`repo`, idempotent) → séjours disparus passés 'cancelled'. La fonction de
fetch est **injectable** (`deps.get_calendar_fetcher`) → tests sans réseau.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Callable

import httpx

from enrich.settings import settings

from . import crypto, ical, repo

log = logging.getLogger("casaguide.calendars")

# Fetch iCal : (url) -> texte du flux. Défaut ci-dessous ; injecté dans les tests.
CalendarFetcher = Callable[[str], str]

# Timeout court : une plateforme lente ne doit pas bloquer la synchro (inv. 3).
FETCH_TIMEOUT_S = 10.0
# Garde-fou taille : un flux iCal légitime pèse quelques dizaines de Ko ; au-delà,
# on refuse (réponse anormale, page d'erreur HTML volumineuse…).
MAX_ICAL_BYTES = 5 * 1024 * 1024


@dataclass
class SyncResult:
    """Bilan d'une synchro de flux (retourné à l'UI « X séjours importés »)."""
    status: str = "ok"                 # 'ok' | 'error'
    error: str | None = None
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    total: int = 0                     # nombre d'événements lus dans le flux

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {"status": self.status, "error": self.error,
                "created": self.created, "updated": self.updated,
                "cancelled": self.cancelled, "total": self.total}


def fetch_ical(url: str, client: httpx.Client | None = None) -> str:
    """Récupère le texte d'un flux iCal. Timeout court, User-Agent propre, suit
    les redirections (les plateformes redirigent souvent vers un CDN). Lève sur
    erreur réseau/HTTP — le moteur (`sync_calendar`) attrape et enregistre."""
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    try:
        resp = client.get(url, headers={"User-Agent": settings.user_agent})
        resp.raise_for_status()
        raw = resp.content
        if len(raw) > MAX_ICAL_BYTES:
            raise ValueError("flux iCal anormalement volumineux")
        return raw.decode("utf-8", errors="replace")
    finally:
        if own:
            client.close()


def sync_calendar(conn, calendar: dict, *, fetch: CalendarFetcher) -> SyncResult:
    """Synchronise **un** flux (déjà chargé, `ical_url_enc` compris).

    Non bloquant : toute erreur (réseau, HTTP, flux illisible) est enregistrée sur
    le flux et renvoyée dans `SyncResult` — jamais propagée. Idempotent : upsert
    par UID, aucun doublon ; les séjours disparus du flux passent 'cancelled'.
    """
    calendar_id = str(calendar["id"])
    property_id = str(calendar["property_id"])
    source = calendar["platform"]
    try:
        url = crypto.decrypt(calendar["ical_url_enc"])
        text = fetch(url)
        events = ical.parse_events(text)
    except Exception as exc:  # noqa: BLE001 — inv. 3 : jamais bloquant
        msg = _short_error(exc)
        log.warning("Synchro du flux %s échouée : %s", calendar_id, msg)
        repo.update_calendar_sync(conn, calendar_id, status="error", error=msg)
        return SyncResult(status="error", error=msg)

    created = updated = 0
    seen: list[str] = []
    for ev in events:
        seen.append(ev.uid)
        inserted = repo.upsert_imported_booking(
            conn, property_id=property_id, calendar_id=calendar_id,
            source=source, external_uid=ev.uid,
            starts_on=ev.starts_on, ends_on=ev.ends_on, nature=ev.nature)
        created += int(inserted)
        updated += int(not inserted)
    cancelled = repo.cancel_missing_bookings(conn, calendar_id, seen)
    repo.update_calendar_sync(conn, calendar_id, status="ok", error=None)
    return SyncResult(status="ok", created=created, updated=updated,
                      cancelled=cancelled, total=len(events))


def sync_property(conn, property_id: str, *,
                  fetch: CalendarFetcher) -> list[SyncResult]:
    """Synchronise **tous** les flux d'un logement. L'échec de l'un n'empêche
    jamais les autres (inv. 3)."""
    results = []
    for cal in repo.list_calendars_with_url(conn, property_id):
        results.append(sync_calendar(conn, cal, fetch=fetch))
    return results


@dataclass
class SyncAllReport:
    calendars: int = 0
    ok: int = 0
    errors: int = 0
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    failures: list[str] = field(default_factory=list)   # ids de flux en erreur


def sync_all(conn, *, fetch: CalendarFetcher = fetch_ical,
             commit_each: bool = True) -> SyncAllReport:
    """Synchronise tous les flux de tous les logements (timer périodique).

    `commit_each` : commit après chaque flux → un flux lent/en échec ne fait pas
    perdre le travail des précédents (chaque flux est indépendant, inv. 3)."""
    report = SyncAllReport()
    for cal in repo.list_all_calendars(conn):
        report.calendars += 1
        res = sync_calendar(conn, cal, fetch=fetch)
        if commit_each:
            conn.commit()
        if res.ok:
            report.ok += 1
            report.created += res.created
            report.updated += res.updated
            report.cancelled += res.cancelled
        else:
            report.errors += 1
            report.failures.append(str(cal["id"]))
    return report


# ── Analyse du calendrier (pur) : chevauchements & rotations ─────────────────
#
# Modèle d'intervalle **semi-ouvert** [arrivée, départ) : deux séjours qui se
# touchent (départ = arrivée le même jour) NE se chevauchent PAS — c'est une
# rotation. L'anti-chevauchement **alerte, ne bloque jamais** (inv. 4 : le
# propriétaire arbitre).
#
# La bonne question n'est pas « est-ce loué » mais « est-ce OCCUPÉ » (V2-23b) :
# un séjour est occupé quand quelqu'un y dort — `nature IN ('reservation',
# 'private')` et cycle de vie 'active'. Un `works` / `unavailable` / `unqualified`
# ne bloque personne : il ne compte ni pour le chevauchement ni pour la rotation.
# Un bloc **rattaché** (`linked_booking_id`) est le miroir d'un autre séjour : il
# est ignoré (sinon il ferait un faux chevauchement avec le séjour qu'il double).

_OCCUPIED_NATURES = ("reservation", "private")


def _is_occupied(b: dict) -> bool:
    """Un séjour occupe le logement (quelqu'un y dort → préparation) : nature
    réservation/privé, cycle de vie actif, non rattaché à un autre séjour."""
    return (b.get("status") == "active"
            and b.get("nature") in _OCCUPIED_NATURES
            and b.get("linked_booking_id") is None)


def effective_times(booking: dict, default_in: _dt.time,
                    default_out: _dt.time) -> tuple[_dt.time, _dt.time]:
    """Heures effectives d'un séjour : celles saisies, sinon les heures standard
    du logement (NULL = « heures standard »)."""
    ci = booking.get("checkin_time") or default_in
    co = booking.get("checkout_time") or default_out
    return ci, co


def compute_overlaps(bookings: list[dict]) -> list[tuple[dict, dict]]:
    """Paires de séjours **occupés** dont les intervalles [arrivée, départ) se
    recouvrent (double réservation). Une occupation privée qui recouvre une
    réservation en est une, exactement comme deux réservations. Les non-occupés
    (works/unavailable/unqualified), 'cancelled' et blocs rattachés sont ignorés."""
    occupied = [b for b in bookings if _is_occupied(b)]
    out: list[tuple[dict, dict]] = []
    for i in range(len(occupied)):
        a = occupied[i]
        for j in range(i + 1, len(occupied)):
            b = occupied[j]
            if a["starts_on"] < b["ends_on"] and b["starts_on"] < a["ends_on"]:
                out.append((a, b))
    return out


def compute_rotations(bookings: list[dict], default_in: _dt.time,
                      default_out: _dt.time) -> list[dict]:
    """Rotations même jour : un séjour **part** le jour où un autre **arrive**
    (départ = arrivée). Renvoie {on, departing, arriving, gap_minutes} — la
    fenêtre de préparation entre le checkout effectif et le checkin effectif.
    Ne concerne que les séjours **occupés** (une rotation suppose une prépa)."""
    occupied = [b for b in bookings if _is_occupied(b)]
    rotations: list[dict] = []
    for a in occupied:                        # a part…
        for b in occupied:                    # …le jour où b arrive
            if a["id"] == b["id"]:
                continue
            if a["ends_on"] == b["starts_on"]:
                _, checkout = effective_times(a, default_in, default_out)
                checkin, _ = effective_times(b, default_in, default_out)
                gap = int((_dt.datetime.combine(a["ends_on"], checkin)
                           - _dt.datetime.combine(a["ends_on"], checkout))
                          .total_seconds() // 60)
                rotations.append({"on": a["ends_on"], "departing": a["id"],
                                  "arriving": b["id"], "gap_minutes": gap})
    rotations.sort(key=lambda r: r["on"])
    return rotations


def mask_url(url: str) -> str:
    """Affichage masqué d'une URL iCal (secret, inv. 1) : on ne montre que la fin
    du chemin, jamais la valeur exploitable. Ex. `airbnb.com/…/…a1b2.ics`."""
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0]
    except Exception:  # noqa: BLE001
        host = ""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = tail[-8:] if len(tail) > 8 else tail
    host = host or "…"
    return f"{host}/…/…{tail}" if tail else f"{host}/…"


def _short_error(exc: Exception) -> str:
    """Message d'erreur court et sûr (jamais l'URL — c'est un secret, inv. 1)."""
    if isinstance(exc, ical.ICalError):
        return "Flux illisible : ce lien ne renvoie pas un calendrier iCal valide."
    if isinstance(exc, httpx.TimeoutException):
        return "Délai dépassé en contactant la plateforme."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"La plateforme a répondu {exc.response.status_code}."
    if isinstance(exc, httpx.HTTPError):
        return "Impossible de contacter la plateforme (erreur réseau)."
    return "Synchronisation impossible."
