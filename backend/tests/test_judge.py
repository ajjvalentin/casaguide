"""Cœur PARTAGÉ du juge (enrich/judge.py, V2-54) — fonctions PURES, aucun réseau.

Le benchmark (ops/poi_judge_benchmark.py) et le pipeline (offre Guide Voyageur)
importent CES fonctions : on les teste ici en IMPORT DIRECT (test_poi_judge.py les
exerce via le ré-export ops — un test direct garde contre une régression masquée)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from enrich import judge  # noqa: E402


PROP = {"name": "Test", "city": "Ardon", "region": "Valais", "country_code": "CH",
        "lat": 46.21, "lon": 7.26}


def test_build_prompt_lists_places_without_status():
    """Le prompt liste les lieux et leurs distances mais JAMAIS un statut/verdict
    (le juge ne doit pas être influencé par une décision antérieure)."""
    batch = [{"id": "a", "name": "Coop", "category_code": "supermarket",
              "drive_min": 6, "walk_min": None, "source": "osm",
              "address": None, "locality": None, "description_md": None}]
    p = judge.build_prompt(PROP, batch, zone_type="rurale")
    assert 'id "a"' in p and "Coop" in p and "6 min en voiture" in p
    assert "DÉTECTEUR DE BRUIT" in p
    assert "approved" not in p and "rejected" not in p and "status" not in p


def test_parse_verdicts_tolerant_and_clamped():
    data = {"verdicts": [
        {"id": "a", "verdict": "KEEP", "confidence": 1.5, "reason": "ok"},   # clampé
        {"id": "b", "verdict": "reject", "confidence": "0.95", "reason": "bruit"},
        {"id": "c", "verdict": "maybe"},        # verdict invalide → ignoré
        {"id": "", "verdict": "keep"},          # id vide → ignoré
    ]}
    out = judge.parse_verdicts(data)
    assert set(out) == {"a", "b"}
    assert out["a"].verdict == "keep" and out["a"].confidence == 1.0
    assert out["b"].verdict == "reject" and out["b"].confidence == 0.95
    assert judge.parse_verdicts({}) == {}


def test_judge_pois_retries_missing_then_defaults():
    """judge_pois re-soumet UNE fois les POI sans verdict, puis finalize comble le
    reste par le défaut PRUDENT (keep, conf 0) — jamais un POI « non jugé »."""
    pois = [{"id": str(i), "name": f"L{i}", "category_code": "sight",
             "drive_min": 10, "walk_min": None, "source": "osm",
             "address": None, "locality": None, "description_md": None}
            for i in range(3)]
    calls = {"n": 0}

    def ask(prompt):
        calls["n"] += 1
        # 1er appel : ne rend QUE le POI "0" ; le retry rendra "1", jamais "2".
        if calls["n"] == 1:
            return ({"verdicts": [{"id": "0", "verdict": "reject",
                                   "confidence": 0.95, "reason": "x"}]},
                    {"attempts": [{"units": 1, "cost_cts": 0.1}]})
        return ({"verdicts": [{"id": "1", "verdict": "keep", "confidence": 0.8}]},
                {"attempts": [{"units": 1, "cost_cts": 0.1}]})

    verdicts, attempts = judge.judge_pois(PROP, pois, ask, batch_size=15)
    assert calls["n"] == 2                       # une passe + un retry
    verdicts, defaulted = judge.finalize_verdicts(pois, verdicts)
    assert defaulted == ["2"]                     # jamais rendu → défaut
    assert verdicts["0"].verdict == "reject" and verdicts["0"].confidence == 0.95
    assert verdicts["2"].verdict == "keep" and verdicts["2"].confidence == 0.0
    assert len(attempts) == 2                     # coût de chaque essai comptabilisé
