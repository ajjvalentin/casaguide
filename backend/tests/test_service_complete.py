"""Complétion des fiches de service + baby-sitting (V2-07 volet 2) — CLAUDE + web.

Tests PURS (aucune base, aucun réseau) du cœur `claude_enrich` :
`complete_service_pois` (téléphone/site/horaires, PREUVE OU RIEN, mention
« Horaires indicatifs », périmètre par catégorie) et `fetch_babysitters`
(création, vide assumé). Le bouchon reflète la SURFACE RÉELLE du SDK avec
recherche web (leçon OPS-1b) : blocs `server_tool_use` + `web_search_tool_result`
+ `text`, et `usage.server_tool_use.web_search_requests`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from enrich import claude_enrich as ce  # noqa: E402


# ── Bouchon fidèle de l'API web_search ───────────────────────────────────────

def _web_msg(text, *, searches=2, stop_reason="end_turn"):
    return NS(
        stop_reason=stop_reason,
        content=[
            NS(type="server_tool_use", name="web_search", input={"query": "x"}),
            NS(type="web_search_tool_result",
               content=[NS(type="web_search_result", url="https://x", title="y")]),
            NS(type="text", text=text),
        ],
        usage=NS(input_tokens=1400, output_tokens=300,
                 server_tool_use=NS(web_search_requests=searches)))


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.prompts = []
        self.tools = []

    def create(self, *, model, max_tokens, messages, tools=None, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        self.tools.append(tools)
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


class Fake:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


# ── Périmètre par catégorie ──────────────────────────────────────────────────

def test_service_fields_perimeter_per_category():
    assert ce.service_fields("taxi") == ("phone", "website")           # appeler
    assert ce.service_fields("supermarket") == ("opening_hours", "website")  # horaires
    # pharmacie = les deux périmètres, sans doublon de website
    assert ce.service_fields("pharmacy") == ("phone", "website", "opening_hours")
    # Restauration (V2-37) : téléphone + site SEULS ; les HORAIRES restent EXCLUS.
    for cat in ("restaurant", "bar", "cafe"):
        assert ce.service_fields(cat) == ("phone", "website")
        assert "opening_hours" not in ce.service_fields(cat)


def test_completion_never_writes_hours_for_restaurants_even_if_offered():
    """V2-37 : même si la recherche web PROPOSE des horaires pour un restaurant, ils
    ne sont JAMAIS retenus (hors périmètre — trop volatils)."""
    poi = {"id": "p1", "name": "La Coopérative", "address": "Ardon",
           "missing": ["phone", "website"]}
    payload = {"p1": {"phone": "+41 27 306 00 00", "website": "https://coop.example",
                      "opening_hours": "Mo-Sa 09:00-23:00",   # proposé mais HORS périmètre
                      "source_url": "https://coop.example", "verified_on": "2026-08-16"}}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, _ = ce.complete_service_pois("restaurant", "Restaurant", [poi], "Ardon", "CH",
                                      cli, today="2026-08-16")
    fields = out["p1"]["fields"]
    assert fields.get("phone") and fields.get("website")
    assert "opening_hours" not in fields          # jamais d'horaires pour un restaurant


# ── Complétion : preuve, périmètre, mention horaires ─────────────────────────

def test_completion_fills_only_missing_perimeter_fields_with_proof():
    poi = {"id": "p1", "name": "Farmacia Centro", "address": "Av. X 1",
           "missing": ["phone", "opening_hours"]}   # pharmacie : périmètre complet
    payload = {"p1": {"phone": "+34 966 111 222", "website": "https://ignore.example",
                      "opening_hours": "Lun–Sam 9h–21h",
                      "source_url": "https://sanidad.example/farmacia",
                      "verified_on": "2026-08-12"}}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, meta = ce.complete_service_pois(
        "pharmacy", "Pharmacie", [poi], "Orihuela Costa", "ES", cli, today="2026-08-12")
    res = out["p1"]
    assert res["fields"]["phone"] == "+34 966 111 222"
    # `website` n'était PAS demandé (pas dans missing) → jamais retenu, même fourni.
    assert "website" not in res["fields"]
    # Mention « Horaires indicatifs » accolée (donnée périssable).
    assert res["fields"]["opening_hours"] == "Lun–Sam 9h–21h" + ce.HOURS_INDICATIVE_SUFFIX
    assert res["source_url"].startswith("https://")
    assert meta["web_searches"] == 2 and meta["cost_cts"] > 0


def test_completion_without_proof_is_dropped():
    """PREUVE OU RIEN : une entrée sans source_url n'est jamais retenue."""
    poi = {"id": "p1", "name": "Radio Taxi", "address": "", "missing": ["phone"]}
    cli = Fake(_web_msg(json.dumps({"p1": {"phone": "+34 900 000 000"}})))  # sans source_url
    out, _ = ce.complete_service_pois(
        "taxi", "Taxi", [poi], "Orihuela Costa", "ES", cli, today="2026-08-12")
    assert out == {}


def test_completion_central_number_hint_for_taxi_and_police():
    poi = {"id": "p1", "name": "Parada", "address": "", "missing": ["phone"]}
    cli = Fake(_web_msg(json.dumps({})))
    ce.complete_service_pois("taxi", "Taxi", [poi], "X", "ES", cli, today="2026-08-12")
    assert "CENTRAL" in cli.messages.prompts[0]
    cli2 = Fake(_web_msg(json.dumps({})))
    ce.complete_service_pois("doctor", "Médecin", [poi], "X", "ES", cli2, today="2026-08-12")
    assert "CENTRAL" not in cli2.messages.prompts[0]   # pas de central pour un médecin


def test_completion_empty_object_is_valid():
    poi = {"id": "p1", "name": "X", "address": "", "missing": ["phone"]}
    cli = Fake(_web_msg("{}"))
    out, _ = ce.complete_service_pois("taxi", "Taxi", [poi], "X", "ES", cli,
                                      today="2026-08-12")
    assert out == {}


def test_completion_malformed_raises_without_write():
    poi = {"id": "p1", "name": "X", "address": "", "missing": ["phone"]}
    for bad in ["je n'ai rien trouvé", '{"p1": {oops}}', '["a", "b"]']:
        cli = Fake(_web_msg(bad))
        try:
            ce.complete_service_pois("taxi", "Taxi", [poi], "X", "ES", cli,
                                     today="2026-08-12")
            assert False, f"aurait dû lever pour {bad!r}"
        except ValueError:
            pass


def test_completion_ignores_unknown_ref():
    """Un id renvoyé qui ne figure pas dans le lot est ignoré (jamais d'écriture
    sur une fiche non demandée)."""
    poi = {"id": "p1", "name": "X", "address": "", "missing": ["phone"]}
    payload = {"intrus": {"phone": "+34 900", "source_url": "https://x"}}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, _ = ce.complete_service_pois("taxi", "Taxi", [poi], "X", "ES", cli,
                                      today="2026-08-12")
    assert out == {}


# ── Baby-sitting : création, vide assumé ─────────────────────────────────────

def test_babysitters_resolved_with_proof():
    payload = {"services": [
        {"name": "Canguros Costa", "phone": "+34 966 000 111",
         "website": "https://canguroscosta.example",
         "source_url": "https://canguroscosta.example", "verified_on": "2026-08-12"},
        {"name": "  ", "phone": "x"},   # sans nom → écarté
    ]}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, meta = ce.fetch_babysitters("Orihuela Costa", "ES", cli, today="2026-08-12")
    assert [s["name"] for s in out] == ["Canguros Costa"]
    assert out[0]["phone"] == "+34 966 000 111"
    assert out[0]["source_url"].startswith("https://")
    assert meta["cost_cts"] > 0


def test_babysitters_empty_is_valid_result():
    cli = Fake(_web_msg(json.dumps({"services": []})))
    out, _ = ce.fetch_babysitters("Nowhere", "ES", cli, today="2026-08-12")
    assert out == []


def test_babysitters_malformed_raises():
    for bad in ["prose", '{"services": [oops]}', '{"foo": 1}']:
        cli = Fake(_web_msg(bad))
        try:
            ce.fetch_babysitters("X", "ES", cli, today="2026-08-12")
            assert False, f"aurait dû lever pour {bad!r}"
        except ValueError:
            pass


def test_babysitter_prompt_uses_web_tool_geo_targeted():
    cli = Fake(_web_msg(json.dumps({"services": []})))
    ce.fetch_babysitters("Berlin", "DE", cli, today="2026-08-12")
    tool = cli.messages.tools[0][0]
    assert tool["type"] == "web_search_20250305"
    assert tool["user_location"]["country"] == "DE"
    assert "BABY-SITTING" in cli.messages.prompts[0]
