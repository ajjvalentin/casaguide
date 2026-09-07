"""Livraison de repas par zone (V2-07 volet 1) — CLAUDE + recherche web.

Tests PURS (aucune base, aucun réseau) du cœur `claude_enrich.fetch_food_delivery`
et du rendu SSR `guide_page._fact_food_delivery`.

Le bouchon de l'API reflète la SURFACE RÉELLE du SDK avec recherche web (leçon
OPS-1b) : blocs `server_tool_use` + `web_search_tool_result` + `text` final, et
`usage.server_tool_use.web_search_requests` pour la facturation. Un mock plus
« simple » que le réel masquerait les bugs de contrat au lieu de les révéler.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import guide_page  # noqa: E402
from enrich import claude_enrich as ce  # noqa: E402


# ── Bouchon fidèle de l'API web_search ───────────────────────────────────────

def _web_msg(text, *, searches=2, stop_reason="end_turn"):
    """Une réponse comme celle d'un vrai appel avec l'outil web_search : bloc
    server_tool_use (la requête), bloc web_search_tool_result (les résultats),
    puis le texte final. usage porte le compteur de recherches web."""
    return NS(
        stop_reason=stop_reason,
        content=[
            NS(type="server_tool_use", name="web_search",
               input={"query": "food delivery"}),
            NS(type="web_search_tool_result",
               content=[NS(type="web_search_result", url="https://example.com",
                           title="Coverage")]),
            NS(type="text", text=text),
        ],
        usage=NS(input_tokens=1500, output_tokens=350,
                 server_tool_use=NS(web_search_requests=searches)))


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)   # une réponse par appel (pause_turn…)
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


# ── Résolution & validation ──────────────────────────────────────────────────

def test_resolves_platforms_with_proof_and_neutral_names():
    payload = {"platforms": [
        {"name": "Glovo", "url": "https://glovoapp.com/es", "verified_on": "2026-08-11"},
        {"name": "Just Eat", "url": "https://just-eat.es", "verified_on": "2026-08-11"},
        {"name": "  ", "url": "https://x"},        # sans nom → écarté
    ], "note": "Bonne couverture."}
    cli = Fake(_web_msg(json.dumps(payload)))
    fd, meta = ce.fetch_food_delivery("Orihuela Costa", "ES", cli, today="2026-08-11")
    content = fd[ce.FOOD_DELIVERY_FACT_TYPE]
    assert [p["name"] for p in content["platforms"]] == ["Glovo", "Just Eat"]
    assert content["platforms"][0]["url"].startswith("https://")
    assert content["platforms"][0]["verified_on"] == "2026-08-11"
    assert content["note"] == "Bonne couverture."


def test_prompt_carries_two_level_brand_context_and_web_tool():
    """La connaissance marques→pays entre comme CONTEXTE du prompt (jamais comme
    réponse), et l'outil web_search est bien passé, géo-ciblé sur la commune."""
    cli = Fake(_web_msg(json.dumps({"platforms": [], "note": ""})))
    ce.fetch_food_delivery("Berlin", "DE", cli, today="2026-08-11")
    prompt = cli.messages.prompts[0]
    for brand in ("Lieferando", "Glovo", "Just Eat", "Uber Eats"):
        assert brand in prompt
    assert "N'invente" in prompt or "invente" in prompt
    tool = cli.messages.tools[0][0]
    assert tool["type"] == "web_search_20250305"
    assert tool["user_location"]["country"] == "DE"
    assert tool["user_location"]["city"] == "Berlin"


def test_empty_list_is_a_valid_result():
    cli = Fake(_web_msg(json.dumps({"platforms": [], "note": ""})))
    fd, _ = ce.fetch_food_delivery("Nowhere", "ES", cli, today="2026-08-11")
    assert fd[ce.FOOD_DELIVERY_FACT_TYPE]["platforms"] == []


def test_web_search_units_and_cost_are_accounted():
    """Les unités de recherche web sont comptées (comptabilité api_costs) et gonflent
    le coût au-delà des seuls tokens (10 $ / 1000 requêtes)."""
    cli = Fake(_web_msg(json.dumps({"platforms": [], "note": ""}), searches=4))
    _, meta = ce.fetch_food_delivery("X", "ES", cli, today="2026-08-11")
    assert meta["web_searches"] == 4
    assert meta["units"] == 1500 + 350 + 4        # tokens + recherches
    assert meta["cost_cts"] > 0


def test_json_wrapped_in_prose_and_citations_is_extracted():
    """Un modèle avec recherche web encadre souvent le JSON de prose/citations :
    on l'isole quand même (mais un contenu réellement non-JSON est rejeté)."""
    wrapped = ('Voici les plateformes qui livrent [1] :\n```json\n'
               '{"platforms":[{"name":"Uber Eats","url":"https://ubereats.com"}],'
               '"note":""}\n```\nSources : [1] example.com')
    cli = Fake(_web_msg(wrapped))
    fd, _ = ce.fetch_food_delivery("Madrid", "ES", cli, today="2026-08-11")
    assert [p["name"] for p in fd[ce.FOOD_DELIVERY_FACT_TYPE]["platforms"]] == ["Uber Eats"]


def test_malformed_response_raises_without_write():
    for bad in ["Je n'ai trouvé aucune plateforme fiable.",   # prose pure
                '{"platforms": [oops]}',                        # JSON cassé
                '["Glovo", "Just Eat"]',                        # pas un objet
                '{"foo": 1}']:                                  # 'platforms' absent
        cli = Fake(_web_msg(bad))
        try:
            ce.fetch_food_delivery("X", "ES", cli, today="2026-08-11")
            assert False, f"aurait dû lever pour {bad!r}"
        except ValueError:
            pass


def test_pause_turn_is_resumed_and_usage_accumulated():
    """La recherche web tourne dans une boucle serveur : un `pause_turn` est
    relancé, et l'usage (tokens + recherches) s'accumule sur les tours."""
    paused = _web_msg("", searches=2, stop_reason="pause_turn")
    done = _web_msg(json.dumps({"platforms": [{"name": "Wolt"}], "note": ""}),
                    searches=1)
    cli = Fake(paused, done)
    fd, meta = ce.fetch_food_delivery("Helsinki", "FI", cli, today="2026-08-11")
    assert cli.messages.calls == 2                 # relancé une fois
    assert [p["name"] for p in fd[ce.FOOD_DELIVERY_FACT_TYPE]["platforms"]] == ["Wolt"]
    assert meta["web_searches"] == 3               # 2 + 1
    assert meta["units"] == (1500 + 350) * 2 + 3


# ── Rendu SSR (motif area_facts) ─────────────────────────────────────────────

def test_ssr_renders_neutral_names_with_proof_links():
    content = {"platforms": [
        {"name": "Glovo", "url": "https://glovoapp.com/es"},
        {"name": "Just Eat", "url": "glovo.com"},   # sans schéma → pilule inerte
    ], "note": "Offre limitée."}
    # Le badge n'est PAS traduit (noms neutres) : identique en FR et EN.
    for lang in ("fr", "en"):
        html = guide_page._fact_food_delivery(content, lang)
        # Pilule-lien (V2-07 volet 1ter) : classe .route-link réutilisée, cible _blank.
        assert ('<a class="route-link" href="https://glovoapp.com/es" '
                'target="_blank" rel="noopener nofollow">Glovo') in html
        # url non http → pilule INERTE (span, pas de lien, pas d'icône externe).
        assert '<span class="route-link">Just Eat</span>' in html
        assert "Offre limitée." in html


def test_ssr_empty_or_no_platforms_renders_nothing():
    assert guide_page._fact_food_delivery({}, "fr") == ""
    assert guide_page._fact_food_delivery({"platforms": [], "note": "x"}, "fr") == ""


# ── V2-44 : plafond « local d'abord » du baby-sitting (règle V2-07 qui a glissé) ─

def test_babysitters_capped_to_three_local_first():
    """6 plateformes sorties au benchmark Op de Boerderie → on n'en garde que 3
    (le prompt les ordonne du plus local au plus national ; on tronque en tête)."""
    services = [{"name": f"Service {i}", "phone": f"+31 000 00{i}",
                 "source_url": "https://x.example", "verified_on": "2026-08-31"}
                for i in range(1, 7)]                # 6 services
    cli = Fake(_web_msg(json.dumps({"services": services})))
    out, meta = ce.fetch_babysitters("Noordgouwe", "NL", cli, today="2026-08-31")
    assert [s["name"] for s in out] == ["Service 1", "Service 2", "Service 3"]


def test_babysitter_prompt_orders_local_first():
    cli = Fake(_web_msg(json.dumps({"services": []})))
    ce.fetch_babysitters("Noordgouwe", "NL", cli, today="2026-08-31")
    prompt = cli.messages.prompts[0]
    assert "LOCAL D'ABORD" in prompt and "3" in prompt


# ── V2-44 volet 2 : découverte web des loueurs (preuve ou rien) ──────────────

def test_rentals_require_name_address_proof_and_contact():
    payload = {"rentals": [
        {"name": "Kassteele Tweewielers", "address": "Kloosterweg 44, Noordgouwe",
         "phone": "+31 111 22 33", "website": "https://kassteele.nl",
         "source_url": "https://kassteele.nl", "verified_on": "2026-08-31"},
        {"name": "Sans adresse", "phone": "+31 000", "source_url": "https://x"},
        {"name": "Sans preuve", "address": "Rue X 1, Ville", "phone": "+31 1"},
        {"name": "Sans contact", "address": "Rue Y 2, Ville", "source_url": "https://y"},
        {"address": "Rue Z 3, Ville", "phone": "+31 3", "source_url": "https://z"},
    ]}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, meta = ce.fetch_rentals("Noordgouwe", "NL", cli, today="2026-08-31")
    assert [r["name"] for r in out] == ["Kassteele Tweewielers"]   # seul le complet
    assert out[0]["address"] == "Kloosterweg 44, Noordgouwe"
    assert out[0]["source_url"].startswith("https://") and out[0]["phone"]


def test_rentals_empty_is_valid_and_prompt_uses_local_language():
    cli = Fake(_web_msg(json.dumps({"rentals": []})))
    out, _ = ce.fetch_rentals("Noordgouwe", "NL", cli, today="2026-08-31")
    assert out == []                                    # vide = résultat valide
    prompt = cli.messages.prompts[0]
    assert "fietsverhuur" in prompt and "LANGUE LOCALE" in prompt
    assert "PREUVE OU RIEN" in prompt and "ADRESSE POSTALE COMPLÈTE" in prompt
    tool = cli.messages.tools[0][0]
    assert tool["type"] == "web_search_20250305"
    assert tool["user_location"]["city"] == "Noordgouwe"


def test_rentals_malformed_raises_without_write():
    cli = Fake(_web_msg("désolé, indisponible"), _web_msg("encore indisponible"))
    try:
        ce.fetch_rentals("Noordgouwe", "NL", cli, today="2026-08-31")
        assert False, "attendu : ValueError"
    except ValueError:
        pass


# ── V2-50 : règles de service (contactabilité + qualification) ───────────────

def test_apply_service_rules_fills_qualifies_and_drops():
    pois = [
        {"name": "Alquiler Furgonetas Gregorio", "phone": "968 850 081"},   # contactable
        {"name": "Bicis Sol", "phone": "", "website": ""},                   # à compléter
        {"name": "Loueur fantôme", "phone": "", "website": ""},              # rien → retiré
    ]
    qual = {
        ce._norm_service_name("Alquiler Furgonetas Gregorio"):
            {"subtype": "fourgonnettes et camions", "source_url": "https://greg.example"},
        ce._norm_service_name("Bicis Sol"):
            {"website": "https://bicissol.example", "subtype": "vélos électriques",
             "source_url": "https://bicissol.example"},
        # « Loueur fantôme » absent → ni contact ni sous-type.
    }
    kept, dropped, qualified = ce.apply_service_rules(pois, qual)
    names = [p["name"] for p in kept]
    assert "Alquiler Furgonetas Gregorio" in names and "Bicis Sol" in names
    assert [p["name"] for p in dropped] == ["Loueur fantôme"]   # non contactable ET non qualifiable
    assert qualified == 2
    greg = next(p for p in kept if "Gregorio" in p["name"])
    assert greg["completion_meta"]["_qualification"]["subtype"] == "fourgonnettes et camions"
    assert greg["description_md"] == "fourgonnettes et camions"   # surfacé dans la fiche
    bici = next(p for p in kept if p["name"] == "Bicis Sol")
    assert bici["website"] == "https://bicissol.example"          # contact complété


def test_apply_service_rules_never_overwrites_existing_contact():
    pois = [{"name": "Taxi Central", "phone": "111", "website": ""}]
    qual = {ce._norm_service_name("Taxi Central"): {"phone": "999",
            "website": "https://taxi.example"}}
    kept, dropped, _ = ce.apply_service_rules(pois, qual)
    assert kept[0]["phone"] == "111"                        # jamais écrasé
    assert kept[0]["website"] == "https://taxi.example"     # complété (était vide)
    assert dropped == []


def test_qualify_services_parses_and_requires_proof_for_subtype():
    payload = {"places": [
        {"name": "Gregorio", "phone": "968 850 081", "subtype": "fourgonnettes",
         "source_url": "https://greg.example"},
        {"name": "SansPreuve", "subtype": "vélos"},              # subtype sans URL → écarté
    ]}
    cli = Fake(_web_msg(json.dumps(payload)))
    out, meta = ce.qualify_services("rental", "Location", [{"name": "Gregorio"},
                                    {"name": "SansPreuve"}], "Murcia", "ES", cli)
    g = out[ce._norm_service_name("Gregorio")]
    assert g["subtype"] == "fourgonnettes" and g["phone"]
    sp = out[ce._norm_service_name("SansPreuve")]
    assert "subtype" not in sp                               # preuve ou rien
