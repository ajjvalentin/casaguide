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
import re
import unicodedata

import anthropic

from .overpass import haversine_m
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
                         city: str, country_code: str,
                         max_searches: int | None = None) -> tuple[dict, dict]:
    """Appel Claude AVEC l'outil de recherche web de l'API Anthropic → (données
    JSON, méta {units, cost_cts, web_searches}). La recherche web tourne côté
    serveur ; un `pause_turn` est relancé (boucle bornée). Les unités de recherche
    web sont comptabilisées dans le coût (10 $ / 1000 requêtes). `max_searches`
    plafonne le nombre de recherches web de CET appel (coût maîtrisé) ; défaut =
    plafond de la livraison de repas (volet 1)."""
    tool = {
        "type": "web_search_20250305", "name": "web_search",
        "max_uses": max_searches or settings.food_delivery_max_searches,
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


# ── Complétion des fiches de service : tel / site / horaires (V2-07 volet 2) ──
#
# On COMPLÈTE (jamais on n'écrase) les fiches RETENUES (approved/edited), avec
# preuve. Deux périmètres de champs par catégorie :
#   - téléphone + site : l'action principale est APPELER (taxi, médecin,
#     vétérinaire, pharmacie, police, location, baby-sitting) ;
#   - horaires + site : ils conditionnent la visite (supérette, centre commercial,
#     boulangerie, laverie, poste, pharmacie).
# EXCLUS volontairement : restaurant/bar (horaires trop volatils — le risque
# d'info fausse dépasse le gain) et market (porté par weekday/weekday_note, V2-33).
SERVICE_PHONE_CATEGORIES = ("taxi", "doctor", "veterinary", "pharmacy",
                            "police", "rental", "babysitter")
SERVICE_HOURS_CATEGORIES = ("supermarket", "mall", "bakery", "laundry",
                            "post_office", "pharmacy")
# Catégories dont on complète au moins un champ (union, ordre stable).
SERVICE_COMPLETE_CATEGORIES = tuple(dict.fromkeys(
    SERVICE_PHONE_CATEGORIES + SERVICE_HOURS_CATEGORIES))
# Champs complétables tout court (garde-fou : on n'écrit jamais ailleurs).
_SERVICE_FIELDS = ("phone", "website", "opening_hours")
# Mention accolée aux horaires (donnée périssable — précédent des notes de marché,
# V2-33). i18n hors périmètre (V2-29) : français, comme weekday_note.
HOURS_INDICATIVE_SUFFIX = " · Horaires indicatifs"
# La valeur voyageur d'un taxi/poste est le numéro du CENTRAL de la zone (leçon des
# taxis Ballarin, 11/08), pas celui d'une borne/parada physique.
_CENTRAL_PHONE_CATEGORIES = ("taxi", "police")


def service_fields(category: str) -> tuple[str, ...]:
    """Champs du périmètre de complétion pour une catégorie (ordre stable)."""
    fields: list[str] = []
    if category in SERVICE_PHONE_CATEGORIES:
        fields += ["phone", "website"]
    if category in SERVICE_HOURS_CATEGORIES:
        fields += ["opening_hours", "website"]
    return tuple(dict.fromkeys(fields))


_FIELD_LABELS = {"phone": "téléphone", "website": "site officiel",
                 "opening_hours": "horaires"}

_SERVICE_COMPLETE_PROMPT = """\
Tu complètes des fiches de LIEUX DE SERVICE du guide d'un logement de vacances à
{city} ({country_code}), catégorie « {category_label} ». Pour CHAQUE lieu listé,
trouve UNIQUEMENT les champs marqués « manquant », en les VÉRIFIANT par recherche web.

CHAMPS possibles :
- phone : le numéro de téléphone à composer.{phone_hint}
- website : le site OFFICIEL de l'établissement (jamais un annuaire tiers).
- opening_hours : les horaires d'ouverture, en TEXTE COURT lisible
  (ex. « Lun–Sam 9h–21h, Dim 9h–14h »). N'utilise QUE des horaires publiés
  RÉCEMMENT (moins d'un an) ; sinon laisse ce champ de côté.

RÈGLES STRICTES :
- PREUVE OU RIEN : ne renseigne un champ QUE si une page en ligne le confirme
  (priorité au site officiel de l'enseigne ou à la mairie). Un numéro FAUX est
  pire qu'absent.
- N'invente JAMAIS. Un champ non vérifié est simplement OMIS (ne le mets pas).
- Ne renvoie QUE les champs marqués « manquant » de chaque lieu.
- Pour chaque lieu, conserve l'URL de preuve (source_url) et la date ({today}).

Lieux (ref — nom — adresse — champs manquants) :
{poi_list}

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "<ref>": {{"phone": "...", "website": "...", "opening_hours": "...",
             "source_url": "https://...", "verified_on": "{today}"}}
}}
Omets tout champ non vérifié ; omets une ref entière si tu n'as rien de fiable.
Une réponse VIDE ({{}}) est acceptable si rien n'est vérifiable.
"""


def complete_service_pois(category: str, category_label: str, pois: list[dict],
                          city: str, country_code: str,
                          client: anthropic.Anthropic,
                          today: str | None = None) -> tuple[dict, dict]:
    """Complète EN UN LOT (un appel web par catégorie/commune, coût maîtrisé) les
    fiches incomplètes d'une catégorie. `pois` = [{id, name, address, missing:
    [champs]}]. Retourne ({id: {"fields": {champ: valeur}, "source_url", "verified_on"}},
    méta coût). Seuls les champs du périmètre ET « manquant » de CHAQUE fiche sont
    retenus, ET seulement avec une preuve (source_url) — « n'invente jamais ». Une
    réponse malformée lève ValueError (aucune écriture). Les horaires reçoivent la
    mention « Horaires indicatifs »."""
    today = today or _dt.date.today().isoformat()
    perim = service_fields(category)
    phone_hint = (" Pour un taxi ou un poste, donne le numéro du CENTRAL/standard "
                  "de la zone, jamais celui d'une borne physique."
                  if category in _CENTRAL_PHONE_CATEGORIES else "")
    lines = []
    for p in pois:
        miss = ", ".join(_FIELD_LABELS.get(f, f) for f in p.get("missing", []))
        addr = (p.get("address") or "").strip() or "adresse inconnue"
        lines.append(f'- {p["id"]} — {p["name"]} — {addr} — manquant : {miss}')
    prompt = _SERVICE_COMPLETE_PROMPT.format(
        city=city, country_code=country_code, category_label=category_label,
        phone_hint=phone_hint, today=today, poi_list="\n".join(lines))
    data, meta = _ask_web_search_json(
        client, prompt, city=city, country_code=country_code,
        max_searches=settings.service_complete_max_searches)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    missing_by_ref = {str(p["id"]): set(p.get("missing", [])) for p in pois}
    out: dict[str, dict] = {}
    for ref, entry in data.items():
        ref = str(ref)
        if not isinstance(entry, dict) or ref not in missing_by_ref:
            continue
        source_url = (entry.get("source_url") or "").strip()
        verified = (entry.get("verified_on") or "").strip() or today
        if not source_url:
            continue  # preuve ou rien
        fields: dict[str, str] = {}
        for f in perim:
            if f not in missing_by_ref[ref]:
                continue
            val = entry.get(f)
            if not isinstance(val, str) or not val.strip():
                continue
            val = val.strip()
            if f == "opening_hours":
                val = val + HOURS_INDICATIVE_SUFFIX
            fields[f] = val
        if fields:
            out[ref] = {"fields": fields, "source_url": source_url,
                        "verified_on": verified}
    return out, meta


# ── Baby-sitting : CRÉATION de fiches par Claude + recherche web (V2-07 volet 2) ─

_BABYSITTER_PROMPT = """\
Tu cherches des services de BABY-SITTING / GARDE D'ENFANTS crédibles pour des
vacanciers séjournant à {city} ({country_code}) : agences locales, plateformes
couvrant la zone, services d'hôtel/conciergerie ouverts au public. Vérifie chacun
par recherche web.

RÈGLES STRICTES :
- PREUVE OU RIEN : ne retiens un service QUE si une page en ligne le confirme
  (site officiel de préférence). Fournis un TÉLÉPHONE quand tu l'as (l'action est
  d'appeler) et l'URL de preuve.
- N'invente JAMAIS. Une liste VIDE est un résultat parfaitement valide (si aucune
  offre identifiable, renvoie une liste vide).
- Reste sobre : le nom du service, son téléphone si vérifié, son site, la preuve.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown :
{{
  "services": [
    {{"name": "...", "phone": "...", "website": "...",
      "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def fetch_babysitters(city: str, country_code: str,
                      client: anthropic.Anthropic,
                      today: str | None = None) -> tuple[list[dict], dict]:
    """Services de baby-sitting crédibles de la zone, vérifiés par recherche web
    (V2-07 volet 2). Retourne (liste de {name, phone?, website?, source_url?,
    verified_on}, méta coût). **Une liste vide est un résultat valide** (« vide
    assumé »). Réponse malformée → ValueError (aucune écriture)."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client, _BABYSITTER_PROMPT.format(city=city, country_code=country_code,
                                          today=today),
        city=city, country_code=country_code,
        max_searches=settings.babysitter_max_searches)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    services = data.get("services")
    if not isinstance(services, list):
        raise ValueError("Réponse IA invalide : 'services' doit être une liste.")
    out: list[dict] = []
    for s in services:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip()
        if not name:
            continue
        entry: dict = {"name": name}
        for f in ("phone", "website", "source_url"):
            v = (s.get(f) or "").strip() if isinstance(s.get(f), str) else ""
            if v:
                entry[f] = v
        entry["verified_on"] = (s.get("verified_on") or "").strip() or today
        out.append(entry)
    return out, meta


# ── Marchés hebdomadaires par zone : découverte CLAUDE + web (V2-07 volet 3) ──
#
# Découverte MUTUALISÉE par (pays, commune), mise en cache area_facts sous
# `MARKET_FACT_TYPE` (deux logements d'une commune partagent la découverte). La
# MATÉRIALISATION en POI `market` (source='claude', status='suggested') est faite
# PAR LOGEMENT depuis ce fait — voir `pipeline` + `db.insert_market_poi`.
MARKET_FACT_TYPE = "markets"
# Un marché découvert au-delà de cette distance du logement n'est pas « local » —
# garde-fou anti-position aberrante (coordonnées hallucinées / mauvaise commune).
MARKET_MAX_DIST_M = 25000
# Déduplication (seuils motivés) : un marché est un événement PONCTUEL à une place
# fixe. Deux marchés DISTINCTS d'une même commune diffèrent en général par le JOUR
# ET par le lieu. On dédoublonne donc contre les POI `market` existants (TOUS
# statuts) ainsi : même jour + (nom proche OU même place) → doublon ; ou nom
# quasi-identique seul (variantes « Mercadillo/Mercado de la Zenia »).
_MARKET_NAME_SOFT = 0.6    # similarité de nom (trigrammes) « proche »
_MARKET_NAME_HARD = 0.82   # « quasi-identique » (dédoublonne même jour inconnu)
_MARKET_SAME_SPOT_M = 250  # deux marchés à moins de 250 m = la même place

_MARKET_PROMPT = """\
Tu prépares la liste des MARCHÉS HEBDOMADAIRES (mercadillos, rastros, marchés de
plein air) autour d'un logement de vacances à {city} ({country_code}), pour un
guide voyageur. Vérifie chaque marché par recherche web.

CE QU'ON CHERCHE : uniquement les marchés qui se tiennent UN (ou plusieurs) JOUR
FIXE par semaine, en plein air, ambulants — étals de fruits/légumes, vêtements,
artisanat, brocante.

À EXCLURE ABSOLUMENT (ne les liste JAMAIS) :
- les commerces FIXES, supérettes, supermarchés, épiceries ;
- les marchés COUVERTS/municipaux ouverts TOUS LES JOURS (halles permanentes) ;
- tout ce qui n'a pas un JOUR hebdomadaire identifiable.

SOURCES à privilégier : site de la MAIRIE, OFFICE DE TOURISME, PRESSE LOCALE
récente. Un marché doit avoir une PREUVE d'activité RÉCENTE : si tu ne trouves que
des mentions anciennes ou des avis disant qu'il n'existe plus, NE l'affirme pas —
soit tu l'écartes, soit tu le marques `doubtful: true` avec une note prudente.

Pour chaque marché retenu, fournis :
- `name` : nom du marché (ex. « Mercadillo de La Zenia ») ;
- `weekday` : le jour, en ENTIER 1-7 (1=lundi, 7=dimanche) — OBLIGATOIRE ;
- `hours` : les horaires en texte court (ex. « 8h00–14h00 ») si connus, sinon "" ;
- `character` : en quelques mots, ce qu'on y trouve (ex. « fruits, légumes,
  vêtements ») si connu, sinon "" ;
- `address` : l'emplacement (place, rue, quartier) le plus précis possible ;
- `lat`, `lon` : les coordonnées SI une source fiable les donne (Google Maps, OSM),
  sinon omets-les (l'adresse suffira au géocodage) ;
- `source_url` : l'URL de la preuve ; `verified_on` : « {today} » ;
- `doubtful` : true seulement si l'activité récente est incertaine.

RÈGLES STRICTES :
- N'invente JAMAIS. Un marché sans preuve ou sans jour hebdomadaire n'est pas retenu.
- Une liste VIDE est un résultat parfaitement valide.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "markets": [
    {{"name": "...", "weekday": 6, "hours": "8h00–14h00",
      "character": "fruits, légumes, vêtements", "address": "...",
      "lat": 37.93, "lon": -0.75, "source_url": "https://...",
      "verified_on": "{today}", "doubtful": false}}
  ]
}}
"""


def _as_float(v) -> float | None:
    """Coordonnée numérique valide, sinon None (jamais une chaîne bidon)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # rejette NaN


def _market_note(m: dict) -> str:
    """`weekday_note` : horaires (suffixés « Horaires indicatifs », donnée
    périssable — précédent V2-33/volet 2) + caractère + réserve si douteux."""
    hours = (m.get("hours") or "").strip()
    character = (m.get("character") or "").strip()
    parts = [p for p in (hours, character) if p]
    note = " — ".join(parts)
    if hours:
        note += HOURS_INDICATIVE_SUFFIX
    if m.get("doubtful"):
        note = (note + " " if note else "") + "(activité à confirmer)"
    return note


def fetch_markets(city: str, country_code: str, client: anthropic.Anthropic,
                  today: str | None = None) -> tuple[dict, dict]:
    """Marchés hebdomadaires de la zone, vérifiés par recherche web (V2-07 volet 3).
    Retourne ({MARKET_FACT_TYPE: {"markets": [...]}}, méta coût). Chaque marché
    retenu porte `name`, `weekday` (ENTIER 1-7 — un marché SANS jour valide est
    ÉCARTÉ), `weekday_note` (horaires + caractère), `address`, `lat`/`lon` si la
    source les donne, `source_url`/`verified_on`, `doubtful`. **Liste vide valide** ;
    réponse malformée → ValueError (aucune écriture — « n'invente jamais »)."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client, _MARKET_PROMPT.format(city=city, country_code=country_code,
                                      today=today),
        city=city, country_code=country_code,
        max_searches=settings.market_max_searches)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    markets = data.get("markets")
    if not isinstance(markets, list):
        raise ValueError("Réponse IA invalide : 'markets' doit être une liste.")
    clean: list[dict] = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        weekday = m.get("weekday")
        # Jour OBLIGATOIRE et VALIDE (1-7) : un marché sans jour n'est pas un marché.
        if not name or not isinstance(weekday, int) or isinstance(weekday, bool) \
                or not (1 <= weekday <= 7):
            continue
        source_url = (m.get("source_url") or "").strip()
        if not source_url:
            continue  # preuve ou rien
        entry: dict = {
            "name": name, "weekday": weekday, "weekday_note": _market_note(m),
            "address": (m.get("address") or "").strip() or None,
            "source_url": source_url,
            "verified_on": (m.get("verified_on") or "").strip() or today,
            "doubtful": bool(m.get("doubtful")),
        }
        lat, lon = _as_float(m.get("lat")), _as_float(m.get("lon"))
        if lat is not None and lon is not None:
            entry["lat"], entry["lon"] = lat, lon
        clean.append(entry)
    return {MARKET_FACT_TYPE: {"markets": clean}}, meta


# ── Déduplication des marchés (pure — testée sans base ni réseau) ─────────────

_MARKET_STOPWORDS = {
    "mercadillo", "mercado", "market", "rastro", "marche", "marché", "feira",
    "wochenmarkt", "markt", "de", "del", "la", "el", "los", "las", "du", "des",
    "le", "of", "the",
}


def _norm_market_name(name: str) -> str:
    """Nom de marché normalisé pour la comparaison : sans accents, minuscule, sans
    mots génériques (« mercadillo », « mercado »…) ni articles, espaces compactés."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    toks = [t for t in s.split() if t and t not in _MARKET_STOPWORDS]
    return " ".join(toks)


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def _dice(a: str, b: str) -> float:
    """Coefficient de Dice sur trigrammes ∈ [0,1] (tolérant aux variantes)."""
    if not a or not b:
        return 1.0 if a == b else 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    return 2 * len(ta & tb) / (len(ta) + len(tb)) if (ta or tb) else 0.0


def market_matches_existing(name: str, weekday: int | None,
                            lat: float | None, lon: float | None,
                            existing: list[dict]) -> dict | None:
    """Retourne le POI `market` EXISTANT que ce candidat dédouble, sinon None.

    `existing` : POI market du logement (TOUS statuts — un `rejected` ne ressuscite
    jamais, un `edited` n'est jamais recréé) : [{name, weekday, lat, lon}].
    Règle (seuils motivés) : **même jour** + (nom proche OU même place ≤ 250 m) →
    doublon ; **ou** nom quasi-identique seul (variante d'orthographe). `lat/lon`
    peuvent être None (pré-contrôle par le nom, avant tout géocodage)."""
    for ex in existing:
        sim = _dice(_norm_market_name(name), _norm_market_name(ex.get("name", "")))
        if sim >= _MARKET_NAME_HARD:
            return ex                                    # nom quasi-identique
        same_day = weekday is not None and ex.get("weekday") == weekday
        close = (lat is not None and lon is not None
                 and ex.get("lat") is not None and ex.get("lon") is not None
                 and haversine_m(lat, lon, ex["lat"], ex["lon"]) <= _MARKET_SAME_SPOT_M)
        if same_day and (sim >= _MARKET_NAME_SOFT or close):
            return ex
    return None
