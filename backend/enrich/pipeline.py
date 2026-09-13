"""Orchestrateur du pipeline d'enrichissement (§5.1 du CdC).

Usage :
    python -m enrich.pipeline --property-id <uuid> [--no-claude]
                              [--categories restaurant,hospital,...]

Étapes (chacune tracée dans enrichment_jobs.steps) :
    1. geocode   : adresse -> lat/lon (sauté si le logement a déjà un geom)
    2. overpass  : POI par catégorie dans le rayon du seed
    3. distances : temps à pied / en voiture (OSRM, fallback estimation)
    4. claude    : area_facts (urgences, tri, bruit) + descriptions éditoriales
    5. save      : upserts en base, statut 'suggested' -> validation propriétaire
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import re
import sys
import time
import unicodedata
from typing import Callable

import anthropic
import httpx

from . import claude_enrich, db, dedup, distance, fusion, geocode, judge, overpass, overture
from .settings import settings

log = logging.getLogger("casaguide.pipeline")


def _slug(name: str, maxlen: int = 48) -> str:
    """Fragment stable pour `source_ref` d'un POI créé (baby-sitting) → l'upsert
    par (property, source, source_ref) reste idempotent d'un run à l'autre."""
    ascii_name = (unicodedata.normalize("NFKD", name or "")
                  .encode("ascii", "ignore").decode())
    s = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return s[:maxlen].strip("-") or "service"


def _progress(msg: str) -> None:
    """Signe de vie d'un run (5-30 min) — OPS-4 Pièce 3. Imprimé IMMÉDIATEMENT
    (flush : le terminal/journal voit chaque étape sans attendre la fin) ET logué."""
    print(msg, flush=True)
    log.info(msg)


def _record_failed_call_cost(conn, property_id: str, job_id: str, operation: str,
                             exc: Exception) -> float:
    """Comptabilise le coût des essais d'un appel Claude qui a ÉCHOUÉ au parsing —
    chemin web (V2-07 3bis) OU sans web (V2-37 1bis, descriptions) : l'argent est
    dépensé à la réponse, pas au succès. À appeler dans le `except` best-effort,
    APRÈS le rollback du SAVEPOINT (donc sur la transaction principale ; `conn.commit()`
    par l'appelant). Renvoie le coût total (0 si l'exception ne porte pas de coût)."""
    attempts = getattr(exc, "attempts", None) or []
    db.record_costs(conn, property_id, job_id, "anthropic", operation, attempts)
    return round(sum(c["cost_cts"] for c in attempts), 4)


def _cap_by_travel(code: str, pois: list[dict]) -> list[dict]:
    """Cape certaines catégories (aéroport, V2-44) aux N plus proches EN TEMPS DE
    TRAJET, une fois les distances calculées — un aéroport de vacances utile est l'un
    des rares hubs les plus proches, pas les 8 aérodromes du rayon (benchmark : 7
    aéroports, Ostende à 132 min). `dedup._travel` = temps de trajet du candidat
    (repli distance à vol d'oiseau)."""
    cap = overpass.NEAREST_BY_TRAVEL.get(code)
    if cap and len(pois) > cap:
        return sorted(pois, key=dedup._travel)[:cap]
    return pois


def _apply_service_rules_step(conn, code: str, pois: list[dict], prop: dict, ai,
                              job_id: str, summary: dict, today: str) -> list[dict]:
    """Règles de service (V2-50) sur une catégorie de SERVICE : cherche par web le
    contact + le sous-type des POI, complète les champs MANQUANTS, puis RETIRE tout
    service à la fois non contactable ET non qualifiable. Best-effort : si le web échoue,
    on GARDE les POI tels quels (jamais de retrait faute de web) et le coût est
    comptabilisé. Renvoie la liste filtrée."""
    if code not in settings.service_rule_categories or not pois or ai is None:
        return pois
    property_id = prop["id"]
    label = db.category_label_fr(conn, code)
    try:
        with conn.transaction():
            qual, meta = claude_enrich.qualify_services(
                code, label, pois, prop["city"], prop["country_code"], ai, today=today)
            db.record_costs(conn, property_id, job_id, "anthropic",
                            "service_rules", meta["attempts"])
            summary["cost_cts"] += meta["cost_cts"]
    except Exception as exc:  # noqa: BLE001 — best-effort : jamais de retrait faute de web
        log.warning("Règles de service (%s / %s) non résolues : %s",
                    code, prop["city"], exc)
        c = _record_failed_call_cost(conn, property_id, job_id, "service_rules", exc)
        summary["cost_cts"] += c
        conn.commit()
        return pois
    kept, dropped, qualified = claude_enrich.apply_service_rules(pois, qual)
    summary["service_dropped"] += len(dropped)
    summary["service_qualified"] += qualified
    if dropped:
        _progress(f"  ✓ règles service {code} : {qualified} qualifié(s), "
                  f"{len(dropped)} sans contact ni offre retiré(s)")
    return kept


def _discover_web_rentals(conn, prop: dict, origin: tuple, ai, job_id: str,
                          http_client: httpx.Client | None, summary: dict) -> list[dict]:
    """Découverte web des LOUEURS (V2-44 volet 2), prêts à fusionner avec l'OSM.

    Chaque loueur (vérifié avec preuve) est GÉOCODÉ par son adresse (l'adresse existe
    même quand le lieu est absent d'OSM) → position réelle + distances OSRM. Échec de
    géocodage → écarté + journalisé (jamais de POI sans position). Plafond aux N plus
    proches. `source='web'`, preuve en `completion_meta`, localité issue du géocodage
    (V2-38). Best-effort : tout échec est journalisé, le coût des essais comptabilisé,
    et renvoie []. Cadence propre par logement (mémoire via api_costs, comme le
    baby-sitting : un vide n'est pas re-cherché à chaque run)."""
    property_id = prop["id"]
    if db.recent_operation(conn, property_id, "rental_web",
                           settings.rental_web_max_age_days):
        return []
    today = _dt.date.today().isoformat()
    try:
        renters, meta = claude_enrich.fetch_rentals(
            prop["city"], prop["country_code"], ai, today=today)
    except Exception as exc:  # noqa: BLE001 — best-effort (web/parse)
        log.warning("Loueurs (web) non résolus (%s) : %s", prop["city"], exc)
        c = _record_failed_call_cost(conn, property_id, job_id, "rental_web", exc)
        summary["cost_cts"] += c
        db.job_step(conn, job_id, "rental_web",
                    {"ok": False, "error": overpass._short(str(exc)),
                     "cost_cts": round(c, 2)})
        conn.commit()
        _progress(f"  ⚠ loueurs (web) non résolus : {overpass._short(str(exc))}")
        return []
    db.record_costs(conn, property_id, job_id, "anthropic", "rental_web",
                    meta["attempts"])
    summary["cost_cts"] += meta["cost_cts"]
    # Géocodage par adresse → position ; échec → écarté journalisé.
    geocoded: list[dict] = []
    skipped_geo = 0
    for r in renters:
        try:
            geo = geocode.geocode(address=r["address"],
                                  country_code=prop["country_code"], client=http_client)
        except geocode.GeocodeError:
            skipped_geo += 1
            log.warning("Loueur web « %s » sauté : adresse non géocodable (%s)",
                        r["name"], r["address"])
            continue
        geocoded.append({
            "name": r["name"], "lat": geo["lat"], "lon": geo["lon"],
            "address": r["address"], "category": "rental", "source": "web",
            "phone": r.get("phone"), "website": r.get("website"),
            "locality": geo.get("locality"),   # V2-38 : commune du géocodage
            "source_ref": "web:rental:" + _slug(r["name"]),
            "crow_m": overpass.haversine_m(origin[0], origin[1], geo["lat"], geo["lon"]),
            "completion_meta": {"_web": {"source_url": r.get("source_url"),
                                         "verified_on": r.get("verified_on")}},
        })
    # Plafond : les N loueurs web les plus proches.
    geocoded.sort(key=lambda p: p["crow_m"])
    kept = geocoded[:settings.rental_web_max_results]
    if kept:
        try:
            distance.compute_distances(origin, kept, client=http_client)
        except Exception as exc:  # noqa: BLE001 — les distances ne bloquent pas
            log.warning("Distances loueurs web non calculées : %s", exc)
    summary["rental_web_kept"] = len(kept)
    db.job_step(conn, job_id, "rental_web",
                {"ok": True, "discovered": len(renters), "kept": len(kept),
                 "skipped_geocode": skipped_geo,
                 "cost_cts": round(meta["cost_cts"], 2)})
    conn.commit()
    _progress(f"  ✓ loueurs (web) : {len(kept)} retenu(s) / {len(renters)} trouvé(s)"
              + (f", {skipped_geo} sans position" if skipped_geo else "")
              + f" — {meta['cost_cts']:.2f} ct")
    return kept


def _discover_editorial_sorties(conn, prop: dict, origin: tuple, ai, job_id: str,
                                http_client: httpx.Client | None,
                                summary: dict) -> dict[str, list[dict]]:
    """Sélection éditoriale « sorties » (V2-56) : découverte des adresses RÉPUTÉES
    (restaurant/bar/cafe) du secteur par Claude+web, UNE seule fois par run (couvre les
    trois catégories), mémorisée sur `summary`. Chaque pick est géocodé par son adresse
    (le lieu réputé a une adresse même absent d'OSM) + distances OSRM ; un pick non
    géocodable est écarté et journalisé. Renvoie {code: [picks]}. Best-effort : tout
    échec web est journalisé, le coût des essais comptabilisé, et renvoie {} (jamais un
    job cassé). Cadence propre par logement (un vide n'est pas re-cherché)."""
    if "_editorial_picks" in summary:
        return summary["_editorial_picks"]
    property_id = prop["id"]
    out: dict[str, list[dict]] = {}
    summary["_editorial_picks"] = out
    if db.recent_operation(conn, property_id, "reputed_sorties",
                           settings.reputed_max_age_days):
        return out
    today = _dt.date.today().isoformat()
    try:
        places, meta = claude_enrich.fetch_reputed_places(
            prop["city"], prop["country_code"], ai, today=today)
    except Exception as exc:  # noqa: BLE001 — best-effort (web/parse)
        log.warning("Sélection éditoriale (%s) non résolue : %s", prop["city"], exc)
        c = _record_failed_call_cost(conn, property_id, job_id, "reputed_sorties", exc)
        summary["cost_cts"] += c
        db.job_step(conn, job_id, "reputed_sorties",
                    {"ok": False, "error": overpass._short(str(exc)),
                     "cost_cts": round(c, 2)})
        conn.commit()
        _progress(f"  ⚠ sélection éditoriale non résolue : {overpass._short(str(exc))}")
        return out
    db.record_costs(conn, property_id, job_id, "anthropic", "reputed_sorties",
                    meta["attempts"])
    summary["cost_cts"] += meta["cost_cts"]
    skipped_geo = 0
    for pl in places:
        code = pl["category"]
        try:
            geo = geocode.geocode(street=pl["address"], city=prop["city"],
                                  country_code=prop["country_code"], client=http_client)
        except geocode.GeocodeError:
            skipped_geo += 1
            log.warning("Pick réputé « %s » sauté : adresse non géocodable (%s)",
                        pl["name"], pl["address"])
            continue
        out.setdefault(code, []).append({
            "name": pl["name"], "lat": geo["lat"], "lon": geo["lon"],
            "address": pl["address"], "locality": geo.get("locality"),
            "category": code, "source": "web",
            "phone": pl.get("phone"), "website": pl.get("website"),
            "opening_hours": None, "cuisine": None, "description_md": None,
            "owner_comment": pl.get("reason") or None,
            "source_ref": "web:reputed:" + _slug(pl["name"]),
            "crow_m": overpass.haversine_m(origin[0], origin[1], geo["lat"], geo["lon"]),
            "completion_meta": {"_editorial": {"source_url": pl.get("source_url"),
                                               "verified_on": pl.get("verified_on")}},
        })
    all_picks = [p for lst in out.values() for p in lst]
    if all_picks:
        try:
            distance.compute_distances(origin, all_picks, client=http_client)
        except Exception as exc:  # noqa: BLE001 — les distances ne bloquent pas
            log.warning("Distances picks éditoriaux non calculées : %s", exc)
    summary["editorial_found"] = len(all_picks)
    db.job_step(conn, job_id, "reputed_sorties",
                {"ok": True, "discovered": len(places), "geocoded": len(all_picks),
                 "skipped_geocode": skipped_geo,
                 "by_category": {k: len(v) for k, v in out.items()},
                 "cost_cts": round(meta["cost_cts"], 2)})
    conn.commit()
    _progress(f"  ✓ sélection éditoriale : {len(all_picks)} pick(s) réputé(s) / "
              f"{len(places)} trouvé(s)"
              + (f", {skipped_geo} sans position" if skipped_geo else "")
              + f" — {meta['cost_cts']:.2f} ct")
    return out


def _best_editorial_match(pk: dict, candidates: list[dict]) -> dict | None:
    """Meilleur candidat « même lieu » (matcher V2-52 `same_place`) pour un pick, ou
    None. Le plus proche à égalité de nom."""
    best, best_d = None, None
    for c in candidates:
        if fusion.same_place(pk, c):
            d = overpass.haversine_m(pk["lat"], pk["lon"], c["lat"], c["lon"])
            if best_d is None or d < best_d:
                best, best_d = c, d
    return best


def _fill_editorial_contacts(target: dict, src: dict) -> None:
    """Comble les contacts NULL de `target` depuis `src` (jamais d'écrasement)."""
    for f in ("phone", "website"):
        if not target.get(f) and src.get(f):
            target[f] = src[f]


def _mark_editorial(poi: dict, pk: dict) -> None:
    """Marque un POI comme pick éditorial (badge « réputé » + raison), sans écraser un
    coup de cœur déjà présent."""
    if not poi.get("owner_comment") and pk.get("owner_comment"):
        poi["owner_comment"] = pk["owner_comment"]
    meta = dict(poi.get("completion_meta") or {})
    meta["_editorial"] = ((pk.get("completion_meta") or {}).get("_editorial")
                          or {"origin": "match"})
    poi["completion_meta"] = meta


def _merge_editorial_picks(pois: list[dict], picks: list[dict],
                           ovt: list[dict] | None) -> tuple[list[dict], int]:
    """Fusionne les picks éditoriaux (V2-56). Un pick apparié à un POI OSM/Overture
    DÉJÀ présent le MARQUE (réputé + raison) et récupère ses contacts manquants — pas de
    doublon. Sinon, un pick apparié à un Overture (hors liste) récupère ses contacts,
    puis entre comme sa propre fiche géocodée. Renvoie `(pois, n_ajoutés)`."""
    added = 0
    for pk in picks:
        m = _best_editorial_match(pk, pois)
        if m is not None:
            _mark_editorial(m, pk)
            _fill_editorial_contacts(m, pk)
            continue
        ov = _best_editorial_match(pk, ovt) if ovt else None
        if ov is not None:
            _fill_editorial_contacts(pk, ov)
        _mark_editorial(pk, pk)
        pois.append(pk)
        added += 1
    return pois, added


def _cap_guest_sorties(pois: list[dict], target: int) -> list[dict]:
    """Cape une catégorie « sorties » d'un guide voyageur (V2-56) à `target` en
    GARANTISSANT les picks éditoriaux (les réputés), puis en complétant par les plus
    proches. Ne jette jamais un réputé (le cap ne descend pas sous leur nombre)."""
    def _is_ed(p: dict) -> bool:
        return bool((p.get("completion_meta") or {}).get("_editorial"))
    ed = [p for p in pois if _is_ed(p)]
    rest = sorted((p for p in pois if not _is_ed(p)), key=dedup._travel)
    return ed + rest[:max(0, target - len(ed))]


def _resolve_market_position(market: dict, prop: dict,
                             http_client: httpx.Client | None
                             ) -> tuple[float | None, float | None]:
    """Position FIABLE d'un marché (V2-07 volet 3) — sinon (None, None) et l'appelant
    saute + journalise. Règle de précision : (1) coordonnées de la SOURCE si
    plausibles (à ≤ MARKET_MAX_DIST_M du logement — garde-fou anti-hallucination) ;
    (2) sinon géocodage de l'adresse par le module existant, accepté SEULEMENT si la
    précision N'EST PAS « city » (jamais de marqueur au niveau ville — un marché mal
    placé est pire qu'absent) et reste dans le rayon plausible."""
    plat, plon = prop["lat"], prop["lon"]
    lat, lon = market.get("lat"), market.get("lon")
    if lat is not None and lon is not None:
        if overpass.haversine_m(plat, plon, lat, lon) <= claude_enrich.MARKET_MAX_DIST_M:
            return lat, lon
        log.warning("Marché « %s » : coordonnées de source aberrantes (%.4f,%.4f) — "
                    "repli géocodage", market.get("name"), lat, lon)
    addr = (market.get("address") or "").strip()
    if addr:
        try:
            geo = geocode.geocode(street=addr, city=prop["city"],
                                  country_code=prop["country_code"], client=http_client)
        except geocode.GeocodeError:
            return None, None
        if (geo["accuracy"] != "city"
                and overpass.haversine_m(plat, plon, geo["lat"], geo["lon"])
                <= claude_enrich.MARKET_MAX_DIST_M):
            return geo["lat"], geo["lon"]
    return None, None


def _default_overture_fetch(lat: float, lon: float, radius_m: int) -> list[dict]:
    """Fetcher Overture de PRODUCTION (DuckDB/S3, réseau). Résout la release depuis la
    config. Injectable via le paramètre `overture_fetch` de `run` (tests sans réseau)."""
    return overture.fetch_places(lat, lon, radius_m, release=settings.overture_release)


def _judge_and_publish_guest(conn, prop: dict, ai, job_id: str,
                             summary: dict, use_claude: bool) -> None:
    """Offre « Guide Voyageur » (V2-54) : arbitre AUTOMATIQUEMENT les POI moissonnés
    puis publie la fiche. Chaque POI `suggested` est jugé (juge IA V2-45) ; un `reject`
    à confiance ≥ `judge_reject_threshold` est écarté (statut 'rejected', motif tracé
    dans `completion_meta._judge`), tout le reste est `approved` (pas de triage humain
    sur cette offre). ROBUSTE : un échec du juge approuve tout (le doute garde, rien de
    détruit) et la fiche est publiée quand même — un guide FR utile vaut mieux qu'un
    échec. Idempotent (n'agit que sur les 'suggested')."""
    property_id = prop["id"]
    pois = db.load_pois_for_judge(conn, property_id)
    threshold = settings.judge_reject_threshold
    approved = rejected = 0
    judge_cost = 0.0
    ok = True

    def _approve_all(remaining: list[dict], reason: str) -> int:
        n = 0
        for p in remaining:
            db.apply_judge_verdict(conn, p["id"], "approved",
                                   {"verdict": "keep", "confidence": 0.0,
                                    "reason": reason, "threshold": threshold})
            n += 1
        return n

    if pois and use_claude and ai is not None:
        try:
            def ask(prompt: str):
                return claude_enrich._ask_json(
                    ai, prompt, max_tokens=settings.judge_max_tokens)
            verdicts, attempts = judge.judge_pois(
                prop, pois, ask, batch_size=settings.judge_batch_size)
            verdicts, _defaulted = judge.finalize_verdicts(pois, verdicts)
            db.record_costs(conn, property_id, job_id, "anthropic", "judge", attempts)
            judge_cost = round(sum(a.get("cost_cts", 0.0) for a in attempts), 4)
            summary["cost_cts"] += judge_cost
            for p in pois:
                v = verdicts[p["id"]]
                status = ("rejected"
                          if v.verdict == "reject" and v.confidence >= threshold
                          else "approved")
                db.apply_judge_verdict(conn, p["id"], status,
                                       {"verdict": v.verdict, "confidence": v.confidence,
                                        "reason": v.reason, "threshold": threshold})
                if status == "rejected":
                    rejected += 1
                else:
                    approved += 1
        except Exception as exc:  # noqa: BLE001 — le juge ne bloque JAMAIS la livraison
            log.warning("Juge (guide voyageur %s) non résolu : %s", property_id, exc)
            c = _record_failed_call_cost(conn, property_id, job_id, "judge", exc)
            summary["cost_cts"] += c
            judge_cost += c
            ok = False
            # Approve-all sur ce qui reste `suggested` : rien de détruit.
            approved += _approve_all(db.load_pois_for_judge(conn, property_id),
                                     "juge indisponible → approuvé par défaut")
    elif pois:
        # Chemin sans IA (--no-claude) : approuver tout, publier (guide FR minimal).
        approved += _approve_all(pois, "juge non exécuté (sans IA)")

    summary["judge_approved"] = approved
    summary["judge_rejected"] = rejected
    db.job_step(conn, job_id, "judge",
                {"ok": ok, "judged": len(pois), "approved": approved,
                 "rejected": rejected, "threshold": threshold,
                 "cost_cts": round(judge_cost, 2)})
    db.publish_property(conn, property_id)
    conn.commit()
    _progress(f"  ✓ juge : {approved} approuvé(s), {rejected} rejeté(s) "
              f"(seuil {threshold}) — {judge_cost:.2f} ct ; guide publié")


def run(property_id: str, *, use_claude: bool = True, trigger: str = "manual",
        only_categories: set[str] | None = None,
        job_id: str | None = None,
        http_client: httpx.Client | None = None,
        anthropic_client: anthropic.Anthropic | None = None,
        overture_fetch: Callable[[float, float, int], list[dict]] | None = None) -> dict:
    """Exécute le pipeline pour un logement. Retourne un résumé.

    Si `job_id` est fourni (job 'pending' pré-créé par l'API pour renvoyer un
    identifiant immédiat), il est réutilisé ; sinon un nouveau job est créé.
    """
    summary: dict = {"pois": 0, "categories": {}, "area_facts": False,
                     "cost_cts": 0.0, "services_completed": 0, "babysitters": 0,
                     "markets_created": 0, "duplicates_merged": 0,
                     "rental_web_kept": 0, "hard_cap_dropped": 0,
                     "network_dropped": 0, "service_dropped": 0,
                     "service_qualified": 0,
                     "overture_added": 0, "overture_contacts": 0,
                     "editorial_found": 0, "editorial_added": 0}
    # OPS-4 Pièce 4 (sortie propre) : si le client Anthropic est créé ICI (CLI), il
    # DOIT être fermé — son pool de connexions httpx, laissé ouvert, empêchait le
    # process de rendre la main après le commit final (~1 h de terminal muet le 12/08).
    # Fermé dans le `finally` de l'étape (ci-dessous), quel que soit le dénouement.
    ai: anthropic.Anthropic | None = None
    owns_ai = False

    with db.connect() as conn:
        prop = db.load_property(conn, property_id)
        if job_id is None:
            job_id = db.job_start(conn, property_id, trigger)
        else:
            db.job_mark_running(conn, job_id)
        conn.commit()
        _progress(f"▶ Enrichissement {prop.get('name') or property_id} "
                  f"({prop['city']}, {prop['country_code']}) — job {job_id}")

        try:
            # ── 1. Géocodage ────────────────────────────────────────────────
            if prop["lat"] is None:
                geo = geocode.geocode(
                    country_code=prop["country_code"], client=http_client,
                    street=prop["address_line1"], postalcode=prop["postal_code"],
                    city=prop["city"])
                db.save_geocode(conn, property_id, geo["lat"], geo["lon"],
                                geo["source"], geo["accuracy"])
                prop["lat"], prop["lon"] = geo["lat"], geo["lon"]
                # V2-46 : commune/CP incohérents avec la saisie (rue homonyme) → NE PAS
                # moissonner (132 POI hors sujet, cas CASA MURCIA). La position est
                # enregistrée en 'mismatch' pour l'ajustement propriétaire, et le job
                # s'arrête proprement (échec motivé, aucune corruption).
                if geo["accuracy"] == "mismatch":
                    mm = geo.get("mismatch")
                    reason = mm.message_fr() if mm is not None else \
                        "commune/code postal incohérents avec la saisie"
                    db.job_step(conn, job_id, "geocode",
                                {"ok": False, "accuracy": "mismatch", "reason": reason})
                    conn.commit()
                    _progress(f"  ✖ géocodage incohérent : {reason}")
                    raise geocode.GeocodeError(reason)
                db.job_step(conn, job_id, "geocode",
                            {"ok": True, "accuracy": geo["accuracy"]})
                _progress(f"  ✓ géocodage : {geo['accuracy']} "
                          f"({geo['lat']:.4f}, {geo['lon']:.4f})")
            else:
                db.job_step(conn, job_id, "geocode", {"ok": True, "skipped": True})
                _progress("  ✓ géocodage : déjà positionné")
            conn.commit()  # progression visible en temps réel
            origin = (prop["lat"], prop["lon"])

            # Client Claude créé TÔT : la découverte web des loueurs (V2-44 volet 2)
            # en a besoin DANS la boucle de catégories (fusion avant la passe V2-40),
            # bien avant l'étape 4. Sans use_claude il reste None. Fermé dans le
            # `finally` (owns_ai) — un client PASSÉ n'est jamais fermé par le pipeline.
            if use_claude:
                ai = anthropic_client or anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"])
                owns_ai = anthropic_client is None

            # ── 2 + 3. POI Overpass puis distances ─────────────────────────
            # Overpass : une requête par palier de rayon (union de sélecteurs),
            # résultats re-ventilés par catégorie via leurs tags (perf, M-01).
            categories = db.load_categories(conn)
            wanted = [c for c in categories
                      if (not only_categories or c["code"] in only_categories)
                      and c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES]
            grouped, failed_categories, harvest = overpass.fetch_grouped(
                wanted, origin[0], origin[1], client=http_client)

            # ── V2-52 volet 1 : acquisition Overture (une seule extraction bbox) ──
            # Décision de sources (benchmark 2026-09-09) : OSM le factuel, Overture le
            # commercial. Best-effort ABSOLU : tout échec (S3/duckdb/mapping) est tracé
            # et le pipeline CONTINUE sur OSM seul (jamais un job cassé par la source
            # secondaire). Gardé par le flag `overture_enabled` (dark-launch).
            fetch_ovt = overture_fetch or (
                _default_overture_fetch if settings.overture_enabled else None)
            overture_by_code: dict[str, list[dict]] = {}
            wanted_scope = {c["code"] for c in wanted} & fusion.SCOPE
            if fetch_ovt is not None and wanted_scope:
                try:
                    max_r = min(settings.overture_bbox_max_radius_m,
                                max(c["default_radius_m"] for c in wanted
                                    if c["code"] in wanted_scope))
                    raw = fetch_ovt(origin[0], origin[1], max_r)
                    cmap = overture.load_category_map()
                    for v in raw:
                        code = overture.map_overture_category(v.get("category"), cmap)
                        if code in fusion.SCOPE:
                            overture_by_code.setdefault(code, []).append(v)
                    mapped = sum(len(x) for x in overture_by_code.values())
                    db.job_step(conn, job_id, "overture",
                                {"ok": True, "fetched": len(raw), "mapped": mapped,
                                 "by_category": {k: len(v)
                                                 for k, v in overture_by_code.items()}})
                    _progress(f"  ✓ Overture : {len(raw)} lieu(x), "
                              f"{mapped} en périmètre commercial")
                except Exception as exc:  # noqa: BLE001 — dégradation douce sur OSM seul
                    db.job_step(conn, job_id, "overture",
                                {"ok": False, "error": overpass._short(str(exc))})
                    _progress(f"  ⚠ Overture indisponible "
                              f"({overpass._short(str(exc))}) — OSM seul")
                    overture_by_code = {}
                conn.commit()

            all_editorial: list[dict] = []
            capped_empty: set[str] = set()   # V2-44 v3 : vidées par le plafond de pertinence
            for cat in wanted:
                code = cat["code"]
                pois = grouped.get(code) or []
                if pois:
                    try:
                        distance.compute_distances(origin, pois, client=http_client)
                    except Exception as exc:
                        # Un échec de distances ne doit pas faire perdre la catégorie :
                        # on la trace (ré-enrichissable) ; on garde une éventuelle
                        # découverte web (loueurs) qui a, elle, ses distances.
                        failed_categories[code] = f"{type(exc).__name__}: {exc}"[:120]
                        pois = []
                    for p in pois:
                        p["category"] = code
                # ── V2-44 volet 2 : découverte web des LOUEURS ────────────────
                # Le loueur du village (Kassteele) est ABSENT d'OSM → introuvable
                # par les tags. Une recherche web (avec preuve) le trouve, on le
                # géocode et on le fusionne AVEC l'OSM avant la passe V2-40 (un
                # loueur trouvé des deux côtés ne fait qu'une fiche, le web — qui
                # apporte tél+site — gagne souvent).
                if code == "rental" and use_claude and ai is not None:
                    pois = pois + _discover_web_rentals(
                        conn, prop, origin, ai, job_id, http_client, summary)
                # ── V2-52 volet 1 : fusion Overture (contacts + comblement) ──
                # Gain 3 : enrichir les contacts des POI OSM appariés (tél/site NULL).
                # Gain 1 : `atm` AUGMENTÉ des banques Overture (toujours). Gain 2 : les
                # autres catégories commerciales COMBLÉES seulement si vides/sous le
                # minimum. Les candidats ajoutés reçoivent leurs distances puis passent
                # par TOUTE la chaîne (pertinence, dédup V2-40, règles V2-50, upsert).
                ovt_contributed = False
                ovt = overture_by_code.get(code) or []
                if ovt and code in fusion.SCOPE:
                    today = _dt.date.today().isoformat()
                    pois, enriched, consumed = fusion.enrich_osm_contacts(
                        pois, ovt, today=today)
                    summary["overture_contacts"] += enriched
                    min_needed = overpass.target_for(code).min_results
                    if code == "atm" or len(pois) < min_needed:
                        radius_m = cat.get("max_radius_m") or cat["default_radius_m"]
                        fill = fusion.build_fill_candidates(
                            ovt, consumed, code=code, lat0=origin[0], lon0=origin[1],
                            radius_m=radius_m, limit=settings.max_pois_per_category,
                            today=today)
                        if fill:
                            try:
                                distance.compute_distances(origin, fill,
                                                           client=http_client)
                            except Exception:  # noqa: BLE001 — garde crow_m en repli
                                pass
                            pois = pois + fill
                            summary["overture_added"] += len(fill)
                            ovt_contributed = True
                # ── V2-44 volet 3 : plafond de PERTINENCE ────────────────────
                # Retire les résultats amenés par l'escalade (hors rayon de préférence)
                # trop LOIN EN ROUTE pour combler le quota — un commissariat à 30 min
                # quand le quota vise le plus proche (le drive_min vient d'être calculé).
                # Mieux vaut une catégorie honnête (voire vide, signalée) que remplie de
                # lieux inutiles.
                if pois:
                    tgt = overpass.target_for(code)
                    pois, capped = overpass.apply_drive_cap(
                        pois, cat["default_radius_m"], tgt.hard_cap_drive_min)
                    if capped:
                        summary["hard_cap_dropped"] += capped
                        if not pois:            # la catégorie devient vide PAR le cap
                            capped_empty.add(code)
                # V2-56 : une catégorie « sorties » VIDE côté OSM/Overture (le cas
                # constaté : bars/restos réputés absents d'OSM) doit tout de même
                # atteindre la passe éditoriale ci-dessous — on ne `continue` pas.
                is_guest_sorties = (prop.get("guest_guide") and use_claude
                                    and ai is not None
                                    and code in claude_enrich.EDITORIAL_SORTIES)
                if not pois and not is_guest_sorties:
                    continue
                # ── Dédoublonnage à la suggestion (V2-40) ────────────────────
                # OSM porte le même lieu en plusieurs éléments (Alicante « (ALC) »
                # + « Miguel Hernández », gare bilingue…) — et un loueur peut venir
                # d'OSM ET du web. On dédoublonne le lot (le mieux renseigné survit)
                # PUIS on retire ce qui double une fiche déjà arbitrée — jamais un
                # successeur légitime (sentinelle XiaoWu).
                pois, in_batch = dedup.deduplicate(pois)
                existing = db.existing_pois_for_dedup(conn, property_id, code)
                pois, vs_existing = dedup.filter_against_existing(pois, existing)
                summary["duplicates_merged"] += in_batch + vs_existing
                # V2-44 : aéroport capé aux 3 plus proches en temps de trajet.
                pois = _cap_by_travel(code, pois)
                # V2-44 volet 2 : rental a DEUX sources (OSM + web) → réconcilie avec
                # les fiches suggested d'un AUTRE source_ref pour ne jamais laisser un
                # doublon inter-run quand le gagnant OSM/web bascule.
                if code == "rental":
                    existing_sugg = db.existing_suggested_pois(conn, property_id, code)
                    pois, stale_ids = dedup.reconcile_suggested(pois, existing_sugg)
                    db.delete_pois(conn, stale_ids)
                # ── V2-50 : contactabilité + qualification des SERVICES ──────
                # Un loueur/taxi/laverie sans tél NI site NI sous-type est du bruit —
                # mais on cherche son contact/offre sur le web AVANT de le retirer (le cas
                # Gregorio : « loue fourgonnettes/camions »). Best-effort.
                if use_claude and code in settings.service_rule_categories:
                    pois = _apply_service_rules_step(
                        conn, code, pois, prop, ai, job_id, summary,
                        _dt.date.today().isoformat())
                    if not pois:
                        continue
                # ── V2-56 : sélection éditoriale « sorties » (GUIDE VOYAGEUR) ──
                # OSM/Overture pauvres sur le commercial touristique + aucun signal de
                # notoriété → on ajoute les adresses RÉPUTÉES (blogs/presse/guides),
                # appariées à OSM/Overture (contacts, position sûre) ou géocodées. Un
                # pick apparié marque la fiche existante ; un pick nouveau entre. Puis
                # cap ENRICHI (restaurant 10, bar 8, cafe 6). Les guides propriétaires
                # ne changent pas (curation humaine). Le juge (guest) passe derrière.
                guest_capped = False
                if is_guest_sorties:
                    picks = _discover_editorial_sorties(
                        conn, prop, origin, ai, job_id, http_client, summary).get(code, [])
                    if picks:
                        # Cap de distance famille F sur les picks comme sur le reste.
                        picks, _dropped = overpass.apply_drive_cap(
                            picks, cat["default_radius_m"],
                            overpass.target_for(code).hard_cap_drive_min)
                        pois, n_ed = _merge_editorial_picks(pois, picks, ovt)
                        summary["editorial_added"] = (
                            summary.get("editorial_added", 0) + n_ed)
                    pois = _cap_guest_sorties(pois, settings.guest_sorties_target(code))
                    guest_capped = True
                # V2-52 : quand Overture a AUGMENTÉ la catégorie (atm, comblement),
                # replafonner aux plus pertinents (banques devant crypto, proche devant
                # lointain) — l'union OSM+Overture peut dépasser le plafond de moisson.
                # Jamais appliqué sur une catégorie OSM seule (déjà plafonnée) →
                # non-régression des catégories pleines. (Le cap guest a déjà tranché.)
                if ovt_contributed and not guest_capped:
                    pois = fusion.cap_after_fusion(pois, settings.max_pois_per_category)
                if code in settings.describe_categories:
                    all_editorial.extend(pois)
                n = db.upsert_pois(conn, property_id, code, pois)
                summary["categories"][code] = n
                summary["pois"] += n
                conn.commit()  # les POI de cette catégorie sont acquis
                db.job_step(conn, job_id, "overpass",
                            {"ok": False, "in_progress": code,
                             "pois": summary["pois"], "failed": failed_categories})
                conn.commit()
            summary["failed_categories"] = failed_categories
            # V2-44 : catégories SANS résultat (rien trouvé, mais pas une erreur) —
            # celles vides à la moisson (volet 1) ET celles VIDÉES par le plafond de
            # pertinence (volet 3). Une catégorie qui a fini avec des POI (ex. rental
            # garni par le web) n'est PAS vide, même si l'OSM n'a rien donné.
            empty_categories = sorted(
                (set(harvest.get("empty") or []) | capped_empty)
                - set(summary["categories"]))
            summary["empty_categories"] = empty_categories
            summary["generic_dropped"] = harvest.get("generic_dropped", 0)
            summary["network_dropped"] = harvest.get("network_dropped", 0)
            db.job_step(conn, job_id, "overpass",
                        {"ok": not failed_categories or summary["pois"] > 0,
                         "pois": summary["pois"],
                         "duplicates_merged": summary["duplicates_merged"],
                         "empty": empty_categories,
                         "generic_dropped": summary["generic_dropped"],
                         "network_dropped": summary["network_dropped"],
                         "hard_cap_dropped": summary["hard_cap_dropped"],
                         "failed": failed_categories})
            db.job_step(conn, job_id, "distances", {"ok": True})
            conn.commit()
            _progress(f"  ✓ Overpass : {summary['pois']} POI"
                      + (f", {summary['duplicates_merged']} doublon(s) fusionné(s)"
                         if summary["duplicates_merged"] else "")
                      + (f", {summary['generic_dropped']} sans-nom écarté(s)"
                         if summary["generic_dropped"] else "")
                      + (f", {summary['network_dropped']} station(s) réseau réduite(s)"
                         if summary["network_dropped"] else "")
                      + (f", {summary['hard_cap_dropped']} hors plafond de route"
                         if summary["hard_cap_dropped"] else "")
                      + (f", +{summary['overture_added']} Overture"
                         if summary["overture_added"] else "")
                      + (f", {summary['overture_contacts']} contact(s) Overture"
                         if summary["overture_contacts"] else "")
                      + (f" — {len(failed_categories)} catégorie(s) en échec : "
                         + ", ".join(sorted(failed_categories))
                         if failed_categories else " — 0 échec")
                      + (f" ; sans résultat : " + ", ".join(sorted(empty_categories))
                         if empty_categories else ""))

            # ── 4. Enrichissement Claude ────────────────────────────────────
            # Le client `ai` est déjà créé plus haut (la découverte web des loueurs
            # en avait besoin dans la boucle) ; ici on l'utilise seulement.
            if use_claude:
                # 4a. Données locales mutualisées (pays + commune)
                if not db.area_facts_fresh(conn, prop["country_code"], prop["city"]):
                    facts, meta = claude_enrich.fetch_area_facts(
                        prop["city"], prop["country_code"], ai)
                    db.upsert_area_facts(conn, prop["country_code"], prop["city"],
                                         facts, source=settings.anthropic_model)
                    db.record_cost(conn, property_id, job_id, "anthropic",
                                   "area_facts", meta["units"], meta["cost_cts"])
                    summary["cost_cts"] += meta["cost_cts"]
                    db.job_step(conn, job_id, "area_facts",
                                {"ok": True, "cost_cts": round(meta["cost_cts"], 2)})
                    _progress(f"  ✓ données locales (urgences/tri/bruit) — "
                              f"{meta['cost_cts']:.2f} ct")
                else:
                    db.job_step(conn, job_id, "area_facts",
                                {"ok": True, "skipped": "frais (mutualisé)"})
                summary["area_facts"] = True
                conn.commit()

                # 4a-bis. Livraison de repas par zone (V2-07 volet 1) : appel
                # SÉPARÉ (recherche web, cadence de rafraîchissement propre),
                # mutualisé par (pays, commune). Best-effort : un JSON malformé ou
                # un échec réseau n'écrit rien et NE fait PAS échouer le job (le
                # reste de l'enrichissement est déjà acquis) — « rejeté sans
                # écriture », doctrine du prompt intacte (« N'invente jamais »).
                if not db.area_fact_fresh(conn, prop["country_code"], prop["city"],
                                          claude_enrich.FOOD_DELIVERY_FACT_TYPE,
                                          settings.food_delivery_max_age_days):
                    try:
                        # SAVEPOINT : un échec ici n'annule QUE ce bloc — les
                        # area_facts et POI déjà écrits dans cette transaction
                        # restent intacts (sinon un pépin de livraison ruinerait
                        # tout l'enrichissement).
                        with conn.transaction():
                            fd, meta = claude_enrich.fetch_food_delivery(
                                prop["city"], prop["country_code"], ai)
                            n_plat = len(fd[claude_enrich.FOOD_DELIVERY_FACT_TYPE]
                                         ["platforms"])
                            db.upsert_area_facts(conn, prop["country_code"],
                                                 prop["city"], fd,
                                                 source=settings.anthropic_model)
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "food_delivery", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            db.job_step(conn, job_id, "food_delivery",
                                        {"ok": True, "platforms": n_plat,
                                         "cost_cts": round(meta["cost_cts"], 2)})
                        _progress(f"  ✓ livraison de repas : {n_plat} plateforme(s) "
                                  f"— {meta['cost_cts']:.2f} ct")
                    except Exception as fd_exc:  # noqa: BLE001 — best-effort
                        log.warning("Livraison de repas (%s) non résolue : %s",
                                    prop["city"], fd_exc)
                        # Coût des essais PAYÉS malgré l'échec (volet 3bis).
                        c = _record_failed_call_cost(conn, property_id, job_id,
                                                     "food_delivery", fd_exc)
                        summary["cost_cts"] += c
                        db.job_step(conn, job_id, "food_delivery",
                                    {"ok": False, "error": overpass._short(str(fd_exc)),
                                     "cost_cts": round(c, 2)})
                        conn.commit()
                        _progress(f"  ⚠ livraison de repas non résolue : "
                                  f"{overpass._short(str(fd_exc))}")

                # 4b. Descriptions courtes des POI éditoriaux — BEST-EFFORT (V2-37 1bis) :
                # une réponse non parsable ne tue plus le JOB (elle le tuait — Ardon
                # 16/08 : JSONDecodeError char 0 → job en échec). SAVEPOINT comme 4c/4d :
                # l'échec est journalisé (compteur + raison), le coût des essais est
                # comptabilisé, et le pipeline continue (complétions, marchés, save).
                if all_editorial:
                    try:
                        with conn.transaction():
                            descs, meta = claude_enrich.describe_pois(
                                all_editorial, prop["city"], prop["country_code"], ai)
                            for p in all_editorial:
                                if p["source_ref"] in descs:
                                    p["description_md"] = descs[p["source_ref"]]
                            for code in {p["category"] for p in all_editorial}:
                                db.upsert_pois(conn, property_id, code,
                                               [p for p in all_editorial
                                                if p["category"] == code])
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "describe_pois", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            db.job_step(conn, job_id, "describe_pois",
                                        {"ok": True, "described": len(descs),
                                         "cost_cts": round(meta["cost_cts"], 2)})
                        _progress(f"  ✓ descriptions : {len(descs)} POI — "
                                  f"{meta['cost_cts']:.2f} ct")
                    except Exception as de_exc:  # noqa: BLE001 — best-effort
                        log.warning("Descriptions (%s) non résolues : %s",
                                    prop["city"], de_exc)
                        c = _record_failed_call_cost(conn, property_id, job_id,
                                                     "describe_pois", de_exc)
                        summary["cost_cts"] += c
                        db.job_step(conn, job_id, "describe_pois",
                                    {"ok": False, "described": 0,
                                     "error": overpass._short(str(de_exc)),
                                     "cost_cts": round(c, 2)})
                        conn.commit()
                        _progress(f"  ⚠ descriptions non résolues : "
                                  f"{overpass._short(str(de_exc))} ({c:.2f} ct)")

                # 4c. Complétion des fiches de SERVICE (V2-07 volet 2) : tel / site /
                # horaires par recherche web, AVEC PREUVE. Coût maîtrisé : UN appel
                # par catégorie/commune, et UNIQUEMENT pour les fiches RETENUES
                # (approved/edited) auxquelles il manque un champ du périmètre (les
                # 'suggested'/'rejected' ne sont jamais touchées). La complétion ne
                # remplit que les champs NULL (COALESCE) et ne change ni le `status`
                # ni le `source`. Best-effort par catégorie (SAVEPOINT) : un échec
                # n'annule ni le reste ni le job.
                today = _dt.date.today().isoformat()
                completed = 0
                svc_by_cat: dict[str, int] = {}   # compteur PAR catégorie (journal)
                svc_cost = 0.0
                svc_errors: dict[str, str] = {}
                for cat in claude_enrich.SERVICE_COMPLETE_CATEGORIES:
                    todo = db.pois_needing_completion(
                        conn, property_id, cat, claude_enrich.service_fields(cat),
                        settings.service_complete_max_age_days)
                    if not todo:
                        continue
                    try:
                        with conn.transaction():
                            label = db.category_label_fr(conn, cat)
                            done, meta = claude_enrich.complete_service_pois(
                                cat, label, todo, prop["city"],
                                prop["country_code"], ai, today=today)
                            filled: set[str] = set()
                            for p in todo:
                                res = done.get(p["id"])
                                if not res:
                                    continue
                                if db.apply_poi_completion(
                                        conn, p["id"], res["fields"],
                                        res["source_url"], res["verified_on"], today):
                                    filled.add(p["id"])
                                    completed += 1
                            # Fiches restées introuvables → marquées revérifiées
                            # (pas de re-appel avant l'échéance).
                            db.mark_pois_checked(
                                conn, [p["id"] for p in todo if p["id"] not in filled],
                                today)
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "service_complete", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            svc_by_cat[cat] = len(filled)
                            svc_cost += meta["cost_cts"]
                        _progress(f"  ✓ complétion {cat} : {len(filled)}/{len(todo)} "
                                  f"fiche(s) — {meta['cost_cts']:.2f} ct")
                    except Exception as sc_exc:  # noqa: BLE001 — best-effort
                        log.warning("Complétion services (%s / %s) non résolue : %s",
                                    cat, prop["city"], sc_exc)
                        c = _record_failed_call_cost(conn, property_id, job_id,
                                                     "service_complete", sc_exc)
                        summary["cost_cts"] += c
                        svc_cost += c
                        svc_errors[cat] = overpass._short(str(sc_exc))
                        _progress(f"  ⚠ complétion {cat} non résolue : "
                                  f"{overpass._short(str(sc_exc))}")
                summary["services_completed"] = completed
                # 4c au journal : compteurs par catégorie + coût + erreurs éventuelles.
                if svc_by_cat or svc_errors:
                    db.job_step(conn, job_id, "service_complete",
                                {"ok": not svc_errors, "by_category": svc_by_cat,
                                 "completed": completed, "cost_cts": round(svc_cost, 2),
                                 **({"errors": svc_errors} if svc_errors else {})})
                    conn.commit()

                # 4d. Baby-sitting (V2-07 volet 2) : CRÉATION de fiches par recherche
                # web (source='claude', status='suggested' → validation propriétaire).
                # Position = celle du logement (service TÉLÉPHONIQUE). Cadence propre
                # par logement, mémorisée via api_costs (un VIDE est un résultat
                # valide qu'on ne re-cherche pas à chaque run). Best-effort.
                if (prop["lat"] is not None
                        and not db.recent_operation(
                            conn, property_id, "babysitter",
                            settings.babysitter_max_age_days)):
                    try:
                        with conn.transaction():
                            sitters, meta = claude_enrich.fetch_babysitters(
                                prop["city"], prop["country_code"], ai, today=today)
                            created = 0
                            for s in sitters:
                                created += db.insert_service_poi(
                                    conn, property_id, "babysitter", s["name"],
                                    prop["lat"], prop["lon"], phone=s.get("phone"),
                                    website=s.get("website"),
                                    source_ref="claude:babysitter:" + _slug(s["name"]),
                                    completion_meta={"_created": {
                                        "source_url": s.get("source_url"),
                                        "verified_on": s.get("verified_on")}})
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "babysitter", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            summary["babysitters"] = created
                            db.job_step(conn, job_id, "babysitter",
                                        {"ok": True, "created": created,
                                         "cost_cts": round(meta["cost_cts"], 2)})
                        _progress(f"  ✓ baby-sitting : {created} créé(s) "
                                  f"— {meta['cost_cts']:.2f} ct")
                    except Exception as bs_exc:  # noqa: BLE001 — best-effort
                        log.warning("Baby-sitting (%s) non résolu : %s",
                                    prop["city"], bs_exc)
                        c = _record_failed_call_cost(conn, property_id, job_id,
                                                     "babysitter", bs_exc)
                        summary["cost_cts"] += c
                        db.job_step(conn, job_id, "babysitter",
                                    {"ok": False, "error": overpass._short(str(bs_exc)),
                                     "cost_cts": round(c, 2)})
                        conn.commit()
                        _progress(f"  ⚠ baby-sitting non résolu : "
                                  f"{overpass._short(str(bs_exc))}")

                # 4e. Marchés hebdomadaires (V2-07 volet 3) : DÉCOUVERTE mutualisée par
                # commune (cache area_facts, fenêtre propre) PUIS MATÉRIALISATION en POI
                # 'market' suggested par logement — idempotents (source_ref), dédoublonnés
                # contre l'existant (owner edited/rejected jamais touchés), position FIABLE
                # exigée (jamais un marqueur ville). Best-effort (SAVEPOINT).
                try:
                    with conn.transaction():
                        if not db.area_fact_fresh(
                                conn, prop["country_code"], prop["city"],
                                claude_enrich.MARKET_FACT_TYPE,
                                settings.market_max_age_days):
                            mk_fact, meta = claude_enrich.fetch_markets(
                                prop["city"], prop["country_code"], ai, today=today)
                            db.upsert_area_facts(conn, prop["country_code"],
                                                 prop["city"], mk_fact,
                                                 source=settings.anthropic_model)
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "markets", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            mk_cost = meta["cost_cts"]
                        else:
                            mk_cost = 0.0  # découverte mutualisée déjà fraîche
                        # Matérialisation depuis le fait (frais ou fraîchement écrit).
                        fact = db.get_area_fact(conn, prop["country_code"], prop["city"],
                                                claude_enrich.MARKET_FACT_TYPE) or {}
                        discovered = fact.get("markets") or []
                        existing = [dict(r) for r in
                                    db.existing_market_pois(conn, property_id)]
                        m_created = m_dup = m_nopos = 0
                        for mk in discovered:
                            ref = ("claude:market:" + _slug(mk["name"]) + ":"
                                   + str(mk["weekday"]))
                            if db.poi_source_ref_exists(conn, property_id, ref):
                                continue  # déjà matérialisé (idempotent, pas de géocodage)
                            # Pré-dédup par NOM (avant tout géocodage).
                            if claude_enrich.market_matches_existing(
                                    mk["name"], mk["weekday"], None, None, existing):
                                m_dup += 1
                                continue
                            lat, lon = _resolve_market_position(mk, prop, http_client)
                            if lat is None:
                                m_nopos += 1
                                log.warning("Marché « %s » sauté : position non fiable",
                                            mk["name"])
                                continue
                            # Dédup par POSITION (même jour + même place).
                            if claude_enrich.market_matches_existing(
                                    mk["name"], mk["weekday"], lat, lon, existing):
                                m_dup += 1
                                continue
                            poi = {"name": mk["name"], "lat": lat, "lon": lon,
                                   "weekday": mk["weekday"],
                                   "weekday_note": mk.get("weekday_note"),
                                   "address": mk.get("address"), "source_ref": ref,
                                   "completion_meta": {"_market": {
                                       "source_url": mk.get("source_url"),
                                       "verified_on": mk.get("verified_on"),
                                       "doubtful": mk.get("doubtful", False)}}}
                            distance.compute_distances(origin, [poi], client=http_client)
                            m_created += db.insert_market_poi(conn, property_id, poi)
                            existing.append({"name": mk["name"], "weekday": mk["weekday"],
                                             "lat": lat, "lon": lon})
                        summary["markets_created"] = m_created
                        db.job_step(conn, job_id, "markets",
                                    {"ok": True, "discovered": len(discovered),
                                     "created": m_created, "skipped_duplicate": m_dup,
                                     "skipped_position": m_nopos,
                                     "cost_cts": round(mk_cost, 2)})
                    _progress(
                        f"  ✓ marchés : {m_created} créé(s), {m_dup} doublon(s), "
                        f"{m_nopos} sans position — {mk_cost:.2f} ct")
                except Exception as mk_exc:  # noqa: BLE001 — best-effort
                    log.warning("Marchés (%s) non résolus : %s", prop["city"], mk_exc)
                    # Appel(s) web PAYÉ(S) malgré l'échec de parsing (volet 3bis) : le
                    # coût est comptabilisé (zéro écriture de données pour autant).
                    c = _record_failed_call_cost(conn, property_id, job_id,
                                                 "markets", mk_exc)
                    summary["cost_cts"] += c
                    db.job_step(conn, job_id, "markets",
                                {"ok": False, "error": overpass._short(str(mk_exc)),
                                 "cost_cts": round(c, 2)})
                    conn.commit()
                    _progress(f"  ⚠ marchés non résolus : "
                              f"{overpass._short(str(mk_exc))} "
                              f"({c:.2f} ct comptabilisé)")

                db.job_step(conn, job_id, "claude",
                            {"ok": True, "cost_cts": round(summary["cost_cts"], 2)})
            else:
                db.job_step(conn, job_id, "claude", {"ok": True, "skipped": True})

            # ── 5. Guide voyageur (V2-54) : arbitrage auto par le juge + publication ─
            # UNIQUEMENT pour une fiche guest (les fiches propriétaires gardent leurs
            # POI 'suggested' pour l'arbitrage humain — invariant 1 intact).
            if prop.get("guest_guide"):
                _judge_and_publish_guest(conn, prop, ai, job_id, summary, use_claude)

            db.job_finish(conn, job_id, "done")
            conn.commit()
            _progress(
                f"✔ job {job_id} terminé — {summary['pois']} POI suggérés, "
                f"{summary['duplicates_merged']} doublon(s) fusionné(s), "
                f"{summary['services_completed']} fiche(s) complétée(s), "
                f"{summary['babysitters']} baby-sitting créé(s), "
                f"{summary['markets_created']} marché(s) créé(s), "
                f"coût IA {summary['cost_cts']:.2f} ct")
        except Exception as exc:  # échec -> job 'failed', rien de corrompu
            conn.rollback()
            db.job_finish(conn, job_id, "failed", error=f"{type(exc).__name__}: {exc}")
            conn.commit()
            _progress(f"✖ job {job_id} en échec : {type(exc).__name__}: {exc}")
            raise
        finally:
            # OPS-4 Pièce 4 : fermeture EXPLICITE du client Anthropic créé ici (son
            # pool httpx laissé ouvert bloquait la sortie du process). Quel que soit
            # le dénouement (succès, échec, re-levée).
            if owns_ai and ai is not None:
                try:
                    ai.close()
                except Exception:  # noqa: BLE001 — la fermeture ne doit jamais lever
                    log.warning("Fermeture du client Anthropic ignorée")

    summary["job_id"] = job_id
    return summary


# ── Fiabilisation de la moisson : ré-essai différé des catégories manquantes ──
# (M-18). Après un job terminé « normalement » mais avec des catégories en échec
# (échecs Overpass transitoires : 406/timeout, surtout le palier aéroport 100 km),
# on rejoue UNIQUEMENT les catégories manquantes, jusqu'à `max_retries` fois, avec
# un délai entre tentatives. C'est le MÊME job logique (même job_id, quota
# inchangé) ; chaque passage est journalisé dans `enrichment_jobs.steps`
# (`retry_1`, `retry_2`…). Aucun POI arbitré n'est touché : l'upsert ne réécrit
# que les POI `status='suggested'` (invariant 1).

RETRY_DELAY_S = 180        # 3 minutes entre tentatives (constat prod)
MAX_RETRIES = 3


def run_with_retries(property_id: str, *, use_claude: bool = True,
                     trigger: str = "manual", job_id: str | None = None,
                     only_categories: set[str] | None = None,
                     http_client: httpx.Client | None = None,
                     anthropic_client: anthropic.Anthropic | None = None,
                     max_retries: int = MAX_RETRIES,
                     retry_delay_s: int = RETRY_DELAY_S,
                     sleep: Callable[[float], None] = time.sleep) -> dict:
    """Exécute le pipeline puis, si des catégories ont échoué, les rejoue en
    différé (mêmes réglages, même job). `sleep` est injectable pour les tests."""
    summary = run(property_id, use_claude=use_claude, trigger=trigger, job_id=job_id,
                  only_categories=only_categories, http_client=http_client,
                  anthropic_client=anthropic_client)
    job_id = summary["job_id"]
    failed = set((summary.get("failed_categories") or {}).keys())
    attempt = 0
    while failed and attempt < max_retries:
        attempt += 1
        sleep(retry_delay_s)
        failed = set(_retry_failed(
            property_id, job_id, failed, attempt, use_claude=use_claude,
            http_client=http_client, anthropic_client=anthropic_client).keys())
    summary["retries"] = attempt
    summary["failed_categories"] = {c: "encore en échec" for c in failed}
    return summary


def _retry_failed(property_id: str, job_id: str, categories: set[str], attempt: int,
                  *, use_claude: bool, http_client: httpx.Client | None,
                  anthropic_client: anthropic.Anthropic | None) -> dict[str, str]:
    """Rejoue la moisson des seules `categories` manquantes et journalise l'étape
    `retry_{attempt}`. N'altère PAS le statut du job (il reste 'done') ni les POI
    arbitrés. Retourne le dict des catégories encore en échec."""
    got = 0
    merged_dups = 0
    resolved: list[str] = []
    with db.connect() as conn:
        prop = db.load_property(conn, property_id)
        try:
            if prop["lat"] is None:  # jamais en pratique (geocode fait au 1er run)
                db.job_step(conn, job_id, f"retry_{attempt}",
                            {"ok": False, "error": "logement sans position"})
                conn.commit()
                return {c: "logement sans position" for c in categories}
            origin = (prop["lat"], prop["lon"])
            all_cats = db.load_categories(conn)
            wanted = [c for c in all_cats if c["code"] in categories
                      and c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES]
            grouped, failed, _harvest = overpass.fetch_grouped(
                wanted, origin[0], origin[1], client=http_client)

            editorial: list[dict] = []
            for cat in wanted:
                code = cat["code"]
                pois = grouped.get(code) or []
                if not pois:
                    continue
                try:
                    distance.compute_distances(origin, pois, client=http_client)
                except Exception as exc:
                    failed[code] = f"{type(exc).__name__}: {exc}"[:120]
                    continue
                for p in pois:
                    p["category"] = code
                # V2-44 volet 3 : plafond de pertinence (comme le run initial).
                pois, _capped = overpass.apply_drive_cap(
                    pois, cat["default_radius_m"],
                    overpass.target_for(code).hard_cap_drive_min)
                if not pois:
                    continue
                # Dédoublonnage à la suggestion (V2-40), comme le run initial.
                pois, in_batch = dedup.deduplicate(pois)
                existing = db.existing_pois_for_dedup(conn, property_id, code)
                pois, vs_existing = dedup.filter_against_existing(pois, existing)
                merged_dups += in_batch + vs_existing
                pois = _cap_by_travel(code, pois)   # V2-44 : aéroport → 3 plus proches
                if code in settings.describe_categories:
                    editorial.extend(pois)
                got += db.upsert_pois(conn, property_id, code, pois)
                resolved.append(code)
                conn.commit()

            # Descriptions IA pour les catégories éditoriales récupérées au retry.
            if use_claude and editorial:
                owns_ai = anthropic_client is None  # fermer si créé ici (Pièce 4)
                ai = anthropic_client or anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"])
                try:
                    descs, meta = claude_enrich.describe_pois(
                        editorial, prop["city"], prop["country_code"], ai)
                finally:
                    if owns_ai:
                        try:
                            ai.close()
                        except Exception:  # noqa: BLE001
                            log.warning("Fermeture du client Anthropic ignorée")
                for p in editorial:
                    if p["source_ref"] in descs:
                        p["description_md"] = descs[p["source_ref"]]
                for code in {p["category"] for p in editorial}:
                    db.upsert_pois(conn, property_id, code,
                                   [p for p in editorial if p["category"] == code])
                db.record_costs(conn, property_id, job_id, "anthropic",
                                "describe_pois", meta["attempts"])

            db.job_step(conn, job_id, f"retry_{attempt}",
                        {"ok": not failed, "pois": got,
                         "duplicates_merged": merged_dups,
                         "resolved": resolved, "failed": failed})
            conn.commit()
            return failed
        except Exception as exc:  # un retry ne doit jamais casser le job 'done'
            conn.rollback()
            db.job_step(conn, job_id, f"retry_{attempt}",
                        {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:120]})
            conn.commit()
            return {c: "erreur de retry" for c in categories}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline d'enrichissement CasaGuide")
    parser.add_argument("--property-id", required=True)
    parser.add_argument("--no-claude", action="store_true",
                        help="sauter l'étape IA (test des étapes géo)")
    parser.add_argument("--categories", default=None,
                        help="liste de catégories séparées par des virgules")
    parser.add_argument("--trigger", default="manual",
                        choices=["manual", "initial", "refresh"])
    args = parser.parse_args()

    cats = set(args.categories.split(",")) if args.categories else None
    result = run(args.property_id, use_claude=not args.no_claude,
                 trigger=args.trigger, only_categories=cats)
    # Résumé final ÉTENDU (OPS-4 Pièce 3) : POI moissonnés PAR catégorie, complétions
    # de service, créations baby-sitting, coût total, et échecs éventuels EN CLAIR.
    print(f"\n=== Job {result['job_id']} terminé ===", flush=True)
    print(f"  POI suggérés          : {result['pois']}")
    print(f"  Doublons fusionnés    : {result.get('duplicates_merged', 0)}")
    for cat, n in sorted(result["categories"].items()):
        print(f"    {cat:<18} {n}")
    print(f"  Fiches complétées     : {result.get('services_completed', 0)}")
    print(f"  Baby-sitting créés    : {result.get('babysitters', 0)}")
    print(f"  Marchés créés         : {result.get('markets_created', 0)}")
    print(f"  Loueurs (web) retenus : {result.get('rental_web_kept', 0)}")
    if result.get("editorial_found"):
        print(f"  Picks réputés (web)   : {result.get('editorial_found', 0)} trouvés, "
              f"{result.get('editorial_added', 0)} ajoutés")
    if result.get("overture_added"):
        print(f"  Overture ajoutés      : {result['overture_added']}")
    if result.get("overture_contacts"):
        print(f"  Contacts Overture     : {result['overture_contacts']}")
    if result.get("generic_dropped"):
        print(f"  Sans-nom écartés      : {result['generic_dropped']}")
    if result.get("network_dropped"):
        print(f"  Stations réseau réduites : {result['network_dropped']}")
    if result.get("hard_cap_dropped"):
        print(f"  Hors plafond de route : {result['hard_cap_dropped']}")
    print(f"  Coût IA               : {result['cost_cts']:.2f} ct")
    failed = result.get("failed_categories") or {}
    if failed:
        print(f"  Catégories en échec   : {len(failed)}")
        for cat, msg in sorted(failed.items()):
            print(f"    {cat:<18} {msg}")
    else:
        print("  Catégories en échec   : 0")
    # V2-44 : catégories sans AUCUN résultat (rien à signaler comme erreur, mais bon
    # à savoir — plage, laverie… absentes du rayon).
    empty = result.get("empty_categories") or []
    if empty:
        print(f"  Catégories sans résultat : " + ", ".join(sorted(empty)))
    sys.stdout.flush()


if __name__ == "__main__":
    # Pièce 4 : la sortie doit être NETTE. Les clients réseau (Anthropic, httpx) sont
    # fermés dans `run()` ; ce garde-fou force la fin du process après le résumé même
    # si un finaliseur tiers s'attardait (aucun thread non-daemon ne doit survivre).
    main()
    sys.exit(0)
