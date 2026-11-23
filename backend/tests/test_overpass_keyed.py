"""Overpass : instance à clé et nouvelles tentatives polies (V2-93b).

Aucun réseau : transport httpx simulé (`MockTransport`), attentes injectées (aucun vrai
sommeil). La clé utilisée est une SENTINELLE : elle ne doit apparaître nulle part —
journaux, exceptions, `enrichment_jobs`, sortie des scripts ops/.
"""
from __future__ import annotations

import datetime as _dt
import logging

import httpx
import psycopg
import pytest

from enrich import overpass, refresh
from enrich.settings import settings

from .test_refresh import (ROOT, _load_ops, _poi, _property, _ref, _row,  # noqa: F401
                           _same, owner)

SENTINEL = "SENTINEL-KEY-9f3c2a7e5b"
KEYED_URL = "https://keyed.test/v1/interpreter"
MIRROR = "https://mirror.test/api/interpreter"


def _fresh() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(elements=()):
    return httpx.Response(200, json={"osm3s": {"timestamp_osm_base": _fresh()},
                                     "elements": list(elements)})


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


@pytest.fixture()
def sleeps(monkeypatch):
    rec = Sleeps()
    monkeypatch.setattr(overpass, "_sleep", rec)
    monkeypatch.setattr(refresh, "_sleep", rec)
    monkeypatch.setattr(settings, "politeness_delay_s", 0)
    return rec


@pytest.fixture()
def keyed(monkeypatch, sleeps):
    monkeypatch.setattr(settings, "overpass_api_key", SENTINEL)
    monkeypatch.setattr(settings, "overpass_url", KEYED_URL)
    monkeypatch.setattr(settings, "overpass_mirrors", (MIRROR,))
    return sleeps


@pytest.fixture()
def public(monkeypatch, sleeps):
    monkeypatch.setattr(settings, "overpass_api_key", "")
    monkeypatch.setattr(settings, "overpass_url", "https://primary.test/api/interpreter")
    monkeypatch.setattr(settings, "overpass_mirrors", (MIRROR,))
    return sleeps


# ── 1-2 : mode à clé / mode public ───────────────────────────────────────────

def test_keyed_mode_header_single_server_no_mirror_even_after_failure(keyed):
    """Test 1 : `X-API-Key` posé, `OVERPASS_URL` seul, AUCUN miroir même après échec."""
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("x-api-key")))
        return httpx.Response(503, text="busy")

    with _client(handler) as c, pytest.raises(overpass.OverpassError):
        overpass._post_overpass(c, "[out:json];node(1);out;")
    assert seen and all(u == KEYED_URL for u, _ in seen)
    assert all(k == SENTINEL for _, k in seen)
    assert SENTINEL not in KEYED_URL and all(SENTINEL not in u for u, _ in seen)
    assert len(seen) == settings.overpass_max_attempts
    assert "instance à clé" in overpass.describe_mode()


def test_public_mode_unchanged_mirrors_and_no_key_header(public):
    """Test 2 : sans clé, URL principale puis miroirs, jamais d'en-tête `X-API-Key`."""
    hosts, keys = [], []

    def handler(req):
        hosts.append(req.url.host)
        keys.append(req.headers.get("x-api-key"))
        return httpx.Response(503) if req.url.host == "primary.test" else _ok()

    with _client(handler) as c:
        assert overpass._post_overpass(c, "q") == []
    assert hosts == ["primary.test", "mirror.test"] and keys == [None, None]
    assert "serveurs publics" in overpass.describe_mode()


def test_key_without_url_never_defaults_to_a_public_server(monkeypatch):
    """Une clé posée sans `OVERPASS_URL` part vers l'instance à clé, jamais vers un
    serveur public (la clé y fuirait)."""
    import importlib, os  # noqa: E401
    from enrich import settings as settings_mod
    monkeypatch.setenv("CASAGUIDE_OVERPASS_API_KEY", SENTINEL)
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    fresh = importlib.reload(settings_mod)
    try:
        s = fresh.Settings()
        assert s.overpass_url == fresh.OVERPASS_KEYED_DEFAULT_URL
        assert SENTINEL not in repr(s)
    finally:
        monkeypatch.undo()
        importlib.reload(settings_mod)
        assert os.environ.get("CASAGUIDE_OVERPASS_API_KEY") != SENTINEL


# ── 3-4 : la clé ne fuit jamais ──────────────────────────────────────────────

def _echo_body(req) -> str:
    """Corps d'erreur qui RENVOIE l'URL et la clé reçues (pire cas)."""
    return f"error at {req.url} with key {req.headers.get('x-api-key')}"


_LEAK_CASES = {
    "network": lambda req: (_ for _ in ()).throw(
        httpx.ConnectError(f"cannot reach {req.url} {req.headers.get('x-api-key')}",
                           request=req)),
    "401": lambda req: httpx.Response(401, json={"error": "unauthorized",
                                                 "message": _echo_body(req)}),
    "403": lambda req: httpx.Response(403, json={"error": "plan_without_overpass",
                                                 "message": _echo_body(req)}),
    "429": lambda req: httpx.Response(429, headers={"Retry-After": "1"},
                                      json={"error": "rate_limited",
                                            "message": _echo_body(req)}),
    "429-quota": lambda req: httpx.Response(429, json={"error": "quota_exceeded",
                                                       "message": _echo_body(req)}),
    "503": lambda req: httpx.Response(503, headers={"Retry-After": "2"},
                                      json={"error": "server_busy",
                                            "message": _echo_body(req)}),
    "504": lambda req: httpx.Response(504, text=_echo_body(req)),
    "400-echo": lambda req: httpx.Response(400, text=_echo_body(req)),
}

KEY_IN_PATH_URL = f"https://keyed.test/v1/{SENTINEL}/interpreter"


@pytest.mark.parametrize("url", [KEYED_URL, KEY_IN_PATH_URL])
@pytest.mark.parametrize("case", sorted(_LEAK_CASES))
def test_sentinel_never_leaks_from_post_overpass(keyed, monkeypatch, caplog, case, url):
    """Tests 3-4 : chaque type d'erreur, clé en en-tête OU (mal) collée dans le chemin
    de l'URL : ni le journal (httpx compris), ni l'exception, ni son `body`."""
    monkeypatch.setattr(settings, "overpass_url", url)
    caplog.set_level(logging.DEBUG)
    with _client(_LEAK_CASES[case]) as c, pytest.raises(Exception) as ei:
        overpass.fetch_by_refs(["node/1"], client=c)
    exc = ei.value
    texts = [caplog.text, str(exc), repr(exc), getattr(exc, "body", ""),
             getattr(exc, "url", "")]
    assert all(SENTINEL not in t for t in texts), case
    assert isinstance(exc, overpass.OverpassError)
    if url == KEY_IN_PATH_URL:
        assert "/v1/***/interpreter" in caplog.text


def test_mask_url_and_scrub():
    assert (overpass.mask_url(f"https://u:p@h.test/v1/{SENTINEL}/interpreter?key=x#f")
            == "https://h.test/v1/***/interpreter")
    assert overpass.mask_url(KEYED_URL) == KEYED_URL                 # rien à masquer
    assert SENTINEL not in overpass.scrub(f"POST /v1/{SENTINEL}/interpreter")
    with overpass.use_server(KEYED_URL, SENTINEL):
        assert overpass.scrub(f"bad key {SENTINEL}") == "bad key ***"
        assert SENTINEL not in repr(overpass.current_server())


def test_sentinel_never_leaks_into_jobs_and_refresh_output(keyed, owner, monkeypatch,
                                                           caplog):
    """Test 3 (suite) : `enrichment_jobs.error`/`steps` et sortie de
    `ops/refresh_pois.py` — passage réel avec un corps d'erreur qui renvoie la clé."""
    monkeypatch.setattr(settings, "overpass_url", KEY_IN_PATH_URL)
    pid = _property(owner)
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        _poi(conn, pid)

    def osm(refs):
        with _client(_LEAK_CASES["504"]) as c:
            return overpass.fetch_by_refs(refs, client=c,
                                          policy=overpass.refresh_policy())

    monkeypatch.setattr(refresh, "due_properties", lambda *_a, **_k: [pid])
    caplog.set_level(logging.DEBUG)
    code = _load_ops().main(["--env-file", "/nonexistent"], osm_fetch=osm,
                            overture_fetch=lambda *_: None)
    assert code == 1
    with psycopg.connect(settings.db_dsn) as c:
        job = c.execute("SELECT error, steps::text FROM enrichment_jobs WHERE "
                        "property_id = %s AND trigger = 'refresh'", (pid,)).fetchone()
    assert job[0] and "504" in job[0]
    assert SENTINEL not in job[0] and SENTINEL not in job[1]
    assert SENTINEL not in caplog.text and "Overpass : instance à clé" in caplog.text


# ── 5-7 : lecture des réponses du service ────────────────────────────────────

def test_retry_after_honoured_capped_or_backoff(keyed, monkeypatch):
    """Test 5 : `Retry-After: 7` ⇒ 7 ; au-delà du plafond ⇒ plafonné ; absent ⇒
    attente croissante de la politique."""
    monkeypatch.setattr(settings, "overpass_retry_after_max_s", 120)
    pol = overpass.RetryPolicy(3, 10, exponential=True)
    for header, expected in (("7", [7, 7]), ("600", [120, 120]), (None, [10, 20])):
        keyed.clear()
        hdrs = {"Retry-After": header} if header else {}
        with _client(lambda req: httpx.Response(503, headers=hdrs)) as c:
            with pytest.raises(overpass.OverpassError):
                overpass._post_overpass(c, "q", policy=pol)
        assert keyed == expected, header


def test_quota_exceeded_single_request_named_error(keyed):
    """Test 6 : `429 quota_exceeded` ⇒ UNE requête, `OverpassQuotaExceeded`."""
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(429, json={"error": "quota_exceeded", "message": "x"})

    with _client(handler) as c, pytest.raises(overpass.OverpassQuotaExceeded) as ei:
        overpass._post_overpass(c, "q", policy=overpass.refresh_policy())
    assert len(n) == 1 and keyed == []
    assert "quota" in str(ei.value) and SENTINEL not in str(ei.value)


def test_rate_limited_429_is_retried(keyed):
    """Un 429 `rate_limited` reste transitoire (≠ quota)."""
    calls = []

    def handler(req):
        calls.append(1)
        return (httpx.Response(429, headers={"Retry-After": "3"},
                               json={"error": "rate_limited"}) if len(calls) == 1
                else _ok())

    with _client(handler) as c:
        assert overpass._post_overpass(c, "q") == []
    assert len(calls) == 2 and keyed == [3]


@pytest.mark.parametrize("status,code,hint", [
    (401, "unauthorized", "CASAGUIDE_OVERPASS_API_KEY"),
    (403, "plan_without_overpass", "formule"),
    (403, "forbidden_referer", "référent"),
])
def test_auth_errors_single_request_named_error(keyed, status, code, hint):
    """Test 7 : 401 / 403 ⇒ UNE requête, `OverpassAuthError`, message utile sans clé."""
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(status, json={"error": code, "message": SENTINEL})

    with _client(handler) as c, pytest.raises(overpass.OverpassAuthError) as ei:
        overpass._post_overpass(c, "q", policy=overpass.refresh_policy())
    msg = str(ei.value)
    assert len(n) == 1 and hint in msg and code in msg and SENTINEL not in msg
    assert isinstance(ei.value, overpass.OverpassAccountError)


# ── 8 : politique propre à la revérification ─────────────────────────────────

def test_refresh_policy_four_attempts_30_60_120_interactive_unchanged(keyed, monkeypatch):
    """Test 8 : la revérification fait 4 tentatives (30/60/120 s) ; la moisson
    interactive garde ses réglages (2 passes, backoff linéaire court)."""
    monkeypatch.setattr(settings, "refresh_overpass_attempts", 4)
    monkeypatch.setattr(settings, "refresh_overpass_backoff_s", 30)
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(503)

    with _client(handler) as c, pytest.raises(overpass.OverpassError):
        overpass.fetch_by_refs(["node/1"], client=c, policy=overpass.refresh_policy())
    assert len(n) == 4 and keyed == [30, 60, 120]

    keyed.clear(); n.clear()
    with _client(handler) as c, pytest.raises(overpass.OverpassError):
        overpass._post_overpass(c, "q")      # défaut : politique interactive
    assert len(n) == settings.overpass_max_attempts
    assert keyed == [settings.overpass_backoff_s * a
                     for a in range(1, settings.overpass_max_attempts)]
    assert overpass.interactive_policy() == overpass.RetryPolicy(
        settings.overpass_max_attempts, settings.overpass_backoff_s)


def test_default_refresh_fetch_uses_refresh_policy(monkeypatch):
    seen = {}
    monkeypatch.setattr(overpass, "fetch_by_refs",
                        lambda refs, **kw: seen.update(kw) or {})
    refresh._default_osm_fetch(["node/1"])
    assert seen["policy"] == overpass.refresh_policy()


# ── 9-10 : passage de plusieurs logements ────────────────────────────────────

def _three(owner):
    pids = [_property(owner) for _ in range(3)]
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        pois = [_poi(conn, p) for p in pids]
        refs = [_ref(conn, p) for p in pois]
        same = [_same(conn, p) for p in pois]
    return pids, pois, refs, same


def _jobs(pids):
    with psycopg.connect(settings.db_dsn) as c:
        return {str(r[0]): r[1] for r in c.execute(
            "SELECT property_id, status FROM enrichment_jobs WHERE "
            "property_id = ANY(%s::uuid[]) AND trigger = 'refresh'", (pids,))}


def test_quota_on_second_property_stops_pass(keyed, owner, monkeypatch, caplog):
    """Test 9 : 2ᵉ logement en `quota_exceeded` ⇒ le 3ᵉ n'est pas tenté (ni job, ni
    appel), bilan explicite, sortie non nulle ; le 1ᵉʳ est validé normalement."""
    pids, pois, refs, same = _three(owner)
    asked = []

    def osm(rs):
        asked.append(rs[0])
        if rs[0] == refs[1]:
            raise overpass.OverpassQuotaExceeded(KEYED_URL, 429, "", "quota_exceeded",
                                                 "quota mensuel épuisé")
        i = refs.index(rs[0])
        return {rs[0]: {"state": "ok", **same[i], "phone": "+34 9"}}

    monkeypatch.setattr(refresh, "due_properties", lambda *_a, **_k: list(pids))
    caplog.set_level(logging.INFO)
    code = _load_ops().main(["--env-file", "/nonexistent"], osm_fetch=osm,
                            overture_fetch=lambda *_: None)
    assert code == 1 and asked == refs[:2]
    jobs = _jobs(pids)
    assert jobs == {pids[0]: "done", pids[1]: "failed"}          # 3ᵉ : aucun job
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        assert _row(conn, pois[0])["phone"] == "+34 9"
        assert _row(conn, pois[2])["last_checked_at"] is None
    assert "Passage ARRÊTÉ" in caplog.text and "non tenté" in caplog.text
    assert "1 non tenté(s)" in caplog.text


def test_503_on_second_property_isolated_pause_respected(keyed, owner, monkeypatch):
    """Test 10 : 2ᵉ logement en 503 épuisé ⇒ échec isolé, le 3ᵉ est tenté ; pause
    entre logements (transaction fermée pendant la pause)."""
    from psycopg import pq
    monkeypatch.setattr(settings, "refresh_property_pause_s", 5)
    pids, pois, refs, same = _three(owner)
    states = []

    def osm(rs):
        if rs[0] == refs[1]:
            raise overpass.OverpassError(KEYED_URL, 503, "busy")
        return {rs[0]: {"state": "ok", **same[refs.index(rs[0])]}}

    monkeypatch.setattr(refresh, "due_properties", lambda *_a, **_k: list(pids))
    with refresh.db.connect() as conn:
        def pause(s):
            states.append(conn.info.transaction_status)
            keyed.append(s)
        reps = refresh.run(conn, osm_fetch=osm, overture_fetch=lambda *_: None,
                           sleep=pause)
    assert [r.ok for r in reps] == [True, False, True]
    assert not any(r.skipped or r.fatal for r in reps)
    assert keyed == [5, 5] and set(states) == {pq.TransactionStatus.IDLE}
    assert _jobs(pids) == {pids[0]: "done", pids[1]: "failed", pids[2]: "done"}


def test_no_transaction_open_during_retry_waits(keyed, owner):
    """Acquis V2-93 étendu : les NOUVELLES attentes (Retry-After / backoff de la
    revérification) se font transaction fermée."""
    from psycopg import pq
    pid = _property(owner)
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as c:
        poi = _poi(c, pid)
        ref = _ref(c, poi)
    calls, states = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, headers={"Retry-After": "7"})
        return _ok([{"type": ref.split("/")[0], "id": int(ref.split("/")[1]),
                     "lat": 1, "lon": 1, "tags": {"name": "Casa Pepe"}}])

    with refresh.db.connect() as conn:
        def wait(s):
            states.append(conn.info.transaction_status)

        def osm(rs):
            with _client(handler) as cl:
                return overpass.fetch_by_refs(rs, client=cl,
                                              policy=overpass.refresh_policy(),
                                              sleep=wait)
        rep = refresh.refresh_property(conn, pid, osm_fetch=osm,
                                       overture_fetch=lambda *_: None)
    assert rep.ok and len(states) == 2
    assert set(states) == {pq.TransactionStatus.IDLE}
