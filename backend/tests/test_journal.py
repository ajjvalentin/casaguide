"""Journal des modifications et retour arrière (V2-92, migration 043, enrich/journal.py).

Contre la base réelle (déclencheur PostgreSQL) — comme le reste de la suite.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from enrich import db, journal, pipeline
from enrich.locks import LOCKABLE_FIELDS
from enrich.settings import settings
from tests.test_pipeline import FakeAnthropic, _mock_handler  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def prop():
    pid, oid = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as c:
        c.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        c.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'J')",
                  (oid, f"{oid}@test.local"))
        c.execute("""INSERT INTO properties (id, owner_id, name, address_line1, city,
                                             country_code)
                     VALUES (%s, %s, 'Villa Journal', 'Calle Ejemplo 1',
                             'Orihuela Costa', 'ES')""", (pid, oid))
        c.commit()
    yield pid
    with psycopg.connect(settings.db_dsn) as c:
        c.execute("DELETE FROM owners WHERE id = %s", (oid,))
        c.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        c.commit()


@pytest.fixture()
def conn(prop):
    # Dépend de `prop` : démontée AVANT lui, donc ses verrous (FOR UPDATE du retour
    # arrière) sont relâchés avant que `prop` ne supprime le propriétaire.
    with db.connect() as c:
        yield c
        c.rollback()


def _poi(conn, pid, *, status="approved", **cols) -> str:
    base = {"category_code": "restaurant", "name": "Casa Pepe", "phone": "+34 1",
            "source": "osm", "source_ref": f"node/{uuid.uuid4().int % 10**9}"}
    base.update(cols)
    names = ", ".join(base)
    ph = ", ".join(f"%({k})s" for k in base)
    poi_id = str(conn.execute(
        f"""INSERT INTO pois (property_id, geom, status, {names})
            VALUES (%(pid)s, ST_SetSRID(ST_MakePoint(-0.7482, 37.928), 4326),
                    %(status)s, {ph}) RETURNING id""",
        {"pid": pid, "status": status, **base}).fetchone()["id"])
    conn.commit()
    return poi_id


def _changes(conn, pid) -> list[dict]:
    return conn.execute("SELECT * FROM content_changes WHERE property_id = %s "
                        "ORDER BY id", (pid,)).fetchall()


def _snap(conn, poi_id):
    r = conn.execute("SELECT journal_poi_snapshot(p) AS s FROM pois p WHERE id = %s",
                     (poi_id,)).fetchone()
    return r["s"] if r else None


# ── Le déclencheur ───────────────────────────────────────────────────────────

def test_tracked_columns_are_exactly_lockable_fields():
    """Même garde qu'en V2-91 : la liste du déclencheur (migration ET schema.sql)
    est EXACTEMENT `locks.LOCKABLE_FIELDS`."""
    for path in (ROOT / "db" / "migrations" / "043_content_changes.sql",
                 ROOT / "db" / "schema.sql"):
        m = re.search(r"tracked CONSTANT text\[\] := ARRAY\[(.*?)\];", path.read_text(), re.S)
        assert m, path
        assert tuple(re.findall(r"'(\w+)'", m.group(1))) == LOCKABLE_FIELDS, path


def test_retained_field_change_one_row_per_field_exact_values(conn, prop):
    """Test 4 : sur une fiche retenue, une ligne PAR champ modifié, old/new exacts,
    geom en {lat, lon} ; auteur par défaut `system` ; source reprise de la fiche."""
    poi = _poi(conn, prop, address="Calle 1")
    conn.execute("UPDATE pois SET phone = '+34 2', address = NULL, "
                 "geom = ST_SetSRID(ST_MakePoint(-0.75, 37.93), 4326), "
                 "dist_walk_m = 99, completion_meta = '{\"x\":1}' WHERE id = %s", (poi,))
    conn.commit()
    allrows = _changes(conn, prop)
    assert allrows[0]["action"] == "create"              # née retenue → instantané
    rows = {r["field"]: r for r in allrows[1:]}
    assert set(rows) == {"phone", "address", "geom"}     # distances / meta non suivies
    assert (rows["phone"]["old_value"], rows["phone"]["new_value"]) == ("+34 1", "+34 2")
    assert (rows["address"]["old_value"], rows["address"]["new_value"]) == ("Calle 1", None)
    assert rows["geom"]["old_value"] == {"lat": 37.928, "lon": -0.7482}
    assert rows["geom"]["new_value"] == {"lat": 37.93, "lon": -0.75}
    for r in rows.values():
        assert r["action"] == "update" and r["actor"] == "system"
        assert r["entity"] == "poi" and str(r["entity_id"]) == poi
        assert r["source"] == "osm" and r["source_ref"]


def test_write_without_real_change_writes_nothing(conn, prop):
    """Test 3 : une écriture qui ne change aucune valeur suivie → aucune ligne."""
    poi = _poi(conn, prop)
    n0 = len(_changes(conn, prop))          # la création d'une fiche retenue
    conn.execute("UPDATE pois SET phone = phone, name = name, fetched_at = now(), "
                 "walk_min = 3, human_fields = '{phone}' WHERE id = %s", (poi,))
    conn.commit()
    assert len(_changes(conn, prop)) == n0


def test_suggested_refresh_writes_nothing_but_status_always(conn, prop):
    """Une fiche `suggested` qui se rafraîchit n'écrit rien ; un changement de
    statut s'écrit toujours (avec status_by/status_at)."""
    poi = _poi(conn, prop, status="suggested")
    conn.execute("UPDATE pois SET phone = '+34 9', name = 'Autre' WHERE id = %s", (poi,))
    conn.execute("DELETE FROM pois WHERE id = %s",
                 (_poi(conn, prop, status="suggested"),))
    conn.commit()
    assert _changes(conn, prop) == []
    conn.execute("UPDATE pois SET status = 'rejected', status_by = 'judge', "
                 "status_at = now() WHERE id = %s", (poi,))
    conn.commit()
    (row,) = _changes(conn, prop)
    assert (row["action"], row["field"]) == ("status", "status")
    assert row["old_value"] == {"status": "suggested", "status_by": None, "status_at": None}
    assert row["new_value"]["status"] == "rejected"
    assert row["new_value"]["status_by"] == "judge" and row["new_value"]["status_at"]


def test_harvest_of_suggested_only_writes_no_row(prop):
    """Test 2 : une vraie moisson (pipeline, réseau simulé) sur un logement neuf ne
    produit que des `suggested` → AUCUNE ligne de journal ; le rejeu non plus."""
    settings.politeness_delay_s = 0
    with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as http:
        for _ in range(2):
            pipeline.run(prop, use_claude=False, only_categories={"restaurant", "supermarket"},
                         http_client=http, anthropic_client=FakeAnthropic())
    with db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM pois WHERE property_id = %s",
                         (prop,)).fetchone()["n"] > 0
        assert _changes(c, prop) == []


def test_pipeline_writes_as_system_with_job_and_batch(prop):
    """Test 1 (pipeline) : une écriture du pipeline sur une fiche retenue — la commune
    comblée (V2-38bis) — est signée `system`, rattachée au job et à un lot."""
    settings.politeness_delay_s = 0
    with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as http:
        pipeline.run(prop, use_claude=False, only_categories={"restaurant"},
                     http_client=http, anthropic_client=FakeAnthropic())
        with db.connect() as c:
            c.execute("UPDATE pois SET status = 'approved', locality = NULL "
                      "WHERE property_id = %s AND name = 'La Marejada'", (prop,))
            c.commit()
        pipeline.run(prop, use_claude=False, only_categories={"restaurant"},
                     http_client=http, anthropic_client=FakeAnthropic())
    with db.connect() as c:
        # (la préparation — commune vidée à la main — est elle aussi journalisée,
        # en `system` SANS lot : on ne garde que les lignes du pipeline.)
        rows = [r for r in _changes(c, prop)
                if r["field"] == "locality" and r["batch_id"] is not None]
        job = c.execute("SELECT id FROM enrichment_jobs WHERE property_id = %s "
                        "ORDER BY created_at DESC LIMIT 1", (prop,)).fetchone()["id"]
    assert len(rows) == 1
    r = rows[0]
    assert (r["actor"], r["old_value"], r["new_value"]) == ("system", None, "Torrevieja")
    assert r["job_id"] == job and r["batch_id"] is not None


def test_bind_survives_intermediate_commits(conn, prop):
    """Test 11 : l'auteur et le lot LIÉS traversent les `commit` — y compris une
    transaction ouverte par `conn.transaction()` sur une connexion au repos (le
    patron SAVEPOINT du pipeline) — puis `acting` (juge) restaure `system`."""
    job = str(conn.execute("INSERT INTO enrichment_jobs (property_id, trigger, status) "
                           "VALUES (%s, 'manual', 'running') RETURNING id",
                           (prop,)).fetchone()["id"])
    poi = _poi(conn, prop)
    batch = str(uuid.uuid4())
    journal.bind(conn, "system", batch_id=batch, job_id=job)
    conn.execute("UPDATE pois SET phone = 'a' WHERE id = %s", (poi,))
    conn.commit()
    with conn.transaction():                  # ouverte AU REPOS : vraie transaction
        conn.execute("UPDATE pois SET phone = 'b' WHERE id = %s", (poi,))
    conn.commit()
    conn.execute("UPDATE pois SET phone = 'c' WHERE id = %s", (poi,))
    with journal.acting(conn, "judge"):
        conn.execute("UPDATE pois SET phone = 'd' WHERE id = %s", (poi,))
    conn.execute("UPDATE pois SET phone = 'e' WHERE id = %s", (poi,))
    conn.commit()
    journal.unbind(conn)
    conn.execute("UPDATE pois SET phone = 'f' WHERE id = %s", (poi,))
    conn.commit()
    rows = [r for r in _changes(conn, prop) if r["field"] == "phone"]
    assert [r["new_value"] for r in rows] == list("abcdef")
    assert [r["actor"] for r in rows] == ["system", "system", "system", "judge",
                                          "system", "system"]
    for r in rows[:5]:
        assert str(r["batch_id"]) == batch and str(r["job_id"]) == job
    assert rows[5]["batch_id"] is None and rows[5]["job_id"] is None   # délié


def test_judge_verdict_is_signed_judge(conn, prop):
    """Test 1 (juge) : `apply_judge_verdict` → ligne `status` d'auteur `judge`."""
    poi = _poi(conn, prop, status="suggested")
    db.apply_judge_verdict(conn, poi, "approved", {"verdict": "keep"})
    conn.commit()
    (row,) = _changes(conn, prop)
    assert (row["action"], row["actor"]) == ("status", "judge")
    assert row["new_value"]["status_by"] == "judge"
    # … et l'auteur ne déborde pas sur la suite de la transaction.
    conn.execute("UPDATE pois SET phone = 'x' WHERE id = %s", (poi,))
    conn.commit()
    assert _changes(conn, prop)[-1]["actor"] == "system"


def test_unknown_actor_is_refused(conn):
    with pytest.raises(ValueError):
        journal.declare(conn, "robot")
    conn.rollback()
    conn.execute("SELECT set_config('holaguia.actor', 'robot', true)")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("""INSERT INTO pois (property_id, category_code, name, geom, source,
                                          status)
                        SELECT id, 'restaurant', 'X', ST_MakePoint(0, 0)::geometry, 'osm',
                               'approved' FROM properties LIMIT 1""")
    conn.rollback()


def test_secrets_never_reach_the_journal(conn, prop):
    """Test 13 : écrire `property_secrets` ne produit aucune ligne, et AUCUN
    déclencheur de journal n'existe ailleurs que sur `pois`."""
    conn.execute("INSERT INTO property_secrets (property_id, keybox_code_enc) "
                 "VALUES (%s, 'secret'::bytea)", (prop,))
    conn.execute("UPDATE property_secrets SET keybox_code_enc = 'autre'::bytea "
                 "WHERE property_id = %s", (prop,))
    conn.commit()
    assert _changes(conn, prop) == []
    tables = {r["t"] for r in conn.execute(
        """SELECT tgrelid::regclass::text AS t FROM pg_trigger
           WHERE tgfoid = 'journal_pois'::regproc""").fetchall()}
    assert tables == {"pois"}


def test_deleting_property_deletes_its_journal(conn):
    """Test 14 : suppression d'un logement (cascade sur ses fiches retenues) ⇒ son
    journal disparaît, et la cascade n'est PAS bloquée par le déclencheur."""
    oid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'D')",
                 (oid, f"{oid}@test.local"))
    conn.execute("INSERT INTO properties (id, owner_id, name, address_line1, city, "
                 "country_code) VALUES (%s, %s, 'V', 'C', 'Orihuela Costa', 'ES')",
                 (pid, oid))
    poi = _poi(conn, pid)
    conn.execute("UPDATE pois SET phone = 'z' WHERE id = %s", (poi,))
    conn.commit()
    assert len(_changes(conn, pid)) == 2                 # create + update
    conn.execute("DELETE FROM owners WHERE id = %s", (oid,))   # → properties → pois
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM content_changes "
                        "WHERE property_id = %s OR entity_id = %s",
                        (pid, poi)).fetchone()["n"] == 0


# ── Retour arrière ───────────────────────────────────────────────────────────

def test_revert_delete_recreates_identical_poi(conn, prop):
    """Test 5 : suppression d'une fiche retenue puis annulation → fiche recréée À
    L'IDENTIQUE (toutes colonnes, geom, provenance, dates), même identifiant."""
    poi = _poi(conn, prop, address="Calle 5", weekday=3, description_md="Bon.",
               human_fields=["phone"], status_by="owner")
    conn.execute("UPDATE pois SET completion_meta = '{\"k\": [1]}', walk_min = 4 "
                 "WHERE id = %s", (poi,))
    conn.commit()
    before = _snap(conn, poi)
    conn.execute("DELETE FROM pois WHERE id = %s", (poi,))
    conn.commit()
    deleted = _changes(conn, prop)[-1]
    assert deleted["action"] == "delete" and deleted["old_value"] == before
    res = journal.revert_change(conn, deleted["id"], actor="support")
    conn.commit()
    assert res.outcome == "reverted", res
    assert _snap(conn, poi) == before
    rev = _changes(conn, prop)[-1]
    assert (rev["action"], rev["reverts_id"], rev["actor"]) == ("revert", deleted["id"],
                                                                "support")


def test_revert_nominal_then_second_attempt_refused(conn, prop):
    """Test 6."""
    poi = _poi(conn, prop)
    conn.execute("UPDATE pois SET phone = '+34 2' WHERE id = %s", (poi,))
    conn.commit()
    ch = _changes(conn, prop)[-1]
    res = journal.revert_change(conn, ch["id"], actor="system")
    conn.commit()
    assert res.outcome == "reverted" and res.revert_id
    assert conn.execute("SELECT phone FROM pois WHERE id = %s", (poi,)).fetchone()["phone"] \
        == "+34 1"
    again = journal.revert_change(conn, ch["id"], actor="system")
    assert (again.outcome, again.reason) == ("refused", "already_reverted")
    # Une ligne `revert` ne s'annule pas non plus.
    assert journal.revert_change(conn, res.revert_id, actor="system").reason == "is_revert"


def test_revert_conflict_when_value_rewritten(conn, prop):
    """Test 7 : valeur réécrite depuis → rien n'est restauré."""
    poi = _poi(conn, prop)
    conn.execute("UPDATE pois SET phone = '+34 2' WHERE id = %s", (poi,))
    conn.commit()
    ch = _changes(conn, prop)[-1]
    conn.execute("UPDATE pois SET phone = '+34 3' WHERE id = %s", (poi,))
    conn.commit()
    n = len(_changes(conn, prop))
    res = journal.revert_change(conn, ch["id"], actor="support")
    conn.commit()
    assert (res.outcome, res.reason) == ("conflict", "value_changed")
    assert conn.execute("SELECT phone FROM pois WHERE id = %s", (poi,)).fetchone()["phone"] \
        == "+34 3"
    assert len(_changes(conn, prop)) == n


def test_revert_conflict_when_field_became_human(conn, prop):
    """Test 8 : champ passé dans `human_fields` depuis → rien n'est restauré, même
    si la valeur courante est encore celle de la modification."""
    poi = _poi(conn, prop)
    conn.execute("UPDATE pois SET website = 'https://a.example' WHERE id = %s", (poi,))
    conn.commit()
    ch = _changes(conn, prop)[-1]
    conn.execute("UPDATE pois SET human_fields = '{website}' WHERE id = %s", (poi,))
    conn.commit()
    res = journal.revert_change(conn, ch["id"], actor="support")
    conn.commit()
    assert (res.outcome, res.reason) == ("conflict", "human_locked")
    assert conn.execute("SELECT website FROM pois WHERE id = %s",
                        (poi,)).fetchone()["website"] == "https://a.example"


def test_revert_refuses_owner_change(conn, prop):
    """Test 9 : une saisie du propriétaire ne s'annule pas par ce chemin."""
    poi = _poi(conn, prop)
    with journal.acting(conn, "owner"):
        conn.execute("UPDATE pois SET phone = 'mine' WHERE id = %s", (poi,))
    conn.commit()
    ch = _changes(conn, prop)[-1]
    assert ch["actor"] == "owner"
    res = journal.revert_change(conn, ch["id"], actor="support")
    assert (res.outcome, res.reason) == ("refused", "actor_owner")
    assert conn.execute("SELECT phone FROM pois WHERE id = %s", (poi,)).fetchone()["phone"] \
        == "mine"


def test_revert_status_restores_status_by_and_at(conn, prop):
    """Annuler un changement de statut restaure statut, status_by ET status_at."""
    poi = _poi(conn, prop, status="suggested")
    db.apply_judge_verdict(conn, poi, "rejected", {"verdict": "reject"})
    conn.commit()
    ch = _changes(conn, prop)[-1]
    res = journal.revert_change(conn, ch["id"], actor="support")
    conn.commit()
    assert res.outcome == "reverted"
    row = conn.execute("SELECT status, status_by, status_at FROM pois WHERE id = %s",
                       (poi,)).fetchone()
    assert (row["status"], row["status_by"], row["status_at"]) == ("suggested", None, None)


def test_revert_batch_reverse_order_partial_conflict(conn, prop):
    """Test 10 : ordre inverse, lot traité EN ENTIER malgré un conflit, détail rendu ;
    les annulations forment un nouveau lot."""
    a, b = _poi(conn, prop, name="A"), _poi(conn, prop, name="B")
    batch = str(uuid.uuid4())
    journal.declare(conn, "system", batch_id=batch)
    conn.execute("UPDATE pois SET phone = 'a1' WHERE id = %s", (a,))
    conn.execute("UPDATE pois SET phone = 'a2' WHERE id = %s", (a,))
    conn.execute("UPDATE pois SET phone = 'b1' WHERE id = %s", (b,))
    conn.commit()
    lot = [r for r in _changes(conn, prop) if r["batch_id"] is not None]
    assert len(lot) == 3
    conn.execute("UPDATE pois SET phone = 'b-hand' WHERE id = %s", (b,))   # conflit sur b
    conn.commit()
    results = journal.revert_batch(conn, batch, actor="support")
    conn.commit()
    assert [r.change_id for r in results] == [lot[2]["id"], lot[1]["id"], lot[0]["id"]]
    assert [r.outcome for r in results] == ["conflict", "reverted", "reverted"]
    assert results[0].reason == "value_changed"
    phones = {r["id"]: r["phone"] for r in conn.execute(
        "SELECT id::text AS id, phone FROM pois WHERE id IN (%s, %s)", (a, b)).fetchall()}
    assert phones == {a: "+34 1", b: "b-hand"}
    revs = [r for r in _changes(conn, prop) if r["action"] == "revert"]
    assert len(revs) == 2 and len({r["batch_id"] for r in revs}) == 1
    assert str(revs[0]["batch_id"]) != batch


def test_ops_script_dry_run_writes_nothing_then_reverts_as_support(conn, prop):
    """`ops/revert_changes.py` : --dry-run n'écrit rien ; sinon auteur `support`."""
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("revert_changes",
                                                  ROOT / "ops" / "revert_changes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    poi = _poi(conn, prop)
    batch = str(uuid.uuid4())
    journal.declare(conn, "system", batch_id=batch)
    conn.execute("UPDATE pois SET phone = 'auto' WHERE id = %s", (poi,))
    conn.commit()
    n = len(_changes(conn, prop))
    assert mod.main(["--batch", batch, "--dry-run", "--dsn", settings.db_dsn,
                     "--env-file", "/nonexistent"]) == 0
    assert len(_changes(conn, prop)) == n
    assert mod.main(["--batch", batch, "--dsn", settings.db_dsn,
                     "--env-file", "/nonexistent"]) == 0
    last = _changes(conn, prop)[-1]
    assert (last["action"], last["actor"]) == ("revert", "support")
    assert conn.execute("SELECT phone FROM pois WHERE id = %s", (poi,)).fetchone()["phone"] \
        == "+34 1"
