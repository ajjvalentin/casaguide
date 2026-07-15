"""Enrichissement par l'API Claude (étape 3 du pipeline, §5.1).

Deux usages, aux coûts maîtrisés :
  1. area_facts : numéros d'urgence, règles de tri, réglementation bruit
     pour le couple (pays, commune) — mutualisé entre logements (§4).
  2. Descriptions courtes des POI "éditoriaux" (restaurants, plages, sites…).

Toutes les réponses sont demandées en JSON strict et validées avant insertion.
Chaque appel est comptabilisé (tokens -> centimes) pour la table api_costs.
"""
from __future__ import annotations

import json

import anthropic

from .settings import settings

AREA_FACT_TYPES = ("emergency_numbers", "waste_rules", "noise_rules")

_AREA_PROMPT = """\
Tu prépares les données locales d'un guide de logement de vacances situé à
{city} ({country_code}). Réponds UNIQUEMENT avec un objet JSON valide, sans
markdown ni commentaire, avec exactement ces clés :

{{
  "emergency_numbers": {{"items": [{{"label": "...", "number": "..."}}],
                         "notes": "..."}},
  "waste_rules": {{"summary": "...",
                   "containers": [{{"color_or_type": "...", "accepts": "..."}}]}},
  "noise_rules": {{"summary": "...", "quiet_hours": "..."}}
}}

OBJECTIF : uniquement l'ESSENTIEL ACTIONNABLE pour un vacancier qui arrive et
doit se débrouiller seul — rien d'autre. Un touriste, pas un administré.

Contraintes de CONTENU :
- emergency_numbers : numéros RÉELLEMENT en vigueur dans ce pays (112 européen
  inclus), avec un libellé court. `notes` : une précision utile seulement si elle
  aide à composer le bon numéro, sinon "".
- waste_rules : pour CHAQUE conteneur, sa couleur (ou son type) et, en une poignée
  de mots, CE QU'ON Y MET (« emballages plastique et métal », « verre », « papier
  et carton », « déchets restants »). `summary` : une phrase pratique maximum
  (ex. jour de sortie des ordures si typique), sinon "".
- noise_rules : `quiet_hours` = la plage horaire de silence (ex. « 23h00–08h00 »)
  et rien d'autre dans ce champ ; `summary` : une phrase pratique maximum.

INTERDICTIONS STRICTES (à ne jamais écrire) :
- Aucun contexte administratif ni juridique : pas de « la commune applique le
  système… », pas de nom de loi, d'ordonnance, d'organisme, de dispositif ni de
  société de collecte.
- Aucune généralité, mise en garde, considération environnementale ou
  pédagogique (« il est important de trier », « respectez le voisinage »…).
- Pas de phrase de remplissage : si tu n'as rien d'actionnable et de fiable pour
  un champ texte, mets une chaîne vide ("") plutôt que de meubler.
- Textes courts, factuels, impératifs, en français. N'invente jamais.
"""

_POI_PROMPT = """\
Voici des points d'intérêt proches d'un logement de vacances à {city}
({country_code}). Pour chacun, écris une description d'UNE phrase (max 25 mots),
utile et factuelle, en français, sans superlatifs inventés.

RÈGLES STRICTES (anti-hallucination) — le respect est impératif :
- N'affirme AUCUN fait qui ne découle pas des données fournies ici (nom,
  catégorie, ville {city}). En particulier, n'invente jamais de localisation,
  de commune, de quartier, de distance, d'horaire, de prix, de note ou
  d'anecdote historique.
- Ne cite pas d'autre ville que {city}, sauf si elle apparaît explicitement
  dans le nom du POI.
- Limite-toi au TYPE d'établissement (déduit de sa catégorie) et à son usage
  pour un vacancier, formulé de façon générique et prudente.
- Si tu n'as rien de fiable et de spécifique à écrire, renvoie une chaîne vide
  ("") pour ce POI plutôt que d'inventer.

Réponds UNIQUEMENT avec un objet JSON valide : {{"<ref>": "description", ...}}

Points d'intérêt (ref, nom, catégorie) :
{poi_list}
"""


def _cost_cts(model: str, usage) -> float:
    """Coût en centimes d'euro à partir de l'usage retourné par l'API."""
    inp, out = settings.model_prices_usd.get(model, (3.0, 15.0))
    usd = usage.input_tokens / 1e6 * inp + usage.output_tokens / 1e6 * out
    return round(usd * settings.usd_to_eur * 100, 4)


def _ask_json(client: anthropic.Anthropic, prompt: str) -> tuple[dict, dict]:
    """Appel Claude -> (données JSON, méta {tokens, cost_cts})."""
    msg = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)  # lève ValueError si non-JSON -> step en échec, jamais de données corrompues
    meta = {
        "units": msg.usage.input_tokens + msg.usage.output_tokens,
        "cost_cts": _cost_cts(settings.anthropic_model, msg.usage),
    }
    return data, meta


def fetch_area_facts(city: str, country_code: str,
                     client: anthropic.Anthropic) -> tuple[dict, dict]:
    """Retourne ({fact_type: content}, méta coût). Valide la présence des 3 clés."""
    data, meta = _ask_json(client, _AREA_PROMPT.format(city=city, country_code=country_code))
    missing = [k for k in AREA_FACT_TYPES if k not in data]
    if missing:
        raise ValueError(f"Réponse IA incomplète, clés manquantes : {missing}")
    return {k: data[k] for k in AREA_FACT_TYPES}, meta


def describe_pois(pois: list[dict], city: str, country_code: str,
                  client: anthropic.Anthropic) -> tuple[dict, dict]:
    """Descriptions courtes pour une liste de POI [{source_ref, name, category}].

    Retourne ({source_ref: description}, méta coût).
    """
    if not pois:
        return {}, {"units": 0, "cost_cts": 0}
    poi_list = "\n".join(
        f'- ref "{p["source_ref"]}" : {p["name"]} ({p["category"]})' for p in pois
    )
    data, meta = _ask_json(
        client, _POI_PROMPT.format(city=city, country_code=country_code, poi_list=poi_list)
    )
    return {k: v for k, v in data.items() if isinstance(v, str) and v.strip()}, meta
