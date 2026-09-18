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


# Appariement par NOM des picks éditoriaux (V2-56b) : sans condition de distance
# (la position du pick n'est PAS fiable — c'est celle de la base qui fait foi), donc
# un seuil de nom PLUS EXIGEANT que le same_place géo (0,55) pour éviter un faux
# appariement ailleurs dans la commune.
_EDITORIAL_NAME_THR = 0.72
# Garde-fou anti-position aberrante d'un géocodage de rue (comme les marchés).
_EDITORIAL_MAX_DIST_M = claude_enrich.MARKET_MAX_DIST_M   # 25 km


def _discover_editorial_sorties(conn, prop: dict, ai, job_id: str,
                                summary: dict) -> dict[str, list[dict]]:
    """Sélection éditoriale « sorties » (V2-56) : DÉCOUVERTE web des adresses RÉPUTÉES
    (restaurant/bar/cafe), UNE seule fois par run (couvre les trois catégories),
    mémorisée sur `summary`. Renvoie {code: [pick BRUT]} — nom/catégorie/adresse/raison/
    contacts/preuve, SANS position (le positionnement se fait au moment de la fusion
    par catégorie : appariement de NOM contre OSM/Overture, puis géocodage de rue
    strict — V2-56b). Best-effort : tout échec web journalisé, coût comptabilisé,
    renvoie {}. Cadence propre par logement."""
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
            prop["city"], prop["country_code"], ai, today=today,
            lang=prop.get("default_lang") or "fr")
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
    for pl in places:
        out.setdefault(pl["category"], []).append(pl)   # pick BRUT (positionné plus tard)
    summary["editorial_found"] = len(places)
    db.job_step(conn, job_id, "reputed_sorties",
                {"ok": True, "discovered": len(places),
                 "by_category": {k: len(v) for k, v in out.items()},
                 "cost_cts": round(meta["cost_cts"], 2)})
    conn.commit()
    _progress(f"  ✓ sélection éditoriale : {len(places)} adresse(s) réputée(s) "
              f"trouvée(s) — {meta['cost_cts']:.2f} ct")
    return out


def _name_match(name: str, candidates: list[dict]) -> dict | None:
    """Meilleur candidat par SIMILARITÉ DE NOM (Dice trigrammes ≥ seuil), SANS condition
    de distance (V2-56b : la position du pick n'est pas fiable). None si rien d'assez
    proche."""
    best, best_s = None, 0.0
    for c in candidates:
        s = fusion.name_similarity(name, c.get("name"))
        if s >= _EDITORIAL_NAME_THR and s > best_s:
            best, best_s = c, s
    return best


def _fill_editorial_contacts(target: dict, src: dict) -> None:
    """Comble les contacts NULL de `target` depuis `src` (jamais d'écrasement)."""
    for f in ("phone", "website"):
        if not target.get(f) and src.get(f):
            target[f] = src[f]


def _mark_editorial(poi: dict, pk: dict, origin_tag: str) -> None:
    """Marque un POI comme pick éditorial (badge « réputé » + raison), sans écraser un
    coup de cœur déjà présent. `pk` = pick BRUT (porte `reason`/`source_url`)."""
    if not poi.get("owner_comment") and pk.get("reason"):
        poi["owner_comment"] = pk["reason"]
    meta = dict(poi.get("completion_meta") or {})
    meta["_editorial"] = {"source_url": pk.get("source_url"),
                          "verified_on": pk.get("verified_on"), "origin": origin_tag}
    # V2-66b cas (a) : si la fiche OSM appariée n'a pas de nom local (OSM sans name:<lang>)
    # mais que la mémoire de secteur en connaît un, le compléter (jamais l'écraser).
    if not meta.get("_name_local"):
        meta.update(overpass.local_meta_from(poi.get("name") or "", pk.get("name_local")))
    # V2-77 : la chicha relevée par la passe éditoriale complète une fiche OSM muette —
    # jamais un sous-type déjà tagué (OSM fait foi quand il parle, esprit V2-71).
    if pk.get("subtype") and not poi.get("subtype"):
        poi["subtype"] = pk["subtype"]
    poi["completion_meta"] = meta


def _build_editorial_poi(pk: dict, code: str, lat: float, lon: float,
                         locality, origin: tuple, origin_tag: str) -> dict:
    """Construit un POI éditorial à une position FIABLE (base ou géocodage de rue).
    V2-66b cas (a) : reporte le nom LOCAL (`pk['name_local']`, écriture d'origine) en
    `completion_meta._name_local` — le nom affiché (web) est latin, l'original passe donc
    en 2e ligne copiable/prononçable (V2-66 cas A)."""
    meta = {"_editorial": {"source_url": pk.get("source_url"),
                           "verified_on": pk.get("verified_on"), "origin": origin_tag}}
    meta.update(overpass.local_meta_from(pk["name"], pk.get("name_local")))
    return {
        "name": pk["name"], "lat": lat, "lon": lon,
        "address": pk.get("address"), "locality": locality,
        "category": code, "source": "web",
        "phone": pk.get("phone"), "website": pk.get("website"),
        "opening_hours": None, "cuisine": None, "description_md": None,
        # V2-77 : la caractéristique « chicha » relevée par la passe éditoriale traverse
        # jusqu'à `pois.subtype` → puce dans le guide, en 7 langues.
        "subtype": pk.get("subtype"),
        "owner_comment": pk.get("reason") or None,
        "source_ref": "web:reputed:" + _slug(pk["name"]),
        "crow_m": overpass.haversine_m(origin[0], origin[1], lat, lon),
        "completion_meta": meta,
    }


def _geocode_pick_strict(pk: dict, prop: dict, origin: tuple,
                         http_client: httpx.Client | None) -> dict | None:
    """Géocodage de rue STRICT d'un pick (V2-56b) : jamais le centroïde communal
    (refuse `accuracy='city'`, règle des marchés) ni une position aberrante. None si
    la rue ne se résout pas proprement → le pick TOMBE (jamais sept punaises empilées)."""
    addr = (pk.get("address") or "").strip()
    if not addr:
        return None
    try:
        geo = geocode.geocode(street=addr, city=prop["city"],
                              country_code=prop["country_code"], client=http_client)
    except geocode.GeocodeError:
        return None
    if geo.get("accuracy") == "city":
        return None
    if overpass.haversine_m(origin[0], origin[1], geo["lat"], geo["lon"]) \
            > _EDITORIAL_MAX_DIST_M:
        return None
    return geo


def _position_pick(pk: dict, pois: list[dict], ovt: list[dict] | None,
                   prop: dict, origin: tuple,
                   http_client: httpx.Client | None) -> tuple | None:
    """Position FIABLE d'un pick, cascade STRICTE (V2-56b) : appariement par NOM contre
    l'OSM moissonné PUIS Overture (sans distance — la position du pick n'est pas fiable,
    celle de la base fait foi), sinon géocodage de rue STRICT (jamais le centroïde).
    Renvoie `(lat, lon, locality, phone_base, website_base, name_local)` ou None (le pick
    tombe). `name_local` (V2-66b cas a) = nom en écriture d'origine de la fiche OSM appariée
    (elle porte `completion_meta._name_local`) ; None hors appariement OSM."""
    m = _name_match(pk["name"], pois)                     # 1. OSM moissonné
    if m is not None:
        local = (m.get("completion_meta") or {}).get("_name_local")
        return m["lat"], m["lon"], m.get("locality"), m.get("phone"), m.get("website"), local
    ov = _name_match(pk["name"], ovt) if ovt else None    # 2. Overture (pas de nom local)
    if ov is not None and ov.get("lat") is not None and ov.get("lon") is not None:
        return (ov["lat"], ov["lon"], ov.get("locality"),
                ov.get("phone"), ov.get("website"), None)
    geo = _geocode_pick_strict(pk, prop, origin, http_client)   # 3. rue stricte
    if geo is not None:
        return geo["lat"], geo["lon"], geo.get("locality"), None, None, None
    return None                                            # 4. tombe


# Placement STRICT des activités du secteur (V2-73). Une activité nomme son LIEU
# (« plage du Gurp, Grayan-et-l'Hôpital ») mais ces spots (plage, massif, spot de
# surf) sont précisément ceux ABSENTS de la moisson commerciale — d'où la passe web.
# Cascade V2-56b : (1) appariement de NOM contre l'union moissonnée OSM/Overture,
# (2) géocodage du lieu-dit STRICT, (3) abandon — JAMAIS un centroïde communal (sept
# punaises empilées au centre-ville). Le garde anti-centroïde ne peut PAS se fier au
# seul `accuracy` (une plage retombe sur « city » alors que sa position est précise) :
# on rejette d'après la CLASSE/TYPE OSM administratifs (boundary, place=city|town…).
_ACT_ADMIN_CLASSES = {"boundary"}
_ACT_ADMIN_PLACE_TYPES = {
    "city", "town", "village", "municipality", "hamlet", "county", "state",
    "region", "province", "district", "suburb", "quarter", "borough",
    "administrative", "locality", "isolated_dwelling",
}


def _geo_is_centroid(geo: dict) -> bool:
    """Vrai si un résultat de géocodage est un CENTROÏDE administratif (à rejeter pour
    une activité) : commune incohérente (`mismatch`) ou classe/type OSM administratifs.
    Un LIEU précis (plage/massif/leisure/tourism…) est accepté même si `accuracy`
    retombe sur « city » (type non cartographié dans `_ACCURACY`)."""
    if geo.get("accuracy") == "mismatch":
        return True
    cls = (geo.get("osm_class") or "").lower()
    typ = (geo.get("osm_type") or "").lower()
    return cls in _ACT_ADMIN_CLASSES or (cls == "place" and typ in _ACT_ADMIN_PLACE_TYPES)


def _activity_place(a: dict) -> tuple[str, str]:
    """Le LIEU à géocoder d'une activité (V2-73c), `(place_name, place_city)`.

    La cascade géocode ces champs STRUCTURÉS, JAMAIS la phrase `where` entière (« Plage du
    Gurp, Grayan-et-l'Hôpital (env. 10 km de Bégadan), côte atlantique » que Nominatim ne
    résout pas — cause du placed:0). Priorité aux champs fournis par la collecte ; à défaut
    (faits d'avant V2-73c), RATTRAPAGE par heuristique simple depuis `where` : 1re partie
    avant virgule = lieu, 2e = commune (parenthèse/commentaire retirés). Aucun appel LLM.
    `place_name` vide → activité DIFFUSE (aucun point)."""
    name = (a.get("place_name") or "").strip()
    city = (a.get("place_city") or "").strip()
    if name:
        return name, city
    where = (a.get("where") or "").strip()
    if not where:
        return "", ""
    parts = [p.strip() for p in where.split(",") if p.strip()]
    if not parts:
        return "", ""
    name = re.sub(r"\(.*", "", parts[0]).strip()          # « Lieu (env. 10 km) » → « Lieu »
    city = re.sub(r"\(.*", "", parts[1]).strip() if len(parts) > 1 else ""
    return name, city


def _geocode_activity_query(query: str, prop: dict, origin: tuple,
                            http_client: httpx.Client | None) -> tuple | None:
    """Géocode une requête (adresse OU « lieu, commune ») et applique le garde STRICT :
    jamais un centroïde, jamais une position aberrante (> 25 km). `(lat, lon)` ou None."""
    query = (query or "").strip()
    if not query:
        return None
    try:
        geo = geocode.geocode(address=query, country_code=prop["country_code"],
                              client=http_client)
    except geocode.GeocodeError:
        return None
    if _geo_is_centroid(geo):                              # jamais un centroïde
        return None
    if overpass.haversine_m(origin[0], origin[1], geo["lat"], geo["lon"]) \
            > _EDITORIAL_MAX_DIST_M:
        return None                                        # position aberrante
    return geo["lat"], geo["lon"]


# Appariement SOUPLE des lieux naturels (V2-73g) : « Plage du Gurp » doit s'accrocher au POI
# moissonné « Le Gurp · Plage » (position OSM vérifiée à 1,5 km de l'adresse géocodée). On
# compare le CŒUR du nom, mots de catégorie (plage/beach/lac/mont/port…) et articles retirés,
# à un seuil abaissé. Garde-fou : un cœur vide (nom = mot de catégorie seul) n'apparie rien ;
# à égalité, le candidat le plus proche du NOM COMPLET gagne (jamais deux plages distinctes).
_ACT_SOFT_NAME_THR = 0.6
_PLACE_WORDS = {
    "plage", "playa", "spiaggia", "strand", "praia", "plazh", "beach",
    "lac", "lago", "lake", "meer", "see", "liqen", "etang", "estany", "lagune", "laguna",
    "marais", "marsh", "riviere", "river", "rio",
    "mont", "montagne", "monte", "mountain", "berg", "pic", "peak", "pico", "cima", "massif",
    "sierra", "serra", "puig", "mal", "maja",
    "port", "porto", "puerto", "harbour", "harbor", "haven",
    "cap", "cape", "cabo", "capo", "pointe", "point", "punta",
    "ile", "island", "isla", "isola", "insel", "ilot", "islet",
    "reserve", "reserva", "riserva", "parc", "park", "parque", "parco",
    "foret", "forest", "bois", "wood", "selva",
    "dune", "dunes", "calanque", "crique", "cala", "baie", "bay", "bahia", "baia",
    "gorges", "gorge", "cascade", "waterfall", "grotte", "cave", "cueva",
    "sentier", "sentiers", "trail", "spot", "site", "zone", "secteur",
}
_PLACE_ARTICLES = {"le", "la", "les", "l", "du", "de", "des", "d", "un", "une", "au", "aux",
                   "el", "los", "las", "il", "lo", "gli", "der", "die", "das", "den", "het",
                   "the", "of"}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def _place_core(name: str | None) -> str:
    """Cœur du nom d'un lieu (V2-73g) : minuscules, sans accents, mots de catégorie et
    articles retirés. « Plage du Gurp » → « gurp », « Le Gurp · Plage » → « gurp ». Vide
    si le nom ne contient qu'un mot de catégorie (pas d'accroche possible)."""
    toks = re.split(r"[^0-9a-zà-ÿ]+", (name or "").lower())
    core = [ta for t in toks if t
            for ta in [_strip_accents(t)]
            if ta and ta not in _PLACE_WORDS and ta not in _PLACE_ARTICLES]
    return " ".join(core)


def _activity_name_match(place: str, candidates: list[dict] | None) -> dict | None:
    """Meilleur candidat pour une activité (V2-73g) : d'abord l'appariement STRICT existant
    (nom brut ≥ 0,72), sinon l'appariement SOUPLE sur le cœur du nom (≥ `_ACT_SOFT_NAME_THR`,
    naturels). Renvoie le candidat (dict avec lat/lon) ou None. À égalité de cœur, le plus
    proche du nom complet l'emporte (« si deux plages matchent, la plus proche du nom »)."""
    candidates = candidates or []
    m = _name_match(place, candidates)                     # strict (comportement préservé)
    if m is not None:
        return m
    core = _place_core(place)
    if not core:
        return None                                        # nom = catégorie seule → pas d'accroche
    best, best_raw = None, -1.0
    for c in candidates:
        cc = _place_core(c.get("name"))
        if not cc or fusion.name_similarity(core, cc) < _ACT_SOFT_NAME_THR:
            continue
        raw = fusion.name_similarity(place, c.get("name"))
        if raw > best_raw:
            best, best_raw = c, raw
    return best


def _position_activity(a: dict, harvested: list[dict], prop: dict, origin: tuple,
                       http_client: httpx.Client | None,
                       osm_places: list[dict] | None = None) -> tuple | None:
    """Position FIABLE d'une activité, cascade STRICTE (V2-73/c/d/g). Géocode le LIEU
    structuré (jamais la phrase `where`). Ordre : (1) appariement de nom contre la MOISSON
    (POI déjà collectés, position OSM VÉRIFIÉE — avant le géocodage, V2-73g), (2) géocodage
    de l'ADRESSE POSTALE `place_address`, (3) géocodage « nom, commune », (4) REPLI OSM PAR
    TAG, (5) abandon. Renvoie `(lat, lon, exact, poi)` — `exact=True` seulement pour un POI
    moissonné apparié (position exacte → pas de cercle d'approximation, V2-73f/g), `poi` = la
    fiche appariée (pour le lien « voir dans le guide »). None → sans marqueur."""
    place, place_city = _activity_place(a)
    if not place:
        return None                                        # activité diffuse → pas de point
    m = _activity_name_match(place, harvested)             # 1. moisson (position VÉRIFIÉE)
    if m is not None and m.get("lat") is not None and m.get("lon") is not None:
        return m["lat"], m["lon"], True, m                 # exact → épingle seule, lien fiche
    addr = (a.get("place_address") or "").strip()
    if addr:                                               # 2. adresse postale de la source
        pos = _geocode_activity_query(addr, prop, origin, http_client)
        if pos is not None:
            return pos[0], pos[1], False, None             # approché → cercle
    pos = _geocode_activity_query(                         # 3. « lieu, commune »
        f"{place}, {place_city or prop['city']}", prop, origin, http_client)
    if pos is not None:
        return pos[0], pos[1], False, None
    om = _activity_name_match(place, osm_places)           # 4. repli OSM par tag
    if om is not None and om.get("lat") is not None and om.get("lon") is not None:
        return om["lat"], om["lon"], False, None           # approché → cercle
    return None                                            # 5. sinon : abandon


# Deux activités résolues à moins de ce rayon = adresse EMPRUNTÉE (V2-73e) : une diffuse
# (sentier balisé, route vicinale, marais Natura 2000) a hérité de l'adresse d'un lieu
# nommé. On ne garde qu'un marqueur au point — même défaut que les punaises empilées (V2-56b).
_ACT_DUP_DIST_M = 50


def _dedup_activity_positions(activities: list[dict]) -> None:
    """Anti-punaises empilées (V2-73e) : quand plusieurs activités résolvent au MÊME point
    (< 50 m), une seule garde le marqueur — celle dont le lieu est NOMMÉ (`place_name`
    explicite de la collecte, pas un nom dérivé de la phrase `where`) ; les autres retournent
    en liste SANS position. Déterministe (nommées d'abord, puis ordre d'origine) → idempotent
    (un re-passage garde le même gagnant, ne réanime jamais une évincée)."""
    placed = [a for a in activities if isinstance(a, dict)
              and a.get("lat") is not None and a.get("lon") is not None]
    order = sorted(range(len(placed)),
                   key=lambda i: (0 if (placed[i].get("place_name") or "").strip() else 1, i))
    kept: list[dict] = []
    for i in order:
        a = placed[i]
        if any(overpass.haversine_m(a["lat"], a["lon"], k["lat"], k["lon"])
               < _ACT_DUP_DIST_M for k in kept):
            a.pop("lat", None)
            a.pop("lon", None)                 # adresse empruntée → sans marqueur
        else:
            kept.append(a)


def _place_activities(activities: list[dict], harvested: list[dict], prop: dict,
                      origin: tuple, http_client: httpx.Client | None) -> int:
    """Pose `lat`/`lon` sur chaque activité plaçable (V2-73), en place. Renvoie le
    nombre placé. Idempotent : une activité déjà positionnée n'est pas re-géocodée.

    Le REPLI OSM par tag (V2-73d) est UN SEUL appel Overpass par secteur, mutualisé entre
    toutes les activités du lot (mission §3), et seulement s'il reste au moins un lieu nommé
    à placer — jamais quand tout est déjà positionné/diffus. Une passe anti-empilement
    (V2-73e) retire ensuite les marqueurs des activités qui ont emprunté l'adresse d'une autre."""
    to_place = [a for a in activities if isinstance(a, dict)
                and a.get("lat") is None and _activity_place(a)[0]]
    osm_places: list[dict] | None = None
    if to_place:
        osm_places = overpass.fetch_natural_places(origin[0], origin[1], http_client,
                                                   radius_m=int(_EDITORIAL_MAX_DIST_M))
    for a in activities:
        if not isinstance(a, dict):
            continue
        if a.get("lat") is not None and a.get("lon") is not None:
            continue
        res = _position_activity(a, harvested, prop, origin, http_client, osm_places)
        if res is not None:
            a["lat"], a["lon"], a["exact"] = res[0], res[1], bool(res[2])
            poi = res[3]
            # V2-73g : accroche à un POI moissonné → lien « voir dans le guide » (cohérence
            # interne). Le POI vit dans sa catégorie (ancre `#autour/{code}`).
            if poi and poi.get("category"):
                a["poi_cat"] = poi["category"]
    _dedup_activity_positions(activities)      # V2-73e : jamais deux marqueurs au même point
    return sum(1 for a in activities if isinstance(a, dict)
               and a.get("lat") is not None and a.get("lon") is not None)


def _backfill_activity_positions(conn, prop: dict, harvested: list[dict], origin: tuple,
                                 http_client: httpx.Client | None, job_id: str,
                                 summary: dict) -> None:
    """MISE À NIVEAU d'un fait 'activities' MÉMORISÉ d'avant V2-73 (V2-73b) : le fait
    existe (mutualisé par commune, l'étape web est sautée) mais ses entrées n'ont AUCUNE
    position → sans rattrapage, `map_data.activities` reste vide et aucune épingle
    n'apparaît (le cache masque le correctif). On exécute la SEULE passe de placement
    (cascade stricte V2-56b — aucun appel web/LLM, quasi gratuit) et on met à jour le
    fait ; les guides suivants du secteur en profitent immédiatement.

    Ne s'exécute QUE si le fait n'est pas déjà au schéma courant (`v` absent = jamais
    tenté). Un fait déjà versionné n'est jamais re-tenté → pas de re-géocodage indéfini
    des activités diffuses (les 16 circuits, une route vicinale)."""
    fact = db.get_area_fact(conn, prop["country_code"], prop["city"],
                            claude_enrich.ACTIVITIES_FACT_TYPE)
    if not fact or fact.get("v") == claude_enrich.ACTIVITIES_SCHEMA_V:
        return  # absent, ou positions déjà tentées (schéma courant) → rien à faire
    acts = fact.get("activities") or []
    # V2-73g : à la montée de schéma, ré-évaluer les positions APPROXIMATIVES (issues d'un
    # géocodage, pas d'un POI apparié) — le matching s'est amélioré (accroche à la moisson,
    # 1,5 km plus juste). Une position EXACTE (POI apparié) est conservée telle quelle.
    for a in acts:
        if isinstance(a, dict) and a.get("lat") is not None and not a.get("exact"):
            a.pop("lat", None)
            a.pop("lon", None)
    try:
        with conn.transaction():
            n_placed = _place_activities(acts, harvested, prop, origin, http_client)
            fact["v"] = claude_enrich.ACTIVITIES_SCHEMA_V
            db.upsert_area_facts(conn, prop["country_code"], prop["city"],
                                 {claude_enrich.ACTIVITIES_FACT_TYPE: fact},
                                 source=settings.anthropic_model)
            db.job_step(conn, job_id, "activities",
                        {"ok": True, "backfilled": True,
                         "activities": len(acts), "placed": n_placed})
        _progress(f"  ✓ activités : positions rattrapées "
                  f"({n_placed}/{len(acts)} sur la carte)")
    except Exception as exc:  # noqa: BLE001 — best-effort (le SAVEPOINT annule le rattrapage)
        log.warning("Rattrapage positions activités (%s) : %s", prop["city"], exc)
        db.job_step(conn, job_id, "activities",
                    {"ok": False, "backfill_error": overpass._short(str(exc))})
        conn.commit()


# Garde de cohérence du géocodage d'un commerce découvert (V2-74b, esprit V2-46) : au-delà de
# ce rayon du CENTRE DE LA COMMUNE annoncée, le géocodage a raté (numéro rural absent d'OSM →
# Nominatim retombe loin, cas « 59 min à pied pour la pharmacie du village »). Repli : le
# centre de la commune, marqué approximatif — « au village » vaut mieux qu'un point faux.
_LOCAL_COMMUNE_MAX_M = 2000
_LEADING_NUMBER_RE = re.compile(r"^\s*\d+\s*(bis|ter|quater)?\s*[,]?\s*", re.IGNORECASE)


def _commune_center(prop: dict, http_client: httpx.Client | None) -> tuple:
    """Centre de la commune du logement (V2-74b) : géocodage du NOM de commune (centroïde),
    repli sur la position du logement (il est dans la commune). `(lat, lon)` ou (None, None)."""
    try:
        geo = geocode.geocode(city=prop["city"], country_code=prop["country_code"],
                              client=http_client)
        return geo["lat"], geo["lon"]
    except geocode.GeocodeError:
        return prop.get("lat"), prop.get("lon")


def _geocode_local_commerce(commerce: dict, prop: dict, commune_center: tuple,
                            http_client: httpx.Client | None) -> tuple | None:
    """Position d'un commerce de village (V2-74b), avec GARDE DE COHÉRENCE + ESCALADE.
    (1) géocode l'adresse (avec numéro), (2) escalade sur la RUE SEULE sans numéro (les
    numéros ruraux manquent souvent d'OSM) — accepte une position PRÉCISE (rue/toit) à moins
    de 2 km du centre de la commune ; (3) sinon REPLI sur le centre de la commune, marqué
    APPROXIMATIF. Renvoie `(lat, lon, locality, approx)` ou None (commune introuvable)."""
    addr = (commerce.get("place_address") or "").strip()
    cc, city = prop["country_code"], prop["city"]
    candidates = [addr] if addr else []
    no_num = _LEADING_NUMBER_RE.sub("", addr).strip()
    if no_num and no_num != addr:
        candidates.append(no_num)                          # escalade : rue sans numéro (part 2)
    for query in candidates:
        try:
            geo = geocode.geocode(street=query, city=city, country_code=cc, client=http_client)
        except geocode.GeocodeError:
            continue
        # Précis (rue/toit, jamais un centroïde) ET proche du centre → position retenue.
        if (geocode.is_precise_enough(geo.get("accuracy"))
                and commune_center[0] is not None
                and overpass.haversine_m(commune_center[0], commune_center[1],
                                         geo["lat"], geo["lon"]) <= _LOCAL_COMMUNE_MAX_M):
            return geo["lat"], geo["lon"], geo.get("locality"), False
    # Repli (part 1) : le centre de la commune, position APPROXIMATIVE (jamais « 59 min »).
    if commune_center[0] is not None:
        return commune_center[0], commune_center[1], city, True
    return None


def _void_essentials(all_harvested: list[dict], wanted_codes: set,
                     origin: tuple, proximity_m: int,
                     country_code: str | None = None) -> list[str]:
    """Catégories ESSENTIELLES (V2-74) DEMANDÉES pour ce logement mais SANS aucun POI dans le
    rayon de proximité — le vide rural qui déclenche la découverte web des commerces de
    village. On ne considère QUE les catégories `wanted` (jamais inventer un besoin non prévu ;
    et une catégorie couverte, même par un seul commerce proche, n'est pas « vide »).

    V2-77 : l'éligibilité dépend aussi du PAYS (`local_commerce_categories_for`) — le tabac
    ne se cherche sur le web que là où le réseau est licencié, donc recensé. Ailleurs, la
    catégorie reste moissonnée par OSM mais ne déclenche aucun appel web."""
    eligible = claude_enrich.local_commerce_categories_for(country_code)
    present = set()
    for p in all_harvested:
        cat = p.get("category")
        if (cat in eligible
                and p.get("lat") is not None and p.get("lon") is not None
                and overpass.haversine_m(origin[0], origin[1], p["lat"], p["lon"]) <= proximity_m):
            present.add(cat)
    return [c for c in eligible if c in wanted_codes and c not in present]



# ── V2-77b : estancos & bars à chicha — la FAUSSE PLÉNITUDE ──────────────────
#
# V2-74 traitait le VIDE (Bégadan : aucune pharmacie moissonnée). Ici le défaut est
# inverse et plus sournois : la catégorie est PLEINE et pourtant fausse. À Adeje, le
# tabac rendait « Radikas », « La Cava La Cubana », « Tobacco Deluxe » — pas un seul
# ESTANCO, le bureau licencié où l'on achète timbres et tickets. Le système croyait avoir
# trouvé. Ces deux passes se déclenchent donc MÊME QUAND OSM A RENDU QUELQUE CHOSE.
#
# Elles s'ACCROCHENT d'abord au lieu déjà moissonné (cascade V2-73g : poser une puce sur
# le bar connu vaut mieux qu'en créer un second), et ne CRÉENT que ce qui manque.

def _web_marks_and_creates(conn, prop: dict, ai, job_id: str, summary: dict, http_client,
                           origin: tuple, *, fact_type: str, items_key: str, fetch,
                           max_age_days: int, category: str, subtype: str, meta_key: str,
                           harvested: list[dict], step_name: str,
                           require_address: bool) -> None:
    """Moteur COMMUN des deux passes V2-77b. Découverte web mutualisée par (pays, commune)
    — un appel par secteur, réutilisé par tous les guides — puis, pour chaque lieu prouvé :
    (1) APPARIEMENT contre les POI déjà moissonnés de la catégorie (`_activity_name_match`,
    souple, V2-73g) → on pose le sous-type sur la fiche existante ; (2) à défaut, CRÉATION
    d'un POI 'suggested' à l'adresse géocodée STRICTEMENT (garde de cohérence V2-74b).
    Un lieu sans jumeau ET sans adresse est simplement écarté. Best-effort (SAVEPOINT) :
    un échec n'annule ni la moisson ni le reste du job."""
    cc, city = prop["country_code"], prop["city"]
    try:
        with conn.transaction():
            if not db.area_fact_fresh(conn, cc, city, fact_type, max_age_days):
                fact, meta = fetch(city, cc, ai)
                db.upsert_area_facts(conn, cc, city, fact, source=settings.anthropic_model)
                db.record_costs(conn, prop["id"], job_id, "anthropic", step_name,
                                meta["attempts"])
                summary["cost_cts"] += meta["cost_cts"]
            fact = db.get_area_fact(conn, cc, city, fact_type) or {}
            found = fact.get(items_key) or []
            # Cibles d'appariement : les POI de CETTE catégorie déjà moissonnés.
            pool = [h for h in harvested if h.get("category") == category]
            commune_center = None
            marked = created = skipped = 0
            for item in found:
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                proof = {"source_url": item.get("source_url"),
                         "verified_on": item.get("verified_on")}
                twin = _activity_name_match(name, pool)
                if twin is not None:
                    # (1) Le lieu EXISTE déjà : on le qualifie, on ne le double pas.
                    marked += db.set_poi_subtype_by_name(
                        conn, prop["id"], category, twin["name"], subtype, meta_key, proof)
                    continue
                addr = (item.get("place_address") or "").strip()
                if not addr and require_address:
                    skipped += 1
                    continue
                if not addr:
                    skipped += 1     # chicha sans jumeau NI adresse → rien à placer
                    continue
                ref = f"claude:{step_name}:" + _slug(name)
                if db.poi_source_ref_exists(conn, prop["id"], ref):
                    continue
                if commune_center is None:
                    commune_center = _commune_center(prop, http_client)
                geo = _geocode_local_commerce({"place_address": addr}, prop,
                                              commune_center, http_client)
                if geo is None:
                    skipped += 1
                    continue
                lat, lon, locality, approx = geo
                poi = {"name": name, "category": category, "lat": lat, "lon": lon,
                       "address": addr, "phone": item.get("phone"),
                       "locality": locality or city, "subtype": subtype, "source_ref": ref,
                       "completion_meta": {meta_key: {**proof, "approx": approx}}}
                distance.compute_distances(origin, [poi], client=http_client)
                created += db.insert_local_commerce_poi(conn, prop["id"], poi)
            db.job_step(conn, job_id, step_name,
                        {"ok": True, "found": len(found), "marked": marked,
                         "created": created, "skipped": skipped})
        _progress(f"  ✓ {step_name} : {len(found)} trouvé(s) sur le web — "
                  f"{marked} fiche(s) qualifiée(s), {created} créée(s)")
    except Exception as exc:  # noqa: BLE001 — best-effort, le job continue
        log.warning("Passe %s en échec (%s) : %s", step_name, city, exc, exc_info=True)
        # Coût des essais APRÈS le rollback du SAVEPOINT (V2-07 3bis) : l'argent est
        # dépensé à la réponse, pas au succès.
        summary["cost_cts"] += _record_failed_call_cost(conn, prop["id"], job_id,
                                                        step_name, exc)
        db.job_step(conn, job_id, step_name,
                    {"ok": False, "error": overpass._short(str(exc))})
        conn.commit()
        _progress(f"  ⚠ {step_name} non résolus : {overpass._short(str(exc))}")


def _discover_estancos(conn, prop, ai, job_id, summary, http_client, origin,
                       harvested: list[dict]) -> None:
    """Estancos de la commune (V2-77b) — UNIQUEMENT dans les pays à réseau licencié, donc
    recensé (`TOBACCO_LICENSED_COUNTRIES`). Ailleurs, aucun appel : il n'y a pas d'annuaire
    d'estancos aux Pays-Bas. Se déclenche même si la catégorie tabac est PLEINE."""
    if (prop.get("country_code") or "").upper() not in claude_enrich.TOBACCO_LICENSED_COUNTRIES:
        return
    _web_marks_and_creates(
        conn, prop, ai, job_id, summary, http_client, origin,
        fact_type=claude_enrich.ESTANCO_FACT_TYPE, items_key="estancos",
        fetch=claude_enrich.fetch_estancos,
        max_age_days=settings.estanco_max_age_days,
        category="tobacco", subtype="estanco", meta_key="_estanco",
        harvested=harvested, step_name="estancos", require_address=True)


def _discover_shisha_bars(conn, prop, ai, job_id, summary, http_client, origin,
                          harvested: list[dict]) -> None:
    """Bars à CHICHA de la commune (V2-77b). Aucune garde pays : un shisha lounge existe
    partout où il y a du tourisme. L'appariement au bar déjà moissonné est le cas NOMINAL
    (c'est une puce qu'on pose, pas un lieu qu'on ajoute)."""
    _web_marks_and_creates(
        conn, prop, ai, job_id, summary, http_client, origin,
        fact_type=claude_enrich.SHISHA_FACT_TYPE, items_key="bars",
        fetch=claude_enrich.fetch_shisha_bars,
        max_age_days=settings.shisha_max_age_days,
        category="bar", subtype="shisha", meta_key="_shisha",
        harvested=harvested, step_name="shisha_bars", require_address=False)



def _discover_and_materialize_local_commerces(conn, prop: dict, ai, job_id: str,
                                              summary: dict, http_client, void_codes: list[str],
                                              origin: tuple) -> None:
    """Commerces & services ESSENTIELS de village (V2-74) : découverte web MUTUALISÉE par
    commune (cache area_facts, fenêtre propre) PUIS matérialisation en POI 'suggested' par
    logement, UNIQUEMENT pour les catégories VIDES localement (OSM couvre déjà les autres).
    Adresse géocodée STRICTEMENT (cascade V2-56b — jamais un centroïde) ; sans position fiable,
    le commerce n'entre pas. Idempotent (source_ref). Best-effort (SAVEPOINT). `locality` =
    commune → honnêteté de la distance (V2-38). Le juge (guest) publie derrière."""
    cc, city = prop["country_code"], prop["city"]
    try:
        with conn.transaction():
            if not db.area_fact_fresh(conn, cc, city, claude_enrich.LOCAL_COMMERCE_FACT_TYPE,
                                      settings.local_commerce_max_age_days):
                fact, meta = claude_enrich.fetch_local_commerces(
                    city, cc, ai, lang=prop.get("default_lang") or "fr")
                db.upsert_area_facts(conn, cc, city, fact, source=settings.anthropic_model)
                db.record_costs(conn, prop["id"], job_id, "anthropic",
                                "local_commerces", meta["attempts"])
                summary["cost_cts"] += meta["cost_cts"]
            fact = db.get_area_fact(conn, cc, city,
                                    claude_enrich.LOCAL_COMMERCE_FACT_TYPE) or {}
            discovered = fact.get("commerces") or []
            commune_center = _commune_center(prop, http_client)   # V2-74b : garde de cohérence
            created = skipped_pos = 0
            for c in discovered:
                cat = c.get("category")
                if cat not in void_codes:      # OSM couvre déjà cette catégorie ici
                    continue
                ref = "claude:local:" + cat + ":" + _slug(c["name"])
                if db.poi_source_ref_exists(conn, prop["id"], ref):
                    continue                   # déjà matérialisé (idempotent, pas de géocodage)
                # V2-74b : géocodage avec garde de cohérence + escalade + repli au centre.
                geo = _geocode_local_commerce(c, prop, commune_center, http_client)
                if geo is None:
                    skipped_pos += 1
                    continue                   # commune introuvable → n'entre pas
                lat, lon, locality, approx = geo
                poi = {"name": c["name"], "category": cat, "lat": lat, "lon": lon,
                       "address": c.get("place_address"), "phone": c.get("phone"),
                       "locality": locality or city, "source_ref": ref,
                       "completion_meta": {"_local": {"source_url": c.get("source_url"),
                                                      "verified_on": c.get("verified_on"),
                                                      "approx": approx}}}
                distance.compute_distances(origin, [poi], client=http_client)
                created += db.insert_local_commerce_poi(conn, prop["id"], poi)
            summary["local_commerces_created"] += created
            db.job_step(conn, job_id, "local_commerces",
                        {"ok": True, "void": sorted(void_codes), "discovered": len(discovered),
                         "created": created, "skipped_position": skipped_pos})
        _progress(f"  ✓ commerces de village : {created} créé(s) "
                  f"(catégories vides : {', '.join(sorted(void_codes))})")
    except Exception as exc:  # noqa: BLE001 — best-effort (web/parse)
        log.warning("Commerces de village (%s) non résolus : %s", city, exc)
        c = _record_failed_call_cost(conn, prop["id"], job_id, "local_commerces", exc)
        summary["cost_cts"] += c
        db.job_step(conn, job_id, "local_commerces",
                    {"ok": False, "error": overpass._short(str(exc)), "cost_cts": round(c, 2)})
        conn.commit()
        _progress(f"  ⚠ commerces de village non résolus : {overpass._short(str(exc))}")


def _memorize_fresh_picks(conn, prop: dict, code: str, pois: list[dict],
                          ovt: list[dict] | None, raw_picks: list[dict],
                          origin: tuple, http_client: httpx.Client | None,
                          city_norm: str) -> int:
    """Positionne les picks FRAIS du run et les MÉMORISE pour le secteur (V2-56c) :
    chaque run enrichit la mémoire commune. Un pick non positionnable TOMBE (jamais un
    centroïde). Renvoie le nombre écarté (sans position)."""
    skipped = 0
    for pk in raw_picks:
        pos = _position_pick(pk, pois, ovt, prop, origin, http_client)
        if pos is None:
            skipped += 1
            log.warning("Pick réputé « %s » sauté : position non fiable (%s)",
                        pk["name"], pk.get("address"))
            continue
        lat, lon, locality, base_phone, base_web, base_local = pos
        # Nom local (V2-66b cas a) : celui de la fiche OSM appariée d'abord (vrai nom OSM),
        # sinon celui fourni par la recherche web (le modèle connaît 銀座 久兵衛).
        name_local = base_local or (pk.get("name_local") or None)
        db.upsert_editorial_pick(
            conn, country_code=prop["country_code"], city=prop["city"],
            city_norm=city_norm, name=pk["name"], name_norm=dedup._norm(pk["name"]),
            category=code, reason=pk.get("reason"), source_url=pk.get("source_url"),
            verified_on=pk.get("verified_on"), lat=lat, lon=lon, locality=locality,
            phone=pk.get("phone") or base_phone,
            website=pk.get("website") or base_web, name_local=name_local)
    return skipped


def _merge_sector_picks(pois: list[dict], memory: list[dict], origin: tuple,
                        http_client: httpx.Client | None, *, preferred_m: int,
                        hard_cap: int | None) -> tuple[list[dict], int]:
    """Fusionne l'UNION mémorisée du secteur (V2-56c) dans le guide : chaque pick est
    DÉJÀ positionné (mémoire). Apparié par nom à l'OSM courant → MARQUE la fiche (pas
    de doublon) ; sinon entre comme sa propre fiche à sa position mémorisée. Distances
    relatives à CE logement + cap de distance famille F. Renvoie `(pois, n_placés)`."""
    matched = 0
    new_pois: list[dict] = []
    for mp in memory:
        m = _name_match(mp["name"], pois)
        if m is not None:
            _mark_editorial(m, mp, "sector_memory")
            _fill_editorial_contacts(m, mp)
            matched += 1
            continue
        new_pois.append(_build_editorial_poi(
            mp, mp["category"], mp["lat"], mp["lon"], mp.get("locality"),
            origin, "sector_memory"))
    if new_pois:
        try:
            distance.compute_distances(origin, new_pois, client=http_client)
        except Exception as exc:  # noqa: BLE001 — les distances ne bloquent pas
            log.warning("Distances picks éditoriaux non calculées : %s", exc)
        new_pois, _dropped = overpass.apply_drive_cap(new_pois, preferred_m, hard_cap)
    pois.extend(new_pois)
    return pois, matched + len(new_pois)


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
                     "markets_created": 0, "local_commerces_created": 0,
                     "duplicates_merged": 0,
                     "rental_web_kept": 0, "hard_cap_dropped": 0,
                     "network_dropped": 0, "service_dropped": 0,
                     "service_qualified": 0,
                     "overture_added": 0, "overture_contacts": 0,
                     "editorial_found": 0, "editorial_added": 0,
                     "editorial_skipped": 0}
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
                wanted, origin[0], origin[1], client=http_client,
                # Nom local pour le chauffeur (V2-66) : langue du pays → capture du
                # nom/adresse en écriture d'origine (JP → name:ja…), non latin uniquement.
                country_lang=overpass.country_language(prop.get("country_code")))

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
            # V2-73 : union des POI moissonnés (nom+position), source du 1er échelon de
            # la cascade STRICTE de placement des activités (appariement de NOM contre
            # OSM/Overture — une activité « surf/plage du Gurp » se pose sur la fiche
            # plage déjà récoltée plutôt que d'être re-géocodée).
            all_harvested: list[dict] = []
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
                    city_norm = dedup._norm(prop["city"])
                    raw = _discover_editorial_sorties(
                        conn, prop, ai, job_id, summary).get(code, [])
                    # A. Positionner les FRAIS et les MÉMORISER pour le secteur (V2-56c).
                    if raw:
                        n_sk = _memorize_fresh_picks(
                            conn, prop, code, pois, ovt, raw, origin, http_client,
                            city_norm)
                        summary["editorial_skipped"] = (
                            summary.get("editorial_skipped", 0) + n_sk)
                        conn.commit()   # mémoire du secteur persistée avant lecture
                    # B. Consommer l'UNION mémorisée du secteur (frais + anciens < 90 j).
                    memory = db.sector_editorial_picks(
                        conn, prop["country_code"], city_norm, code,
                        settings.reputed_max_age_days)
                    if memory:
                        pois, n_ed = _merge_sector_picks(
                            pois, memory, origin, http_client,
                            preferred_m=cat["default_radius_m"],
                            hard_cap=overpass.target_for(code).hard_cap_drive_min)
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
                # V2-74b : dédup INTER-SOURCES par cœur de nom, APRÈS la fusion des picks
                # éditoriaux (OSM + web à positions distinctes que la dédup par distance
                # rate : « Le Canoé »/« Restaurant Le Canoe »). Position OSM préférée au web.
                pois, name_merged = dedup.dedupe_name_core(pois)
                summary["duplicates_merged"] += name_merged
                if code in settings.describe_categories:
                    all_editorial.extend(pois)
                # V2-73 : mémorise nom+position (+ catégorie, V2-73g : lien « voir dans le
                # guide ») pour l'appariement des activités — seules les fiches géolocalisées
                # servent de cible d'ancrage.
                all_harvested.extend(
                    {"name": p.get("name"), "lat": p.get("lat"), "lon": p.get("lon"),
                     "category": code}
                    for p in pois if p.get("lat") is not None and p.get("lon") is not None)
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
            # Densité déduite de la moisson (V2-68 p3) : sert au PLANCHER VITAL (p4) —
            # une zone urbaine sans hôpital/pharmacie/police proche = mauvais ancrage.
            summary["dense"] = bool(harvest.get("dense"))
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

                # 4f. Activités du secteur (V2-71) : passe web « que fait-on ici ? »,
                # mutualisée par commune (area_fact), best-effort SAVEPOINT (patron
                # food_delivery). Preuve ou rien ; liste vide = résultat valide.
                if not db.area_fact_fresh(conn, prop["country_code"], prop["city"],
                                          claude_enrich.ACTIVITIES_FACT_TYPE,
                                          settings.activities_max_age_days):
                    try:
                        with conn.transaction():
                            act, meta = claude_enrich.fetch_activities(
                                prop["city"], prop["country_code"], ai,
                                lang=prop.get("default_lang") or "fr")
                            acts = act[claude_enrich.ACTIVITIES_FACT_TYPE]["activities"]
                            n_act = len(acts)
                            # V2-73 : placer les activités sur la carte (cascade stricte,
                            # jamais un centroïde) AVANT de mémoriser le fait de zone.
                            n_placed = _place_activities(acts, all_harvested, prop,
                                                         origin, http_client)
                            db.upsert_area_facts(conn, prop["country_code"],
                                                 prop["city"], act,
                                                 source=settings.anthropic_model)
                            db.record_costs(conn, property_id, job_id, "anthropic",
                                            "activities", meta["attempts"])
                            summary["cost_cts"] += meta["cost_cts"]
                            db.job_step(conn, job_id, "activities",
                                        {"ok": True, "activities": n_act,
                                         "placed": n_placed,
                                         "cost_cts": round(meta["cost_cts"], 2)})
                        _progress(f"  ✓ activités du secteur : {n_act} "
                                  f"({n_placed} sur la carte) — "
                                  f"{meta['cost_cts']:.2f} ct")
                    except Exception as ac_exc:  # noqa: BLE001 — best-effort
                        log.warning("Activités (%s) non résolues : %s",
                                    prop["city"], ac_exc)
                        c = _record_failed_call_cost(conn, property_id, job_id,
                                                     "activities", ac_exc)
                        summary["cost_cts"] += c
                        db.job_step(conn, job_id, "activities",
                                    {"ok": False, "error": overpass._short(str(ac_exc)),
                                     "cost_cts": round(c, 2)})
                        conn.commit()
                        _progress(f"  ⚠ activités non résolues : "
                                  f"{overpass._short(str(ac_exc))}")
                else:
                    # V2-73b : le fait existe déjà (étape web sautée) — rattraper les
                    # positions s'il date d'avant V2-73 (aucun appel web/LLM).
                    _backfill_activity_positions(conn, prop, all_harvested, origin,
                                                 http_client, job_id, summary)

                # 4g. Commerces de village (V2-74) : quand une catégorie ESSENTIELLE demandée
                # est VIDE dans le rayon de proximité (OSM muet sur le village), découverte web
                # mutualisée + matérialisation. En zone dense, aucune catégorie n'est vide →
                # l'étape ne part pas (contre-épreuve La Zenia/Tokyo : aucun changement).
                void_codes = _void_essentials(
                    all_harvested, {c["code"] for c in wanted}, origin,
                    settings.local_commerce_proximity_m, prop.get("country_code"))
                if void_codes:
                    _discover_and_materialize_local_commerces(
                        conn, prop, ai, job_id, summary, http_client, void_codes, origin)

                # (4h) V2-77b — ESTANCOS & CHICHA : la FAUSSE plénitude. Contrairement à
                # 4g, ces passes ne regardent PAS si la catégorie est vide : à Adeje elle
                # était pleine et pourtant sans un seul estanco. Elles ne partent que si la
                # catégorie concernée est DEMANDÉE (jamais inventer un besoin non prévu).
                wanted_codes = {c["code"] for c in wanted}
                if "tobacco" in wanted_codes:
                    _discover_estancos(conn, prop, ai, job_id, summary, http_client,
                                       origin, all_harvested)
                if "bar" in wanted_codes:
                    _discover_shisha_bars(conn, prop, ai, job_id, summary, http_client,
                                          origin, all_harvested)

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
                f"{summary['local_commerces_created']} commerce(s) de village créé(s), "
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
                wanted, origin[0], origin[1], client=http_client,
                country_lang=overpass.country_language(prop.get("country_code")))

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
    print(f"  Commerces de village  : {result.get('local_commerces_created', 0)}")
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
