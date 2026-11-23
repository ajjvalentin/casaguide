#!/usr/bin/env python3
"""Comparaison serveur Overpass PUBLIC ↔ instance À CLÉ, pour un logement (V2-93b).

LECTURE SEULE : la base n'est lue que pour la position du logement, ses catégories et
ses fiches retenues (transaction `read_only`, annulée à la fin) ; aucune écriture, aucun
job, aucun appel IA.

Pour le logement `--property <uuid>` :
  1. rejoue la MOISSON (`overpass.fetch_grouped` avec les catégories et rayons du
     logement, comme le pipeline) et la REVÉRIFICATION par identifiant
     (`overpass.fetch_by_refs` sur ses fiches OSM retenues) contre DEUX serveurs : le
     serveur public (`--public-url`, défaut l'instance principale overpass-api.de, sans
     miroir) et l'instance à clé (`OVERPASS_URL` + `CASAGUIDE_OVERPASS_API_KEY`) ;
  2. compare : `source_ref` rendus par catégorie et, pour les éléments communs,
     `phone` / `website` / `opening_hours` ; à la revérification, l'état
     (`ok` / `closed` / `missing`) et les mêmes champs ;
  3. imprime un bilan : identiques, présents d'un seul côté (lesquels), valeurs
     divergentes (lesquelles), date des données (`timestamp_osm_base`) et durée de
     chaque côté, nombre de requêtes envoyées.

Si le serveur public ne répond pas, le bilan le dit et donne l'instance à clé seule.
Coût : environ 5 à 10 requêtes par côté (paliers de rayon + paquets d'identifiants),
décomptées du quota du compte côté instance à clé.

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/overpass_compare.py \\
        --property <uuid> [--public-url URL] [--max-list N]

Code de sortie : 0 bilan produit (même avec divergences), 1 instance à clé en échec,
2 pas de clé configurée / logement introuvable. La clé n'est JAMAIS imprimée : toute
ligne passe par `overpass.scrub`.

Fetchers injectables (`main(..., fetchers={"public": f, "keyed": f})`) : tests sans
réseau. Un fetcher reçoit `(lat, lon, categories, country_code, refs)` et rend un
`dict` : `grouped` {code: {ref: {phone, website, opening_hours}}}, `failed`
{code: message}, `refs` {ref: {state, phone, website, opening_hours}} | None,
`refs_error`, `stamp`, `duration_s`, `requests`, `error`.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                  # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # enrich.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.overpass_compare")

PUBLIC_URL = "https://overpass-api.de/api/interpreter"
FIELDS = ("phone", "website", "opening_hours")


# ── Fetcher réel ─────────────────────────────────────────────────────────────

def live_fetcher(url: str, api_key: str = ""):
    """Fetcher réel vers UN serveur (aucun miroir : on compare deux serveurs, pas deux
    listes). Ne lève jamais : une panne est rendue dans `error` / `refs_error`."""
    from enrich import overpass   # noqa: PLC0415 — après chargement du .env

    def fetch(lat, lon, categories, country_code, refs):
        out = {"grouped": {}, "failed": {}, "refs": None, "refs_error": None,
               "stamp": None, "duration_s": 0.0, "requests": 0, "error": None}
        sent0, t0 = overpass.request_count, time.monotonic()
        with overpass.use_server(url, api_key):
            try:
                overpass.last_osm_base = None
                grouped, failed, _stats = overpass.fetch_grouped(
                    categories, lat, lon,
                    country_lang=overpass.country_language(country_code),
                    country_code=country_code)
                out["stamp"] = overpass.last_osm_base
                out["grouped"] = {code: {p["source_ref"]: {f: p.get(f) for f in FIELDS}
                                         for p in pois}
                                  for code, pois in grouped.items()}
                out["failed"] = {c: overpass.scrub(m) for c, m in failed.items()}
                if refs:
                    try:
                        out["refs"] = overpass.fetch_by_refs(refs)
                        out["stamp"] = overpass.last_osm_base or out["stamp"]
                    except Exception as exc:  # noqa: BLE001
                        out["refs_error"] = overpass.scrub(f"{type(exc).__name__}: {exc}")
                else:
                    out["refs"] = {}
            except Exception as exc:  # noqa: BLE001
                out["error"] = overpass.scrub(f"{type(exc).__name__}: {exc}")
        out["duration_s"] = time.monotonic() - t0
        out["requests"] = overpass.request_count - sent0
        # `fetch_grouped` absorbe l'échec d'un palier (catégories vides + `failed`) : si
        # TOUTES les catégories ont échoué, le serveur n'a pas répondu.
        if out["failed"] and not out["error"] and set(out["grouped"]) <= set(out["failed"]):
            out["error"] = "aucune catégorie rendue : " + next(iter(out["failed"].values()))
        return out

    return fetch


# ── Comparaison (pure) ───────────────────────────────────────────────────────

def compare(public: dict, keyed: dict) -> dict:
    """Compare deux résultats de fetcher. PURE. Une catégorie en échec d'un côté est
    « non comparable » (jamais comptée comme « présente d'un seul côté »)."""
    cats: dict[str, dict] = {}
    for code in sorted(set(public.get("grouped", {})) | set(keyed.get("grouped", {}))
                       | set(public.get("failed", {})) | set(keyed.get("failed", {}))):
        if code in public.get("failed", {}) or code in keyed.get("failed", {}):
            cats[code] = {"comparable": False,
                          "failed_public": public.get("failed", {}).get(code),
                          "failed_keyed": keyed.get("failed", {}).get(code)}
            continue
        a = public.get("grouped", {}).get(code, {})
        b = keyed.get("grouped", {}).get(code, {})
        common = sorted(set(a) & set(b))
        diffs = [{"ref": r, "field": f, "public": a[r].get(f), "keyed": b[r].get(f)}
                 for r in common for f in FIELDS if a[r].get(f) != b[r].get(f)]
        cats[code] = {"comparable": True, "common": len(common),
                      "only_public": sorted(set(a) - set(b)),
                      "only_keyed": sorted(set(b) - set(a)), "diffs": diffs}
    refs = None
    pa, pb = public.get("refs"), keyed.get("refs")
    if pa is not None and pb is not None:
        common = sorted(set(pa) & set(pb))
        diffs = [{"ref": r, "field": f, "public": pa[r].get(f), "keyed": pb[r].get(f)}
                 for r in common for f in ("state", *FIELDS)
                 if pa[r].get(f) != pb[r].get(f)]
        refs = {"total": len(common), "diffs": diffs,
                "identical": len(common) - len({d["ref"] for d in diffs}),
                "only_public": sorted(set(pa) - set(pb)),
                "only_keyed": sorted(set(pb) - set(pa))}
    return {"categories": cats, "refs": refs}


def _side_lines(label: str, side: dict) -> list[str]:
    lines = [f"  {label} : données du {side.get('stamp') or '?'} · "
             f"{side.get('duration_s', 0):.1f} s · {side.get('requests', 0)} requête(s)"]
    if side.get("error"):
        lines.append(f"    ✗ {side['error']}")
    for code, msg in sorted((side.get("failed") or {}).items()):
        lines.append(f"    ✗ palier {code} : {msg}")
    if side.get("refs_error"):
        lines.append(f"    ✗ revérification par identifiant : {side['refs_error']}")
    return lines


def _states(refs: dict | None) -> str:
    if not refs:
        return "—"
    counts: dict[str, int] = {}
    for v in refs.values():
        counts[v.get("state") or "?"] = counts.get(v.get("state") or "?", 0) + 1
    return ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))


def render(prop: dict, public: dict, keyed: dict, *, max_list: int = 10) -> list[str]:
    """Bilan lisible (liste de lignes). PURE."""
    def cut(items):
        shown = ", ".join(items[:max_list])
        return shown + (f" … (+{len(items) - max_list})" if len(items) > max_list else "")

    lines = [f"Comparaison Overpass — {prop.get('name')} ({prop['id']})",
             "Serveurs :"]
    lines += _side_lines("public", public)
    lines += _side_lines("à clé ", keyed)
    public_down = bool(public.get("error"))
    if keyed.get("error"):
        lines.append("✗ L'instance à clé n'a pas répondu : aucune comparaison possible.")
        return lines
    if public_down:
        lines.append("⚠ Le serveur public n'a pas répondu : bilan de l'instance à clé seule.")
        lines.append("Moisson (instance à clé) :")
        for code, items in sorted(keyed.get("grouped", {}).items()):
            lines.append(f"  {code:<18} {len(items)} élément(s)")
        refs = keyed.get("refs")
        lines.append(f"Revérification par identifiant (instance à clé) : "
                     f"{len(refs or {})} fiche(s) — {_states(refs)}")
        return lines

    cmp_ = compare(public, keyed)
    same = sum(1 for c in cmp_["categories"].values()
               if c["comparable"] and not c["only_public"] and not c["only_keyed"]
               and not c["diffs"])
    comparable = sum(1 for c in cmp_["categories"].values() if c["comparable"])
    lines.append(f"Moisson : {same}/{comparable} catégorie(s) comparables identiques "
                 f"({len(cmp_['categories']) - comparable} non comparable(s)).")
    for code, c in cmp_["categories"].items():
        if not c["comparable"]:
            lines.append(f"  {code:<18} non comparable (échec : "
                         f"{'public' if c['failed_public'] else 'à clé'})")
            continue
        if not (c["only_public"] or c["only_keyed"] or c["diffs"]):
            lines.append(f"  {code:<18} identique ({c['common']} élément(s))")
            continue
        lines.append(f"  {code:<18} {c['common']} commun(s), "
                     f"{len(c['only_public'])} public seul, {len(c['only_keyed'])} à clé "
                     f"seule, {len(c['diffs'])} valeur(s) divergente(s)")
        if c["only_public"]:
            lines.append(f"      public seul : {cut(c['only_public'])}")
        if c["only_keyed"]:
            lines.append(f"      à clé seule : {cut(c['only_keyed'])}")
        for d in c["diffs"][:max_list]:
            lines.append(f"      ≠ {d['ref']} {d['field']} : public {d['public']!r} · "
                         f"à clé {d['keyed']!r}")
    r = cmp_["refs"]
    if r is None:
        lines.append("Revérification par identifiant : non comparable "
                     f"(public : {public.get('refs_error') or 'ok'} ; "
                     f"à clé : {keyed.get('refs_error') or 'ok'}).")
    else:
        lines.append(f"Revérification par identifiant : {r['identical']}/{r['total']} "
                     f"fiche(s) identiques ; états public [{_states(public.get('refs'))}] "
                     f"· à clé [{_states(keyed.get('refs'))}].")
        for d in r["diffs"][:max_list]:
            lines.append(f"      ≠ {d['ref']} {d['field']} : public {d['public']!r} · "
                         f"à clé {d['keyed']!r}")
        if len(r["diffs"]) > max_list:
            lines.append(f"      … (+{len(r['diffs']) - max_list} divergence(s))")
    return lines


# ── Lecture de la base (seule) ───────────────────────────────────────────────

def load_inputs(conn, property_id: str) -> tuple[dict, list[dict], list[str]]:
    """Logement, catégories moissonnées (même filtre que le pipeline) et références OSM
    des fiches retenues. SELECT uniquement."""
    from enrich import claude_enrich, db, overpass   # noqa: PLC0415
    prop = dict(db.load_property(conn, property_id))
    cats = [dict(c) for c in db.load_categories(conn)
            if c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES
            and (c["code"] != "tobacco"
                 or claude_enrich.tobacco_rubric_applies(prop.get("country_code")))]
    refs = [r["source_ref"] for r in conn.execute(
        """SELECT source_ref FROM pois WHERE property_id = %s
             AND status IN ('approved', 'edited') AND source = 'osm'
             AND source_ref IS NOT NULL ORDER BY source_ref""", (property_id,))]
    return prop, cats, refs


def main(argv: list[str] | None = None, *, fetchers: dict | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare le serveur Overpass public et l'instance à clé (V2-93b).")
    parser.add_argument("--property", dest="property_id", required=True)
    parser.add_argument("--public-url", default=PUBLIC_URL)
    parser.add_argument("--max-list", type=int, default=10,
                        help="nombre maximal d'éléments listés par différence.")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut : backend/.env).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)

    import psycopg   # noqa: PLC0415
    from enrich import db, overpass   # noqa: PLC0415
    from enrich.settings import settings   # noqa: PLC0415

    def say(line: str) -> None:
        log.info("%s", overpass.scrub(line))

    key = settings.overpass_api_key
    if fetchers is None:
        if not key:
            say("✗ CASAGUIDE_OVERPASS_API_KEY absente : pas d'instance à clé à comparer.")
            return 2
        fetchers = {"public": live_fetcher(args.public_url),
                    "keyed": live_fetcher(settings.overpass_url, key)}
    say(f"· {overpass.describe_mode()}")
    say(f"· public : {overpass.mask_url(args.public_url)} · à clé : "
        f"{overpass.mask_url(settings.overpass_url)}")

    try:
        conn = db.connect()
    except psycopg.OperationalError as exc:
        say(f"✗ connexion à la base impossible : {exc}")
        return 1
    with conn:
        conn.read_only = True            # garantie : aucune écriture possible
        try:
            prop, cats, refs = load_inputs(conn, args.property_id)
        except LookupError as exc:
            say(f"✗ {exc}")
            return 2
        finally:
            conn.rollback()              # aucune transaction ouverte pendant le réseau
    if prop.get("lat") is None:
        say("✗ logement sans position : rien à comparer.")
        return 2
    say(f"· {len(cats)} catégorie(s), {len(refs)} fiche(s) OSM retenue(s)")

    results = {}
    for side in ("public", "keyed"):
        results[side] = fetchers[side](prop["lat"], prop["lon"], cats,
                                       prop.get("country_code"), refs)
    for line in render(prop, results["public"], results["keyed"], max_list=args.max_list):
        say(line)
    total = results["public"].get("requests", 0) + results["keyed"].get("requests", 0)
    say(f"Requêtes Overpass envoyées : {total} (dont {results['keyed'].get('requests', 0)} "
        f"sur le quota de l'instance à clé).")
    return 1 if results["keyed"].get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
