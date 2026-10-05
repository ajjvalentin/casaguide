#!/usr/bin/env python3
"""ÉTUDE V2-83 — coût d'exploitation d'une base locale (mesure, PAS du code de production).

  1. Taille du thème `places` d'Overture (release courante, listing S3 public) et cadence
     des releases.
  2. Extraction d'une ZONE (île, province, département) en parquet local : lignes, durée,
     taille sur disque, puis temps de requête d'une bbox de 25 km sur le fichier local.
  3. Taille des extraits OSM Geofabrik (.pbf) des mêmes zones — ordre de grandeur d'un
     Overpass auto-hébergé.
Écrit `cout.json` à côté. `PYTHONPATH=backend python docs/etudes/v2-83/cout.py`.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
from enrich import overture  # noqa: E402

BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com"
ZONES = {   # (minlon, minlat, maxlon, maxlat), point de test
    "Bali (île)": ((114.42, -8.86, 115.72, -8.06), (-8.6901, 115.1646)),
    "Province d'Alicante": ((-1.10, 37.84, 0.24, 38.92), (37.930, -0.730)),
    "Gironde": ((-1.27, 44.19, 0.32, 45.58), (45.355, -0.890)),
}
GEOFABRIK = {
    "Indonésie (pays entier)": "https://download.geofabrik.de/asia/indonesia-latest.osm.pbf",
    "Comunitat Valenciana": "https://download.geofabrik.de/europe/spain/valencia-latest.osm.pbf",
    "Nouvelle-Aquitaine": "https://download.geofabrik.de/europe/france/aquitaine-latest.osm.pbf",
}


def s3_sizes(prefix: str) -> tuple[int, int]:
    total = n = 0
    token = None
    with httpx.Client(timeout=60) as c:
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            root = ET.fromstring(c.get(BUCKET + "/", params=params).content)
            ns = {"s": root.tag.split("}")[0].strip("{")}
            for k in root.findall("s:Contents", ns):
                total += int(k.find("s:Size", ns).text)
                n += 1
            nxt = root.find("s:NextContinuationToken", ns)
            if nxt is None:
                return total, n
            token = nxt.text


def releases() -> list[str]:
    with httpx.Client(timeout=60) as c:
        root = ET.fromstring(c.get(BUCKET + "/", params={
            "list-type": "2", "prefix": "release/", "delimiter": "/"}).content)
    ns = {"s": root.tag.split("}")[0].strip("{")}
    return sorted(re.sub(r"release/|/", "", p.find("s:Prefix", ns).text)
                  for p in root.findall("s:CommonPrefixes", ns))


def main() -> None:
    rel = overture.latest_overture_release()
    out = {"release": rel, "releases_visibles": releases()}
    size, files = s3_sizes(f"release/{rel}/theme=places/type=place/")
    out["places_monde_go"] = round(size / 1e9, 2)
    out["places_monde_fichiers"] = files
    print(f"✓ places monde : {out['places_monde_go']} Go, {files} fichiers", flush=True)
    con = overture._duckdb_connect()
    src = overture._places_src(rel)
    sch = overture.detect_schema(con, src)
    out["zones"] = {}
    tmp = Path(tempfile.mkdtemp(prefix="v283-"))
    for name, ((x0, y0, x1, y1), (la, lo)) in ZONES.items():
        dest = tmp / (re.sub(r"\W+", "_", name) + ".parquet")
        t0 = time.time()
        con.execute(f"""COPY (SELECT * FROM read_parquet('{src}', hive_partitioning=1)
                       WHERE bbox.xmin BETWEEN {x0} AND {x1} AND bbox.ymin BETWEEN {y0} AND {y1})
                       TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        t_ext = round(time.time() - t0, 1)
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        minlon, minlat, maxlon, maxlat = overture._bbox(la, lo, 25000)
        t0 = time.time()
        n25 = con.execute(f"""SELECT count(*) FROM read_parquet('{dest}')
            WHERE bbox.xmin BETWEEN {minlon} AND {maxlon}
              AND bbox.ymin BETWEEN {minlat} AND {maxlat}""").fetchone()[0]
        t_q = round(time.time() - t0, 3)
        out["zones"][name] = {"lieux": rows, "extraction_s": t_ext,
                              "taille_mo": round(dest.stat().st_size / 1e6, 1),
                              "requete_bbox_25km_s": t_q, "lieux_bbox_25km": n25}
        print(f"✓ {name} : {rows} lieux, {out['zones'][name]['taille_mo']} Mo, "
              f"extraction {t_ext} s, requête 25 km {t_q} s", flush=True)
        dest.unlink()
    out["geofabrik_mo"] = {}
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        for name, url in GEOFABRIK.items():
            r = c.head(url)
            out["geofabrik_mo"][name] = round(int(r.headers.get("content-length", 0)) / 1e6)
    print("✓ geofabrik", out["geofabrik_mo"], flush=True)
    Path(__file__).with_name("cout.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
