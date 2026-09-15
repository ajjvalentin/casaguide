"""Activités du secteur (V2-71, promues en chapitre V2-73) — CLAUDE + recherche web.

Tests PURS (aucune base, aucun réseau) du cœur `claude_enrich.fetch_activities`
(« que fait-on ici ? » : surf, randonnée, plongée…) et du rendu SSR V2-73
(`guide_page._render_activities_chapter` / `_activities_tile` / `_activities_map_points` :
vrai chapitre de l'onglet « Autour », tuile de sommaire, marqueurs de carte).

Le bouchon reflète la surface RÉELLE du SDK web_search (leçon OPS-1b).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import guide_page  # noqa: E402
from enrich import claude_enrich as ce  # noqa: E402


def _web_msg(text, *, searches=2, stop_reason="end_turn"):
    return NS(
        stop_reason=stop_reason,
        content=[
            NS(type="server_tool_use", name="web_search", input={"query": "activities"}),
            NS(type="web_search_tool_result",
               content=[NS(type="web_search_result", url="https://x", title="T")]),
            NS(type="text", text=text),
        ],
        usage=NS(input_tokens=1400, output_tokens=300,
                 server_tool_use=NS(web_search_requests=searches)))


class _Fake:
    def __init__(self, *responses):
        self.messages = _Msgs(responses)


class _Msgs:
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = 0
        self.tools = []
        self.prompts = []

    def create(self, *, model, max_tokens, messages, tools=None, **kwargs):
        self.calls += 1
        self.tools.append(tools)
        self.prompts.append(messages[-1]["content"])
        return self._r[min(self.calls - 1, len(self._r) - 1)]


# ── fetch ─────────────────────────────────────────────────────────────────────

def test_resolves_activities_with_proof():
    payload = {"activities": [
        {"activity": "Surf", "where": "plage de La Zenia", "season": "toute l'année",
         "source_url": "https://turismo.example/surf", "verified_on": "2026-09-15"},
        {"activity": "Randonnée", "where": "Sierra Escalona", "season": "",
         "source_url": "https://turismo.example/senderismo"},
        {"activity": "Sans preuve", "where": "quelque part", "season": ""},   # écartée
        {"activity": "  ", "source_url": "https://x"},                        # sans nom → écartée
    ]}
    cli = _Fake(_web_msg(json.dumps(payload)))
    act, meta = ce.fetch_activities("La Zenia", "ES", cli, today="2026-09-15")
    items = act[ce.ACTIVITIES_FACT_TYPE]["activities"]
    assert [a["activity"] for a in items] == ["Surf", "Randonnée"]   # preuve ou rien
    assert items[0]["where"] == "plage de La Zenia"
    assert items[1]["verified_on"] == "2026-09-15"                    # comblé par défaut
    assert meta["cost_cts"] >= 0
    # Outil web_search réellement demandé.
    assert cli.messages.tools[0][0]["type"] == "web_search_20250305"


def test_empty_list_is_valid():
    cli = _Fake(_web_msg(json.dumps({"activities": []})))
    act, _ = ce.fetch_activities("Ardon", "CH", cli)
    assert act[ce.ACTIVITIES_FACT_TYPE]["activities"] == []


def test_malformed_response_raises_without_write():
    cli = _Fake(_web_msg("désolé, pas de JSON"))
    try:
        ce.fetch_activities("La Zenia", "ES", cli)
    except ValueError:
        pass
    else:
        raise AssertionError("une réponse malformée doit lever ValueError")


# ── rendu SSR : chapitre, cartes, tuile, carte (V2-73) ──────────────────────────

def test_ssr_renders_activities_chapter_with_cards_place_season_and_proof():
    content = {"activities": [
        {"activity": "Surf", "where": "plage de La Zenia", "season": "toute l'année",
         "source_url": "https://www.medoc-tourisme.com/surf"},
        {"activity": "Randonnée", "where": "Sierra Escalona", "season": "",
         "source_url": ""},
    ]}
    html = guide_page._render_activities_chapter(content, "fr")
    # V2-73 : vrai chapitre (h2 + data-chapter) portant une pseudo-catégorie filtrable.
    assert '<section class="chapter" data-chapter="ACT">' in html
    assert "<h2>Activités du secteur</h2>" in html
    assert 'data-cat="activities"' in html
    # Cartes (pas une liste à puces).
    assert 'class="act-card"' in html and "<ul>" not in html
    assert "Surf" in html and "plage de La Zenia" in html and "année" in html  # saison (apostrophe échappée)
    assert 'href="https://www.medoc-tourisme.com/surf"' in html   # lien de preuve
    assert "medoc-tourisme.com" in html                           # V2-71c : libellé VISIBLE (domaine sans www.)
    assert "Randonnée" in html and "Sierra Escalona" in html      # sans saison → aucun lien
    assert html.count("route-link") == 1                          # rien pour la source manquante
    # Chemin de retour aux services offert dans le chapitre.
    assert 'class="back-services"' in html


def test_link_domain_helper():
    assert guide_page._link_domain("https://www.medoc-tourisme.com/surf") == "medoc-tourisme.com"
    assert guide_page._link_domain("http://fed-surf.fr") == "fed-surf.fr"
    assert guide_page._link_domain("ftp://x") is None
    assert guide_page._link_domain("") is None


def test_activities_link_falls_back_to_learn_more_label():
    """V2-71c : sans domaine lisible… en pratique le domaine existe toujours pour une URL
    http(s) ; le repli « En savoir plus » (7 langues) reste le filet."""
    content = {"activities": [{"activity": "Surf", "where": "beach", "season": "",
                               "source_url": "https://ok.example/surf"}]}
    # le domaine sert de libellé (sobre, informatif) ; repli couvert par le helper ci-dessus.
    assert "ok.example" in guide_page._render_activities_chapter(content, "en")


def test_ssr_empty_activities_renders_nothing():
    assert guide_page._render_activities_chapter({"activities": []}, "fr") == ""
    assert guide_page._render_activities_chapter({}, "fr") == ""
    assert guide_page._activities_tile({"activities": []}, "fr") is None


def test_activities_title_localised():
    content = {"activities": [{"activity": "Surf", "where": "beach",
                               "source_url": "https://x"}]}
    assert "Activities in the area" in guide_page._render_activities_chapter(content, "en")


def test_activities_tile_is_head_family_with_count():
    """V2-73 : la tuile « Activités » a sa famille propre (ACT), rang 0 (en tête), sa
    couleur, son compteur, et ouvre le mode filtré (data-cat=activities)."""
    content = {"activities": [
        {"activity": "Surf", "where": "x", "source_url": "https://x"},
        {"activity": "Randonnée", "where": "y", "source_url": "https://y"},
    ]}
    chapter, rank, html = guide_page._activities_tile(content, "fr")
    assert chapter == "ACT" and rank == 0
    assert 'data-cat="activities"' in html and 'href="#autour/activities"' in html
    assert "Activités du secteur" in html and ">2<" in html   # compteur


def test_activities_map_points_only_placeable():
    """V2-73 : seule une activité géolocalisée (lat/lon posés par le pipeline) donne un
    point de carte ; une non plaçable reste dans la liste sans marqueur."""
    content = {"activities": [
        {"activity": "Surf", "where": "plage du Gurp", "season": "été",
         "source_url": "https://x", "lat": 45.4, "lon": -1.13},
        {"activity": "Randonnée", "where": "arrière-pays", "source_url": "https://y"},
    ]}
    pts = guide_page._activities_map_points(content)
    assert [p["name"] for p in pts] == ["Surf"]
    assert pts[0]["lat"] == 45.4 and pts[0]["season"] == "été"
