"""Revérification planifiée des lieux auprès de leur source d'origine (V2-93).

Chaque nuit (`ops/refresh_pois.py` + timer), pour chaque logement DÛ :
  1. interroger les sources PAR IDENTIFIANT (OSM : Overpass `node(id:…)` ; Overture :
     cache de zone local uniquement) — aucun appel IA ;
  2. poser `last_checked_at`, `source_state` (+ `source_state_since` si l'état change)
     sur toutes les fiches contrôlées ;
  3. reporter `phone` / `website` / `opening_hours` selon `locks.plan_refresh` (verrou
     par champ : une saisie humaine n'est jamais écrasée, une source muette n'efface
     rien) — UNE écriture par fiche, journalisée par le déclencheur (auteur `system`,
     lot + job de l'exécution).

Périmètre : logements `published`, hors `guest_guide` (instantané figé acheté à vie,
V2-54) et hors `demo` ; fiches `approved`/`edited`, `source` ∈ {osm, overture},
`source_ref` non NULL. Les autres fiches retenues (`web`, `claude`, `owner`) n'ont pas
d'identifiant réinterrogeable : comptées « non revérifiables », jamais touchées. Une
fiche `suggested` ou `rejected` n'est jamais lue.

Un lieu FERMÉ ou DISPARU à la source est CONSTATÉ (`source_state`) et rien d'autre :
aucun champ n'est reporté depuis un élément fermé/disparu (un `opening_hours=closed`
n'est pas un horaire), et la fiche reste servie dans le guide (retrait : V2-94).

Les deux fetchers sont injectables (tests sans réseau) :
  osm_fetch(refs) -> {ref: {state, phone, website, opening_hours}}
  overture_fetch(refs, lat, lon) -> même forme, ou None (hors zone en cache).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import db, journal, locks, overpass, overture
from .settings import settings

log = logging.getLogger("casaguide.refresh")

CHECKABLE_SOURCES = ("osm", "overture")
RETAINED = ("approved", "edited")

OsmFetch = Callable[[list[str]], dict[str, dict]]
OvertureFetch = Callable[[list[str], float, float], "dict[str, dict] | None"]


def _default_osm_fetch(refs: list[str]) -> dict[str, dict]:
    return overpass.fetch_by_refs(refs)


def _default_overture_fetch(refs: list[str], lat: float, lon: float):
    return overture.fetch_cached_by_gers(refs, lat, lon)


# ── Sélection ────────────────────────────────────────────────────────────────

_SCOPE_SQL = """
    FROM properties pr JOIN pois p ON p.property_id = pr.id
    WHERE pr.status = 'published' AND NOT pr.guest_guide AND NOT pr.demo
      AND p.status IN ('approved', 'edited')
      AND p.source IN ('osm', 'overture') AND p.source_ref IS NOT NULL
"""


def due_properties(conn, *, max_age_days: int | None = None,
                   property_id: str | None = None,
                   limit: int | None = None) -> list[str]:
    """Logements du périmètre dont au moins une fiche revérifiable n'a jamais été
    contrôlée ou l'a été il y a plus de `max_age_days`. Les plus en retard d'abord.
    `property_id` (passage à la main) : ce logement seul, QUEL QUE SOIT l'âge de ses
    contrôles, mais toujours soumis au périmètre (jamais un guide voyageur ni une
    démonstration)."""
    age = settings.refresh_max_age_days if max_age_days is None else max_age_days
    sql = f"""SELECT pr.id
              {_SCOPE_SQL}
                AND (%(pid)s::uuid IS NOT NULL
                     OR p.last_checked_at IS NULL
                     OR p.last_checked_at < now() - make_interval(days => %(age)s))
                AND (%(pid)s::uuid IS NULL OR pr.id = %(pid)s::uuid)
              GROUP BY pr.id
              ORDER BY min(COALESCE(p.last_checked_at, '-infinity')), pr.id"""
    if limit is not None:
        sql += " LIMIT %(lim)s"
    rows = conn.execute(sql, {"age": age, "pid": property_id, "lim": limit}).fetchall()
    return [str(r["id"]) for r in rows]


def _load_retained(conn, property_id: str) -> list[dict]:
    return conn.execute(
        """SELECT id, source, source_ref, phone, website, opening_hours, human_fields,
                  source_state
           FROM pois WHERE property_id = %s AND status IN ('approved', 'edited')
           ORDER BY id""", (property_id,)).fetchall()


# ── Bilan ────────────────────────────────────────────────────────────────────

@dataclass
class PropertyReport:
    property_id: str
    job_id: str | None = None
    batch_id: str | None = None
    ok: bool = True
    error: str | None = None
    checked: int = 0
    modified: int = 0                                   # fiches modifiées
    modified_by_field: dict[str, int] = field(default_factory=dict)
    closed: int = 0
    missing: int = 0
    locked_skipped: int = 0                             # champs différents mais verrouillés
    not_recheckable: int = 0                            # web / claude / owner
    not_checked: int = 0                                # Overture hors zone en cache
    changes: list[dict] = field(default_factory=list)   # détail (rapport, dry-run)

    def steps(self) -> dict[str, Any]:
        return {"checked": self.checked, "modified": self.modified,
                "modified_by_field": dict(sorted(self.modified_by_field.items())),
                "closed": self.closed, "missing": self.missing,
                "locked_skipped": self.locked_skipped,
                "not_recheckable": self.not_recheckable,
                "not_checked": self.not_checked}


def _locked_differences(poi: dict, sv: dict) -> int:
    """Champs où la source dit autre chose MAIS que le propriétaire a verrouillés."""
    human = poi.get("human_fields") or ()
    return sum(1 for f in locks.REFRESH_FIELDS
               if f in human and not locks._is_empty(sv.get(f))
               and sv.get(f) != poi.get(f))


# ── Un logement ──────────────────────────────────────────────────────────────

def refresh_property(conn, property_id: str, *, osm_fetch: OsmFetch | None = None,
                     overture_fetch: OvertureFetch | None = None,
                     dry_run: bool = False) -> PropertyReport:
    """Revérifie un logement. Exécution réelle : un job `trigger='refresh'` et un lot
    de journal ; aucune transaction ouverte pendant les appels réseau ; les écritures
    sont validées ensemble à la fin (un échec réseau n'écrit RIEN sur les
    fiches — `last_checked_at` non posé, repris la nuit suivante — et le job passe
    `failed` avec sa cause). `dry_run` : mêmes lectures et même plan, aucune écriture
    (ni job, ni fiche), bilan identique à l'exécution réelle."""
    osm_fetch = osm_fetch or _default_osm_fetch
    overture_fetch = overture_fetch or _default_overture_fetch
    rep = PropertyReport(property_id)
    if not dry_run:
        rep.job_id = db.job_start(conn, property_id, "refresh")
        rep.batch_id = str(uuid.uuid4())
        conn.commit()                       # le job survit à un échec du logement
        journal.bind(conn, "system", batch_id=rep.batch_id, job_id=rep.job_id)
    try:
        prop = db.load_property(conn, property_id)
        pois = _load_retained(conn, property_id)
        checkable = [p for p in pois if p["source"] in CHECKABLE_SOURCES
                     and p["source_ref"]]
        rep.not_recheckable = len(pois) - len(checkable)
        osm_refs = [p["source_ref"] for p in checkable if p["source"] == "osm"]
        ovt_refs = [p["source_ref"] for p in checkable if p["source"] == "overture"]
        # Clore la lecture AVANT le réseau : une transaction « idle in transaction »
        # pendant des minutes d'Overpass bloquerait tout verrou exclusif sur `pois`
        # (migration d'un déploiement nocturne). L'écriture se fait dans une
        # transaction neuve ; ses gardes (statut, verrou) relisent l'état courant.
        conn.rollback()
        answers: dict[str, dict] = {}
        if osm_refs:
            answers.update({("osm", r): v for r, v in osm_fetch(osm_refs).items()})
        ovt = None
        if ovt_refs:
            if prop.get("lat") is not None:
                ovt = overture_fetch(ovt_refs, prop["lat"], prop["lon"])
            if ovt is not None:
                answers.update({("overture", r): v for r, v in ovt.items()})

        for p in checkable:
            ans = answers.get((p["source"], p["source_ref"]))
            if ans is None:     # Overture hors cache (ou ref illisible) : non contrôlée
                rep.not_checked += 1
                continue
            rep.checked += 1
            state = ans.get("state") or "ok"
            if state == "closed":
                rep.closed += 1
            elif state == "missing":
                rep.missing += 1
            # Rien n'est reporté depuis un élément fermé ou disparu.
            plan = locks.plan_refresh(p, ans) if state == "ok" else []
            if state == "ok":
                rep.locked_skipped += _locked_differences(p, ans)
            if plan:
                rep.modified += 1
                for ch in plan:
                    rep.modified_by_field[ch.field] = rep.modified_by_field.get(ch.field, 0) + 1
                    rep.changes.append({"poi_id": str(p["id"]),
                                        "source_ref": p["source_ref"],
                                        "field": ch.field, "old": ch.old, "new": ch.new})
            if dry_run:
                continue
            sets = ["last_checked_at = now()",
                    "source_state_since = CASE WHEN source_state IS DISTINCT FROM "
                    "%(state)s THEN now() ELSE source_state_since END",
                    "source_state = %(state)s"]
            params: dict[str, Any] = {"id": p["id"], "state": state}
            for ch in plan:   # noms issus de REFRESH_FIELDS (code-contrôlés)
                sets.append(f"{ch.field} = %({ch.field})s")
                params[ch.field] = ch.new
            # Garde : la fiche est toujours retenue et le champ toujours libre
            # (une édition concurrente du propriétaire gagne).
            guard = "".join(f" AND NOT ('{ch.field}' = ANY(human_fields))" for ch in plan)
            conn.execute(f"UPDATE pois SET {', '.join(sets)} WHERE id = %(id)s "
                         f"AND status IN ('approved', 'edited'){guard}", params)
        if not dry_run:
            db.job_step(conn, rep.job_id, "refresh", rep.steps())
            db.job_finish(conn, rep.job_id, "done")
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — un logement n'arrête pas les suivants
        rep.ok = False
        rep.error = overpass._short(f"{type(exc).__name__}: {exc}")
        log.warning("Revérification de %s en échec : %s", property_id, rep.error)
        conn.rollback()
        if not dry_run:
            db.job_step(conn, rep.job_id, "refresh", {"ok": False, "error": rep.error})
            db.job_finish(conn, rep.job_id, "failed", rep.error)
            conn.commit()
    finally:
        if dry_run:
            conn.rollback()          # lectures seules : aucune transaction laissée ouverte
        else:
            journal.unbind(conn)
    return rep


def run(conn, *, property_id: str | None = None, max_properties: int | None = None,
        dry_run: bool = False, osm_fetch: OsmFetch | None = None,
        overture_fetch: OvertureFetch | None = None,
        max_age_days: int | None = None) -> list[PropertyReport]:
    """Revérifie tous les logements dus (ou `property_id`), dans la limite de
    `max_properties`. Chaque logement est indépendant (échec isolé)."""
    ids = due_properties(conn, max_age_days=max_age_days, property_id=property_id,
                         limit=max_properties)
    conn.commit()
    return [refresh_property(conn, pid, osm_fetch=osm_fetch,
                             overture_fetch=overture_fetch, dry_run=dry_run)
            for pid in ids]
