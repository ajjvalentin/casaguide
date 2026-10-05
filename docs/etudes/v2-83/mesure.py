#!/usr/bin/env python3
"""ÉTUDE V2-83 — mesure, PAS du code de production.

Compare, sur trois terrains (Seminyak, La Zenia, Bégadan), pour chacune de nos catégories :
  · OSM  : la moisson RÉELLE de production (`overpass.fetch_grouped`, plafonnée à 8/catégorie,
           paliers dense-first) — présence, plus proche, contacts, horaires ;
  · Overture : release S3 courante, bbox 25 km, classée par notre mapping commercial
           (`map_overture_place`) + un classement par mots-clés de taxonomie POUR LA MESURE
           (vitaux, transport, géographie — que le mapping de production exclut à dessein).
Écrit `resultats.json` à côté. Relançable : `PYTHONPATH=backend python docs/etudes/v2-83/mesure.py`.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
from enrich import db, overpass, overture  # noqa: E402

TERRAINS = {
    "Seminyak (ID)": (-8.6901427, 115.1646258, "ID"),
    "La Zenia (ES)": (37.9300, -0.7300, "ES"),
    "Bégadan (FR)": (45.3550, -0.8900, "FR"),
}
SKIP = {"babysitter", "food_delivery"}          # catégories sans moisson géographique

# Classement Overture POUR LA MESURE (ordre = priorité) : mots-clés cherchés dans la feuille
# ET la hiérarchie taxonomique. Le mapping de production (commercial) est essayé d'abord.
KEYWORDS = [
    ("hospital", ("hospital", "emergency_room")),
    ("pharmacy", ("pharmacy", "drugstore")),
    ("police", ("police",)),
    ("veterinary", ("veterinar", "animal_hospital")),
    ("doctor", ("doctor", "general_practitioner", "medical_center", "family_practice")),
    ("post_office", ("post_office",)),
    ("airport", ("airport",)),
    ("train_station", ("train_station", "railway_station")),
    ("bus_station", ("bus_station",)),
    ("bus_stop", ("bus_stop",)),
    ("charging_station", ("ev_charging", "charging_station")),
    ("fuel", ("gas_station", "fuel")),
    ("parking", ("parking",)),
    ("beach", ("beach",)),
    ("family_activity", ("amusement", "zoo", "aquarium", "water_park", "playground",
                         "theme_park")),
    ("sport", ("gym", "fitness", "golf", "tennis", "surf", "diving", "sports_club",
               "sport_or_fitness")),
    ("sight", ("landmark", "historic", "monument", "museum", "attraction", "viewpoint")),
    ("taxi", ("taxi",)),
]


def classify(place: dict, cmap: dict) -> str | None:
    code = overture.map_overture_place(place, cmap)
    if code:
        return code
    toks = " ".join([place.get("category") or ""] + list(place.get("category_hierarchy") or []))
    for c, kws in KEYWORDS:
        if any(k in toks for k in kws):
            return c
    return None


def overture_bbox(con, release: str, lat: float, lon: float, radius: int) -> list[dict]:
    src = overture._places_src(release)
    sch = overture.detect_schema(con, src)
    minlon, minlat, maxlon, maxlat = overture._bbox(lat, lon, radius)
    rows = con.execute(f"""
        SELECT names.primary, ST_Y({sch['geom']}), ST_X({sch['geom']}),
               {sch['category']}, {sch['hierarchy']}, phones[1], websites[1],
               operating_status, list_max(list_transform(sources, s -> s.update_time)),
               names.common IS NOT NULL AND cardinality(names.common) > 0
          FROM read_parquet('{src}', hive_partitioning=1)
         WHERE bbox.xmin BETWEEN {minlon} AND {maxlon}
           AND bbox.ymin BETWEEN {minlat} AND {maxlat}""").fetchall()
    return [{"name": r[0], "lat": r[1], "lon": r[2], "category": r[3],
             "category_hierarchy": r[4], "phone": r[5], "website": r[6],
             "status": r[7], "updated": r[8], "local_names": r[9]} for r in rows]


def pct(n: int, d: int) -> float | None:
    return round(100 * n / d) if d else None


def main() -> None:
    with db.connect() as conn:
        cats = [c for c in conn.execute(
            "SELECT * FROM poi_categories ORDER BY chapter, code").fetchall()
            if c["code"] not in SKIP]
    cmap = overture.load_category_map()
    release = overture.latest_overture_release()
    con = overture._duckdb_connect()
    today = dt.date.today()
    out = {"release_overture": release, "date": today.isoformat(), "terrains": {}}
    for tname, (lat, lon, cc) in TERRAINS.items():
        t0 = time.time()
        ovt = overture_bbox(con, release, lat, lon, 25000)
        t_ovt = round(time.time() - t0, 1)
        t0 = time.time()
        with httpx.Client(timeout=120) as cl:
            grouped, failed, harvest = overpass.fetch_grouped(
                cats, lat, lon, client=cl, country_lang=overpass.country_language(cc))
        t_osm = round(time.time() - t0, 1)
        by = {}
        for p in ovt:
            c = classify(p, cmap)
            if c:
                by.setdefault(c, []).append(p)
        ages = [(today - dt.date.fromisoformat(str(p["updated"])[:10])).days
                for p in ovt if p.get("updated")]
        rows = {}
        for c in cats:
            code, r_pref = c["code"], c["default_radius_m"]
            o = grouped.get(code) or []
            near = [p for p in by.get(code, [])
                    if overpass.haversine_m(lat, lon, p["lat"], p["lon"]) <= r_pref]
            d_o = min((p.get("crow_m") or overpass.haversine_m(lat, lon, p["lat"], p["lon"])
                       for p in o), default=None)
            d_v = min((overpass.haversine_m(lat, lon, p["lat"], p["lon"]) for p in near),
                      default=None)
            rows[code] = {
                "rayon_m": r_pref,
                "osm_n": len(o), "osm_echec": code in failed,
                "osm_plus_proche_m": round(d_o) if d_o is not None else None,
                "osm_tel_pct": pct(sum(1 for p in o if p.get("phone")), len(o)),
                "osm_web_pct": pct(sum(1 for p in o if p.get("website")), len(o)),
                "osm_horaires_pct": pct(sum(1 for p in o if p.get("opening_hours")), len(o)),
                "ovt_n_rayon": len(near),
                "ovt_plus_proche_m": round(d_v) if d_v is not None else None,
                "ovt_tel_pct": pct(sum(1 for p in near if p.get("phone")), len(near)),
                "ovt_web_pct": pct(sum(1 for p in near if p.get("website")), len(near)),
            }
        out["terrains"][tname] = {
            "overture_lieux_25km": len(ovt), "overture_lecture_s": t_ovt,
            "osm_moisson_s": t_osm, "osm_dense": harvest.get("dense"),
            "osm_echecs": sorted(failed),
            "overture_age_source_median_j": statistics.median(ages) if ages else None,
            "overture_age_source_p90_j": (sorted(ages)[int(0.9 * len(ages))] if ages else None),
            "overture_statut_non_open_pct": pct(
                sum(1 for p in ovt if p.get("status") not in (None, "open")), len(ovt)),
            "overture_noms_locaux_pct": pct(sum(1 for p in ovt if p.get("local_names")), len(ovt)),
            "categories": rows,
        }
        print(f"✓ {tname} : Overture {len(ovt)} lieux ({t_ovt} s), OSM {t_osm} s, "
              f"échecs OSM {sorted(failed)}", flush=True)
        time.sleep(5)
    Path(__file__).with_name("resultats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("écrit resultats.json")


if __name__ == "__main__":
    main()
