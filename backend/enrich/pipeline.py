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

from . import claude_enrich, db, distance, geocode, overpass
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


def _record_web_failure_cost(conn, property_id: str, job_id: str, operation: str,
                             exc: Exception) -> float:
    """Comptabilise le coût des essais d'un appel web_search qui a ÉCHOUÉ au parsing
    (V2-07 volet 3bis) : l'argent est dépensé à la réponse, pas au succès. À appeler
    dans le `except` best-effort, APRÈS le rollback du SAVEPOINT (donc sur la
    transaction principale : `conn.commit()` par l'appelant). Renvoie le coût total
    comptabilisé (0 si l'exception ne porte pas de coût — panne réseau, etc.)."""
    attempts = getattr(exc, "attempts", None) or []
    db.record_costs(conn, property_id, job_id, "anthropic", operation, attempts)
    return round(sum(c["cost_cts"] for c in attempts), 4)


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


def run(property_id: str, *, use_claude: bool = True, trigger: str = "manual",
        only_categories: set[str] | None = None,
        job_id: str | None = None,
        http_client: httpx.Client | None = None,
        anthropic_client: anthropic.Anthropic | None = None) -> dict:
    """Exécute le pipeline pour un logement. Retourne un résumé.

    Si `job_id` est fourni (job 'pending' pré-créé par l'API pour renvoyer un
    identifiant immédiat), il est réutilisé ; sinon un nouveau job est créé.
    """
    summary: dict = {"pois": 0, "categories": {}, "area_facts": False,
                     "cost_cts": 0.0, "services_completed": 0, "babysitters": 0,
                     "markets_created": 0}
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
                db.job_step(conn, job_id, "geocode",
                            {"ok": True, "accuracy": geo["accuracy"]})
                _progress(f"  ✓ géocodage : {geo['accuracy']} "
                          f"({geo['lat']:.4f}, {geo['lon']:.4f})")
            else:
                db.job_step(conn, job_id, "geocode", {"ok": True, "skipped": True})
                _progress("  ✓ géocodage : déjà positionné")
            conn.commit()  # progression visible en temps réel
            origin = (prop["lat"], prop["lon"])

            # ── 2 + 3. POI Overpass puis distances ─────────────────────────
            # Overpass : une requête par palier de rayon (union de sélecteurs),
            # résultats re-ventilés par catégorie via leurs tags (perf, M-01).
            categories = db.load_categories(conn)
            wanted = [c for c in categories
                      if (not only_categories or c["code"] in only_categories)
                      and c["code"] not in overpass.CLAUDE_ONLY_CATEGORIES]
            grouped, failed_categories = overpass.fetch_grouped(
                wanted, origin[0], origin[1], client=http_client)

            all_editorial: list[dict] = []
            for cat in wanted:
                code = cat["code"]
                pois = grouped.get(code) or []
                if not pois:
                    continue
                try:
                    distance.compute_distances(origin, pois, client=http_client)
                except Exception as exc:
                    # Un échec de distances ne doit pas faire perdre la catégorie :
                    # on la trace et on continue (ré-enrichissable plus tard).
                    failed_categories[code] = f"{type(exc).__name__}: {exc}"[:120]
                    continue
                for p in pois:
                    p["category"] = code
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
            db.job_step(conn, job_id, "overpass",
                        {"ok": not failed_categories or summary["pois"] > 0,
                         "pois": summary["pois"],
                         "failed": failed_categories})
            db.job_step(conn, job_id, "distances", {"ok": True})
            conn.commit()
            _progress(f"  ✓ Overpass : {summary['pois']} POI"
                      + (f" — {len(failed_categories)} catégorie(s) en échec : "
                         + ", ".join(sorted(failed_categories))
                         if failed_categories else " — 0 échec"))

            # ── 4. Enrichissement Claude ────────────────────────────────────
            if use_claude:
                ai = anthropic_client or anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"])
                owns_ai = anthropic_client is None

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
                        c = _record_web_failure_cost(conn, property_id, job_id,
                                                     "food_delivery", fd_exc)
                        summary["cost_cts"] += c
                        db.job_step(conn, job_id, "food_delivery",
                                    {"ok": False, "error": overpass._short(str(fd_exc)),
                                     "cost_cts": round(c, 2)})
                        conn.commit()
                        _progress(f"  ⚠ livraison de repas non résolue : "
                                  f"{overpass._short(str(fd_exc))}")

                # 4b. Descriptions courtes des POI éditoriaux
                if all_editorial:
                    descs, meta = claude_enrich.describe_pois(
                        all_editorial, prop["city"], prop["country_code"], ai)
                    for p in all_editorial:
                        if p["source_ref"] in descs:
                            p["description_md"] = descs[p["source_ref"]]
                    # ré-upsert : seule la description change
                    for code in {p["category"] for p in all_editorial}:
                        db.upsert_pois(conn, property_id, code,
                                       [p for p in all_editorial
                                        if p["category"] == code])
                    db.record_cost(conn, property_id, job_id, "anthropic",
                                   "describe_pois", meta["units"], meta["cost_cts"])
                    summary["cost_cts"] += meta["cost_cts"]
                    db.job_step(conn, job_id, "describe_pois",
                                {"ok": True, "described": len(descs),
                                 "cost_cts": round(meta["cost_cts"], 2)})
                    conn.commit()
                    _progress(f"  ✓ descriptions : {len(descs)} POI — "
                              f"{meta['cost_cts']:.2f} ct")

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
                        c = _record_web_failure_cost(conn, property_id, job_id,
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
                        c = _record_web_failure_cost(conn, property_id, job_id,
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
                    c = _record_web_failure_cost(conn, property_id, job_id,
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

            db.job_finish(conn, job_id, "done")
            conn.commit()
            _progress(
                f"✔ job {job_id} terminé — {summary['pois']} POI suggérés, "
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
            grouped, failed = overpass.fetch_grouped(
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
                db.record_cost(conn, property_id, job_id, "anthropic",
                               "describe_pois", meta["units"], meta["cost_cts"])

            db.job_step(conn, job_id, f"retry_{attempt}",
                        {"ok": not failed, "pois": got,
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
    for cat, n in sorted(result["categories"].items()):
        print(f"    {cat:<18} {n}")
    print(f"  Fiches complétées     : {result.get('services_completed', 0)}")
    print(f"  Baby-sitting créés    : {result.get('babysitters', 0)}")
    print(f"  Marchés créés         : {result.get('markets_created', 0)}")
    print(f"  Coût IA               : {result['cost_cts']:.2f} ct")
    failed = result.get("failed_categories") or {}
    if failed:
        print(f"  Catégories en échec   : {len(failed)}")
        for cat, msg in sorted(failed.items()):
            print(f"    {cat:<18} {msg}")
    else:
        print("  Catégories en échec   : 0")
    sys.stdout.flush()


if __name__ == "__main__":
    # Pièce 4 : la sortie doit être NETTE. Les clients réseau (Anthropic, httpx) sont
    # fermés dans `run()` ; ce garde-fou force la fin du process après le résumé même
    # si un finaliseur tiers s'attardait (aucun thread non-daemon ne doit survivre).
    main()
    sys.exit(0)
