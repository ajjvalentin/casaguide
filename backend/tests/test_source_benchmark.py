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

def test_category_map_flat_values_and_unmapped():
    cmap = SB.load_category_map()
    # V2-48c : taxonomie PLATE réelle de la release d'août (le défaut du run V2-48).
    assert SB.map_overture_category("restaurant", cmap) == "restaurant"
    assert SB.map_overture_category("tapas_bar", cmap) == "restaurant"   # exact avant suffixe _bar
    assert SB.map_overture_category("italian_restaurant", cmap) == "restaurant"  # suffixe
    assert SB.map_overture_category("bank_credit_union", cmap) == "atm"
    assert SB.map_overture_category("grocery_store", cmap) == "supermarket"
    assert SB.map_overture_category("landmark_and_historical_building", cmap) == "sight"
    assert SB.map_overture_category("ski_resort", cmap) == "sport"       # Valais
    assert SB.map_overture_category("car_rental", cmap) == "rental"
    # Rétrocompat : l'ancien slug pointé matche par son DERNIER segment.
    assert SB.map_overture_category("eat_and_drink.restaurant", cmap) == "restaurant"
    # Hors périmètre / prospection / inconnu → None (annexe, jamais tordu).
    assert SB.map_overture_category("holiday_rental_home", cmap) is None
    assert SB.map_overture_category("hospital", cmap) is None
    assert SB.map_overture_category("office", cmap) is None
    assert SB.map_overture_category("", cmap) is None
    assert SB.map_overture_category(None, cmap) is None


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
    cmap = SB.load_category_map()
    overture = [
        _ovt("Catedral de Murcia", "landmark_and_historical_building"),
        _ovt("Catedral Consultores", "office_supply_store"),   # LEURRE (substring "catedral")
        _ovt("Real Casino de Murcia", "landmark_and_historical_building"),
        _ovt("Gran Casino de Ceuta", "casino"),                # LEURRE (substring "casino")
        _ovt("Banco Santander", "bank_credit_union"),
        _ovt("CaixaBank", "bank_credit_union"),
        _ovt("Bitcoin ATM - Shitcoins.club", "atm"),
        _ovt("MUyBICI Estación 12", "bike_rental"),
    ]
    probes = SB.run_probes("murcia", [], overture, cmap)
    answers = {p["question"]: p["answer"] for p in probes}
    # Exact-d'abord : la sonde ne remonte PAS « Catedral Consultores » ni « Gran Casino de Ceuta ».
    cat = next(a for q, a in answers.items() if "Catedral" in q)
    assert cat == "OUI — Catedral de Murcia"
    casino = next(a for q, a in answers.items() if "Casino" in q)
    assert casino == "OUI — Real Casino de Murcia"
    banks = next(a for q, a in answers.items() if "banques nommées" in q)
    assert "2 banque(s) / 1 crypto sur 3 ATM Overture" in banks   # bank_credit_union → atm
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


# ── V2-48b : correctifs révélés par l'exécution réelle (VPS, duckdb 1.5.5) ────

import pytest  # noqa: E402


def test_resolve_release_validates_format_and_autodetects():
    # Valide → passe tel quel.
    assert SB.resolve_release("2026-08-19.0", detector=lambda: "X") == "2026-08-19.0"
    # Format invalide (ancien défaut « AAAA-MM ») → erreur explicite montrant le format.
    with pytest.raises(ValueError) as ei:
        SB.resolve_release("2026-08", detector=lambda: "X")
    assert "AAAA-MM-JJ.N" in str(ei.value) and "2026-08-19.0" in str(ei.value)
    # Absente → détection injectée.
    assert SB.resolve_release(None, detector=lambda: "2026-08-19.0") == "2026-08-19.0"


def test_latest_overture_release_picks_max_valid():
    class _FakeCon:
        def execute(self, sql):
            self._rows = [
                (f"{SB._OVERTURE_S3}/2026-07-16.1/",),
                (f"{SB._OVERTURE_S3}/2026-08-19.0/",),
                (f"{SB._OVERTURE_S3}/latest/",),        # non conforme → ignoré
            ]
            return self
        def fetchall(self):
            return self._rows
        def close(self):
            pass
    assert SB.latest_overture_release(connect=_FakeCon) == "2026-08-19.0"


# ── Exécution RÉELLE de la requête contre DuckDB (leçon V2-48b : suite verte ≠
#    script exécutable ; la recette inclut une invocation réelle) ──────────────

duckdb = pytest.importorskip("duckdb")


def _write_parquet(tmp_path, geom_sql: str) -> str:
    """Écrit un parquet façon Overture (names/categories struct, phones/websites list,
    bbox struct) avec la géométrie produite par `geom_sql`. Renvoie le chemin."""
    pq = str(tmp_path / "places.parquet")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"""
      COPY (SELECT {{'primary': 'Catedral de Murcia'}} AS names,
                   {{'primary': 'attractions_and_activities.landmark'}} AS categories,
                   {geom_sql} AS geometry,
                   ['+34 1'] AS phones, ['http://x'] AS websites,
                   {{'xmin': -1.128, 'ymin': 37.984, 'xmax': -1.128, 'ymax': 37.984}} AS bbox)
      TO '{pq}' (FORMAT PARQUET)""")
    con.close()
    return pq


def _query(pq: str):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        return SB._query_places(con, "x", (-1.2, 37.9, -1.0, 38.1), source=pq)
    finally:
        con.close()


def test_query_places_native_geometry_real_duckdb(tmp_path):
    """geometry NATIVE (duckdb-spatial récent) → ST_X/ST_Y(geometry) direct (le défaut
    corrigé). Round-trip réel contre DuckDB, pas un mock."""
    rows = _query(_write_parquet(tmp_path, "ST_Point(-1.128, 37.984)"))
    assert rows == [("Catedral de Murcia", 37.984, -1.128,
                     "attractions_and_activities.landmark", "+34 1", "http://x")]


def test_query_places_wkb_fallback_real_duckdb(tmp_path):
    """geometry en WKB BLOB (vieux duckdb-spatial) → ST_X(geometry) natif échoue, repli
    ST_GeomFromWKB(geometry). Round-trip réel : le repli renvoie bien la ligne."""
    rows = _query(_write_parquet(tmp_path, "ST_AsWKB(ST_Point(-1.128, 37.984))"))
    assert rows == [("Catedral de Murcia", 37.984, -1.128,
                     "attractions_and_activities.landmark", "+34 1", "http://x")]


# ── V2-48c : mapping plat, santé, sondes resserrées, détection release ────────

def test_mapping_health_guard_fails_loudly_on_broken_mapping():
    # 0 lieu mappé (le désastre V2-48) → MappingHealthError, jamais de grille.
    broken = [{"prop": {"_key": "murcia"}, "mapped_denominator": 100,
               "overture_mapped": 0, "mapped_pct": 0.0}]
    with pytest.raises(SB.MappingHealthError) as ei:
        SB.check_mapping_health(broken)
    assert "murcia" in str(ei.value) and "0" in str(ei.value)
    # Au-dessus du seuil → pas d'erreur.
    ok = [{"prop": {"_key": "murcia"}, "mapped_denominator": 100,
           "overture_mapped": 45, "mapped_pct": 45.0}]
    SB.check_mapping_health(ok)   # ne lève pas


def test_run_terrain_reports_mapping_health():
    prop = {"id": "P", "_key": "murcia", "_label": "M", "city": "Murcia",
            "country_code": "ES", "lat": LAT, "lon": LON}
    cmap = SB.load_category_map()
    overture = [_ovt("Mercadona", "supermarket"),           # mappé
                _ovt("Bar Sol", "bar"),                     # mappé
                _ovt("Mairie", "town_hall"),                # IGNORÉ (hors dénominateur V2-48d)
                _ovt("Truc obscur", "widget_repository"),   # LACUNE (au dénominateur)
                _ovt("Sans catégorie", "")]                 # sans catégorie → hors tout
    r = SB.run_terrain(prop, [], overture, {c: 20000 for c in SB.IN_SCOPE}, cmap)
    assert r["overture_total"] == 5 and r["overture_with_cat"] == 4
    assert r["overture_mapped"] == 2 and r["overture_ignored"] == 1
    assert r["overture_gap"] == 1                           # widget_repository = lacune
    assert r["mapped_denominator"] == 3                     # 4 catégorisés − 1 ignoré
    assert r["mapped_pct"] == round(200 / 3, 1)             # 2 mappés / 3 mappables
    assert "widget_repository" in r["gaps"] and "town_hall" in r["ignored_cats"]


# ── V2-48d : panier « ignore », dénominateur, diagnostic ─────────────────────

def test_classify_category_mapped_ignored_gap():
    cmap = SB.load_category_map()
    assert SB.classify_category("gas_station", cmap) == "mapped"    # lacune V2-48d comblée
    assert SB.classify_category("winery", cmap) == "mapped"         # terrain valaisan
    assert SB.classify_category("spa", cmap) == "mapped"
    assert SB.classify_category("hotel", cmap) == "ignored"
    assert SB.classify_category("beauty_salon", cmap) == "ignored"  # mot-clé « salon »
    assert SB.classify_category("holiday_rental_home", cmap) == "ignored"
    assert SB.classify_category("real_estate_agent", cmap) == "ignored"
    assert SB.classify_category("some_new_shop_type", cmap) == "gap"


def test_diagnostic_report_lists_gaps_not_ignored():
    prop = {"id": "P", "_key": "murcia", "_label": "CASA MURCIA", "city": "Murcia",
            "country_code": "ES", "lat": LAT, "lon": LON}
    cmap = SB.load_category_map()
    overture = ([_ovt("Hotel X", "hotel")] * 3            # ignorés (hors dénominateur)
                + [_ovt("Truc", "widget_repository")] * 2  # lacune (au dénominateur)
                + [_ovt("Bar", "bar")])                    # mappé
    r = SB.run_terrain(prop, [], overture, {c: 20000 for c in SB.IN_SCOPE}, cmap)
    diag = SB.render_diagnostic([r], "2026-08-19.0", "2026-09-11 10:00")
    assert "Diagnostic" in diag and "LACUNES" in diag
    assert "widget_repository" in diag        # la lacune est listée (à mapper)
    assert "hotel" not in diag                # l'ignoré n'encombre pas le diagnostic


def test_exact_first_probe_rejects_substring_lures():
    places = [_ovt("Catedral de Murcia", "x"),
              _ovt("Catedral Consultores", "x"),
              _ovt("Parroquia de la Catedral", "x")]
    hits = SB._name_match_exact_first(places, "Catedral de Murcia")
    assert hits == ["Catedral de Murcia"]                  # exact seul, pas les leurres


def test_release_extraction_from_deep_paths_and_latest():
    p = "s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/type=place/x.parquet"
    assert SB._release_from_path(p) == "2026-08-19.0"
    assert SB._release_from_path("s3://.../release/nope/theme=places/") is None

    class _FakeCon:
        def execute(self, sql):
            assert "theme=places" in sql   # V2-48c : glob PROFOND (pas 'release/*' shallow)
            self._rows = [
                (f"{SB._OVERTURE_S3}/2026-07-16.1/theme=places/type=place/a.parquet",),
                (f"{SB._OVERTURE_S3}/2026-08-19.0/theme=places/type=place/b.parquet",),
                (f"{SB._OVERTURE_S3}/2026-08-19.0/theme=places/type=place/c.parquet",),
            ]
            return self
        def fetchall(self):
            return self._rows
        def close(self):
            pass
    assert SB.latest_overture_release(connect=_FakeCon) == "2026-08-19.0"
