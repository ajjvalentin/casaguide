"""Marchés hebdomadaires par zone (V2-07 volet 3) — CLAUDE + recherche web.

Tests PURS (aucune base, aucun réseau) : `claude_enrich.fetch_markets`
(validation stricte : jour obligatoire, preuve obligatoire, mention « Horaires
indicatifs », vide valide, malformé rejeté) et la déduplication
`market_matches_existing` (nom normalisé + jour + distance — un `edited` n'est
jamais recréé, un `rejected` ne ressuscite jamais). Bouchon à surface SDK réelle
(OPS-1b : blocs server_tool_use/web_search_tool_result + usage.server_tool_use).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrich import claude_enrich as ce  # noqa: E402


def _web_msg(text, *, searches=2, stop_reason="end_turn"):
    return NS(
        stop_reason=stop_reason,
        content=[
            NS(type="server_tool_use", name="web_search", input={"query": "x"}),
            NS(type="web_search_tool_result",
               content=[NS(type="web_search_result", url="https://x", title="y")]),
            NS(type="text", text=text),
        ],
        usage=NS(input_tokens=1500, output_tokens=350,
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


# ── Découverte : validation stricte ──────────────────────────────────────────

def test_markets_resolved_with_day_note_and_proof():
    payload = {"markets": [
        {"name": "Mercadillo de La Zenia", "weekday": 6, "hours": "8h00–14h00",
         "character": "fruits, vêtements", "address": "Plaza de La Zenia",
         "lat": 37.93, "lon": -0.75, "source_url": "https://orihuela.es/mercadillos",
         "verified_on": "2026-08-12", "doubtful": False},
        {"name": "Sans jour", "source_url": "https://x"},            # pas de weekday → écarté
        {"name": "Jour hors bornes", "weekday": 9, "source_url": "https://x"},  # 9 → écarté
        {"name": "Sans preuve", "weekday": 3},                       # pas de source_url → écarté
    ]}
    cli = Fake(_web_msg(json.dumps(payload)))
    fact, meta = ce.fetch_markets("Orihuela Costa", "ES", cli, today="2026-08-12")
    markets = fact[ce.MARKET_FACT_TYPE]["markets"]
    assert [m["name"] for m in markets] == ["Mercadillo de La Zenia"]  # seul le valide
    m = markets[0]
    assert m["weekday"] == 6
    assert m["weekday_note"] == "8h00–14h00 — fruits, vêtements" + ce.HOURS_INDICATIVE_SUFFIX
    assert m["lat"] == 37.93 and m["lon"] == -0.75
    assert m["source_url"].startswith("https://") and meta["cost_cts"] > 0


def test_markets_weekday_must_be_int_not_bool_or_string():
    payload = {"markets": [
        {"name": "Bool", "weekday": True, "source_url": "https://x"},   # bool → écarté
        {"name": "Str", "weekday": "6", "source_url": "https://x"},     # "6" → écarté
    ]}
    fact, _ = ce.fetch_markets("X", "ES", Fake(_web_msg(json.dumps(payload))),
                               today="2026-08-12")
    assert fact[ce.MARKET_FACT_TYPE]["markets"] == []


def test_markets_doubtful_is_flagged_in_note():
    payload = {"markets": [
        {"name": "Torretas", "weekday": 2, "hours": "", "character": "brocante",
         "address": "Los Dolses", "source_url": "https://x", "doubtful": True}]}
    fact, _ = ce.fetch_markets("X", "ES", Fake(_web_msg(json.dumps(payload))),
                               today="2026-08-12")
    m = fact[ce.MARKET_FACT_TYPE]["markets"][0]
    assert m["doubtful"] is True and "à confirmer" in m["weekday_note"]
    assert ce.HOURS_INDICATIVE_SUFFIX not in m["weekday_note"]  # pas d'horaires → pas de mention


def test_markets_empty_is_valid():
    fact, _ = ce.fetch_markets("Nowhere", "ES", Fake(_web_msg(json.dumps({"markets": []}))),
                               today="2026-08-12")
    assert fact[ce.MARKET_FACT_TYPE]["markets"] == []


def test_markets_malformed_raises_without_write():
    for bad in ["aucun marché trouvé", '{"markets": [oops]}', '{"foo": 1}',
                '["a", "b"]']:
        try:
            ce.fetch_markets("X", "ES", Fake(_web_msg(bad)), today="2026-08-12")
            assert False, f"aurait dû lever pour {bad!r}"
        except ValueError:
            pass


def test_market_prompt_excludes_fixed_shops_and_uses_web_geo():
    ce.fetch_markets("Berlin", "DE", Fake(_web_msg(json.dumps({"markets": []}))),
                     today="2026-08-12")
    cli = Fake(_web_msg(json.dumps({"markets": []})))
    ce.fetch_markets("Berlin", "DE", cli, today="2026-08-12")
    prompt = cli.messages.prompts[0]
    assert "MARCHÉS HEBDOMADAIRES" in prompt
    for excl in ("supérettes", "supermarchés", "COUVERTS"):
        assert excl in prompt          # écueil « supérette classée marché »
    assert "RÉCENTE" in prompt          # écueil « données mortes »
    tool = cli.messages.tools[0][0]
    assert tool["type"] == "web_search_20250305" and tool["user_location"]["country"] == "DE"


# ── Déduplication (pure) ─────────────────────────────────────────────────────

def _ex(name, weekday, lat, lon, status="edited"):
    return {"name": name, "weekday": weekday, "lat": lat, "lon": lon, "status": status}


def test_dedup_name_variant_same_day_is_duplicate():
    existing = [_ex("Mercadillo de La Zenia", 6, 37.93, -0.75)]
    assert ce.market_matches_existing("Mercado de la Zenia", 6, None, None, existing)


def test_dedup_rejected_never_resurrected():
    existing = [_ex("Rastro de Los Dolses", 3, 37.94, -0.74, status="rejected")]
    # Même marché redécouvert → reconnu comme doublon (donc jamais recréé).
    assert ce.market_matches_existing("Rastro Los Dolses", 3, 37.9401, -0.7401, existing)


def test_dedup_same_spot_but_different_day_is_not_duplicate():
    """Deux marchés à la MÊME place mais des JOURS différents sont DISTINCTS."""
    existing = [_ex("Mercadillo del martes", 2, 37.93, -0.75)]
    assert ce.market_matches_existing("Mercadillo del sábado", 6, 37.9301, -0.7501,
                                      existing) is None


def test_dedup_distinct_market_is_created():
    existing = [_ex("Mercadillo de La Zenia", 6, 37.93, -0.75)]
    assert ce.market_matches_existing("Mercadillo de Torrevieja", 5, 37.98, -0.68,
                                      existing) is None


def test_dedup_name_prefilter_without_position():
    """Pré-dédup par NOM seul (avant géocodage) : nom quasi-identique → doublon
    même sans coordonnées."""
    existing = [_ex("Mercadillo de La Zenia", 6, 37.93, -0.75)]
    assert ce.market_matches_existing("Mercadillo La Zenia", 6, None, None, existing)


# ── V2-07 volet 3bis : robustesse de l'appel web_search (retry, coût, troncature) ─

def test_web_search_truncation_error_mentions_stop_reason_max_tokens():
    """Réponse tronquée (stop_reason='max_tokens') non parsable → l'erreur MENTIONNE
    max_tokens (smoking gun du plafond de sortie) et porte le coût de CHAQUE essai."""
    truncated = '{"markets": [{"name": "A", "weekday": 6, "source_url": "http'  # coupé
    cli = Fake(_web_msg(truncated, searches=2, stop_reason="max_tokens"))
    try:
        ce._ask_web_search_json(cli, "prompt", city="X", country_code="ES")
        assert False, "aurait dû lever WebSearchJSONError"
    except ce.WebSearchJSONError as e:
        assert "max_tokens" in str(e)
        assert e.stop_reason == "max_tokens"
        assert len(e.attempts) == 2                 # essai + retry, tous deux comptés
        assert all(a["cost_cts"] >= 0 for a in e.attempts)
    assert cli.messages.calls == 2                   # la réponse a bien été RÉGÉNÉRÉE


def test_web_search_retry_first_malformed_then_valid_counts_both_costs():
    """1er essai malformé + 2e valide → résultat écrit, DEUX coûts comptés
    (`meta.attempts`), et l'appel a bien été relancé (calls == 2)."""
    valid = json.dumps({"markets": []})
    cli = Fake(_web_msg("pas du JSON", searches=1),
               _web_msg(valid, searches=3))
    data, meta = ce._ask_web_search_json(cli, "p", city="X", country_code="ES")
    assert data == {"markets": []}
    assert len(meta["attempts"]) == 2
    assert meta["web_searches"] == 1 + 3             # somme des deux essais
    assert meta["cost_cts"] == round(sum(a["cost_cts"] for a in meta["attempts"]), 4)
    assert cli.messages.calls == 2


def test_web_search_success_first_try_is_single_attempt():
    """Un succès du premier coup n'engage qu'UN essai (pas de retry inutile)."""
    cli = Fake(_web_msg(json.dumps({"markets": []})))
    _, meta = ce._ask_web_search_json(cli, "p", city="X", country_code="ES")
    assert len(meta["attempts"]) == 1 and cli.messages.calls == 1
