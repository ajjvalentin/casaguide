#!/usr/bin/env python3
"""Recette de l'instance Overpass À CLÉ contre NOS données (V2-93c).

La référence est la base : les fiches OSM retenues (`approved`/`edited`) ont été
récoltées pendant des mois sur les serveurs Overpass d'origine. L'instance à clé
(`OVERPASS_URL` + `CASAGUIDE_OVERPASS_API_KEY`) doit les retrouver ; sinon on veut
savoir pourquoi. Aucun autre serveur n'est interrogé. Sans clé : refus (code 2).

LECTURE SEULE : la base n'est lue que dans une transaction `read_only`, annulée avant
tout appel réseau ; aucune écriture, aucun job, aucun appel IA.

Pour chaque logement du passage :

1. RAPPEL de la moisson. Pour chaque catégorie qui a au moins une fiche retenue de
   `source='osm'`, la requête de moisson est rejouée (même constructeur, mêmes
   sélecteurs, même palier de rayon et même délai que `_run_buckets`). Règle des rayons
   (le pipeline est adaptatif) : une fiche située dans le rayon de DÉPART de sa
   catégorie (`min(préférence, 5 km)`, ou la préférence entière pour une catégorie
   lointaine type aéroport) est attendue dans la requête de départ ; une fiche au-delà
   est attendue dans la requête d'ESCALADE (rayon maximal du seed). Les catégories sont
   regroupées par (passe, palier), une requête chacune, comme le pipeline. Le rappel est
   mesuré sur les éléments BRUTS rendus par le serveur, avant toute sélection (plafonds,
   dédoublonnage, tri, minimums). Chaque fiche non retrouvée est réinterrogée par
   identifiant et classée : `supprime` · `ferme` · `requalifie` · `hors_rayon` ·
   `inexplique` (moteur suspect).
2. COUVERTURE de toutes les formes de requête de `enrich/overpass.py`, appelées comme
   le pipeline les appelle : statut HTTP, `remark`, `osm3s.timestamp_osm_base`, nombre
   d'éléments ; un zéro là où le logement a des fiches de cette provenance est signalé.
3. CONSOMMATION et FRAÎCHEUR : requêtes (moisson / revérification), durée, date des
   données à chaque réponse et retard maximal sur l'heure courante.

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/overpass_compare.py \\
        (--property <uuid> | --all [--include-guest]) [--max-requests N] \\
        [--json bilan.json] [--check-auth]

`--check-auth` envoie UNE requête avec une clé volontairement fausse (valeur fixe,
jamais dérivée de la vraie) et imprime ce que le fournisseur répond.

Code de sortie : 0 recette complète sans anomalie ; 1 au moins une anomalie (fiche
`inexplique`, forme en erreur ou zéro suspect, `--check-auth` inattendu) ou bilan
PARTIEL (plafond atteint) ; 2 refus (pas de clé, logement introuvable, rien à tester).
La clé n'est JAMAIS imprimée : toute ligne et le fichier JSON passent par `scrub`.

Tests : `main(..., client=httpx.Client(transport=MockTransport(...)))`, aucun réseau.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                  # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # enrich.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.overpass_compare")

CLASSES = ("supprime", "ferme", "requalifie", "hors_rayon", "inexplique")
DEFAULT_MAX_REQUESTS = 2000
# Clé FACTICE de `--check-auth` : valeur fixe, manifestement fausse, jamais dérivée de
# la vraie clé.
FAKE_KEY = "recette-cle-factice-0000000000"
AUTH_QUERY = "[out:json][timeout:5];node(1);out ids;"

# Formes de requête recensées dans enrich/overpass.py (V2-93c § 3). Une forme qu'aucun
# logement du passage n'a exercée est nommée « non couverte » dans le bilan.
FORMS = {
    "fetch_grouped · départ": "moisson, 1re passe : union de sélecteurs par palier "
                              "(≤ 5 km), `out center tags`",
    "fetch_grouped · lointain": "moisson, palier lointain (aéroport, ≥ 50 km), délai long",
    "fetch_grouped · escalade": "moisson, 2e passe au rayon maximal (catégories en "
                                "déficit)",
    "fetch_category": "une catégorie seule (compatibilité, hors pipeline)",
    "fetch_named_sorties": "restaurants/bars/cafés nommés à 15 km, expression régulière "
                           "+ `[name]`, `out center 2000`",
    "fetch_natural_places": "lieux naturels nommés à 25 km, expressions régulières, "
                            "`out center 250`",
    "nearby_neighborhoods": "quartiers (`node[place~…]`) à 8 km, `out center 80`",
    "fetch_by_refs": "revérification par identifiant (`node(id:…)`), paquets de 100",
}
# Provenance attendue d'une forme : catégories dont une fiche retenue dans le rayon
# rend un zéro suspect.
_SORTIES_CATS = ("restaurant", "bar", "cafe")
_NATURAL_CATS = ("beach",)
_AROUND_RE = re.compile(r"around:(\d+),")


# ── Lecture de la base (seule) ───────────────────────────────────────────────

def select_properties(conn, *, include_guest: bool) -> list[str]:
    """Logements publiés hors démonstration (et hors guides voyageur, sauf
    `include_guest`). SELECT uniquement."""
    return [str(r["id"]) for r in conn.execute(
        """SELECT id FROM properties
            WHERE status = 'published' AND NOT demo AND (%s OR NOT guest_guide)
            ORDER BY name, id""", (include_guest,))]


def load_inputs(conn, property_id: str) -> dict:
    """Logement, catégories moissonnées (même filtre que le pipeline), toutes les
    catégories (rayons du rappel) et fiches OSM retenues avec leur position. SELECT
    uniquement."""
    from enrich import claude_enrich, db, overpass   # noqa: PLC0415
    prop = dict(db.load_property(conn, property_id))
    prop["id"] = str(prop["id"])
    all_cats = [dict(c) for c in db.load_categories(conn)]
    cats = [c for c in all_cats
            if c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES
            and (c["code"] != "tobacco"
                 or claude_enrich.tobacco_rubric_applies(prop.get("country_code")))]
    retained = [dict(r) for r in conn.execute(
        """SELECT source_ref AS ref, name, category_code AS category,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon
             FROM pois WHERE property_id = %s
              AND status IN ('approved', 'edited') AND source = 'osm'
              AND source_ref IS NOT NULL ORDER BY category_code, source_ref""",
        (property_id,))]
    for p in retained:
        p["dist_m"] = (overpass.haversine_m(prop["lat"], prop["lon"], p["lat"], p["lon"])
                       if None not in (prop.get("lat"), p.get("lat")) else None)
    return {"prop": prop, "cats": cats, "all_cats": all_cats, "retained": retained}


# ── Rappel (plan et classement : PURS) ───────────────────────────────────────

def recall_plan(prop: dict, all_cats: list[dict],
                retained: list[dict]) -> tuple[list[dict], list[dict]]:
    """Requêtes de moisson à rejouer pour retrouver `retained`. PURE.

    Renvoie `(groupes, non_testables)`. Un groupe = une requête : `stage` (« départ » /
    « escalade »), `bucket_m`, `codes`, `query` (texte exact), `pois` (fiches attendues,
    chacune avec `radius_m` = rayon du pipeline et `bucket_m` = rayon interrogé)."""
    from enrich import overpass   # noqa: PLC0415
    settings = overpass.settings
    by_code = {c["code"]: c for c in all_cats}
    far = settings.overpass_far_bucket_m
    groups: dict[tuple[str, int], dict] = {}
    untestable: list[dict] = []
    for poi in retained:
        code = poi["category"]
        cat = by_code.get(code)
        if cat is None or code not in overpass.CATEGORY_TAGS:
            untestable.append({**poi, "reason": "catégorie sans requête OSM"})
            continue
        pref = cat["default_radius_m"]
        top = cat.get("max_radius_m") or pref
        start = pref if pref >= far else min(pref, overpass._DENSE_START_M)
        d = poi.get("dist_m")
        if d is not None and d > start and top > start:
            stage, radius = "escalade", top
        else:
            stage, radius = "départ", start
        bucket = overpass._bucket_radius(radius)
        g = groups.setdefault((stage, bucket), {"stage": stage, "bucket_m": bucket,
                                                "codes": [], "pois": []})
        if code not in g["codes"]:
            g["codes"].append(code)
        g["pois"].append({**poi, "radius_m": radius, "bucket_m": bucket})
    out = []
    for key in sorted(groups, key=lambda k: (k[0] != "départ", k[1])):
        g = groups[key]
        selectors: list[str] = []
        for code in g["codes"]:
            for sel in overpass._selectors_for(code, prop.get("country_code")):
                if sel not in selectors:
                    selectors.append(sel)
        g["timeout_s"] = overpass._bucket_timeout(g["bucket_m"])
        g["query"] = overpass._build_query(selectors, prop["lat"], prop["lon"],
                                           g["bucket_m"], timeout_s=g["timeout_s"])
        out.append(g)
    return out, untestable


def element_position(el: dict) -> tuple[float, float] | None:
    c = el.get("center") or {}
    lat = el.get("lat") if el.get("lat") is not None else c.get("lat")
    lon = el.get("lon") if el.get("lon") is not None else c.get("lon")
    return (float(lat), float(lon)) if lat is not None and lon is not None else None


def classify(poi: dict, el: dict | None, prop: dict) -> tuple[str, int | None]:
    """Classe d'une fiche NON retrouvée, d'après son élément réinterrogé par identifiant
    (`None` : n'existe plus). PURE. Ordre du brief : supprime, ferme, requalifie,
    hors_rayon, inexplique. Le rayon de référence est celui de la requête (`bucket_m`)."""
    from enrich import overpass   # noqa: PLC0415
    if el is None:
        return "supprime", None
    tags = el.get("tags") or {}
    pos = element_position(el)
    dist = overpass.haversine_m(prop["lat"], prop["lon"], *pos) if pos else None
    if overpass.is_closed(tags):
        return "ferme", dist
    if not overpass.category_matches(poi["category"], tags, prop.get("country_code")):
        return "requalifie", dist
    if dist is not None and dist > poi["bucket_m"]:
        return "hors_rayon", dist
    return "inexplique", dist


# ── Observation des réponses ─────────────────────────────────────────────────

def _remark(data) -> str | None:
    remark = (data.get("remark") or "").strip() if isinstance(data, dict) else ""
    return remark or None


def _is_error_remark(remark: str | None) -> bool:
    r = (remark or "").lower()
    return "error" in r or "timed out" in r or "timeout" in r


class Recorder:
    """Reçoit les événements de `overpass.observe` et les étiquette par forme courante.
    Ne garde que le résumé de chaque réponse (jamais le document entier)."""

    def __init__(self, now=None):
        self.form: str | None = None
        self.events: list[dict] = []
        self._now = now or (lambda: _dt.datetime.now(_dt.timezone.utc))

    def __call__(self, ev: dict) -> None:
        data = ev.get("data")
        stamp = ((data.get("osm3s") or {}).get("timestamp_osm_base")
                 if isinstance(data, dict) else None)
        lag = None
        if stamp:
            try:
                base = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                lag = max(0.0, (self._now() - base).total_seconds())
            except ValueError:
                lag = None
        self.events.append({
            "form": self.form, "stage": ev.get("stage"), "query": ev["query"],
            "status": ev["status"], "error": ev.get("error"),
            "kind": "revérification" if "(id:" in ev["query"] else "moisson",
            "stamp": stamp, "stamp_ok": lag is not None, "lag_s": lag,
            "remark": _remark(data),
            "elements": (len(data.get("elements") or [])
                         if isinstance(data, dict) else None)})


def _form_label(base: str, ev: dict, far_m: int) -> str:
    if base != "fetch_grouped":
        return base
    m = _AROUND_RE.search(ev["query"])
    r = int(m.group(1)) if m else 0
    if ev.get("stage") == "escalade":
        return f"fetch_grouped · escalade · palier {r} m"
    if r >= far_m:
        return f"fetch_grouped · lointain · palier {r} m"
    return f"fetch_grouped · départ · palier {r} m"


def _family(label: str) -> str:
    return (" · ".join(label.split(" · ")[:2]) if label.startswith("fetch_grouped")
            else label)


def summarize_forms(events: list[dict], expected: dict, far_m: int, called: list[str],
                    query_expected=None) -> list[dict]:
    """Une ligne par requête distincte d'une forme. PURE. `expected[forme]` = fiches de
    la base que la forme devrait voir (None : pas de référence) ; `query_expected(query)`
    la donne pour chaque palier de `fetch_grouped`."""
    rows: dict[tuple[str, str], dict] = {}
    for ev in events:
        if ev["form"] in (None, "rappel"):
            continue
        label = _form_label(ev["form"], ev, far_m)
        row = rows.setdefault((label, ev["query"]), {
            "form": label, "family": _family(label), "query": ev["query"],
            "attempts": 0, "http_errors": [], "status": None, "remark": None,
            "stamp": None, "stamp_ok": False, "elements": None, "expected": None})
        row["attempts"] += 1
        if ev["status"] != 200:
            row["http_errors"].append(ev["status"])
        row.update(status=ev["status"], remark=ev["remark"], stamp=ev["stamp"],
                   stamp_ok=ev["stamp_ok"], elements=ev["elements"])
    out = []
    for row in rows.values():
        exp = (query_expected(row["query"])
               if query_expected and row["form"].startswith("fetch_grouped")
               else expected.get(row["form"]))
        row["expected"] = exp
        problems = []
        if row["status"] != 200:
            problems.append(f"HTTP {row['status'] or 'réseau'} (dernier essai)")
        elif _is_error_remark(row["remark"]):
            problems.append(f"remark : {row['remark'][:160]}")
        elif not row["stamp_ok"]:
            problems.append("osm3s.timestamp_osm_base absent ou illisible")
        if row["status"] == 200 and row["elements"] == 0 and exp:
            problems.append(f"zéro élément alors que la base a {exp} fiche(s) de cette "
                            "provenance")
        row["problems"] = problems
        row["verdict"] = "anomalie" if problems else "ok"
        out.append(row)
    seen = {r["form"] for r in out}
    for name in called:
        if not any(f == name or f.startswith(name + " ·") for f in seen):
            out.append({"form": name, "family": name, "query": None, "attempts": 0,
                        "http_errors": [], "status": None, "remark": None,
                        "stamp": None, "stamp_ok": False, "elements": None,
                        "expected": expected.get(name), "problems": [],
                        "verdict": "non applicable"})
    return out


# ── Passage d'un logement ────────────────────────────────────────────────────

class BudgetReached(Exception):
    """Plafond `--max-requests` atteint : arrêt propre, bilan partiel."""


def _codes_of_query(query: str, codes, cc: str | None) -> list[str]:
    from enrich import overpass   # noqa: PLC0415
    return [c for c in codes if c in overpass.CATEGORY_TAGS
            and all(f"nwr[{s}](around:" in query for s in overpass._selectors_for(c, cc))]


def _run_recall(recall: dict, prop, all_cats, retained, *, client, sleep,
                stop_if_exhausted) -> None:
    """Remplit `recall` au fil de l'eau : un arrêt sur plafond garde le bilan partiel."""
    from enrich import overpass   # noqa: PLC0415
    settings = overpass.settings
    plan, untestable = recall_plan(prop, all_cats, retained)
    recall |= {"tested": 0, "found": 0, "classes": dict.fromkeys(CLASSES, 0),
              "by_category": {}, "misses": [], "unmeasured": [],
              "untestable": [{"ref": p["ref"], "name": p["name"],
                              "category": p["category"], "reason": p["reason"]}
                             for p in untestable],
              "queries": [{"stage": g["stage"], "bucket_m": g["bucket_m"],
                           "codes": g["codes"], "pois": len(g["pois"])} for g in plan]}
    missing: list[dict] = []
    for i, g in enumerate(plan):
        if i:
            sleep(settings.politeness_delay_s)
        try:
            data = overpass._post_overpass(client, g["query"], timeout_s=g["timeout_s"],
                                           raw=True)
            err = _remark(data) if _is_error_remark(_remark(data)) else None
        except overpass.OverpassBudgetExhausted:
            stop_if_exhausted()
            raise
        except Exception as exc:  # noqa: BLE001 — palier non mesurable, tracé
            data, err = None, overpass.scrub(f"{type(exc).__name__}: {exc}")
        if err:
            for p in g["pois"]:
                recall["unmeasured"].append({"ref": p["ref"], "name": p["name"],
                                             "category": p["category"], "error": err})
            continue
        raw = {f'{el.get("type")}/{el.get("id")}' for el in data.get("elements", [])}
        for p in g["pois"]:
            cat = recall["by_category"].setdefault(p["category"], {"tested": 0, "found": 0})
            cat["tested"] += 1
            recall["tested"] += 1
            if p["ref"] in raw:
                cat["found"] += 1
                recall["found"] += 1
            else:
                missing.append({**p, "stage": g["stage"], "query": g["query"]})
    if not missing:
        return
    sleep(settings.politeness_delay_s)
    try:
        els = overpass.fetch_elements_by_refs([p["ref"] for p in missing],
                                              client=client, sleep=sleep)
    except overpass.OverpassBudgetExhausted:
        stop_if_exhausted()
        raise
    except Exception as exc:  # noqa: BLE001
        err = overpass.scrub(f"{type(exc).__name__}: {exc}")
        for p in missing:
            recall["misses"].append({"ref": p["ref"], "name": p["name"],
                                     "category": p["category"], "class": None,
                                     "error": f"classement impossible : {err}"})
        return
    for p in missing:
        cls, dist = classify(p, els.get(p["ref"]), prop)
        recall["classes"][cls] += 1
        miss = {"ref": p["ref"], "name": p["name"], "category": p["category"],
                "class": cls, "distance_m": dist, "db_distance_m": p.get("dist_m"),
                "radius_m": p["radius_m"], "bucket_m": p["bucket_m"], "stage": p["stage"]}
        if cls == "inexplique":
            miss["query"] = p["query"]
        recall["misses"].append(miss)


def run_property(inp: dict, *, client, probe, recorder: Recorder, sleep) -> dict:
    from enrich import overpass   # noqa: PLC0415
    settings = overpass.settings
    prop, cats, retained = inp["prop"], inp["cats"], inp["retained"]
    t0 = time.monotonic()
    first_event = len(recorder.events)
    res: dict = {"id": prop["id"], "name": prop.get("name"),
                 "country_code": prop.get("country_code"), "partial": False,
                 "retained_osm": len(retained)}
    lat, lon, cc = prop["lat"], prop["lon"], prop.get("country_code")

    def stop_if_exhausted():
        if probe.exhausted:
            raise BudgetReached

    def near(codes, radius):
        return sum(1 for p in retained if p["category"] in codes
                   and p.get("dist_m") is not None and p["dist_m"] <= radius)

    by_code = {c["code"]: c for c in cats}
    retained_cats = sorted({p["category"] for p in retained})
    tested = [c for c in retained_cats if c in overpass.CATEGORY_TAGS and c in by_code]
    one = max(tested, key=lambda c: (near((c,), by_code[c]["default_radius_m"]), c),
              default="pharmacy")
    one_radius = (by_code.get(one) or {}).get("default_radius_m", 3000)
    expected = {"fetch_category": near((one,), one_radius),
                "fetch_named_sorties": near(_SORTIES_CATS, 15000),
                "fetch_natural_places": near(_NATURAL_CATS, 25000),
                "nearby_neighborhoods": None,
                "fetch_by_refs": len(retained)}
    refs = [p["ref"] for p in retained]
    steps = [
        ("fetch_grouped", lambda: overpass.fetch_grouped(
            cats, lat, lon, client=client,
            country_lang=overpass.country_language(cc), country_code=cc)),
        ("fetch_category", lambda: overpass.fetch_category(
            one, lat, lon, one_radius, client=client,
            country_lang=overpass.country_language(cc))),
        ("fetch_named_sorties", lambda: overpass.fetch_named_sorties(
            lat, lon, client=client)),
        ("fetch_natural_places", lambda: overpass.fetch_natural_places(
            lat, lon, client, radius_m=25000)),
        ("nearby_neighborhoods", lambda: overpass.nearby_neighborhoods(
            lat, lon, client=client)),
        ("fetch_by_refs", lambda: (overpass.fetch_by_refs(refs, client=client, sleep=sleep)
                                   if refs else {})),
    ]
    called: list[str] = []
    durations: dict[str, float] = {}
    raised: dict[str, str] = {}
    try:
        recorder.form = "rappel"
        res["recall"] = {}
        _run_recall(res["recall"], prop, inp.get("all_cats") or cats, retained,
                    client=client, sleep=sleep, stop_if_exhausted=stop_if_exhausted)
        for name, call in steps:
            stop_if_exhausted()
            recorder.form = name
            called.append(name)
            s0 = time.monotonic()
            try:
                call()
            except overpass.OverpassBudgetExhausted:
                pass                       # traité par `stop_if_exhausted` ci-dessous
            except Exception as exc:  # noqa: BLE001 — les événements portent le statut
                raised[name] = overpass.scrub(f"{type(exc).__name__}: {exc}")
            finally:
                durations[name] = round(time.monotonic() - s0, 2)
                recorder.form = None
            stop_if_exhausted()
    except BudgetReached:
        res["partial"] = True
    finally:
        recorder.form = None
        evs = recorder.events[first_event:]

        def grouped_expected(query: str) -> int:
            m = _AROUND_RE.search(query)
            return near(_codes_of_query(query, retained_cats, cc), int(m.group(1)) if m else 0)

        forms = summarize_forms(evs, expected, settings.overpass_far_bucket_m,
                                called, grouped_expected)
        for f in forms:
            fam = f["form"].split(" · ")[0]
            f["duration_s"] = durations.get(fam)
            if fam in raised and f["verdict"] == "ok":
                f["problems"].append(raised[fam])
                f["verdict"] = "anomalie"
        res["forms"] = forms
        lags = [e["lag_s"] for e in evs if e["lag_s"] is not None]
        res["requests"] = {"moisson": sum(e["kind"] == "moisson" for e in evs),
                           "revérification": sum(e["kind"] == "revérification"
                                                 for e in evs),
                           "total": len(evs)}
        res["freshness"] = {"stamps": sorted({e["stamp"] for e in evs if e["stamp"]}),
                            "max_lag_s": max(lags) if lags else None}
        res["requests_log"] = evs
        res["duration_s"] = round(time.monotonic() - t0, 2)
    return res


# ── Bilan (PUR) ──────────────────────────────────────────────────────────────

def _lag(s: float | None) -> str:
    if s is None:
        return "?"
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


def totals(props: list[dict]) -> dict:
    t = {"properties": len(props), "tested": 0, "found": 0,
         "classes": dict.fromkeys(CLASSES, 0), "unmeasured": 0, "unclassified": 0,
         "requests": {"moisson": 0, "revérification": 0, "total": 0},
         "duration_s": 0.0, "max_lag_s": None, "form_anomalies": 0}
    for p in props:
        r = p.get("recall") or {}
        t["tested"] += r.get("tested", 0)
        t["found"] += r.get("found", 0)
        t["unmeasured"] += len(r.get("unmeasured", []))
        t["unclassified"] += sum(1 for m in r.get("misses", []) if not m.get("class"))
        for c in CLASSES:
            t["classes"][c] += (r.get("classes") or {}).get(c, 0)
        for k in t["requests"]:
            t["requests"][k] += p["requests"][k]
        t["duration_s"] = round(t["duration_s"] + p["duration_s"], 2)
        lag = p["freshness"]["max_lag_s"]
        if lag is not None:
            t["max_lag_s"] = lag if t["max_lag_s"] is None else max(t["max_lag_s"], lag)
        t["form_anomalies"] += sum(f["verdict"] == "anomalie" for f in p.get("forms", []))
    return t


def _classes_line(classes: dict) -> str:
    return " · ".join(f"{c} {classes.get(c, 0)}" for c in CLASSES)


def render(report: dict) -> list[str]:
    """Résumé imprimé (liste de lignes), cohérent avec le JSON. PUR."""
    lines: list[str] = []
    for p in report["properties"]:
        lines.append(f"━ {p['name']} ({p['id']}) · {p.get('country_code') or '?'}"
                     + (" · PARTIEL" if p["partial"] else ""))
        if p.get("skipped"):
            lines.append(f"  ⚠ ignoré : {p['skipped']}")
            continue
        r = p.get("recall")
        if r is not None:
            lines.append(f"  Rappel : {r['found']}/{r['tested']} fiche(s) retrouvée(s) · "
                         f"{_classes_line(r['classes'])}")
            weak = [f"{c} {v['found']}/{v['tested']}"
                    for c, v in sorted(r["by_category"].items()) if v["found"] < v["tested"]]
            if weak:
                lines.append(f"    catégories incomplètes : {', '.join(weak)}")
            for m in r["misses"]:
                if m.get("class") == "inexplique":
                    lines.append(f"    ✗ INEXPLIQUÉ {m['ref']} « {m['name']} » "
                                 f"({m['category']}) à {m['distance_m']} m, rayon "
                                 f"interrogé {m['bucket_m']} m ({m['stage']})")
                    lines.append(f"      requête : {m['query']}")
                elif m.get("class"):
                    d = f" à {m['distance_m']} m" if m.get("distance_m") is not None else ""
                    lines.append(f"    · {m['class']} {m['ref']} « {m['name']} » "
                                 f"({m['category']}){d}")
                else:
                    lines.append(f"    ⚠ {m['ref']} « {m['name']} » : {m['error']}")
            for u in r["unmeasured"]:
                lines.append(f"    ⚠ non mesuré {u['ref']} ({u['category']}) : {u['error']}")
            for u in r["untestable"]:
                lines.append(f"    · non testable {u['ref']} ({u['category']}) : "
                             f"{u['reason']}")
        if p.get("forms"):
            lines.append("  Couverture :")
            for f in p["forms"]:
                if f["verdict"] == "non applicable":
                    lines.append(f"    – {f['form']} : non applicable (rien à demander)")
                    continue
                mark = "✓" if f["verdict"] == "ok" else "✗"
                retry = (f" · {f['attempts']} essais (HTTP "
                         f"{', '.join(str(s) for s in f['http_errors'])})"
                         if f["http_errors"] else "")
                n = f["elements"] if f["elements"] is not None else "—"
                lines.append(f"    {mark} {f['form']} : {n} élément(s) · données "
                             f"{f['stamp'] or '?'}{retry}")
                for pb in f["problems"]:
                    lines.append(f"        ✗ {pb}")
        q = p["requests"]
        lines.append(f"  Consommation : {q['total']} requête(s) (moisson {q['moisson']}, "
                     f"revérification {q['revérification']}) · {p['duration_s']:.1f} s · "
                     f"retard max des données {_lag(p['freshness']['max_lag_s'])}")
    t = report["totals"]
    lines.append("━ Bilan global" + (" — PARTIEL : " + report["partial_reason"]
                                     if report["partial"] else ""))
    lines.append(f"  Logements : {t['properties']} · rappel {t['found']}/{t['tested']} · "
                 f"{_classes_line(t['classes'])} · non mesurées {t['unmeasured']} · "
                 f"non classées {t['unclassified']}")
    lines.append(f"  Formes en anomalie : {t['form_anomalies']}")
    if report["forms_not_covered"]:
        lines.append("  Formes non couvertes : " + ", ".join(report["forms_not_covered"]))
    lines.append(f"  Requêtes : {t['requests']['total']} (moisson "
                 f"{t['requests']['moisson']}, revérification "
                 f"{t['requests']['revérification']}) · {t['duration_s']:.1f} s · "
                 f"retard max des données {_lag(t['max_lag_s'])}")
    return lines


# ── --check-auth ─────────────────────────────────────────────────────────────

def check_auth(client, say) -> int:
    """UNE requête, clé FACTICE fixe, vers l'instance configurée. Attendu :
    `OverpassAuthError`. Imprime le statut et le code réellement renvoyés."""
    from enrich import overpass   # noqa: PLC0415
    settings = overpass.settings
    # Si l'URL porte un segment de clé (`/v1/<seg>/interpreter`), il est remplacé lui
    # aussi : la vraie clé ne part jamais, ni en en-tête ni dans le chemin.
    url = overpass._KEY_SEGMENT_RE.sub(r"\1cle-factice\2", settings.overpass_url)
    seen: list[dict] = []
    probe = overpass.Probe(on_event=seen.append, budget=1)
    say(f"Contrôle d'authentification : 1 requête vers {overpass.mask_url(url)} "
        "avec une clé factice fixe.")
    rc = 1
    with overpass.use_server(url, FAKE_KEY), overpass.observe(probe):
        try:
            overpass._post_overpass(client, AUTH_QUERY, timeout_s=5, raw=True,
                                    policy=overpass.RetryPolicy(1, 0))
            outcome = "✗ la clé factice a été ACCEPTÉE (réponse 200) : anomalie."
        except overpass.OverpassAuthError as exc:
            outcome = (f"✓ OverpassAuthError comme attendu : statut {exc.status}, code "
                       f"{exc.code or '(aucun)'} — {exc}")
            rc = 0
        except overpass.OverpassError as exc:
            outcome = (f"✗ refus inattendu ({type(exc).__name__}) : statut {exc.status} "
                       f"— {exc}")
        except Exception as exc:  # noqa: BLE001
            outcome = f"✗ échec inattendu : {type(exc).__name__}: {exc}"
    say(outcome)
    for ev in seen:
        say(f"  réponse du fournisseur : HTTP {ev['status'] or 'réseau'} · corps : "
            f"{(ev.get('error') or '')[:300] or '—'}")
    say(f"  requête(s) envoyée(s) : {probe.sent}")
    return rc if probe.sent == 1 else 1


# ── Point d'entrée ───────────────────────────────────────────────────────────

def _skipped(prop: dict, why: str, partial: bool) -> dict:
    return {"id": prop["id"], "name": prop.get("name"),
            "country_code": prop.get("country_code"), "partial": partial,
            "skipped": why, "requests": {"moisson": 0, "revérification": 0, "total": 0},
            "freshness": {"stamps": [], "max_lag_s": None}, "duration_s": 0.0}


def main(argv: list[str] | None = None, *, client=None, sleep=None, now=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recette de l'instance Overpass à clé contre nos données (V2-93c).")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--property", dest="property_id")
    target.add_argument("--all", action="store_true",
                        help="tous les logements publiés hors démonstration.")
    parser.add_argument("--include-guest", action="store_true",
                        help="avec --all : inclut les guides voyageur.")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                        help=f"plafond de requêtes du passage (défaut "
                             f"{DEFAULT_MAX_REQUESTS}).")
    parser.add_argument("--check-auth", action="store_true",
                        help="une requête avec une clé factice fixe : réponse réelle du "
                             "fournisseur à une clé refusée.")
    parser.add_argument("--json", dest="json_path", help="bilan détaillé en JSON.")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut : backend/.env).")
    args = parser.parse_args(argv)
    if not (args.check_auth or args.property_id or args.all):
        parser.error("indiquer --property <uuid>, --all ou --check-auth")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)

    import httpx   # noqa: PLC0415
    from enrich import overpass   # noqa: PLC0415
    settings = overpass.settings

    def say(line: str) -> None:
        log.info("%s", overpass.scrub(line))

    if not settings.overpass_api_key:
        say("✗ CASAGUIDE_OVERPASS_API_KEY absente : cet outil ne teste que l'instance à "
            "clé. Aucune requête envoyée.")
        return 2
    say(f"· {overpass.describe_mode()}")
    sleep = sleep or overpass._sleep
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    try:
        rc = check_auth(client, say) if args.check_auth else 0
        if args.property_id or args.all:
            rc = max(rc, _recette(args, client, sleep, now, say))
        return rc
    finally:
        if own_client:
            client.close()


def _recette(args, client, sleep, now, say) -> int:
    import psycopg   # noqa: PLC0415
    from enrich import db, overpass   # noqa: PLC0415
    try:
        conn = db.connect()
    except psycopg.OperationalError as exc:
        say(f"✗ connexion à la base impossible : {exc}")
        return 2
    inputs: list[dict] = []
    with conn:
        conn.read_only = True            # garantie : aucune écriture possible
        try:
            ids = ([args.property_id] if args.property_id
                   else select_properties(conn, include_guest=args.include_guest))
            inputs = [load_inputs(conn, pid) for pid in ids]
        except LookupError as exc:
            say(f"✗ {exc}")
            return 2
        finally:
            conn.rollback()              # aucune transaction ouverte pendant le réseau
    if not inputs:
        say("✗ aucun logement à tester.")
        return 2
    say(f"· {len(inputs)} logement(s) · plafond {args.max_requests} requête(s)")

    recorder = Recorder(now)
    probe = overpass.Probe(on_event=recorder, budget=args.max_requests)
    props: list[dict] = []
    partial_reason = None
    with overpass.observe(probe):
        for inp in inputs:
            prop = inp["prop"]
            if partial_reason:
                props.append(_skipped(prop, "non traité : plafond de requêtes atteint",
                                      True))
                continue
            if prop.get("lat") is None:
                props.append(_skipped(prop, "logement sans position", False))
                continue
            say(f"… {prop.get('name')} ({prop['id']}) : {len(inp['retained'])} fiche(s) "
                "OSM retenue(s)")
            res = run_property(inp, client=client, probe=probe, recorder=recorder,
                               sleep=sleep)
            props.append(res)
            if res["partial"]:
                partial_reason = (f"plafond de {args.max_requests} requête(s) atteint "
                                  f"pendant « {prop.get('name')} »")

    covered = {f["family"] for p in props for f in p.get("forms", [])
               if f["verdict"] != "non applicable"}
    report = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": overpass.describe_mode(), "max_requests": args.max_requests,
        "partial": partial_reason is not None, "partial_reason": partial_reason,
        "forms_catalogue": FORMS,
        "forms_not_covered": [f for f in FORMS if f not in covered],
        "properties": props,
    }
    report["totals"] = totals(props)
    for line in render(report):
        say(line)
    if args.json_path:
        Path(args.json_path).write_text(
            overpass.scrub(json.dumps(report, ensure_ascii=False, indent=2, default=str)),
            encoding="utf-8")
        say(f"· bilan JSON : {args.json_path}")
    t = report["totals"]
    anomalies = t["classes"]["inexplique"] + t["form_anomalies"] + t["unclassified"]
    return 1 if (anomalies or report["partial"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
