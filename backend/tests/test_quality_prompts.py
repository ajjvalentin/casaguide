"""Passe qualité des prompts d'enrichissement (V2-35) — tests PURS.

Interdiction du REMPLISSAGE dans `describe_pois` (« n'invente jamais » étendu au
style), validation de forme des tags de cuisine, et extension de `service_fields`
aux catégories « en savoir plus » (site seul). Bouchon `_ask_json` à surface SDK
réelle (blocs `text` + `usage`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrich import claude_enrich as ce  # noqa: E402
from enrich import overpass  # noqa: E402


class _PlainMessages:
    """Bouchon de `client.messages.create` pour `_ask_json` (sans recherche web)."""

    def __init__(self, payload_json: str):
        self.payload = payload_json
        self.prompts: list[str] = []

    def create(self, *, model, max_tokens, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return NS(content=[NS(type="text", text=self.payload)],
                  usage=NS(input_tokens=500, output_tokens=200))


class _PlainClient:
    def __init__(self, payload_json: str):
        self.messages = _PlainMessages(payload_json)


# ── Pièce (a) : descriptions — interdiction du remplissage ───────────────────

def test_describe_prompt_carries_the_bans_and_silence_rule():
    """Le prompt envoyé porte les tournures BANNIES explicites et la règle du
    silence (motif du test « contexte marques » du volet 1)."""
    cli = _PlainClient(json.dumps({"node/1": ""}))
    ce.describe_pois([{"source_ref": "node/1", "name": "La Marquesa",
                       "category": "sport"}], "Orihuela", "ES", cli)
    prompt = cli.messages.prompts[0]
    for banned in ("à visiter", "accessible aux vacanciers", "durant votre séjour",
                   "intéressés par la culture locale", "pour un repas sur place",
                   "pour les visiteurs", "option de restauration"):
        assert banned in prompt
    assert "ÉTENDU AU STYLE" in prompt and "VIDE" in prompt
    assert "FAIT SPÉCIFIQUE" in prompt


def test_describe_empty_is_written_empty_never_generic():
    """Une description vide (ou blanche) est ACCEPTÉE telle quelle : absente du
    résultat → l'upsert n'écrit rien (jamais de repli vers du générique)."""
    cli = _PlainClient(json.dumps({"node/1": "", "node/2": "   "}))
    descs, _ = ce.describe_pois(
        [{"source_ref": "node/1", "name": "X", "category": "sight"},
         {"source_ref": "node/2", "name": "Y", "category": "sight"}],
        "Orihuela", "ES", cli)
    assert descs == {}


def test_describe_filler_response_is_dropped_factual_kept():
    """Garde-fou de réception : une réponse de REMPLISSAGE est traitée comme vide
    (jamais écrite) même si le modèle désobéit ; une phrase FACTUELLE est gardée."""
    cli = _PlainClient(json.dumps({
        "node/1": "Site à visiter à Orihuela, accessible aux vacanciers.",  # remplissage
        "node/2": "Restaurant de tapas réputé pour ses gambas al ajillo."}))  # factuel
    descs, _ = ce.describe_pois(
        [{"source_ref": "node/1", "name": "La Marquesa", "category": "sight"},
         {"source_ref": "node/2", "name": "Casa Pepe", "category": "restaurant"}],
        "Orihuela", "ES", cli)
    assert "node/1" not in descs
    assert descs["node/2"].startswith("Restaurant de tapas")


def test_is_filler_detects_family_and_spares_factual():
    assert ce.is_filler_description("À DÉCOUVRIR lors de votre séjour à Orihuela.")
    assert ce.is_filler_description("Accessible aux vacanciers intéressés par la culture locale.")
    assert not ce.is_filler_description(
        "Marché couvert de 1900, spécialité de turrón d'Alicante.")
    assert not ce.is_filler_description("")


def test_filler_v2_catches_ardon_escapees():
    """V2-37 : les quatre variantes qui avaient échappé au recensement d'Ardon."""
    escapees = [
        "Restaurant où les vacanciers peuvent prendre leurs repas.",
        "Établissement pouvant convenir aux vacanciers cherchant un repas à proximité.",
        "Une option de restauration disponible dans le village.",
        "Espace muséal pour les visiteurs.",   # Fondation Domus
    ]
    for e in escapees:
        assert ce.is_filler_description(e), e


def test_filler_v2_spares_factual_never_overblocks():
    """Sans sur-bloquer : le factuel (Chaplin, Bungy « saut à l'élastique ») ne
    matche JAMAIS le motif généralisé."""
    factual = [
        "Bains thermaux de Saillon, complexe apprécié depuis le tournage de Chaplin.",
        "Bungy : saut à l'élastique de 190 m depuis le barrage du Verzasca.",
        "Restaurant de tapas réputé pour ses gambas al ajillo.",
        "Musée ouvert du mardi au dimanche, collection de peinture valaisanne.",
    ]
    for f in factual:
        assert not ce.is_filler_description(f), f


def test_describe_prompt_forbids_inventing_commune_and_passes_locality():
    """V2-37 : la règle « ne jamais affirmer une commune non fournie » est au prompt,
    et la localité RÉELLE de chaque POI (ville de l'adresse OSM) y est transmise."""
    cli = _PlainClient(json.dumps({"node/1": ""}))
    ce.describe_pois([{"source_ref": "node/1", "name": "Le Régence",
                       "category": "restaurant", "locality": "Vétroz"}],
                     "Ardon", "CH", cli)
    prompt = cli.messages.prompts[0]
    assert "n'affirme JAMAIS la commune" in prompt        # règle localité
    assert "localité : Vétroz" in prompt                  # localité RÉELLE transmise


def test_describe_prompt_omits_locality_when_unknown():
    """Sans localité fournie, rien n'est transmis (le prompt interdit toute
    mention de lieu — jamais « à {city} » par défaut)."""
    cli = _PlainClient(json.dumps({"node/1": ""}))
    ce.describe_pois([{"source_ref": "node/1", "name": "Chez Machin",
                       "category": "restaurant"}], "Ardon", "CH", cli)
    prompt = cli.messages.prompts[0]
    assert "localité :" not in prompt.split("Points d'intérêt")[1]  # aucune localité listée


# ── Pièce (b) : tags de cuisine — 1 à 3 mots, jamais une phrase ──────────────

def test_cuisine_phrase_is_ignored_not_truncated():
    # Prose (virgules, pas de `;`) → IGNORÉE entièrement (ni « modern » ni fragment).
    assert overpass._norm_cuisine("Modern, international cuisine and mixology") is None
    # Tags valides (1-3 mots) conservés ; multi-valué `;` → 1er terme.
    assert overpass._norm_cuisine("Seafood;spanish") == "seafood"
    assert overpass._norm_cuisine("fish and chips") == "fish and chips"
    assert overpass._norm_cuisine("italian") == "italian"
    assert overpass._norm_cuisine("very fancy modern cuisine") is None   # 4 mots → ignoré
    assert overpass._norm_cuisine("") is None


# ── Pièce (c) : site web pour les catégories « en savoir plus » ──────────────

def test_service_fields_site_only_for_sight_family_sport():
    for cat in ("sight", "family_activity", "sport"):
        assert ce.service_fields(cat) == ("website",)   # site SEUL (ni tel ni horaires)
        assert cat in ce.SERVICE_COMPLETE_CATEGORIES
    # Les catégories existantes ne changent pas.
    assert ce.service_fields("pharmacy") == ("phone", "website", "opening_hours")
    # Restauration : tél + site (V2-37), jamais d'horaires.
    assert ce.service_fields("restaurant") == ("phone", "website")
