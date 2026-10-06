"""V2-84 — cache Overture par zone : lire en local, pas en ligne.

Aucun réseau : un parquet au schéma RÉEL de la release 2026-09-23.1 (`taxonomy`, géométrie
native, id GERS — recopié du DESCRIBE, leçon V2-76b) tient lieu de « S3 ».
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from enrich import overture, pipeline
from enrich.settings import settings

duckdb = pytest.importorskip("duckdb")

JAVEA = (38.7698, 0.1483)
ZONE = {"id": "costa_blanca", "name": "Costa Blanca", "bbox": [-1.40, 37.55, 0.45, 39.10]}


def _connect():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    overture._bound_memory(con)
    return con


@pytest.fixture()
def src(tmp_path):
    """« S3 » local : deux lieux près de Jávea + un lieu à Bali (hors zone)."""
    raw = str(tmp_path / "raw.parquet")
    out = str(tmp_path / "places.parquet")
    con = _connect()
    con.execute(f"""COPY (SELECT * FROM (VALUES
        ('gers-1', {{'primary': 'Bon Amb'}}, 'restaurant',
         {{'primary': 'european_restaurant',
           'hierarchy': ['food_and_drink', 'restaurant', 'european_restaurant'],
           'alternates': []}}, ['+34 965 08 44 40'], ['https://bonamb.com'], 0.1483, 38.7698),
        ('gers-2', {{'primary': 'Farmacia Arenal'}}, 'pharmacy',
         {{'primary': 'pharmacy', 'hierarchy': ['health_care', 'pharmacy'],
           'alternates': []}}, ['+34 966 00 00 00'], [], 0.1900, 38.7800),
        ('gers-3', {{'primary': 'La Favela Bali'}}, 'restaurant',
         {{'primary': 'bar_and_grill_restaurant',
           'hierarchy': ['food_and_drink', 'restaurant', 'bar_and_grill_restaurant'],
           'alternates': []}}, ['+62361730010'], [], 115.1629, -8.6840)
      ) t(id, names, basic_category, taxonomy, phones, websites, x, y)) TO '{raw}'
      (FORMAT PARQUET)""")
    con.execute(f"""COPY (SELECT id, names, basic_category, taxonomy, phones, websites,
                          ST_Point(x, y) AS geometry,
                          {{'xmin': x, 'xmax': x, 'ymin': y, 'ymax': y}} AS bbox
                     FROM read_parquet('{raw}')) TO '{out}' (FORMAT PARQUET)""")
    con.close()
    return out


@pytest.fixture()
def cache(tmp_path, src):
    d = tmp_path / "cache"
    meta = overture.build_zone_cache(ZONE, release="2026-09-23.1", cache_dir=d,
                                     connect=_connect, source=src)
    return d, meta


def test_zones_come_from_configuration_and_malformed_ones_are_ignored(tmp_path):
    p = tmp_path / "zones.json"
    p.write_text(json.dumps({"zones": [
        ZONE, {"id": "Bad Id!", "bbox": [0, 0, 1, 1]},
        {"id": "inverse", "bbox": [1, 1, 0, 0]}, {"id": "court", "bbox": [0, 0]}]}))
    zones = overture.load_zones(p)
    assert [z["id"] for z in zones] == ["costa_blanca"]
    # La configuration livrée déclare bien les zones actives.
    assert {"costa_blanca", "bali", "gironde"} <= {z["id"] for z in overture.load_zones()}


def test_build_writes_zone_extract_and_sidecar_atomically(cache):
    d, meta = cache
    assert meta["places"] == 2                      # Bali (hors bbox) filtré
    assert meta["release"] == "2026-09-23.1" and meta["built_at"]
    assert (d / "costa_blanca.parquet").exists() and (d / "costa_blanca.json").exists()
    assert not list(d.glob("*.tmp"))                # écriture atomique : aucun reste
    assert overture.cache_meta("costa_blanca", d)["places"] == 2


def test_coverage_requires_full_containment_and_prefers_the_smallest_zone(cache):
    d, _ = cache
    javea_bbox = overture._bbox(*JAVEA, 25000)
    assert overture.find_cached_zone(javea_bbox, zones=[ZONE], cache_dir=d)[0]["id"] \
        == "costa_blanca"
    # Bordure : un logement dont les 25 km débordent la zone → pas de cache (partiel).
    edge = overture._bbox(39.05, 0.10, 25000)
    assert overture.find_cached_zone(edge, zones=[ZONE], cache_dir=d) is None
    # Zone déclarée mais JAMAIS construite → pas de cache.
    other = {"id": "gironde", "name": "G", "bbox": [-1.55, 43.95, 0.60, 45.85]}
    assert overture.find_cached_zone(overture._bbox(45.35, -0.89, 25000),
                                     zones=[other], cache_dir=d) is None


def test_local_read_returns_the_same_places_as_the_source(cache, src, monkeypatch):
    """Mêmes lieux en lecture cache qu'en lecture « S3 » sur le même point ; la trace dit
    d'où l'on a lu, la zone, la release et l'âge."""
    d, _ = cache
    t_now = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731
    places, trace = overture.fetch_places_traced(*JAVEA, 25000, zones=[ZONE],
                                                 cache_dir=d, connect=_connect, now=t_now)
    con = _connect()
    direct = overture._rows_to_places(overture._query_places(
        con, "x", overture._bbox(*JAVEA, 25000), source=src))
    con.close()
    key = lambda ps: sorted(p["source_ref"] for p in ps)   # noqa: E731
    assert key(places) == key(direct) == ["gers:gers-1", "gers:gers-2"]
    assert trace["source"] == "cache" and trace["zone"] == "costa_blanca"
    assert trace["release"] == "2026-09-23.1" and trace["age_days"] == 0
    assert trace["stale"] is False
    assert "zone costa_blanca, release 2026-09-23.1, âge 0 j" in \
        pipeline._overture_source_note(trace)


def test_outside_any_cached_zone_reads_s3_unchanged(cache, monkeypatch):
    d, _ = cache
    seen = {}

    def fake_query(con, release, bbox, source=None):
        seen["source"], seen["release"] = source, release
        return []
    monkeypatch.setattr(overture, "_query_places", fake_query)
    places, trace = overture.fetch_places_traced(45.355, -0.890, 25000, release="2026-09-23.1",
                                                 zones=[ZONE], cache_dir=d, connect=_connect)
    assert trace["source"] == "s3" and seen["source"] is None      # lecture S3, comme avant
    assert pipeline._overture_source_note(trace) == "lecture S3 (release 2026-09-23.1)"


def test_explicit_release_different_from_cache_forces_s3(cache, monkeypatch):
    d, _ = cache
    monkeypatch.setattr(overture, "_query_places", lambda *a, **k: [])
    _, trace = overture.fetch_places_traced(*JAVEA, 25000, release="2026-10-21.0",
                                            zones=[ZONE], cache_dir=d, connect=_connect)
    assert trace["source"] == "s3"


def test_old_cache_is_flagged_stale(cache, monkeypatch):
    d, _ = cache
    later = lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(   # noqa: E731
        days=settings.overture_cache_max_age_days + 1)
    _, trace = overture.fetch_places_traced(*JAVEA, 25000, zones=[ZONE], cache_dir=d,
                                            connect=_connect, now=later)
    assert trace["source"] == "cache" and trace["stale"] is True


def test_duckdb_is_memory_bounded_and_spills_to_disk():
    """Contrainte mémoire (VPS 3,8 Go) : plafond, répertoire de débordement, threads."""
    con = _connect()
    lim = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    tmp = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    thr = con.execute("SELECT current_setting('threads')").fetchone()[0]
    con.close()
    assert lim.replace(" ", "").upper().startswith(("1.0GIB", "953.6MIB", "1GB", "1.0GB",
                                                     "953.6MB"))
    assert tmp.endswith("duckdb_tmp") and int(thr) == settings.duckdb_threads
