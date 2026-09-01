"""Watchdog local & supervision (OPS-3).

Le cœur `decide` et l'orchestrateur `run_once` sont PURS (sonde et actions
injectées) → testables sans réseau ni systemd. On vérifie aussi la présence des
unités systemd et leur installation par deploy.sh (syntaxe si systemd-analyze est là).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
import watchdog as wd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _fail(detail="503 database: OperationalError"):
    return lambda: wd.Probe(False, detail)


def _ok():
    return wd.Probe(True)


# ── decide / run_once (purs) ─────────────────────────────────────────────────

def test_below_threshold_takes_no_action():
    state = {"failures": 0, "last_restart": None}
    restarts, notifies = [], []
    for i in range(2):                          # 2 échecs seulement
        state = wd.run_once(
            state, probe_public=_fail(), probe_caddy=lambda: _ok(),
            restart=restarts.append, notify=lambda r, n: notifies.append(r),
            now=1000 + i, threshold=3, cooldown_s=600)
    assert state["failures"] == 2
    assert restarts == [] and notifies == []    # rien avant le 3e


def test_third_failure_restarts_casaguide_alerts_and_logs():
    state = {"failures": 2, "last_restart": None}
    restarts, notifies = [], []
    state = wd.run_once(
        state, probe_public=_fail("503 database: OperationalError"),
        probe_caddy=lambda: _ok(),
        restart=restarts.append, notify=lambda r, n: notifies.append((r, n)),
        now=2000, threshold=3, cooldown_s=600)
    assert restarts == ["casaguide"]            # action
    assert len(notifies) == 1                   # email
    reason, when = notifies[0]
    assert "3 échecs" in reason and "casaguide redémarré" in reason and "503" in reason
    assert when == 2000
    assert state == {"failures": 0, "last_restart": 2000}   # compteur remis, restart mémorisé


def test_success_resets_the_counter():
    state = {"failures": 2, "last_restart": None}
    restarts = []
    state = wd.run_once(
        state, probe_public=lambda: _ok(), probe_caddy=lambda: _ok(),
        restart=restarts.append, notify=lambda r, n: None, now=3000)
    assert state["failures"] == 0 and restarts == []   # un succès efface l'ardoise


def test_cooldown_prevents_a_second_restart_then_allows_after():
    state = {"failures": 0, "last_restart": 5000}   # un restart vient d'avoir lieu
    restarts, now = [], 5000
    for _ in range(3):                              # 3 échecs DANS le cooldown
        now += 30
        state = wd.run_once(
            state, probe_public=_fail("x"), probe_caddy=lambda: _ok(),
            restart=restarts.append, notify=lambda r, n: None,
            now=now, threshold=3, cooldown_s=600)
    assert restarts == []                            # jamais de 2e restart dans les 10 min
    assert state["failures"] == 3                    # compteur conservé
    # Après le cooldown, un nouvel échec redémarre bien.
    state = wd.run_once(
        state, probe_public=_fail("x"), probe_caddy=lambda: _ok(),
        restart=restarts.append, notify=lambda r, n: None,
        now=5000 + 601, threshold=3, cooldown_s=600)
    assert restarts == ["casaguide"]


def test_caddy_unresponsive_restarts_caddy():
    state = {"failures": 2, "last_restart": None}
    restarts, notifies = [], []
    state = wd.run_once(
        state, probe_public=_fail("injoignable (ConnectError)"),
        probe_caddy=lambda: wd.Probe(False),     # Caddy muet localement
        restart=restarts.append, notify=lambda r, n: notifies.append(r),
        now=1000, threshold=3, cooldown_s=600)
    assert restarts == ["caddy"]
    assert "caddy redémarré" in notifies[0] and "Caddy ne répond pas" in notifies[0]


def test_notify_failure_never_blocks_the_restart():
    state = {"failures": 2, "last_restart": None}
    restarts = []

    def boom(reason, now):
        raise RuntimeError("smtp down")

    state = wd.run_once(
        state, probe_public=_fail("x"), probe_caddy=lambda: _ok(),
        restart=restarts.append, notify=boom, now=1000, threshold=3, cooldown_s=600)
    assert restarts == ["casaguide"]                 # l'auto-guérison a eu lieu
    assert state == {"failures": 0, "last_restart": 1000}


def test_caddy_probe_only_when_public_fails():
    """Économie : un tick sain ne sonde jamais Caddy localement."""
    caddy_calls = []
    wd.run_once(
        {"failures": 0, "last_restart": None},
        probe_public=lambda: _ok(),
        probe_caddy=lambda: caddy_calls.append(1) or wd.Probe(True),
        restart=lambda s: None, notify=lambda r, n: None, now=1000)
    assert caddy_calls == []


# ── Unités systemd & installation par deploy.sh ──────────────────────────────

def test_watchdog_units_present():
    assert (ROOT / "ops" / "casaguide-watchdog.service").is_file()
    assert (ROOT / "ops" / "casaguide-watchdog.timer").is_file()


def test_deploy_installs_units():
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "sync_systemd_units" in deploy               # fonction d'installation
    assert "casaguide-watchdog.timer" in deploy         # activée par deploy.sh


def test_watchdog_timer_runs_every_minute():
    timer = (ROOT / "ops" / "casaguide-watchdog.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1min" in timer
    assert "WantedBy=timers.target" in timer


def test_units_syntax_when_systemd_analyze_available():
    sa = shutil.which("systemd-analyze")
    if not sa:
        import pytest
        pytest.skip("systemd-analyze indisponible (hors Linux)")
    for unit in ("casaguide-watchdog.service", "casaguide-watchdog.timer"):
        r = subprocess.run([sa, "verify", str(ROOT / "ops" / unit)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
