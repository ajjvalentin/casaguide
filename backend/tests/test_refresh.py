"""Revérification planifiée des lieux (V2-93) : tâche `enrich/refresh.py`,
script `ops/refresh_pois.py`, parité avec `db.upsert_pois`, `repo.edit_poi`.

Contre la base réelle (déclencheurs PostgreSQL) ; sources FACTICES injectées (aucun
réseau). Les passages de la tâche sont toujours restreints aux logements du test
(`property_id`, ou `due_properties` remplacé) : jamais un logement préexistant.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import psycopg
import pytest

from api import repo
from enrich import db, journal, refresh
from enrich.locks import REFRESH_FIELDS, plan_refresh
from enrich.settings import settings

ROOT = Path(__file__).resolve().parents[2]
LAT, LON = 37.928, -0.7482


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def owner():
    oid = str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as c:
        c.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'R')",
                  (oid, f"{oid}@test.local"))
        c.commit()
    yield oid
    with psycopg.connect(settings.db_dsn) as c:
        c.execute("DELETE FROM owners WHERE id = %s", (oid,))
        c.commit()


def _property(owner_id, *, status="published", guest_guide=False, demo=False) -> str:
    pid = str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as c:
        c.execute("""INSERT INTO properties (id, owner_id, name, address_line1, city,
                         country_code, status, guest_guide, demo, geom)
                     VALUES (%s, %s, 'Villa Refresh', 'Calle 1', 'Orihuela Costa', 'ES',
                             %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
                  (pid, owner_id, status, guest_guide, demo, LON, LAT))
        c.commit()
    return pid


@pytest.fixture()
def prop(owner):
    return _property(owner)


@pytest.fixture()
def conn(prop):
    with db.connect() as c:
        yield c
        c.rollback()


def _poi(conn, pid, *, status="approved", source="osm", source_ref=None,
         human_fields=(), **cols) -> str:
    base = {"category_code": "restaurant", "name": "Casa Pepe", "phone": "+34 1",
            "website": "https://casa.example", "opening_hours": "Mo-Fr 09:00-18:00",
            "address": "Calle 2", "description_md": "Paella au feu de bois.",
            "owner_comment": "Notre cantine", "dist_walk_m": 300, "walk_min": 4}
    base.update(cols)
    if source_ref is None and source in ("osm", "overture"):
        source_ref = (f"node/{uuid.uuid4().int % 10**9}" if source == "osm"
                      else f"gers:{uuid.uuid4()}")
    names = ", ".join(base)
    ph = ", ".join(f"%({k})s" for k in base)
    poi_id = str(conn.execute(
        f"""INSERT INTO pois (property_id, geom, status, source, source_ref,
                              human_fields, {names})
            VALUES (%(pid)s, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                    %(status)s, %(source)s, %(ref)s, %(hf)s, {ph}) RETURNING id""",
        {"pid": pid, "status": status, "source": source, "ref": source_ref,
         "hf": list(human_fields), "lon": LON, "lat": LAT, **base}).fetchone()["id"])
    conn.commit()
    return poi_id


def _row(conn, poi_id) -> dict:
    return conn.execute("SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM pois "
                        "WHERE id = %s", (poi_id,)).fetchone()


def _ref(conn, poi_id) -> str:
    return _row(conn, poi_id)["source_ref"]


def _changes(conn, poi_id=None, pid=None) -> list[dict]:
    if poi_id:
        return conn.execute("SELECT * FROM content_changes WHERE entity_id = %s "
                            "AND action <> 'create' ORDER BY id", (poi_id,)).fetchall()
    return conn.execute("SELECT * FROM content_changes WHERE property_id = %s "
                        "AND action <> 'create' ORDER BY id", (pid,)).fetchall()


class FakeOSM:
    """Source OSM factice : `{ref: {state, phone, website, opening_hours}}` ; une ref
    absente est « disparue » ; `fail` lève (panne réseau)."""

    def __init__(self, answers: dict | None = None, *, fail: bool = False):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def __call__(self, refs):
        self.calls.append(list(refs))
        if self.fail:
            raise RuntimeError("Overpass injoignable (test)")
        out = {}
        for r in refs:
            a = self.answers.get(r)
            out[r] = ({"state": "missing", "phone": None, "website": None,
                       "opening_hours": None} if a is None else
                      {"state": "ok", **{f: None for f in REFRESH_FIELDS}, **a})
        return out


def _same(conn, poi_id) -> dict:
    """Réponse source identique à la fiche (rien n'a changé)."""
    r = _row(conn, poi_id)
    return {f: r[f] for f in REFRESH_FIELDS}


def _run(conn, pid, osm, ovt=None, **kw):
    return refresh.refresh_property(conn, pid, osm_fetch=osm,
                                    overture_fetch=ovt or (lambda *_: None), **kw)


# ── 2-6 : report, verrou, source muette, rien n'a changé ──────────────────────

def test_hours_changed_at_source_updates_and_journals_as_system(conn, prop):
    """Test 2 : fiche approved non verrouillée, horaire changé à la source ⇒ champ mis
    à jour, UNE ligne de journal `update` d'auteur `system`, avec lot et job."""
    poi = _poi(conn, prop)
    osm = FakeOSM({_ref(conn, poi): {**_same(conn, poi),
                                     "opening_hours": "Mo-Sa 08:00-20:00"}})
    rep = _run(conn, prop, osm)
    assert rep.ok and rep.modified == 1 and rep.modified_by_field == {"opening_hours": 1}
    row = _row(conn, poi)
    assert row["opening_hours"] == "Mo-Sa 08:00-20:00"
    assert row["status"] == "approved" and row["source_state"] == "ok"
    assert row["last_checked_at"] is not None
    (ch,) = _changes(conn, poi)
    assert (ch["action"], ch["field"], ch["actor"]) == ("update", "opening_hours", "system")
    assert (ch["old_value"], ch["new_value"]) == ("Mo-Fr 09:00-18:00", "Mo-Sa 08:00-20:00")
    assert str(ch["batch_id"]) == rep.batch_id and str(ch["job_id"]) == rep.job_id
    job = conn.execute("SELECT trigger, status, steps FROM enrichment_jobs WHERE id = %s",
                       (rep.job_id,)).fetchone()
    assert (job["trigger"], job["status"]) == ("refresh", "done")
    assert job["steps"]["refresh"]["checked"] == 1
    assert job["steps"]["refresh"]["modified_by_field"] == {"opening_hours": 1}


def test_locked_field_not_written_counted_locked(conn, prop):
    """Test 3 : même cas, champ dans `human_fields` ⇒ rien n'est écrit, compté
    « verrouillé »."""
    poi = _poi(conn, prop, human_fields=["opening_hours"])
    osm = FakeOSM({_ref(conn, poi): {**_same(conn, poi),
                                     "opening_hours": "Mo-Sa 08:00-20:00"}})
    rep = _run(conn, prop, osm)
    assert rep.modified == 0 and rep.locked_skipped == 1
    assert _row(conn, poi)["opening_hours"] == "Mo-Fr 09:00-18:00"
    assert _changes(conn, poi) == []


def test_field_cleared_by_owner_stays_empty(conn, prop):
    """Test 4 : champ vidé par le propriétaire (edit_poi) puis source renseignée ⇒
    reste vide."""
    poi = _poi(conn, prop)
    repo.edit_poi(conn, prop, poi, {"phone": ""})
    conn.commit()
    assert _row(conn, poi)["phone"] is None
    osm = FakeOSM({_ref(conn, poi): {**_same(conn, poi), "phone": "+34 777"}})
    rep = _run(conn, prop, osm)
    assert _row(conn, poi)["phone"] is None and rep.locked_skipped == 1


def test_silent_source_keeps_value(conn, prop):
    """Test 5 : source muette ⇒ la valeur en base est conservée."""
    poi = _poi(conn, prop)
    osm = FakeOSM({_ref(conn, poi): {"phone": None, "website": "",
                                     "opening_hours": None}})
    rep = _run(conn, prop, osm)
    row = _row(conn, poi)
    assert (row["phone"], row["website"], row["opening_hours"]) == (
        "+34 1", "https://casa.example", "Mo-Fr 09:00-18:00")
    assert rep.modified == 0 and rep.checked == 1


def test_nothing_changed_only_check_columns_written(conn, prop):
    """Test 6 : rien n'a changé ⇒ aucune écriture hormis les colonnes de contrôle ;
    aucune ligne de journal ; `updated_at` intact."""
    poi = _poi(conn, prop)
    before = _row(conn, poi)
    rep = _run(conn, prop, FakeOSM({_ref(conn, poi): _same(conn, poi)}))
    after = _row(conn, poi)
    assert rep.modified == 0 and rep.checked == 1
    assert _changes(conn, poi) == []
    assert after["last_checked_at"] is not None and after["source_state"] == "ok"
    ignore = {"last_checked_at", "source_state", "source_state_since"}
    assert {k: v for k, v in after.items() if k not in ignore} == \
        {k: v for k, v in before.items() if k not in ignore}


# ── 7-9 : fermé, disparu, état stable ────────────────────────────────────────

def _served(conn, prop, poi) -> bool:
    return any(str(p["id"]) == poi for p in repo.guide_pois(conn, prop))


def test_closed_at_source_recorded_never_removed(conn, prop):
    """Test 7 : élément fermé (`is_closed`) ⇒ `source_state='closed'`,
    `source_state_since` posé, fiche toujours approved et servie ; rien n'est reporté
    depuis un élément fermé."""
    poi = _poi(conn, prop)
    osm = FakeOSM({_ref(conn, poi): {"opening_hours": "closed", "phone": "+34 9"}})
    osm.answers[_ref(conn, poi)]["state"] = "closed"
    rep = _run(conn, prop, osm)
    row = _row(conn, poi)
    assert rep.closed == 1 and rep.modified == 0
    assert row["source_state"] == "closed" and row["source_state_since"] is not None
    assert row["status"] == "approved" and row["opening_hours"] == "Mo-Fr 09:00-18:00"
    assert _served(conn, prop, poi)


def test_missing_at_source_recorded_never_removed(conn, prop):
    """Test 8 : élément disparu ⇒ `source_state='missing'`, même garantie."""
    poi = _poi(conn, prop)
    rep = _run(conn, prop, FakeOSM({}))
    row = _row(conn, poi)
    assert rep.missing == 1
    assert row["source_state"] == "missing" and row["source_state_since"] is not None
    assert row["status"] == "approved" and _served(conn, prop, poi)


def test_unchanged_state_keeps_since(conn, prop):
    """Test 9 : état inchangé au passage suivant ⇒ `source_state_since` ne bouge pas ;
    un changement d'état le repose."""
    poi = _poi(conn, prop)
    _run(conn, prop, FakeOSM({}))
    since = _row(conn, poi)["source_state_since"]
    first_check = _row(conn, poi)["last_checked_at"]
    _run(conn, prop, FakeOSM({}))
    row = _row(conn, poi)
    assert row["source_state_since"] == since
    assert row["last_checked_at"] > first_check
    _run(conn, prop, FakeOSM({_ref(conn, poi): _same(conn, poi)}))
    row = _row(conn, poi)
    assert row["source_state"] == "ok" and row["source_state_since"] > since


# ── 10-12 : périmètre ────────────────────────────────────────────────────────

def test_guest_guide_and_demo_never_selected(conn, owner, prop):
    """Test 10 : guide voyageur, démonstration et brouillon ⇒ jamais sélectionnés,
    même nommés explicitement ; le logement publié l'est."""
    guest = _property(owner, guest_guide=True)
    demo = _property(owner, demo=True)
    draft = _property(owner, status="draft")
    for pid in (prop, guest, demo, draft):
        _poi(conn, pid)
    due = refresh.due_properties(conn)
    assert prop in due and not {guest, demo, draft} & set(due)
    for pid in (guest, demo, draft):
        assert refresh.due_properties(conn, property_id=pid) == []
        osm = FakeOSM()
        assert refresh.run(conn, property_id=pid, osm_fetch=osm) == []
        assert osm.calls == []


def test_recently_checked_property_not_due(conn, prop):
    """Sélection : contrôlée il y a moins de CASAGUIDE_REFRESH_MAX_AGE_DAYS ⇒ pas due
    (sauf passage à la main `--property`) ; plus ancienne ⇒ due."""
    poi = _poi(conn, prop)
    _run(conn, prop, FakeOSM({_ref(conn, poi): _same(conn, poi)}))
    assert prop not in refresh.due_properties(conn)
    assert refresh.due_properties(conn, property_id=prop) == [prop]
    conn.execute("UPDATE pois SET last_checked_at = now() - interval '8 days' "
                 "WHERE id = %s", (poi,))
    conn.commit()
    assert prop in refresh.due_properties(conn)


def test_non_recheckable_sources_counted_untouched(conn, prop):
    """Test 11 : fiches `web` / `claude` / `owner` ⇒ comptées non revérifiables,
    intouchées (aucune requête source, aucune colonne de contrôle)."""
    ids = [_poi(conn, prop, source="web", source_ref="web:reputed:casa"),
           _poi(conn, prop, source="claude", source_ref="claude:babysitter:x"),
           _poi(conn, prop, source="owner", source_ref=None)]
    osm_poi = _poi(conn, prop)
    osm = FakeOSM({_ref(conn, osm_poi): _same(conn, osm_poi)})
    rep = _run(conn, prop, osm)
    assert rep.not_recheckable == 3 and rep.checked == 1
    assert osm.calls == [[_ref(conn, osm_poi)]]
    for i in ids:
        r = _row(conn, i)
        assert r["last_checked_at"] is None and r["source_state"] is None


def test_rejected_untouched_by_task_and_upsert(conn, prop):
    """Test 12 : fiche `rejected` ⇒ intouchée, par la tâche et par `upsert_pois`."""
    poi = _poi(conn, prop, status="rejected")
    ref = _ref(conn, poi)
    before = _row(conn, poi)
    osm = FakeOSM({ref: {"phone": "+34 NEW", "opening_hours": "24/7"}})
    rep = _run(conn, prop, osm)
    assert rep.checked == 0 and osm.calls == []
    db.upsert_pois(conn, prop, "restaurant", [{
        "name": "Autre nom", "lat": LAT + 0.01, "lon": LON, "source": "osm",
        "source_ref": ref, "phone": "+34 NEW", "opening_hours": "24/7",
        "website": "https://new.example"}])
    conn.commit()
    after = _row(conn, poi)
    # `updated_at` : l'upsert réécrit la ligne à l'identique (comportement antérieur à
    # V2-93, sans effet sur le contenu) — tout le reste est strictement intact.
    assert {k: v for k, v in after.items() if k != "updated_at"} == \
        {k: v for k, v in before.items() if k != "updated_at"}
    assert _changes(conn, poi) == []


# ── 13-14 : échec isolé, dry-run ─────────────────────────────────────────────

def _load_ops():
    spec = importlib.util.spec_from_file_location("refresh_pois_ops",
                                                  ROOT / "ops" / "refresh_pois.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_network_failure_isolated_job_failed_exit_nonzero(conn, owner, prop,
                                                          monkeypatch):
    """Test 13 : échec réseau sur un logement ⇒ job `failed` avec sa cause, les autres
    logements traités, `last_checked_at` non posé, code de sortie non nul."""
    other = _property(owner)
    bad = _poi(conn, prop)
    good = _poi(conn, other)
    bad_ref, good_ref = _ref(conn, bad), _ref(conn, good)

    def osm(refs):
        if bad_ref in refs:
            raise RuntimeError("HTTP 504 de overpass.test")
        return {good_ref: {"state": "ok", **_same(conn, good), "phone": "+34 2"}}

    monkeypatch.setattr(refresh, "due_properties", lambda *_a, **_k: [prop, other])
    code = _load_ops().main(["--env-file", "/nonexistent"], osm_fetch=osm,
                            overture_fetch=lambda *_: None)
    assert code == 1
    assert _row(conn, bad)["last_checked_at"] is None
    assert _row(conn, good)["phone"] == "+34 2"
    jobs = {str(r["property_id"]): r for r in conn.execute(
        "SELECT property_id, status, error FROM enrichment_jobs "
        "WHERE property_id IN (%s, %s) AND trigger = 'refresh'", (prop, other))}
    assert jobs[prop]["status"] == "failed" and "504" in jobs[prop]["error"]
    assert jobs[other]["status"] == "done"


def test_dry_run_writes_nothing_same_report(conn, prop):
    """Test 14 : `--dry-run` ⇒ aucune écriture (ni fiche, ni job, ni journal) ; bilan
    identique à l'exécution réelle qui suit."""
    a = _poi(conn, prop)
    b = _poi(conn, prop, human_fields=["phone"])
    c = _poi(conn, prop)
    _poi(conn, prop, source="web", source_ref="web:reputed:x")
    answers = {_ref(conn, a): {**_same(conn, a), "website": "https://neuf.example"},
               _ref(conn, b): {**_same(conn, b), "phone": "+34 LOCK"}}
    before = {i: _row(conn, i) for i in (a, b, c)}
    jobs0 = conn.execute("SELECT count(*) AS n FROM enrichment_jobs WHERE property_id = %s",
                         (prop,)).fetchone()["n"]
    dry = _run(conn, prop, FakeOSM(answers), dry_run=True)
    assert {i: _row(conn, i) for i in (a, b, c)} == before
    assert conn.execute("SELECT count(*) AS n FROM enrichment_jobs WHERE property_id = %s",
                        (prop,)).fetchone()["n"] == jobs0
    assert _changes(conn, pid=prop) == [] and dry.job_id is None
    real = _run(conn, prop, FakeOSM(answers))
    assert dry.steps() == real.steps()
    assert dry.steps() == {"checked": 3, "modified": 1,
                           "modified_by_field": {"website": 1}, "closed": 0,
                           "missing": 1, "locked_skipped": 1, "not_recheckable": 1,
                           "not_checked": 0}
    assert dry.changes == real.changes


def test_overture_cache_lookup_and_out_of_cache(conn, prop):
    """Overture : relu par identifiant dans le cache local ; hors zone en cache ⇒
    fiche « non contrôlée » (rien n'est écrit, `last_checked_at` reste NULL)."""
    ovt_poi = _poi(conn, prop, source="overture", opening_hours=None)
    ref = _ref(conn, ovt_poi)
    rep = _run(conn, prop, FakeOSM(), ovt=lambda refs, lat, lon: None)
    assert rep.not_checked == 1 and rep.checked == 0
    assert _row(conn, ovt_poi)["last_checked_at"] is None
    seen = {}

    def ovt(refs, lat, lon):
        seen["args"] = (list(refs), round(lat, 3), round(lon, 3))
        return {ref: {"state": "ok", "phone": "+34 OVT", "website": None,
                      "opening_hours": None}}
    rep = _run(conn, prop, FakeOSM(), ovt=ovt)
    assert seen["args"] == ([ref], round(LAT, 3), round(LON, 3))
    row = _row(conn, ovt_poi)
    assert row["phone"] == "+34 OVT" and row["website"] == "https://casa.example"
    assert row["source_state"] == "ok" and rep.checked == 1


def test_overture_fetch_cached_by_gers_reads_local_cache_only(tmp_path):
    """`overture.fetch_cached_by_gers` : zone couverte ⇒ lecture du parquet local
    (connexion injectée) ; zone non couverte ⇒ None, AUCUNE connexion (jamais S3)."""
    from enrich import overture
    (tmp_path / "z.parquet").write_bytes(b"x")
    (tmp_path / "z.json").write_text('{"release": "r"}')
    zones = [{"id": "z", "name": "z", "bbox": [-2.0, 36.0, 1.0, 40.0]}]
    calls = []

    class Con:
        def execute(self, sql, params):
            calls.append((sql, params))
            return self

        def fetchall(self):
            return [("a1", "+34 1", ""), ]

        def close(self):
            pass

    out = overture.fetch_cached_by_gers(["gers:a1", "gers:b2", "node/1"], LAT, LON,
                                        connect=Con, zones=zones, cache_dir=tmp_path)
    assert out == {"gers:a1": {"state": "ok", "phone": "+34 1", "website": None,
                               "opening_hours": None},
                   "gers:b2": {"state": "missing", "phone": None, "website": None,
                               "opening_hours": None}}
    assert "z.parquet" in calls[0][0] and calls[0][1] == [["a1", "b2"]]
    calls.clear()
    far = overture.fetch_cached_by_gers(["gers:a1"], 48.85, 2.35, connect=Con,
                                        zones=zones, cache_dir=tmp_path)
    assert far is None and calls == []


# ── 15 : parité upsert_pois ↔ plan_refresh ───────────────────────────────────

_MATRIX = [  # (courant, source, verrouillé)
    (cur, src, locked)
    for cur in (None, "+34 1")
    for src in (None, "", "   ", "+34 1", "+34 2")
    for locked in (False, True)
]


@pytest.mark.parametrize("field", REFRESH_FIELDS)
def test_upsert_matches_plan_refresh_on_retained(conn, prop, field):
    """Test 15 : sur une fiche retenue, le SQL de l'upsert et `plan_refresh` donnent le
    même résultat sur la même matrice ; nom, adresse, position, description, coup de
    cœur, distances, catégorie, statut intouchés."""
    for status in ("approved", "edited"):
        for cur, src, locked in _MATRIX:
            poi = _poi(conn, prop, status=status, **{field: cur},
                       human_fields=[field] if locked else [])
            before = _row(conn, poi)
            harvested = {"name": "Nom OSM", "lat": LAT + 0.01, "lon": LON + 0.01,
                         "address": "Autre rue", "description_md": "Autre texte",
                         "owner_comment": "Autre", "dist_walk_m": 9999, "walk_min": 99,
                         "source": "osm", "source_ref": before["source_ref"],
                         "phone": "+34 fusion", "website": "https://fusion",
                         "opening_hours": "fusion",
                         "source_values": {f: (src if f == field else None)
                                           for f in REFRESH_FIELDS}}
            db.upsert_pois(conn, prop, "bar", [harvested])
            conn.commit()
            after = _row(conn, poi)
            plan = plan_refresh(before, {field: src})
            expected = plan[0].new if plan else cur
            assert after[field] == expected, (status, cur, src, locked)
            for k in ("name", "address", "lat", "lon", "description_md", "owner_comment",
                      "dist_walk_m", "walk_min", "category_code", "status",
                      "human_fields", *(f for f in REFRESH_FIELDS if f != field)):
                assert after[k] == before[k], (k, status, cur, src, locked)


def test_upsert_secondary_fill_never_reaches_retained(conn, prop):
    """Le téléphone qu'Overture a comblé sur un POI OSM moissonné (OSM muet) n'atteint
    pas une fiche retenue : la source d'origine est muette ⇒ rien ne change."""
    poi = _poi(conn, prop, phone="+34 web")
    db.upsert_pois(conn, prop, "restaurant", [{
        "name": "Casa Pepe", "lat": LAT, "lon": LON, "source": "osm",
        "source_ref": _ref(conn, poi), "phone": "+34 ovt",
        "completion_meta": {"_overture": {"fields": ["phone"]}}}])
    conn.commit()
    assert _row(conn, poi)["phone"] == "+34 web"


def test_upsert_suggested_unchanged_behaviour(conn, prop):
    """Non-régression : une fiche `suggested` suit toujours la moisson entière."""
    poi = _poi(conn, prop, status="suggested")
    db.upsert_pois(conn, prop, "restaurant", [{
        "name": "Nouveau", "lat": LAT, "lon": LON, "source": "osm",
        "source_ref": _ref(conn, poi), "phone": None, "website": "https://n",
        "opening_hours": None}])
    conn.commit()
    r = _row(conn, poi)
    assert (r["name"], r["phone"], r["website"], r["opening_hours"]) == (
        "Nouveau", None, "https://n", None)


# ── 16 : retour arrière de bout en bout ──────────────────────────────────────

def test_revert_batch_restores_then_next_pass_reapplies(conn, prop):
    """Test 16 : la tâche modifie un horaire, `revert_batch` sur son lot le restaure ;
    au passage suivant la tâche le remodifie (attendu : la source dit toujours la
    nouvelle valeur ; pour figer l'ancienne, le propriétaire la saisit — verrou)."""
    poi = _poi(conn, prop)
    osm = FakeOSM({_ref(conn, poi): {**_same(conn, poi), "opening_hours": "24/7"}})
    rep = _run(conn, prop, osm)
    assert _row(conn, poi)["opening_hours"] == "24/7"
    res = journal.revert_batch(conn, rep.batch_id, actor="support")
    conn.commit()
    assert [r.outcome for r in res] == ["reverted"]
    assert _row(conn, poi)["opening_hours"] == "Mo-Fr 09:00-18:00"
    rep2 = _run(conn, prop, osm)
    assert rep2.modified_by_field == {"opening_hours": 1}
    assert _row(conn, poi)["opening_hours"] == "24/7"


# ── 17 : edit_poi ────────────────────────────────────────────────────────────

def test_edit_poi_clear_locks_null_to_null_does_not(conn, prop):
    """Test 17a : champ vidé ⇒ NULL + verrou ; NULL → NULL (champ jamais rempli que le
    formulaire renvoie) ⇒ aucun verrou."""
    poi = _poi(conn, prop, cuisine=None)
    repo.edit_poi(conn, prop, poi, {"website": None, "cuisine": None,
                                    "name": "Casa Pepe"})
    conn.commit()
    r = _row(conn, poi)
    assert r["website"] is None and r["human_fields"] == ["website"]
    assert r["status"] == "edited"
    repo.edit_poi(conn, prop, poi, {"phone": "  "})
    conn.commit()
    r = _row(conn, poi)
    assert r["phone"] is None and r["human_fields"] == ["phone", "website"]


def test_edit_poi_without_change_keeps_status_journal_translations(conn, prop):
    """Test 17b : enregistrement sans changement ⇒ statut, status_by, status_at,
    journal et traductions inchangés."""
    poi = _poi(conn, prop, cuisine=None)
    conn.execute("UPDATE pois SET status_by = 'owner', status_at = now() - interval '3 days' "
                 "WHERE id = %s", (poi,))
    conn.execute("INSERT INTO poi_translations (poi_id, lang, description_md, is_stale) "
                 "VALUES (%s, 'en', 'Wood-fired paella.', FALSE)", (poi,))
    conn.commit()
    before = _row(conn, poi)
    n0 = len(_changes(conn, pid=prop))
    form = {"category_code": "restaurant", "name": "Casa Pepe", "locality": None,
            "phone": "+34 1", "website": "https://casa.example", "cuisine": None,
            "description_md": "Paella au feu de bois.", "owner_comment": "Notre cantine"}
    out = repo.edit_poi(conn, prop, poi, form)
    conn.commit()
    assert out["status"] == "approved"
    assert _row(conn, poi) == before
    assert len(_changes(conn, pid=prop)) == n0
    assert conn.execute("SELECT is_stale FROM poi_translations WHERE poi_id = %s",
                        (poi,)).fetchone()["is_stale"] is False


@pytest.mark.parametrize("field", ["name", "category_code"])
def test_edit_poi_refuses_emptying_required(conn, prop, field):
    """`name` / `category_code` ne peuvent pas être vidés : refus propre, rien écrit."""
    poi = _poi(conn, prop)
    before = _row(conn, poi)
    with pytest.raises(repo.PoiEditError):
        repo.edit_poi(conn, prop, poi, {field: ""})
    conn.rollback()
    assert _row(conn, poi) == before


# ── Divers ───────────────────────────────────────────────────────────────────

def test_refresh_jobs_do_not_consume_enrichment_quota(conn, prop):
    """Un job `refresh` (système) ne compte pas dans le quota mensuel du propriétaire."""
    poi = _poi(conn, prop)
    n0 = repo.count_jobs_current_month(conn, prop)
    _run(conn, prop, FakeOSM({_ref(conn, poi): _same(conn, poi)}))
    assert repo.count_jobs_current_month(conn, prop) == n0


def test_refresh_makes_no_ai_cost(conn, prop):
    """Aucun appel IA : un passage n'écrit aucune ligne `api_costs`."""
    poi = _poi(conn, prop)
    rep = _run(conn, prop, FakeOSM({_ref(conn, poi): {"phone": "+34 5"}}))
    assert conn.execute("SELECT count(*) AS n FROM api_costs WHERE job_id = %s",
                        (rep.job_id,)).fetchone()["n"] == 0


def test_migration_044_and_schema_declare_check_columns():
    """Migration 044 et schema.sql portent les trois colonnes et la contrainte."""
    for path in (ROOT / "db" / "migrations" / "044_poi_source_check.sql",
                 ROOT / "db" / "schema.sql"):
        t = path.read_text()
        for col in ("last_checked_at", "source_state", "source_state_since",
                    "pois_source_state_check", "idx_pois_refresh_due"):
            assert col in t, (path, col)


def test_timer_has_explicit_timezone():
    """Piège connu : un `OnCalendar` sans fuseau suit l'heure du serveur."""
    t = (ROOT / "ops" / "casaguide-refresh-pois.timer").read_text()
    assert "OnCalendar=*-*-* 02:15:00 Europe/Madrid" in t


# ── Overpass par identifiant (transport simulé) ──────────────────────────────

import datetime as _dt  # noqa: E402

import httpx  # noqa: E402

from enrich import overpass  # noqa: E402


def _fresh() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _overpass_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def fast_overpass(monkeypatch):
    monkeypatch.setattr(settings, "politeness_delay_s", 0)
    monkeypatch.setattr(settings, "overpass_backoff_s", 0)
    monkeypatch.setattr(settings, "overpass_url", "https://primary.test/api/interpreter")
    monkeypatch.setattr(settings, "overpass_mirrors", ("https://mirror.test/api/interpreter",))


def test_fetch_by_refs_states_normalisation_and_batches(fast_overpass):
    """Par identifiant, par paquets : `ok` (valeurs normalisées comme à la moisson —
    `contact:phone` repris), `closed` (`is_closed`), `missing` (absent de la réponse),
    élément sans nom toujours « existant »."""
    queries = []
    els = {
        ("node", 1): {"tags": {"name": "A", "contact:phone": "+34 1",
                               "opening_hours": "Mo-Fr 09:00-14:00"}, "lat": 1, "lon": 1},
        ("way", 2): {"tags": {"name": "B", "disused": "yes"}, "center": {"lat": 1, "lon": 1}},
        ("relation", 3): {"tags": {"website": "https://c.example"},
                          "center": {"lat": 1, "lon": 1}},
    }

    def handler(req):
        q = dict(httpx.QueryParams(req.content.decode()))["data"]
        queries.append(q)
        out = []
        for (t, i), el in els.items():
            if f"{t}(id:" in q and str(i) in q.split(f"{t}(id:")[1].split(")")[0].split(","):
                out.append({"type": t, "id": i, **el})
        return httpx.Response(200, json={"osm3s": {"timestamp_osm_base": _fresh()},
                                         "elements": out})

    with _overpass_client(handler) as client:
        res = overpass.fetch_by_refs(["node/1", "way/2", "relation/3", "node/4",
                                      "web:x"], client=client, batch_size=2)
    assert len(queries) == 2 and all(q.endswith("out center tags;") for q in queries)
    assert res["node/1"] == {"state": "ok", "phone": "+34 1", "website": None,
                             "opening_hours": "Mo-Fr 09:00-14:00"}
    assert res["way/2"]["state"] == "closed"
    assert res["relation/3"] == {"state": "ok", "phone": None,
                                 "website": "https://c.example", "opening_hours": None}
    assert res["node/4"]["state"] == "missing"
    assert "web:x" not in res


def test_fetch_by_refs_refuses_stale_mirror_and_falls_back(fast_overpass):
    """Un miroir aux données périmées (retard > CASAGUIDE_REFRESH_MAX_SOURCE_LAG_HOURS)
    est refusé : le miroir suivant répond. Sans quoi un lieu récent passerait pour
    disparu (constat réel du 22/11/2026)."""
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "primary.test":
            return httpx.Response(200, json={
                "osm3s": {"timestamp_osm_base": "2026-06-01T00:00:00Z"}, "elements": []})
        return httpx.Response(200, json={
            "osm3s": {"timestamp_osm_base": _fresh()},
            "elements": [{"type": "node", "id": 7, "lat": 1, "lon": 1,
                          "tags": {"name": "Récent"}}]})

    with _overpass_client(handler) as client:
        res = overpass.fetch_by_refs(["node/7"], client=client)
    assert hosts == ["primary.test", "mirror.test"]
    assert res["node/7"]["state"] == "ok"


@pytest.mark.parametrize("doc", [
    {"osm3s": {"timestamp_osm_base": "2026-06-01T00:00:00Z"}, "elements": []},
    {"osm3s": {"timestamp_osm_base": "__now__"}, "elements": [],
     "remark": "runtime error: Query timed out in \"query\" at line 1 after 25 seconds."},
    {"elements": []},
])
def test_fetch_by_refs_never_reports_missing_on_doubtful_answer(fast_overpass, doc):
    """Partout périmé, résultat partiel (`remark`) ou date illisible ⇒ LÈVE (le
    logement échoue et sera repris), jamais « disparu »."""
    if (doc.get("osm3s") or {}).get("timestamp_osm_base") == "__now__":
        doc = {**doc, "osm3s": {"timestamp_osm_base": _fresh()}}
    with _overpass_client(lambda req: httpx.Response(200, json=doc)) as client:
        with pytest.raises(overpass.OverpassError):
            overpass.fetch_by_refs(["node/7"], client=client)


def test_no_transaction_open_during_source_calls(conn, prop):
    """Aucune transaction ouverte pendant les appels réseau (Overpass peut prendre des
    minutes) : sinon « idle in transaction » bloque tout verrou exclusif sur `pois`
    (constaté : une migration rejouée bloquée 10 min derrière un passage à blanc)."""
    from psycopg import pq
    poi = _poi(conn, prop, source="osm")
    _poi(conn, prop, source="overture")
    same = _same(conn, poi)
    seen = []

    def osm(refs):
        seen.append(conn.info.transaction_status)
        return {r: {"state": "ok", **same} for r in refs}

    def overture_fetch(refs, lat, lon):
        seen.append(conn.info.transaction_status)
        return None

    for dry in (True, False):
        _run(conn, prop, osm, ovt=overture_fetch, dry_run=dry)
    assert len(seen) == 4 and set(seen) == {pq.TransactionStatus.IDLE}
