"""V2-56 — parsing de la découverte éditoriale « sorties » (`fetch_reputed_places`).

Aucun réseau : le client Claude est bouché (surface web_search réelle, OPS-1b)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from enrich import claude_enrich  # noqa: E402


def _web_reply(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              server_tool_use=SimpleNamespace(web_search_requests=2)))


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.messages = self

    def create(self, *, model, max_tokens, messages, tools=None, **kw):
        assert tools and tools[0]["type"] == "web_search_20250305"
        assert "RÉPUTÉES" in messages[0]["content"]
        return _web_reply(self._payload)


def _fetch(places):
    client = _FakeClient(json.dumps({"places": places}))
    return claude_enrich.fetch_reputed_places("La Zenia", "ES", client,
                                              today="2026-09-13")


def test_keeps_valid_places_with_category_and_reason():
    out, meta = _fetch([
        {"name": "Brown's Cocktail Bar", "category": "bar",
         "address": "Calle Brown 1, La Zenia", "reason": "Cocktails réputés.",
         "phone": "+34 966 111 222", "website": "https://browns.example",
         "source_url": "https://guide.example/bares"},
        {"name": "Casa Manolo", "category": "restaurant",
         "address": "Av Manolo 2", "reason": "Arroces.",
         "source_url": "https://guide.example/restos"},
    ])
    assert [p["name"] for p in out] == ["Brown's Cocktail Bar", "Casa Manolo"]
    assert out[0]["category"] == "bar" and out[0]["phone"] and out[0]["website"]
    assert out[0]["reason"] == "Cocktails réputés."
    assert out[1]["verified_on"] == "2026-09-13"       # comblé par défaut
    assert "website" not in out[1]                       # absent → non porté
    assert meta["cost_cts"] >= 0


def test_rejects_without_proof_or_unknown_category():
    out, _ = _fetch([
        {"name": "Sans preuve", "category": "bar", "address": "Rue X"},   # pas de source
        {"name": "Sans adresse", "category": "cafe",
         "source_url": "https://x.example"},                              # pas d'adresse
        {"name": "Hôtel Luxe", "category": "hotel", "address": "Av Y",
         "source_url": "https://x.example"},                             # catégorie hors sorties
        {"name": "Café Bon", "category": "CAFE", "address": "Plaza Z",
         "source_url": "https://x.example"},                             # OK (casse tolérée)
    ])
    assert [p["name"] for p in out] == ["Café Bon"]
    assert out[0]["category"] == "cafe"


def test_empty_list_is_valid():
    out, _ = _fetch([])
    assert out == []
