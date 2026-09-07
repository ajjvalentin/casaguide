"""Benchmark de sources Overture vs OSM (V2-48).

Cœur DÉTERMINISTE (mapping, appariement, métriques, sondes, rendu) testé PUR avec
fixtures ; le fetch Overture est INJECTÉ (aucun réseau/DuckDB). Un test d'intégration
prouve la LECTURE SEULE (aucune écriture base) contre le vrai PostgreSQL.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/
import source_benchmark as SB  # noqa: E402
from enrich.settings import settings  # noqa: E402

LAT, LON = 37.984, -1.128     # Murcie centre


def _ovt(name, code_slug, dlat=0.0, dlon=0.0, **f):
    return {"name": name, "lat": LAT + dlat, "lon": LON + dlon,
            "category": code_slug, "phone": None, "website": None,
            "has_hours": False, "operator": None, **f}


def _osm(name, code, dlat=0.0, dlon=0.0, **f):
    return {"name": name, "category_code": code, "lat": LAT + dlat, "lon": LON + dlon,
            "phone": None, "website": None, "has_hours": False, "status": "approved", **f}


# ── Mapping de taxonomie ──────────────────────────────────────────────────────

def test_category_map_prefix_and_unmapped():
    rules = SB.load_category_map()
    assert SB.map_overture_category("eat_and_drink.restaurant", rules) == "restaurant"
    # Sous-catégorie hiérarchique → même code (préfixe).
    assert SB.map_overture_category(
        "eat_and_drink.restaurant.italian_restaurant", rules) == "restaurant"
    assert SB.map_overture_category("financial_service.atm", rules) == "atm"
    assert SB.map_overture_category("arts_and_entertainment.museum", rules) == "sight"
    # Hors périmètre / inconnu → None (listé en annexe, jamais tordu).
    assert SB.map_overture_category("health_and_medical.hospital", rules) is None
    assert SB.map_overture_category("", rules) is None
    assert SB.map_overture_category(None, rules) is None


# ── Appariement ───────────────────────────────────────────────────────────────

def test_places_match_distance_and_name():
    a = _osm("Restaurante El Puerto", "restaurant")
    near_same = _ovt("El Puerto Restaurant", "eat_and_drink.restaurant", dlat=0.0003)  # ~33m
    far_same = _ovt("El Puerto Restaurant", "eat_and_drink.restaurant", dlat=0.01)     # ~1.1km
    near_diff = _ovt("Pizzería Roma", "eat_and_drink.restaurant", dlat=0.0003)
    assert SB.places_match(a, near_same)          # proche + noms proches
    assert not SB.places_match(a, far_same)       # même nom mais trop loin
    assert not SB.places_match(a, near_diff)      # proche mais noms distincts


def test_match_sets_overlap_and_uniques():
    osm = [_osm("Mercadona", "supermarket"),
           _osm("Consum", "supermarket", dlat=0.002)]
    overture = [_ovt("Mercadona", "retail.food.supermarket"),          # apparie Mercadona
                _ovt("Lidl", "retail.food.supermarket", dlat=0.004)]   # unique Overture
    m = SB.match_sets(osm, overture)
    assert m == {"overlap": 1, "osm_unique": 1, "overture_unique": 1}


# ── Complétude & lisibilité ──────────────────────────────────────────────────

def test_completeness_and_readability():
    places = [_ovt("A", "x", phone="+34 1", website="http://a"),
              _ovt("B", "x", phone="+34 2", has_hours=True),
              _ovt("Zona Infantil", "x")]     # générique
    comp = SB.completeness(places)
    assert comp["phone_pct"] == round(200 / 3, 1)     # 2/3
    assert comp["website_pct"] == round(100 / 3, 1)   # 1/3
    assert comp["hours_pct"] == round(100 / 3, 1)
    assert SB.readability(places) == round(200 / 3, 1)  # 2/3 non génériques


# ── Recommandation (grille de décision) ──────────────────────────────────────

def test_recommend_covers_the_four_outcomes():
    # OSM vide + Overture présent → remplace.
    r1 = SB.compute_category_metrics(
        "sight", [], [_ovt("Catedral", "sight")])
    assert r1.recommendation.startswith("Overture remplace")
    # Ni l'un ni l'autre → web-discovery.
    r2 = SB.compute_category_metrics("sight", [], [])
    assert r2.recommendation.startswith("web-discovery")
    # Overture apporte des uniques + plus riche → complète.
    ovt_rich = [_ovt(f"R{i}", "eat_and_drink.restaurant", dlat=0.001 * i,
                     phone="+34", website="http://x") for i in range(5)]
    r3 = SB.compute_category_metrics("restaurant", [_osm("R0", "restaurant")], ovt_rich)
    assert "Overture complète" in r3.recommendation
    # Overture n'ajoute rien → OSM seul.
    r4 = SB.compute_category_metrics(
        "bar", [_osm("Bar Central", "bar"), _osm("Bar Sol", "bar", dlat=0.002)],
        [_ovt("Bar Central", "eat_and_drink.bar")])
    assert r4.recommendation.startswith("OSM seul")


# ── Sondes qualitatives (Murcie) ─────────────────────────────────────────────

def test_murcia_probes_answer_named_questions():
    rules = SB.load_category_map()
    overture = [
        _ovt("Catedral de Murcia", "attractions_and_activities.landmark"),
        _ovt("Real Casino de Murcia", "attractions_and_activities.landmark"),
        _ovt("Banco Santander", "financial_service.banking_and_finance"),
        _ovt("CaixaBank", "financial_service.banking_and_finance"),
        _ovt("Bitcoin ATM - Shitcoins.club", "financial_service.atm"),
        _ovt("MUyBICI Estación 12", "active_life.bike_rental"),
    ]
    probes = SB.run_probes("murcia", [], overture, rules)
    answers = {p["question"]: p["answer"] for p in probes}
    cat = next(a for q, a in answers.items() if "Catedral" in q)
    assert cat.startswith("OUI")
    casino = next(a for q, a in answers.items() if "Casino" in q)
    assert casino.startswith("OUI")
    banks = next(a for q, a in answers.items() if "banques nommées" in q)
    assert "2 banque(s) / 1 crypto" in banks
    muy = next(a for q, a in answers.items() if "MUyBICI" in q)
    assert "1 entrée" in muy


# ── Rendu rapport + CSV ──────────────────────────────────────────────────────

def _fixture_result():
    prop = {"id": "PID", "_key": "murcia", "_label": "CASA MURCIA TEST 4",
            "city": "Murcia", "country_code": "ES", "lat": LAT, "lon": LON}
    rules = SB.load_category_map()
    osm = [_osm("Mercadona", "supermarket")]
    overture = [_ovt("Mercadona", "retail.food.supermarket"),
                _ovt("Catedral de Murcia", "attractions_and_activities.landmark"),
                _ovt("Truc Inconnu", "public_service_and_government.town_hall")]
    return SB.run_terrain(prop, osm, overture, {"supermarket": 3000, "sight": 20000},
                          rules)


def test_render_report_and_csv_structure():
    res = _fixture_result()
    report = SB.render_report([res], release="2026-08", when="2026-09-08 10:00")
    assert "# Benchmark de sources" in report
    assert "CAVEAT VOLUME" in report
    assert "CASA MURCIA TEST 4" in report
    assert "Grille de décision" in report
    assert "Catedral de Murcia" in report              # sonde nominative
    # Annexe : la catégorie Overture non mappée est listée.
    assert "public_service_and_government.town_hall" in report
    csv_txt = SB.render_csv(res)
    lines = csv_txt.strip().splitlines()
    assert lines[0].startswith("terrain,category,osm_count,overture_count")
    assert any(row.startswith("murcia,supermarket,") for row in lines)


# ── Intégration : LECTURE SEULE contre le vrai PostgreSQL ────────────────────

def test_run_benchmark_is_read_only(tmp_path, monkeypatch):
    oid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s,%s,'T')",
                     (oid, f"{oid}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city,
                   country_code, geom) VALUES (%s,%s,'Bench','X','Murcia','ES',
                   ST_SetSRID(ST_MakePoint(%s,%s),4326))""", (pid, oid, LON, LAT))
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, source, status)
               VALUES (%s,'supermarket','Mercadona',
                   ST_SetSRID(ST_MakePoint(%s,%s),4326),'osm','approved')""",
            (pid, LON, LAT))
        conn.commit()

    # Un seul terrain, pointant sur la propriété de test ; fetch Overture injecté.
    monkeypatch.setattr(SB, "TERRAINS", [
        {"key": "murcia", "label": "Bench (test)", "id": pid}])

    def fake_fetch(lat, lon, radius_m, release):
        return [_ovt("Mercadona", "retail.food.supermarket"),
                _ovt("Catedral de Murcia", "attractions_and_activities.landmark")]

    try:
        with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
            before = conn.execute(
                "SELECT count(*) c FROM pois WHERE property_id=%s", (pid,)).fetchone()["c"]
            costs_before = conn.execute("SELECT count(*) c FROM api_costs").fetchone()["c"]
            report_path = SB.run_benchmark(conn, fake_fetch, release="2026-08",
                                           out_dir=tmp_path, when="2026-09-08 10:00")
            after = conn.execute(
                "SELECT count(*) c FROM pois WHERE property_id=%s", (pid,)).fetchone()["c"]
            costs_after = conn.execute("SELECT count(*) c FROM api_costs").fetchone()["c"]
        # Lecture seule : aucun POI ni coût écrits.
        assert after == before == 1 and costs_after == costs_before
        # Fichiers produits (rapport + CSV du terrain).
        assert Path(report_path).is_file()
        assert (tmp_path / "source_benchmark_murcia_2026-09-08.csv").exists() or \
            any(p.name.startswith("source_benchmark_murcia_") for p in tmp_path.glob("*.csv"))
        report = Path(report_path).read_text(encoding="utf-8")
        assert "Bench (test)" in report and "Catedral de Murcia" in report
    finally:
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute("DELETE FROM owners WHERE id=%s", (oid,))
            conn.commit()
