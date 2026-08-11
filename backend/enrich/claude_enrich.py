"""Enrichissement par l'API Claude (étape 3 du pipeline, §5.1).

Deux usages, aux coûts maîtrisés :
  1. area_facts : numéros d'urgence, règles de tri, réglementation bruit
     pour le couple (pays, commune) — mutualisé entre logements (§4).
  2. Descriptions courtes des POI "éditoriaux" (restaurants, plages, sites…).

Toutes les réponses sont demandées en JSON strict et validées avant insertion.
Chaque appel est comptabilisé (tokens -> centimes) pour la table api_costs.
"""
from __future__ import annotations

import datetime as _dt
import json

import anthropic

from .settings import settings

AREA_FACT_TYPES = ("emergency_numbers", "waste_rules", "noise_rules")

# Fait mutualisé (pays + commune) résolu par CLAUDE + recherche web (V2-07 volet 1).
FOOD_DELIVERY_FACT_TYPE = "food_delivery"

# Tarif de la recherche web de l'API Anthropic : ~10 $ / 1000 requêtes.
_WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000
# La recherche web tourne dans une boucle serveur : elle peut rendre la main en
# `pause_turn`. On relance quelques fois avant d'abandonner (best-effort).
_MAX_WEB_SEARCH_TURNS = 4

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


_FOOD_DELIVERY_PROMPT = """\
Tu prépares la section « Livraison de repas » du guide d'un logement de vacances
situé à {city} ({country_code}). Ton rôle : établir quelles PLATEFORMES de
livraison de repas livrent RÉELLEMENT dans cette commune, sous leur MARQUE LOCALE.

CONTEXTE (connaissance de fond, jamais une réponse toute faite) : les grands
groupes opèrent sous des marques différentes selon le pays —
- Just Eat Takeaway : « Just Eat » (ES, FR, IT, UK, IE…), « Lieferando » (DE, AT),
  « Thuisbezorgd » (NL) ;
- Delivery Hero : « Glovo », « Foodora »… ;
- « Uber Eats » ; « Wolt » ; « Deliveroo ».
Ce n'est qu'un repère : selon le pays et la commune, certaines de ces marques
n'existent pas, et d'autres, locales, peuvent exister.

MÉTHODE : utilise l'outil de recherche web pour VÉRIFIER, pour {city}
({country_code}), quelles marques y livrent effectivement aujourd'hui. Ne retiens
QUE les plateformes pour lesquelles tu as une PREUVE en ligne (une page montrant
la couverture de cette zone). Pour chacune : son nom de marque local, l'URL de la
preuve, et la date de vérification ({today}).

RÈGLES STRICTES :
- N'invente JAMAIS. Une plateforme sans preuve n'est pas retenue.
- Une liste VIDE est un résultat parfaitement valide (si rien ne livre, dis-le).
- Noms de marques tels quels (neutres, non traduits). Pas de prose superflue :
  `note` = une phrase courte au maximum (ex. « Zone périphérique, offre limitée »),
  sinon "".

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "platforms": [
    {{"name": "Glovo", "url": "https://...", "verified_on": "{today}"}}
  ],
  "note": ""
}}
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


# ── Livraison de repas par zone : CLAUDE + recherche web (V2-07 volet 1) ──────

def _extract_text(content) -> str:
    """Concatène les blocs `text` d'une réponse (ignore server_tool_use /
    web_search_tool_result). C'est là que vit le JSON final."""
    return "".join(
        b.text for b in content
        if getattr(b, "type", "") == "text" and getattr(b, "text", None))


def _parse_strict_json(text: str) -> dict:
    """JSON strict (même contrat que `_ask_json`). Un modèle avec recherche web
    peut encadrer sa réponse de citations : on isole alors l'objet `{...}`. Reste
    STRICT : un contenu réellement non-JSON lève ValueError (aucune écriture)."""
    s = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s)
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            return json.loads(s[i:j + 1])   # relève ValueError si toujours invalide
        raise


def _ask_web_search_json(client: anthropic.Anthropic, prompt: str, *,
                         city: str, country_code: str) -> tuple[dict, dict]:
    """Appel Claude AVEC l'outil de recherche web de l'API Anthropic → (données
    JSON, méta {units, cost_cts, web_searches}). La recherche web tourne côté
    serveur ; un `pause_turn` est relancé (boucle bornée). Les unités de recherche
    web sont comptabilisées dans le coût (10 $ / 1000 requêtes)."""
    tool = {
        "type": "web_search_20250305", "name": "web_search",
        "max_uses": settings.food_delivery_max_searches,
        "user_location": {"type": "approximate", "country": country_code,
                          "city": city},
    }
    messages = [{"role": "user", "content": prompt}]
    inp = out = searches = 0
    final = None
    for _ in range(_MAX_WEB_SEARCH_TURNS):
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            messages=messages, tools=[tool])
        usage = msg.usage
        inp += getattr(usage, "input_tokens", 0) or 0
        out += getattr(usage, "output_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        if stu is not None:
            searches += getattr(stu, "web_search_requests", 0) or 0
        final = msg
        if getattr(msg, "stop_reason", None) == "pause_turn":
            messages = messages + [{"role": "assistant", "content": msg.content}]
            continue
        break
    data = _parse_strict_json(_extract_text(final.content))
    inp_p, out_p = settings.model_prices_usd.get(settings.anthropic_model, (3.0, 15.0))
    usd = (inp / 1e6 * inp_p + out / 1e6 * out_p
           + searches * _WEB_SEARCH_USD_PER_REQUEST)
    meta = {
        "units": inp + out + searches,   # unités web_search incluses
        "cost_cts": round(usd * settings.usd_to_eur * 100, 4),
        "web_searches": searches,
    }
    return data, meta


def fetch_food_delivery(city: str, country_code: str,
                        client: anthropic.Anthropic,
                        today: str | None = None) -> tuple[dict, dict]:
    """Plateformes de livraison de repas actives dans la zone du logement, sous leur
    marque locale, vérifiées par recherche web (V2-07 volet 1). Retourne
    ({fact_type: content}, méta coût). `content` = {"platforms": [{name, url?,
    verified_on?}], "note": ...}. Une **liste vide est un résultat valide** ; une
    réponse malformée lève ValueError (aucune écriture — « N'invente jamais »)."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client,
        _FOOD_DELIVERY_PROMPT.format(city=city, country_code=country_code, today=today),
        city=city, country_code=country_code)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    platforms = data.get("platforms")
    if not isinstance(platforms, list):
        raise ValueError("Réponse IA invalide : 'platforms' doit être une liste.")
    clean: list[dict] = []
    for p in platforms:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        entry: dict = {"name": name}
        url = (p.get("url") or "").strip()
        if url:
            entry["url"] = url
        verified = (p.get("verified_on") or "").strip()
        if verified:
            entry["verified_on"] = verified
        clean.append(entry)
    note = (data.get("note") or "").strip() if isinstance(data.get("note"), str) else ""
    return {FOOD_DELIVERY_FACT_TYPE: {"platforms": clean, "note": note}}, meta
