"""V2-57 — plancher de qualité du guide payant : reprise de traduction + traçabilité.

Fonctions PURES / I/O entièrement monkeypatchées (aucun réseau, aucune base)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from api import guest_guides  # noqa: E402


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_build_quality_names_every_gap():
    q = guest_guides._build_quality(
        {"failed_categories": {"atm": "x", "cafe": "y"},
         "empty_categories": ["laundry"]},
        ["de", "nl"], "TimeoutError: boom")
    assert "catégories non moissonnées (échec réseau) : atm, cafe" in q["notes"]
    assert "catégories sans résultat : laundry" in q["notes"]
    assert "traduction de/nl échouée : TimeoutError: boom" in q["notes"]
    assert q["missing_langs"] == ["de", "nl"]
    assert q["failed_categories"] == ["atm", "cafe"]


def test_build_quality_silent_when_complete():
    assert guest_guides._build_quality({}, [], None)["notes"] == ""


def test_translate_retry_recovers_after_one_failure(monkeypatch):
    calls = []

    def fake_run(pid, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Claude 529 overloaded")
        # 2e tentative : succès (toutes les cibles publiées)

    monkeypatch.setattr(guest_guides.translate, "run", fake_run)
    monkeypatch.setattr(guest_guides, "_target_langs", lambda pid: ["en", "es"])
    monkeypatch.setattr(guest_guides.db, "connect", lambda: _Conn())
    monkeypatch.setattr(guest_guides.repo, "published_langs",
                        lambda conn, pid: ["en", "es"])

    missing, err = guest_guides._translate_with_retry("pid")
    assert len(calls) == 2           # une reprise avant publication
    assert missing == [] and err is None   # recouvré → aucune réserve


def test_translate_retry_persistent_failure_reports_missing(monkeypatch):
    def always_fail(pid, **kw):
        raise RuntimeError("Claude indisponible")

    monkeypatch.setattr(guest_guides.translate, "run", always_fail)
    monkeypatch.setattr(guest_guides, "_target_langs", lambda pid: ["en", "es", "de"])
    monkeypatch.setattr(guest_guides.db, "connect", lambda: _Conn())
    monkeypatch.setattr(guest_guides.repo, "published_langs", lambda conn, pid: [])

    missing, err = guest_guides._translate_with_retry("pid")
    assert set(missing) == {"en", "es", "de"}      # livraison FR, réserves nommées
    assert "Claude indisponible" in err


def test_translate_retry_partial_publish_reports_only_missing(monkeypatch):
    # Un run qui publie en/es mais pas de (échec partiel simulé au 2e passage).
    monkeypatch.setattr(guest_guides.translate, "run",
                        lambda pid, **kw: (_ for _ in ()).throw(RuntimeError("de KO")))
    monkeypatch.setattr(guest_guides, "_target_langs", lambda pid: ["en", "es", "de"])
    monkeypatch.setattr(guest_guides.db, "connect", lambda: _Conn())
    monkeypatch.setattr(guest_guides.repo, "published_langs",
                        lambda conn, pid: ["en", "es"])
    missing, err = guest_guides._translate_with_retry("pid")
    assert missing == ["de"] and "de KO" in err
