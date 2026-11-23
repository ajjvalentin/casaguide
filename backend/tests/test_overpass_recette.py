"""Recette de l'instance Overpass à clé contre nos données (V2-93c).

`ops/overpass_compare.py` de bout en bout, SANS réseau : un faux serveur Overpass
(`httpx.MockTransport`) rend un monde OSM simulé — réponse de moisson configurable,
éléments par identifiant —, la base est la base de test locale. Clé SENTINELLE : elle ne
doit apparaître nulle part (sortie, fichier JSON, requêtes de `--check-auth`).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
from urllib.parse import parse_qs

import httpx
import psycopg
import pytest

from enrich import overpass
from enrich.settings import settings

from .test_overpass_keyed import (KEY_IN_PATH_URL, KEYED_URL, SENTINEL,  # noqa: F401
                                  _fresh, keyed, sleeps)
from .test_refresh import LAT, LON, ROOT, _poi, _property, owner  # noqa: F401

_IDS_RE = re.compile(r"(node|way|relation)\(id:([\d,]+)\)")


def _load_tool():
    spec = importlib.util.spec_from_file_location("overpass_recette_ops",
                                                  ROOT / "ops" / "overpass_compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(nid, tags, *, dlat=0.0):
    return {"type": "node", "id": nid, "lat": LAT + dlat, "lon": LON, "tags": tags}


class FakeOverpass:
    """Faux serveur : `harvest` = éléments rendus à TOUTE requête `around` (sauf règle
    `by_query`) ; `world` = éléments connus par identifiant (absent ⇒ supprimé)."""

    def __init__(self, harvest=(), world=(), by_query=None):
        self.harvest = list(harvest)
        self.world = {f"{e['type']}/{e['id']}": e for e in world}
        self.by_query = by_query or (lambda q: None)
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        query = parse_qs(req.content.decode())["data"][0]
        special = self.by_query(query)
        if special is not None:
            return special
        if "(id:" in query:
            els = [self.world[f"{t}/{i}"] for t, ids in _IDS_RE.findall(query)
                   for i in ids.split(",") if f"{t}/{i}" in self.world]
        else:
            els = self.harvest
        return httpx.Response(200, json={"osm3s": {"timestamp_osm_base": _fresh()},
                                         "elements": els})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def _ref_id(ref: str) -> int:
    return int(ref.split("/")[1])


def _run(tool, argv, fake, caplog, sleeps):
    caplog.set_level(logging.INFO)
    code = tool.main([*argv, "--env-file", "/nonexistent"], client=fake.client(),
                     sleep=sleeps)
    return code, caplog.text


def _all_counts() -> dict:
    with psycopg.connect(settings.db_dsn) as c:
        tables = [r[0] for r in c.execute(
            """SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'""")]
        return {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}


@pytest.fixture()
def pid(owner):
    return _property(owner)


def _rest(conn, pid, n, name):
    return _poi(conn, pid, source_ref=f"node/{n}", category_code="restaurant", name=name)


# ── 1 : sans clé ─────────────────────────────────────────────────────────────

def test_without_key_refuses_no_request(monkeypatch, caplog, sleeps, pid):
    monkeypatch.setattr(settings, "overpass_api_key", "")
    fake = FakeOverpass()
    for argv in (["--property", pid], ["--all"], ["--check-auth"]):
        code, out = _run(_load_tool(), argv, fake, caplog, sleeps)
        assert code == 2
    assert fake.requests == []
    assert "CASAGUIDE_OVERPASS_API_KEY absente" in out


# ── 2 : chaque classe ────────────────────────────────────────────────────────

def test_each_class_once_and_only_there(keyed, owner, pid, caplog, tmp_path):
    """Six fiches retenues : une retrouvée, puis une par classe. Chacune tombe dans SA
    classe et nulle part ailleurs ; l'inexpliquée est listée avec la requête exacte."""
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 101, "Trouvé")
        _rest(conn, pid, 102, "Supprimé")
        _rest(conn, pid, 103, "Fermé")
        _rest(conn, pid, 104, "Requalifié")
        _rest(conn, pid, 105, "Loin")
        _rest(conn, pid, 106, "Mystère")
    resto = {"amenity": "restaurant"}
    fake = FakeOverpass(
        harvest=[_node(101, {**resto, "name": "Trouvé"})],
        world=[_node(101, {**resto, "name": "Trouvé"}),
               _node(103, {**resto, "name": "Fermé", "disused": "yes"}),
               _node(104, {"amenity": "cafe", "name": "Requalifié"}),
               _node(105, {**resto, "name": "Loin"}, dlat=0.09),      # ~10 km
               _node(106, {**resto, "name": "Mystère"})])
    out_json = tmp_path / "b.json"
    code, out = _run(_load_tool(), ["--property", pid, "--json", str(out_json)], fake,
                     caplog, keyed)
    assert code == 1                                   # un inexpliqué ⇒ anomalie
    rep = json.loads(out_json.read_text())
    recall = rep["properties"][0]["recall"]
    assert (recall["tested"], recall["found"]) == (6, 1)
    assert recall["classes"] == {"supprime": 1, "ferme": 1, "requalifie": 1,
                                 "hors_rayon": 1, "inexplique": 1}
    got = {m["ref"]: m["class"] for m in recall["misses"]}
    assert got == {"node/102": "supprime", "node/103": "ferme", "node/104": "requalifie",
                   "node/105": "hors_rayon", "node/106": "inexplique"}
    mystery = next(m for m in recall["misses"] if m["ref"] == "node/106")
    assert mystery["query"].startswith("[out:json][timeout:")
    assert f"around:5000,{LAT},{LON}" in mystery["query"]
    assert "✗ INEXPLIQUÉ node/106 « Mystère » (restaurant) à 0 m" in out
    assert f"requête : {mystery['query']}" in out


def test_classify_is_pure_and_ordered():
    tool = _load_tool()
    prop = {"lat": LAT, "lon": LON, "country_code": "ES"}
    poi = {"category": "restaurant", "bucket_m": 5000}
    r = {"amenity": "restaurant", "name": "X"}
    assert tool.classify(poi, None, prop) == ("supprime", None)
    # fermé ET requalifié ET loin : la 1re classe du brief l'emporte
    assert tool.classify(poi, _node(1, {"amenity": "cafe", "disused": "yes"}, dlat=0.2),
                         prop)[0] == "ferme"
    assert tool.classify(poi, _node(1, {"amenity": "cafe"}, dlat=0.2), prop)[0] \
        == "requalifie"
    assert tool.classify(poi, _node(1, r, dlat=0.2), prop)[0] == "hors_rayon"
    assert tool.classify(poi, _node(1, r, dlat=0.01), prop)[0] == "inexplique"


def test_recall_plan_reproduces_pipeline_radii():
    """Départ = min(préférence, 5 km) ; au-delà ⇒ escalade au rayon max ; aéroport =
    palier lointain d'emblée. Même texte de requête que `_run_buckets`."""
    tool = _load_tool()
    prop = {"lat": LAT, "lon": LON, "country_code": "ES"}
    cats = [{"code": "restaurant", "default_radius_m": 3000, "max_radius_m": 25000},
            {"code": "airport", "default_radius_m": 100000, "max_radius_m": None}]
    retained = [{"ref": "node/1", "name": "A", "category": "restaurant", "dist_m": 800},
                {"ref": "node/2", "name": "B", "category": "restaurant", "dist_m": 9000},
                {"ref": "node/3", "name": "C", "category": "airport", "dist_m": 60000},
                {"ref": "node/4", "name": "D", "category": "babysitter", "dist_m": 10}]
    plan, untestable = tool.recall_plan(prop, cats, retained)
    got = [(g["stage"], g["bucket_m"], g["codes"], [p["ref"] for p in g["pois"]])
           for g in plan]
    assert got == [("départ", 5000, ["restaurant"], ["node/1"]),
                   ("départ", 100000, ["airport"], ["node/3"]),
                   ("escalade", 25000, ["restaurant"], ["node/2"])]
    assert [u["ref"] for u in untestable] == ["node/4"]
    far = plan[1]
    assert far["query"] == overpass._build_query(
        overpass._selectors_for("airport", "ES"), LAT, LON, 100000,
        timeout_s=settings.overpass_timeout_far_s)


# ── 3 : rappel mesuré sur les éléments BRUTS ─────────────────────────────────

def test_raw_recall_counts_element_dropped_by_selection_cap(keyed, owner, pid, caplog,
                                                            tmp_path):
    """La fiche n'est pas parmi les 8 plus proches (plafond de sélection) : elle est
    absente de la sélection du pipeline mais PRÉSENTE dans la réponse brute ⇒
    retrouvée, aucune classe."""
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 900, "Au-delà du plafond")
    closer = [_node(800 + i, {"amenity": "restaurant", "name": f"Proche {i}"},
                    dlat=0.0001 * i) for i in range(12)]
    target = _node(900, {"amenity": "restaurant", "name": "Au-delà du plafond"},
                   dlat=0.02)
    fake = FakeOverpass(harvest=[*closer, target], world=[*closer, target])
    grouped, _f, _s = overpass.fetch_grouped(
        [{"code": "restaurant", "default_radius_m": 3000, "max_radius_m": 25000}],
        LAT, LON, client=fake.client(), country_code="ES")
    assert "node/900" not in {p["source_ref"] for p in grouped["restaurant"]}
    out_json = tmp_path / "b.json"
    code, _out = _run(_load_tool(), ["--property", pid, "--json", str(out_json)], fake,
                      caplog, keyed)
    recall = json.loads(out_json.read_text())["properties"][0]["recall"]
    assert (recall["tested"], recall["found"]) == (1, 1)
    assert recall["misses"] == [] and sum(recall["classes"].values()) == 0
    assert code == 0


# ── 4 : plafond de requêtes ──────────────────────────────────────────────────

def test_max_requests_stops_cleanly_partial(keyed, owner, pid, caplog, tmp_path):
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 101, "Trouvé")
    resto = _node(101, {"amenity": "restaurant", "name": "Trouvé"})
    fake = FakeOverpass(harvest=[resto], world=[resto])
    out_json = tmp_path / "b.json"
    code, out = _run(_load_tool(), ["--property", pid, "--max-requests", "2",
                                    "--json", str(out_json)], fake, caplog, keyed)
    assert code == 1
    assert len(fake.requests) == 2                     # jamais une de plus
    rep = json.loads(out_json.read_text())
    assert rep["partial"] is True and rep["properties"][0]["partial"] is True
    assert "plafond de 2 requête(s) atteint" in rep["partial_reason"]
    assert rep["totals"]["requests"]["total"] == 2
    assert rep["properties"][0]["recall"]["found"] == 1      # le partiel est gardé
    assert "· PARTIEL" in out and "Bilan global — PARTIEL" in out
    # une forme qu'aucun logement n'a exercée est NOMMÉE
    assert "fetch_by_refs" in rep["forms_not_covered"]
    assert "Formes non couvertes : " in out and "fetch_by_refs" in out


def test_budget_never_exceeded_by_post_overpass(keyed):
    seen = []
    probe = overpass.Probe(on_event=seen.append, budget=1)
    fake = FakeOverpass(by_query=lambda q: httpx.Response(504, text="busy"))
    with overpass.observe(probe), pytest.raises(overpass.OverpassBudgetExhausted):
        overpass._post_overpass(fake.client(), "[out:json];node(1);out;")
    assert len(fake.requests) == 1 and probe.sent == 1 and probe.exhausted
    assert [e["status"] for e in seen] == [504]


# ── 5 : --check-auth ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [KEYED_URL, KEY_IN_PATH_URL])
def test_check_auth_fake_key_one_request_no_leak(keyed, monkeypatch, caplog, capsys, url):
    monkeypatch.setattr(settings, "overpass_url", url)
    tool = _load_tool()

    def deny(q):
        return httpx.Response(401, json={"error": "invalid_api_key",
                                         "message": "unknown key"})
    fake = FakeOverpass(by_query=deny)
    caplog.set_level(logging.DEBUG)
    code = tool.main(["--check-auth", "--env-file", "/nonexistent"], client=fake.client(),
                     sleep=keyed)
    out = caplog.text + capsys.readouterr().out
    assert code == 0
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.headers["x-api-key"] == tool.FAKE_KEY
    assert SENTINEL not in str(req.url) and SENTINEL not in str(req.headers) \
        and SENTINEL not in req.content.decode()
    assert SENTINEL not in out
    assert "OverpassAuthError comme attendu : statut 401, code invalid_api_key" in out
    assert "requête(s) envoyée(s) : 1" in out


def test_check_auth_accepted_fake_key_is_an_anomaly(keyed, caplog):
    fake = FakeOverpass()
    code, out = _run(_load_tool(), ["--check-auth"], fake, caplog, keyed)
    assert code == 1 and len(fake.requests) == 1
    assert "clé factice a été ACCEPTÉE" in out


# ── 6 : lecture seule ────────────────────────────────────────────────────────

def test_no_database_write_any_table(keyed, owner, pid, caplog):
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 101, "Trouvé")
        _rest(conn, pid, 102, "Supprimé")
    resto = _node(101, {"amenity": "restaurant", "name": "Trouvé"})
    fake = FakeOverpass(harvest=[resto], world=[resto])
    before = _all_counts()
    _run(_load_tool(), ["--property", pid], fake, caplog, keyed)
    assert _all_counts() == before


# ── 7 : --json cohérent avec le résumé ───────────────────────────────────────

def test_json_valid_and_consistent_with_summary(keyed, owner, pid, caplog, tmp_path):
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 101, "Trouvé")
        _rest(conn, pid, 102, "Supprimé")
        _poi(conn, pid, source_ref="node/201", category_code="beach", name="Playa")
    resto = _node(101, {"amenity": "restaurant", "name": "Trouvé"})

    def no_nature(q):        # `fetch_natural_places` ne rend rien
        if "natural~" in q:
            return httpx.Response(200, json={"osm3s": {"timestamp_osm_base": _fresh()},
                                             "elements": []})
        return None
    fake = FakeOverpass(harvest=[resto], world=[resto], by_query=no_nature)
    out_json = tmp_path / "b.json"
    code, out = _run(_load_tool(), ["--property", pid, "--json", str(out_json)], fake,
                     caplog, keyed)
    text = out_json.read_text()
    rep = json.loads(text)
    t, p = rep["totals"], rep["properties"][0]
    assert SENTINEL not in text and SENTINEL not in out
    assert f"Rappel : {t['found']}/{t['tested']} fiche(s) retrouvée(s)" in out
    assert f"rappel {t['found']}/{t['tested']}" in out
    assert (f"Requêtes : {t['requests']['total']} (moisson {t['requests']['moisson']}, "
            f"revérification {t['requests']['revérification']})") in out
    assert t["requests"]["total"] == len(fake.requests) == len(p["requests_log"])
    assert t["classes"]["supprime"] == 2 and "· supprime node/102" in out
    # la plage retenue n'est pas rendue par `fetch_natural_places` : zéro suspect
    natural = next(f for f in p["forms"] if f["form"] == "fetch_natural_places")
    assert natural["verdict"] == "anomalie" and natural["expected"] == 1
    assert t["form_anomalies"] == sum(f["verdict"] == "anomalie" for f in p["forms"])
    assert f"Formes en anomalie : {t['form_anomalies']}" in out
    assert code == 1
    for f in p["forms"]:
        if f["verdict"] != "non applicable":
            assert f["stamp"] in out
    assert p["freshness"]["max_lag_s"] is not None


# ── Couverture des formes ────────────────────────────────────────────────────

def test_coverage_all_forms_remark_and_not_covered(keyed, owner, pid, caplog, tmp_path):
    """Toutes les formes s'exécutent (escalade comprise : seules les sorties abondent) ;
    un `remark` d'erreur sur l'une est une anomalie."""
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _rest(conn, pid, 101, "Trouvé")

    def remark_on_sorties(q):
        if "amenity\"~\"^(restaurant|bar|pub|cafe)$\"][\"name\"]" in q:
            return httpx.Response(200, json={
                "osm3s": {"timestamp_osm_base": _fresh()}, "elements": [],
                "remark": "runtime error: Query timed out in \"query\" at line 1"})
        return None
    many = [_node(300 + i, {"amenity": "restaurant", "name": f"R{i}"}) for i in range(60)]
    resto = _node(101, {"amenity": "restaurant", "name": "Trouvé"})
    fake = FakeOverpass(harvest=[resto, *many], world=[resto], by_query=remark_on_sorties)
    out_json = tmp_path / "b.json"
    code, out = _run(_load_tool(), ["--property", pid, "--json", str(out_json)], fake,
                     caplog, keyed)
    rep = json.loads(out_json.read_text())
    forms = {f["form"]: f for f in rep["properties"][0]["forms"]}
    fams = {f["family"] for f in forms.values()}
    assert {"fetch_grouped · départ", "fetch_grouped · lointain", "fetch_category",
            "fetch_named_sorties", "fetch_natural_places", "nearby_neighborhoods",
            "fetch_by_refs"} <= fams
    assert forms["fetch_named_sorties"]["verdict"] == "anomalie"
    assert "remark : runtime error: Query timed out" in out
    assert rep["forms_not_covered"] == []
    assert "Formes non couvertes" not in out
    assert code == 1


# ── Sélection --all ──────────────────────────────────────────────────────────

def test_select_properties_published_non_demo_guest_optional(owner):
    tool = _load_tool()
    normal = _property(owner)
    demo = _property(owner, demo=True)
    draft = _property(owner, status="draft")
    guest = _property(owner, guest_guide=True)
    from enrich import db
    with db.connect() as conn:
        base = set(tool.select_properties(conn, include_guest=False))
        wide = set(tool.select_properties(conn, include_guest=True))
        conn.rollback()
    assert normal in base and guest not in base and guest in wide
    assert demo not in wide and draft not in wide
