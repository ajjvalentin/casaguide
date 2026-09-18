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
  "emergency_numbers": {{"items": [{{"role": "...", "label": "...", "number": "..."}}],
                         "notes": "..."}},
  "waste_rules": {{"summary": "...",
                   "containers": [{{"color_or_type": "...", "accepts": "..."}}]}},
  "noise_rules": {{"summary": "...", "quiet_hours": "..."}}
}}

OBJECTIF : uniquement l'ESSENTIEL ACTIONNABLE pour un vacancier qui arrive et
doit se débrouiller seul — rien d'autre. Un touriste, pas un administré.

Contraintes de CONTENU :
- emergency_numbers : numéros RÉELLEMENT en vigueur dans ce pays (112 européen
  inclus). CHAQUE item porte un `role` STRUCTUREL en snake_case (le libellé d'affichage
  est traduit côté guide À PARTIR du rôle — ne le laisse JAMAIS vide). Rôles connus,
  traduits automatiquement : `eu_emergency` (112 ou équivalent), `police` (police
  générique, ex. 117 suisse), `national_police`, `municipal_police` (ou locale),
  `gendarmerie` (Guardia Civil), `police_nonemergency`, `fire`, `medical` (SAMU,
  ambulance, urgences médicales), `doctor_on_call` (médecin/garde médicale),
  `poison_control` (toxicologie/antipoison), `air_rescue` (Rega/sauvetage aérien).
  Pour un numéro utile qui n'entre dans AUCUN de ces rôles, invente un `role`
  snake_case DESCRIPTIF (ex. `red_cross`, `mountain_rescue`) — jamais vide — et donne
  un `label` court en français (il servira de repli). `notes` : une précision utile
  seulement si elle aide à composer le bon numéro, sinon "".
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

# Tournures de REMPLISSAGE (V2-35) : « n'invente jamais » étendu au style — le
# remplissage est une invention polie (constat 14/08, « La Marquesa » : « Site à
# visiter à Orihuela, accessible aux vacanciers intéressés par la culture locale »
# = zéro information). Ces marqueurs (sans accents, minuscules) servent DEUX fois :
# ils sont cités au prompt comme INTERDITS, et détectés à la réception (`describe_
# pois` traite une description de remplissage comme vide → jamais écrite) ; le script
# `ops/list_filler_descriptions.py` s'en sert pour recenser l'existant.
FILLER_MARKERS = (
    "a visiter a", "site a visiter", "a decouvrir", "lieu a decouvrir",
    "accessible aux vacanciers", "ouvert aux vacanciers",
    "interesses par la culture locale", "amateurs de culture locale",
    "pour un repas sur place", "un repas sur place", "une option de restauration",
    "durant votre sejour", "lors de votre sejour", "pendant votre sejour",
)

# FILLER_MARKERS v2 (V2-37) : plutôt qu'empiler des chaînes littérales, on capte le
# MOTIF — un PUBLIC générique (vacanciers/visiteurs/touristes/voyageurs) associé à un
# but/verbe CREUX (peuvent prendre, pouvant convenir, option de restauration, « pour
# les visiteurs »…). Quatre variantes avaient échappé au recensement d'Ardon (16/08).
# Ancré sur le mot de public (ou « option de … ») pour ne PAS sur-bloquer le factuel
# (« … apprécié depuis Chaplin », « saut à l'élastique de 190 m » ne matchent jamais).
_FILLER_AUDIENCE = r"(?:vacanciers|visiteurs|touristes|voyageurs)"
_FILLER_PATTERNS = tuple(re.compile(p) for p in (
    rf"\bpour (?:les |le |la |des |un |une )?{_FILLER_AUDIENCE}\b",          # « pour les visiteurs »
    rf"\b(?:aux?|des|les) {_FILLER_AUDIENCE} (?:cherchant|souhaitant|desirant|"
    rf"voulant|en quete|qui cherchent|qui souhaitent|pouvant)\b",            # « aux vacanciers cherchant… »
    r"\b(?:pouvant|peut|peuvent) convenir\b",                                # « pouvant convenir aux… »
    rf"\b{_FILLER_AUDIENCE} (?:peuvent|peut|pourront|pourraient)\b",         # « où les vacanciers peuvent… »
    r"\bpeuvent (?:y )?(?:prendre|deguster|savourer|profiter)\b",            # « peuvent (y) prendre leurs repas »
    r"\boption de (?:restauration|repas|restaurant|nourriture)\b",           # « une option de restauration »
))


def _strip_accents(s: str) -> str:
    return (unicodedata.normalize("NFKD", s or "")
            .encode("ascii", "ignore").decode().lower())


def is_filler_description(text: str) -> bool:
    """True si la description est du REMPLISSAGE (tournure bannie littérale OU motif
    générique « public + but creux »), quelle que soit la casse/les accents — donc à
    ne JAMAIS écrire (V2-35, marqueurs v2 V2-37)."""
    norm = _strip_accents(text)
    if any(m in norm for m in FILLER_MARKERS):
        return True
    return any(p.search(norm) for p in _FILLER_PATTERNS)


_POI_PROMPT = """\
Voici des points d'intérêt proches d'un logement de vacances à {city}
({country_code}). Pour chacun, écris une description d'UNE phrase (max 25 mots),
utile et FACTUELLE, en français.

RÈGLE ABSOLUE — « n'invente jamais » ÉTENDU AU STYLE : le remplissage est une
invention polie. Si tu n'as pas de connaissance PROPRE à ce lieu précis (ce qu'il
est vraiment, ce qu'on y fait de particulier), renvoie une chaîne VIDE ("") — ne
MEUBLE JAMAIS. Le silence vaut mieux qu'une phrase creuse.

Une description n'est acceptable QUE si :
- chaque phrase porte AU MOINS UN FAIT SPÉCIFIQUE au lieu (type précis, spécialité,
  particularité) — jamais une généralité vraie de n'importe quel lieu du même type ;
- elle n'est PAS une paraphrase du nom ni de la catégorie.

TOURNURES BANNIES (et toute leur famille) — leur présence rend la description
IRRECEVABLE, renvoie "" plutôt :
- « à visiter », « site à visiter », « à découvrir » ;
- « accessible aux vacanciers », « pour les vacanciers », « pour les visiteurs » ;
- « intéressés par la culture locale » ;
- « où les vacanciers/visiteurs peuvent … », « pouvant convenir aux vacanciers … »,
  « une option de restauration » ;
- « pour un repas sur place », « durant votre séjour », « lors de votre séjour ».

LOCALITÉ — RÈGLE ABSOLUE : n'affirme JAMAIS la commune/localité d'un lieu qui ne
t'est pas fournie. Chaque POI peut porter un champ « localité : … » : ne cite une
commune QUE si elle t'y est donnée (ou figure dans le nom du POI). SANS localité
fournie, n'indique AUCUN lieu — ni « à {city} » (la commune du LOGEMENT, pas
forcément celle du POI), ni aucune autre.

SPORT & LOISIR (V2-71) : pour un lieu de sport ou d'activité, dis en priorité LA
DISCIPLINE (football, tennis, natation, padel, escalade…), si l'accès est PUBLIC ou
RÉSERVÉ aux clubs/licenciés, et la NATURE du lieu (complexe couvert, stade, terrain
municipal, piscine). Un champ « sous-type : … » (ex. soccer, swimming_pool,
sports_centre) t'aide à identifier la discipline/le type — appuie-toi dessus.

ÉQUIPEMENT PUBLIC (V2-71b) — EXCEPTION à la règle du silence : pour un ÉQUIPEMENT
sportif/de loisir (stade, complexe, gymnase, piscine, aire de skate, aire de jeux,
terrain…), même SANS connaissance propre à ce lieu, tu PEUX décrire sa NATURE d'après
son TYPE et son NOM (« Complexe sportif municipal », « Stade avec terrains de plein
air », « Aire de skate »), SANS RIEN inventer sur ses activités, horaires, tarifs ou
services précis. Un équipement doit toujours dire ce qu'il EST — le silence ("") ne
reste la règle que pour les COMMERCES et lieux à identité propre (où une phrase creuse
nuirait).

Contraintes factuelles : n'invente ni distance, ni horaire, ni prix, ni note, ni
anecdote.

Réponds UNIQUEMENT avec un objet JSON valide, la valeur étant la description OU une
chaîne vide : {{"<ref>": "description ou \\"\\"", ...}}

Points d'intérêt (ref, nom, catégorie[, sous-type][, localité]) :
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


def _ask_json(client: anthropic.Anthropic, prompt: str, *,
              max_tokens: int | None = None) -> tuple[dict, dict]:
    """Appel Claude SANS recherche web → (données JSON, méta {units, cost_cts,
    attempts, stop_reason}). PARITÉ de robustesse avec le chemin web (V2-37 volet
    1bis) via le motif commun `_json_retry` : isolation d'un JSON encadré de prose/
    clôtures de code, **un retry** sur JSON invalide (réponse RÉGÉNÉRÉE), coût
    comptabilisé par essai, `stop_reason` du dernier essai gravé dans l'erreur
    (`ClaudeJSONError`) — une réponse vide se diagnostique enfin."""
    mt = max_tokens or settings.anthropic_max_tokens
    return _json_retry(lambda m: _one_plain_call(client, prompt, m),
                       label="Claude", max_tokens=mt)


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
    # Localité RÉELLE du POI transmise quand connue (V2-37) : le prompt ne suppose
    # PLUS la commune du logement (« restaurant à Ardon » pour un lieu de Vétroz).
    lines = []
    for p in pois:
        loc = (p.get("locality") or "").strip()
        loc_txt = f' — localité : {loc}' if loc else ""
        sub = (p.get("subtype") or "").strip()
        sub_txt = f' — sous-type : {sub}' if sub else ""   # V2-71 : aide à nommer le sport
        lines.append(
            f'- ref "{p["source_ref"]}" : {p["name"]} ({p["category"]}){sub_txt}{loc_txt}')
    poi_list = "\n".join(lines)
    data, meta = _ask_json(
        client, _POI_PROMPT.format(city=city, country_code=country_code, poi_list=poi_list),
        max_tokens=settings.describe_max_tokens)  # lot de ~40 POI → sortie longue
    # Validation (V2-35) : une description VIDE est acceptée telle quelle (le POI
    # reste sans description — JAMAIS de repli vers du générique) ; une description
    # de REMPLISSAGE est traitée comme vide (garde-fou si le modèle désobéit au
    # prompt). Dans les deux cas la clé est ABSENTE du résultat → l'upsert n'écrit
    # rien (NULL pour un POI neuf ; COALESCE conserve l'existant au ré-enrichissement).
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, str) and v.strip() and not is_filler_description(v):
            out[k] = v.strip()
    return out, meta


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


class ClaudeJSONError(ValueError):
    """Réponse Claude NON parsable en JSON, après retry — chemin web (V2-07 3bis)
    OU sans web (V2-37 1bis). Sous-classe `ValueError` → les gardes best-effort
    existantes (`except ValueError`/`Exception`) la rattrapent inchangées. Porte le
    coût de CHAQUE essai (`attempts`) — l'argent est dépensé à la réponse, pas au
    succès, donc l'appelant DOIT le comptabiliser malgré l'échec — et le `stop_reason`
    du dernier essai (`max_tokens` = troncature ; `end_turn` mais texte vide = refus
    ou bloc non-texte)."""

    def __init__(self, message: str, attempts: list[dict], stop_reason: str | None):
        super().__init__(message)
        self.attempts = attempts
        self.stop_reason = stop_reason


# Rétrocompat : l'ancien nom (3bis) reste un alias du nom neutre (1bis).
WebSearchJSONError = ClaudeJSONError


def _json_retry(one_call, *, label: str, max_tokens: int) -> tuple[dict, dict]:
    """MOTIF COMMUN de robustesse des réponses JSON de Claude (V2-07 3bis, étendu au
    chemin sans web par V2-37 1bis — un seul motif pour les deux chemins).

    `one_call(max_tokens) -> (texte, coût {units, cost_cts, web_searches}, stop_reason)`
    est un appel logique (le web fait sa boucle `pause_turn`). On isole le JSON encadré
    de prose/clôtures (`_parse_strict_json`), on **régénère UNE fois** la réponse sur
    JSON invalide (jamais un re-parse ; borné par `web_search_max_attempts`), on
    comptabilise le coût de CHAQUE essai (`meta['attempts']`), et l'échec final lève
    `ClaudeJSONError` PORTANT les coûts + le `stop_reason` du dernier essai."""
    attempts: list[dict] = []
    last_stop: str | None = None
    for attempt in range(1, settings.web_search_max_attempts + 1):
        text, cost, stop_reason = one_call(max_tokens)
        attempts.append(cost)
        last_stop = stop_reason
        try:
            data = _parse_strict_json(text)
        except ValueError:
            if attempt < settings.web_search_max_attempts:
                continue  # réponse RÉGÉNÉRÉE au tour suivant (jamais un re-parse)
            raise ClaudeJSONError(
                f"Réponse {label} non parsable après {attempt} essai(s) "
                f"(stop_reason={stop_reason})", attempts=attempts,
                stop_reason=last_stop)
        meta = {
            "units": sum(c["units"] for c in attempts),
            "cost_cts": round(sum(c["cost_cts"] for c in attempts), 4),
            "web_searches": sum(c.get("web_searches", 0) for c in attempts),
            "attempts": attempts,
            "stop_reason": last_stop,
        }
        return data, meta


def _one_plain_call(client: anthropic.Anthropic, prompt: str,
                    max_tokens: int) -> tuple[str, dict, str | None]:
    """UN appel Claude sans outil → (texte, coût {units, cost_cts, web_searches:0},
    stop_reason). Pendant sans-web de `_one_web_call`."""
    msg = client.messages.create(
        model=settings.anthropic_model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    usage = msg.usage
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cost = {"units": inp + out,
            "cost_cts": _cost_cts(settings.anthropic_model, usage),
            "web_searches": 0}
    return (_extract_text(msg.content), cost, getattr(msg, "stop_reason", None))


def _one_web_call(client: anthropic.Anthropic, prompt: str, tool: dict,
                  max_tokens: int) -> tuple[str, dict, str | None]:
    """UN appel logique (boucle de `pause_turn` incluse) → (texte final, coût de
    l'appel {units, cost_cts, web_searches}, stop_reason du dernier tour)."""
    messages = [{"role": "user", "content": prompt}]
    inp = out = searches = 0
    final = None
    for _ in range(_MAX_WEB_SEARCH_TURNS):
        msg = client.messages.create(
            model=settings.anthropic_model, max_tokens=max_tokens,
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
    inp_p, out_p = settings.model_prices_usd.get(settings.anthropic_model, (3.0, 15.0))
    usd = (inp / 1e6 * inp_p + out / 1e6 * out_p
           + searches * _WEB_SEARCH_USD_PER_REQUEST)
    cost = {"units": inp + out + searches,
            "cost_cts": round(usd * settings.usd_to_eur * 100, 4),
            "web_searches": searches}
    return (_extract_text(final.content), cost,
            getattr(final, "stop_reason", None))


def _ask_web_search_json(client: anthropic.Anthropic, prompt: str, *,
                         city: str, country_code: str,
                         max_searches: int | None = None,
                         max_tokens: int | None = None) -> tuple[dict, dict]:
    """Appel Claude AVEC l'outil de recherche web → (données JSON, méta {units,
    cost_cts, web_searches, **attempts**}). `attempts` = coût de CHAQUE essai (retry
    V2-07 volet 3bis compris) → l'appelant en comptabilise une ligne par essai.

    `max_searches` plafonne les recherches web (coût maîtrisé) ; `max_tokens`
    surcharge le plafond de SORTIE (marchés : réponse longue). Sur JSON invalide, on
    **régénère UNE fois** la réponse (pas un re-parse de la même — bornée par
    `web_search_max_attempts`) ; l'échec final lève `WebSearchJSONError` PORTANT le
    coût de chaque essai et le `stop_reason` (troncature `max_tokens` = smoking gun)."""
    tool = {
        "type": "web_search_20250305", "name": "web_search",
        "max_uses": max_searches or settings.food_delivery_max_searches,
        "user_location": {"type": "approximate", "country": country_code,
                          "city": city},
    }
    mt = max_tokens or settings.anthropic_max_tokens
    return _json_retry(lambda m: _one_web_call(client, prompt, tool, m),
                       label="web_search", max_tokens=mt)


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
# Site web SEUL (ni téléphone ni horaires) — catégories « en savoir plus » (V2-35) :
# on ne complète que le lien officiel où le voyageur approfondira (l'horaire d'un
# site/activité est trop variable, le téléphone rarement l'action principale).
SERVICE_SITE_CATEGORIES = ("sight", "family_activity", "sport")
# Téléphone + site SEULS — restauration (V2-37) : réintégrés dans la complétion (ils
# avaient perdu tél/site en étant exclus du volet 2 pour la volatilité des horaires).
# Les HORAIRES restent EXCLUS pour ces catégories (jamais complétés, trop volatils).
SERVICE_TELSITE_CATEGORIES = ("restaurant", "bar", "cafe")
# Catégories dont on complète au moins un champ (union, ordre stable).
SERVICE_COMPLETE_CATEGORIES = tuple(dict.fromkeys(
    SERVICE_PHONE_CATEGORIES + SERVICE_HOURS_CATEGORIES + SERVICE_SITE_CATEGORIES
    + SERVICE_TELSITE_CATEGORIES))
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
    if category in SERVICE_SITE_CATEGORIES:      # V2-35 : « en savoir plus » = site seul
        fields += ["website"]
    if category in SERVICE_TELSITE_CATEGORIES:   # V2-37 : restauration = tél + site (jamais horaires)
        fields += ["phone", "website"]
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
- LOCAL D'ABORD : privilégie les agences et services de PROXIMITÉ (ceux basés dans
  ou près de {city}) aux grandes plateformes nationales. ORDONNE ta liste du plus
  LOCAL au plus national. Ne renvoie AU PLUS que 3 services — les plus pertinents
  pour un vacancier sur place, jamais un annuaire de plateformes.
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
    # Plafond « local d'abord » (V2-44) : le prompt ordonne du plus local au plus
    # national → on garde les premiers. Empêche l'annuaire de plateformes nationales
    # (6 sorties au benchmark Op de Boerderie) de noyer les vraies agences de zone.
    return out[:settings.babysitter_max_results], meta


# ── Services : CONTACTABILITÉ + QUALIFICATION par recherche web (V2-50) ────────
#
# Deux règles d'arbitrage (André 07/09), catégories de SERVICE : un loueur/taxi/laverie
# est UTILE par ce qu'on peut FAIRE avec (l'appeler, le contacter) et par CE QU'IL OFFRE
# (fourgonnettes ? vélos électriques ?). Un service sans téléphone NI site NI sous-type
# identifiable est du bruit ; mais avant de le retirer, on cherche son contact/offre sur
# le web (pattern V2-44 volet 2, qui a trouvé les contacts de SOBIKES). Le COMMERCE de
# passage (boulangerie, bar…) et l'INFRASTRUCTURE (arrêts, plages, bornes) sont hors
# périmètre : leur position EST leur contact.

_QUALIFY_PROMPT = """\
Pour un guide de vacances à {city} ({country_code}), tu QUALIFIES des lieux de
SERVICE de catégorie « {label} ». Pour CHACUN des lieux listés, cherche sur le web :
- son TÉLÉPHONE et/ou son SITE officiel (contactabilité) ;
- son SOUS-TYPE d'offre PRÉCIS — ce qu'il loue/propose vraiment, en 1 à 5 mots
  (ex. « fourgonnettes et camions », « vélos électriques », « taxi 24h »).
PREUVE OU RIEN : ne renseigne un champ QUE si une page en ligne le confirme (fournis
l'URL). Si rien de fiable pour un lieu, renvoie-le avec des champs vides. N'invente jamais.

LIEUX À QUALIFIER :
{poi_list}

Réponds UNIQUEMENT par un objet JSON valide, sans markdown :
{{"places": [{{"name": "...", "phone": "...", "website": "...", "subtype": "...",
   "source_url": "https://..."}}]}}
"""


def qualify_services(category: str, label: str, pois: list[dict], city: str,
                     country_code: str, client: anthropic.Anthropic,
                     today: str | None = None) -> tuple[dict, dict]:
    """Cherche par recherche web le CONTACT (tél/site) et le SOUS-TYPE d'offre de lieux de
    SERVICE (V2-50). Retourne ({nom_normalisé: {phone?, website?, subtype?, source_url?}},
    méta coût). PREUVE OU RIEN : un `subtype` sans `source_url` est écarté. Réponse
    malformée → ValueError (robustesses V2-37 héritées de `_ask_web_search_json`)."""
    today = today or _dt.date.today().isoformat()
    poi_list = "\n".join(f'- {p["name"]}' for p in pois)
    data, meta = _ask_web_search_json(
        client, _QUALIFY_PROMPT.format(city=city, country_code=country_code,
                                       label=label, poi_list=poi_list),
        city=city, country_code=country_code,
        max_searches=settings.service_complete_max_searches)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    places = data.get("places")
    if not isinstance(places, list):
        raise ValueError("Réponse IA invalide : 'places' doit être une liste.")
    out: dict[str, dict] = {}
    for pl in places:
        if not isinstance(pl, dict):
            continue
        name = (pl.get("name") or "").strip()
        if not name:
            continue
        def _s(k: str) -> str:
            v = pl.get(k)
            return v.strip() if isinstance(v, str) else ""
        entry: dict = {}
        for f in ("phone", "website", "source_url"):
            if _s(f):
                entry[f] = _s(f)
        # PREUVE OU RIEN : le sous-type n'est retenu qu'avec une source.
        if _s("subtype") and entry.get("source_url"):
            entry["subtype"] = _s("subtype")
        out[_norm_service_name(name)] = entry
    return out, meta


def _norm_service_name(s: str | None) -> str:
    """Nom normalisé (accents/casse/ponctuation) pour apparier le web à la moisson."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", s.lower()).split())


def apply_service_rules(pois: list[dict],
                        qualification: dict) -> tuple[list[dict], list[dict], int]:
    """Applique les règles de service (V2-50) à une catégorie, PUR. Pour chaque POI :
    complète tél/site MANQUANTS depuis le web (jamais d'écrasement), pose le sous-type
    (`completion_meta._qualification` + `description_md` pour l'affichage), PUIS RETIRE
    tout service à la fois NON contactable ET NON qualifiable. Renvoie (gardés, retirés,
    n_qualifiés)."""
    kept: list[dict] = []
    dropped: list[dict] = []
    qualified = 0
    for p in pois:
        r = qualification.get(_norm_service_name(p.get("name")), {})
        if r.get("phone") and not (p.get("phone") or "").strip():
            p["phone"] = r["phone"]
        if r.get("website") and not (p.get("website") or "").strip():
            p["website"] = r["website"]
        subtype = r.get("subtype")
        if subtype:
            meta = dict(p.get("completion_meta") or {})
            meta["_qualification"] = {"subtype": subtype,
                                      "source_url": r.get("source_url")}
            p["completion_meta"] = meta
            if not (p.get("description_md") or "").strip():
                p["description_md"] = subtype     # surfacé dans la fiche
            qualified += 1
        contactable = bool((p.get("phone") or "").strip()
                           or (p.get("website") or "").strip())
        (kept if (contactable or subtype) else dropped).append(p)
    return kept, dropped, qualified


# ── Location : DÉCOUVERTE de loueurs par Claude + recherche web (V2-44 volet 2) ─
#
# Cas d'or (31/08) : le loueur du village — Kassteele Tweewielers, Kloosterweg 44,
# 4 min du logement — est ABSENT d'OSM, donc introuvable par toute requête de tags,
# alors qu'une seule recherche web (« fietsverhuur Noordgouwe ») le trouve. Même
# famille que le baby-sitting (`_ask_web_search_json`, robustesses V2-37), mais chaque
# loueur est GÉOCODÉ par son adresse (position réelle) et FUSIONNÉ avec l'OSM (V2-40) :
# un loueur trouvé par les deux ne fait qu'une fiche, le mieux renseigné gagne.

_RENTAL_PROMPT = """\
Tu cherches des LOUEURS de proximité pour des vacanciers séjournant à {city}
({country_code}) : location de VÉLOS en PRIORITÉ, mais aussi bateaux, skis ou
voitures si la zone s'y prête. Cherche dans la LANGUE LOCALE du pays et choisis
tes termes (ex. aux Pays-Bas « fietsverhuur {city} », en France « location de
vélos {city} », en Espagne « alquiler de bicicletas {city} »). Vérifie chaque
loueur par recherche web.

RÈGLES STRICTES :
- PROXIMITÉ D'ABORD : privilégie les loueurs DANS ou tout près de {city}. Ordonne
  du plus proche au plus éloigné ; au plus 5.
- PREUVE OU RIEN : ne retiens un loueur QUE si tu peux vérifier en ligne son NOM,
  son ADRESSE POSTALE COMPLÈTE (rue + numéro + commune — indispensable pour le
  situer sur la carte) ET un TÉLÉPHONE OU un SITE. Fournis l'URL de preuve.
- N'invente JAMAIS. Une liste VIDE est un résultat parfaitement valide.
- Reste sobre : nom, adresse complète, téléphone si vérifié, site, la preuve.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown :
{{
  "rentals": [
    {{"name": "...", "address": "rue et numéro, commune", "phone": "...",
      "website": "...", "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def fetch_rentals(city: str, country_code: str,
                  client: anthropic.Anthropic,
                  today: str | None = None) -> tuple[list[dict], dict]:
    """Loueurs de proximité (vélos d'abord) vérifiés par recherche web (V2-44 volet 2).
    Retourne (liste de {name, address, phone?, website?, source_url, verified_on}, méta
    coût). **PREUVE OU RIEN** : une entrée sans nom, sans adresse complète, sans preuve,
    ou sans téléphone NI site est ÉCARTÉE (l'adresse est requise — le pipeline la
    géocode). **Une liste vide est un résultat valide.** Réponse malformée → ValueError
    (aucune écriture) — robustesses V2-37 héritées de `_ask_web_search_json`."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client, _RENTAL_PROMPT.format(city=city, country_code=country_code,
                                      today=today),
        city=city, country_code=country_code,
        max_searches=settings.rental_web_max_searches)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    rentals = data.get("rentals")
    if not isinstance(rentals, list):
        raise ValueError("Réponse IA invalide : 'rentals' doit être une liste.")
    out: list[dict] = []
    for r in rentals:
        if not isinstance(r, dict):
            continue
        def _s(key: str) -> str:
            v = r.get(key)
            return v.strip() if isinstance(v, str) else ""
        name, address = _s("name"), _s("address")
        source_url, phone, website = _s("source_url"), _s("phone"), _s("website")
        # Preuve ou rien : nom + adresse complète (géocodable) + (tél OU site) + preuve.
        if not (name and address and source_url and (phone or website)):
            continue
        entry: dict = {"name": name, "address": address, "source_url": source_url,
                       "verified_on": _s("verified_on") or today}
        if phone:
            entry["phone"] = phone
        if website:
            entry["website"] = website
        out.append(entry)
    return out, meta


# ── Sélection éditoriale « sorties » : découverte CLAUDE + web (V2-56) ────────
#
# OSM est pauvre sur le commercial touristique (constat Catedral/La Zenia), le
# réservoir Overture est fermé, et aucune source ne porte de signal de NOTORIÉTÉ.
# Cette passe demande à Claude les adresses RÉPUTÉES/incontournables du secteur
# (restaurant/bar/cafe) — la famille « sorties ». Chaque pick est géocodé (le
# pipeline s'en charge), apparié à OSM/Overture (récupération tél/site/position
# sûre), et entre quand même s'il géocode proprement mais reste introuvable en base.
EDITORIAL_SORTIES = ("restaurant", "bar", "cafe")

_REPUTED_LANG_NAMES = {"fr": "français", "en": "anglais", "es": "espagnol",
                       "de": "allemand", "nl": "néerlandais", "it": "italien",
                       "sq": "albanais"}

_REPUTED_PROMPT = """\
Tu prépares, pour un GUIDE VOYAGEUR, la sélection des adresses RÉPUTÉES et
INCONTOURNABLES du secteur autour d'un logement de vacances à {city}
({country_code}) — la famille « sorties » : RESTAURANTS, BARS et CAFÉS.

CHERCHE LARGE, dans la LANGUE LOCALE : blogs, guides de voyage, presse locale,
articles « meilleurs restaurants/bars de {city} », mentions RÉPÉTÉES d'une même
adresse. L'objectif est la NOTORIÉTÉ : les lieux dont les habitants et les guides
parlent, pas un annuaire exhaustif.

VISE 12 À 15 ADRESSES au total (échantillonne LARGE — le pipeline vérifie et
positionne strictement, il élaguera ce qui n'est pas prouvé ; mieux vaut proposer
généreusement et laisser filtrer).

RATISSE PAR QUARTIER, pas seulement par commune : {city} regroupe souvent plusieurs
QUARTIERS / URBANIZACIONES / stations balnéaires (par ex. sur la Costa Blanca : La
Zenia, Playa Flamenca, Cabo Roig, Punta Prima, Villamartín…). Identifie ceux du
secteur et interroge-les EXPLICITEMENT (« mejores bares La Zenia », « cocktail bar
Cabo Roig »…). Couvre des types VARIÉS : restaurants, bars, BARS À COCKTAILS,
BEACH CLUBS / chiringuitos, cafés/salons de thé réputés.

N'OUBLIE PAS LES LIEUX À FORTE PRÉSENCE SUR LES RÉSEAUX SOCIAUX (Instagram,
TripAdvisor, Google) même s'ils sont discrets dans la presse : bars à cocktails et
beach clubs branchés ont souvent leur PROPRE SITE (par ex. un « Brown's Cocktail
Bar » → browns-cocktailbar.com). Cherche aussi « [type] {city} instagram / tripadvisor ».

Pour chaque adresse retenue, fournis :
- `name` : le nom exact du lieu, en écriture LATINE (translittéré/romanisé si besoin) ;
- `name_local` : le nom du lieu dans l'écriture LOCALE du pays (ex. « 銀座 久兵衛 » au
  Japon, « Ταβέρνα » en Grèce) SI le pays n'utilise pas l'alphabet latin ; sinon "".
  C'est ce nom que le voyageur montrera au chauffeur ;
- `category` : « restaurant », « bar » ou « cafe » (choisis le plus juste ; un beach
  club / cocktail bar = « bar ») ;
- `address` : l'adresse postale la plus COMPLÈTE possible (rue + numéro + quartier +
  commune) — indispensable pour situer le lieu sur la carte ;
- `reason` : UNE phrase courte, EN {lang_name}, disant POURQUOI il est réputé
  (spécialité, ambiance, ce qu'on y va chercher). Factuelle, jamais du remplissage.
- `shisha` : « yes » UNIQUEMENT si le lieu est un bar à CHICHA (shisha / hookah /
  narguilé) — c'est une caractéristique que le voyageur cherche et qu'OSM tague
  rarement ; sinon "". Ne le devine pas : seulement si la source le dit ;
- `phone`, `website` : si vérifiés en ligne, sinon "" (le pipeline complètera) ;
- `source_url` : l'URL de la preuve (le guide/l'article/la mention) ;
- `verified_on` : « {today} ».

RÈGLES STRICTES :
- PREUVE OU RIEN : pas de nom, pas d'adresse géocodable, ou pas de source → écarté.
- N'invente JAMAIS un lieu ni une réputation. Une liste VIDE est un résultat valide.
- Reste dans le secteur de {city} et ses environs immédiats (le voyageur est motorisé
  mais cherche des sorties de proximité, pas à l'autre bout de la province).

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown :
{{
  "places": [
    {{"name": "...", "name_local": "", "category": "restaurant",
      "address": "rue et numéro, quartier, commune",
      "reason": "...", "shisha": "", "phone": "...", "website": "...",
      "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def fetch_reputed_places(city: str, country_code: str,
                         client: anthropic.Anthropic,
                         today: str | None = None,
                         lang: str = "fr") -> tuple[list[dict], dict]:
    """Adresses réputées « sorties » (restaurant/bar/cafe) du secteur, vérifiées par
    recherche web (V2-56). Retourne (liste de {name, category, address, reason,
    phone?, website?, source_url, verified_on}, méta coût). **PREUVE OU RIEN** : une
    entrée sans nom, sans adresse (géocodable), sans source, ou de catégorie hors
    (restaurant/bar/cafe) est ÉCARTÉE. **Une liste vide est un résultat valide.**
    Réponse malformée → ValueError (robustesses V2-37 héritées)."""
    today = today or _dt.date.today().isoformat()
    lang_name = _REPUTED_LANG_NAMES.get(lang, "français")
    data, meta = _ask_web_search_json(
        client, _REPUTED_PROMPT.format(city=city, country_code=country_code,
                                       today=today, lang_name=lang_name),
        city=city, country_code=country_code,
        max_searches=settings.reputed_max_searches,
        max_tokens=settings.reputed_max_tokens)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    places = data.get("places")
    if not isinstance(places, list):
        raise ValueError("Réponse IA invalide : 'places' doit être une liste.")
    out: list[dict] = []
    for r in places:
        if not isinstance(r, dict):
            continue
        def _s(key: str) -> str:
            v = r.get(key)
            return v.strip() if isinstance(v, str) else ""
        name, address = _s("name"), _s("address")
        category = _s("category").lower()
        source_url = _s("source_url")
        if not (name and address and source_url) or category not in EDITORIAL_SORTIES:
            continue
        entry: dict = {"name": name, "category": category, "address": address,
                       "reason": _s("reason"), "source_url": source_url,
                       "verified_on": _s("verified_on") or today}
        name_local = _s("name_local")   # V2-66b : nom en écriture d'origine (pays non latins)
        if name_local:
            entry["name_local"] = name_local
        # V2-77 : caractéristique FERMÉE (drapeau), jamais la chaîne du modèle → le
        # sous-type canonique « shisha », celui que le rendu sait étiqueter en 7 langues.
        if _s("shisha").lower() in ("yes", "oui", "true", "1", "shisha"):
            entry["subtype"] = "shisha"
        phone, website = _s("phone"), _s("website")
        if phone:
            entry["phone"] = phone
        if website:
            entry["website"] = website
        out.append(entry)
    return out, meta


# ── Estancos & bars à chicha par zone (V2-77b) ───────────────────────────────
#
# Recette réelle Adeje (Tenerife) : la catégorie tabac était PLEINE — « Radikas »,
# « La Cava La Cubana », « Tobacco Deluxe » — et pourtant sans un seul ESTANCO, le bureau
# de tabac licencié où l'on achète timbres, tickets de transport et recharges. Aucun tag
# OSM ne distingue le réseau licencié d'une cave à cigares touristique. Même chose pour la
# chicha : six bars moissonnés, aucune puce, alors que deux en sont.
#
# D'où la règle de ces deux passes, qui les distingue de TOUTES les précédentes : elles se
# déclenchent **même quand OSM a rendu quelque chose**. Le piège n'est pas le vide (Bégadan,
# V2-74) mais la FAUSSE PLÉNITUDE — le système croit avoir trouvé. Mutualisées par
# (pays, commune) comme les marchés, les activités et les commerces : un appel par secteur,
# réutilisé par tous les guides. Preuve ou rien, liste vide valide.
ESTANCO_FACT_TYPE = "estancos"
SHISHA_FACT_TYPE = "shisha_bars"
# VERSION DU CONTENU (motif V2-73b, « le cache masque le correctif »). Ces faits sont
# mutualisés par commune pendant 90 jours : sans version, un fait écrit par le prompt ÉTROIT
# de V2-77b empêche à jamais le prompt élargi de V2-77e de tourner — on corrige le prompt et
# rien ne change, parce que l'appel n'a plus lieu. C'est exactement ce qui a produit, en
# production à Adeje, « 3 candidats sur 6 » et « website/phone NULL ». Bump = re-collecte.
#   1 → V2-77b/c : prompt étroit, sans site ni ratissage par quartier
#   2 → V2-77e/f : vocabulaire multilingue, ratissage par quartier, site + téléphone exigés
ESTANCO_SCHEMA_V = 2
SHISHA_SCHEMA_V = 2

_ESTANCO_PROMPT = """\
Tu recenses les ESTANCOS (bureaux de tabac LICENCIÉS) de la commune de {city} ({country_code}).

CE QU'ON CHERCHE — le réseau d'État, sous son nom local : « estanco » / « expendeduría de
tabaco y timbre » (Espagne), « tabaccheria » (Italie), « bureau de tabac » (France),
« tabacaria » (Portugal), « Trafik » (Autriche). C'est le commerce qui vend AUSSI les
timbres, les tickets de transport, les recharges téléphoniques, parfois la vignette de
stationnement — c'est POUR CELA que le voyageur le cherche.

CE QU'ON NE VEUT PAS : les caves à cigares et boutiques à touristes (« La Casa del Habano »,
« Cigar Shop »), les boutiques de vapotage/CBD, les supérettes qui vendent des cigarettes.
Elles ne rendent AUCUN de ces services.

MÉTHODE : recherche web. Sources fiables : annuaire officiel du monopole, site de la MAIRIE,
pages jaunes locales, presse locale. Pour chaque estanco :
- `name` : le nom ou l'enseigne (« Estanco nº 12 », « Expendeduría 3 — Tabacos Pérez ») ;
- `place_address` : l'ADRESSE POSTALE telle qu'elle figure sur la source (rue + numéro +
  code postal + commune) — INDISPENSABLE pour le placer ; sans elle, écarte l'entrée ;
- `phone` : si disponible, sinon "" ;
- `website` : l'URL du site officiel si le lieu en a un, sinon "" ;
- `source_url` : l'URL de la preuve (https), `verified_on` : « {today} ».

RÈGLES STRICTES :
- PREUVE OU RIEN : pas d'adresse précise + pas de source https → écartée. N'invente JAMAIS.
- CHAQUE établissement a SON ADRESSE PROPRE. N'emprunte JAMAIS l'adresse (ni le nom) d'un
  autre lieu de la liste : deux établissements distincts au même point sont un MENSONGE sur
  la carte. Si tu n'as pas l'adresse propre d'un lieu, laisse `place_address` VIDE plutôt
  que d'en recopier une autre — il sera rattaché autrement, ou écarté.
- Reste DANS la commune de {city}, pas la ville voisine.
- Une liste VIDE est un résultat parfaitement valide.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "estancos": [
    {{"name": "Estanco nº 12", "place_address": "12 calle Grande, {city}",
      "phone": "", "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""

_SHISHA_PROMPT = """\
Tu recenses les BARS À CHICHA du secteur de {city} ({country_code}) — les lieux où l'on
fume la chicha SUR PLACE.

CHERCHE LARGE, ET POSE **TOUTES** LES VARIANTES DANS LA MÊME PASSE — un seul terme rate
l'essentiel (constat mesuré : 3 lieux trouvés sur 6 avec « shisha » seul) :
- espagnol : « cachimba », « cachimbas », « bar de cachimbas », « chicha », « shisha » ;
- anglais : « shisha », « hookah », « shisha lounge », « hookah lounge », « shisha bar » ;
- français : « chicha », « bar à chicha », « narguilé » ;
- translittérations : « narguile », « nargile », « narghile » ;
- formules d'ENSEIGNE, celles qu'ils emploient vraiment : « lounge », « lounge bar »,
  « gastrobar », « cocktail & shisha », « terraza lounge ».
Beaucoup ne se présentent JAMAIS comme « bar à chicha » : ils se disent « lounge » ou
« gastrobar ». Croise ces termes avec la langue du PAYS **et** avec l'anglais.

RATISSE PAR QUARTIER, pas seulement par commune : {city} regroupe souvent plusieurs
QUARTIERS / URBANIZACIONES / stations balnéaires. Identifie ceux du secteur et
interroge-les EXPLICITEMENT (« shisha lounge <quartier> », « cachimbas <quartier> »).

VISE 10 À 12 ADRESSES (échantillonne LARGE — le pipeline vérifie et positionne
strictement, il élaguera ce qui n'est pas prouvé ; mieux vaut proposer généreusement).

CES LIEUX PUBLIENT LEURS COORDONNÉES : la quasi-totalité a son PROPRE SITE et une fiche
Google/Instagram/TripAdvisor. Cherche « <nom> {city} », « <nom> instagram », « <nom>
tripadvisor » pour remonter le site officiel, le téléphone et l'adresse.

CE QU'ON NE VEUT PAS : les boutiques qui VENDENT du matériel de chicha sans service sur
place, les bars ordinaires sans offre de chicha, les restaurants sans chicha.

Pour chaque lieu :
- `name` : le nom EXACT de l'établissement, tel qu'il s'écrit sur sa devanture ou sa page ;
- `place_address` : l'adresse postale (rue + numéro + quartier + commune) si la source la
  donne, sinon "" ;
- `website` : l'URL du site OFFICIEL du lieu (pas un annuaire, pas TripAdvisor), sinon "" ;
- `phone` : le téléphone, sinon "" ;
- `source_url` : l'URL de la preuve (https), `verified_on` : « {today} ».

RÈGLES STRICTES :
- PREUVE OU RIEN : pas de source https → écartée. N'invente JAMAIS un bar à chicha.
- CHAQUE établissement a SON ADRESSE PROPRE. N'emprunte JAMAIS celle d'un autre lieu de la
  liste : trois bars distincts au même point sont un MENSONGE sur la carte (constat réel
  d'Adeje). Sans adresse propre, laisse `place_address` VIDE — le lieu sera rattaché au bar
  déjà connu, ou écarté.
- Reste DANS le secteur de {city}, pas une autre station à 30 km.
- Une liste VIDE est un résultat parfaitement valide (beaucoup de communes n'en ont aucun).

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "bars": [
    {{"name": "Kalani Lounge", "place_address": "avenue …, {city}",
      "website": "https://...", "phone": "+34 …",
      "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def _clean_web_places(data: dict, key: str, *, require_address: bool,
                      today: str) -> tuple[list[dict], int]:
    """Nettoyage COMMUN des deux passes V2-77b : preuve obligatoire (source https),
    adresse obligatoire ou non selon la passe, champs normalisés. PUR.

    Retourne `(retenus, brut)` — V2-77c : le BRUT est ce que le modèle a rendu AVANT
    « preuve ou rien ». Sans lui, un journal à « 0 estanco » ne permet pas de distinguer
    « le web n'a rien trouvé » de « nous avons tout écarté », et le diagnostic tourne en
    rond (constat de la recette Adeje)."""
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    items = data.get(key)
    if not isinstance(items, list):
        raise ValueError(f"Réponse IA invalide : '{key}' doit être une liste.")
    clean: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        def _s(k: str) -> str:
            v = it.get(k)
            return v.strip() if isinstance(v, str) else ""
        name, addr, src = _s("name"), _s("place_address"), _s("source_url")
        if not name or not src.startswith("https://"):
            continue                                   # PREUVE OU RIEN
        if require_address and not addr:
            continue                                   # sans adresse, pas plaçable
        entry = {"name": name, "place_address": addr, "source_url": src,
                 "verified_on": _s("verified_on") or today}
        # V2-77e : ces lieux PUBLIENT leurs coordonnées — la passe doit les rapporter,
        # sinon le voyageur a un nom sans moyen de réserver ni de vérifier les horaires.
        for k in ("phone", "website"):
            v = _s(k)
            if v:
                entry[k] = v
        clean.append(entry)
    return clean, len(items)


def fetch_estancos(city: str, country_code: str, client: anthropic.Anthropic,
                   today: str | None = None) -> tuple[dict, dict]:
    """Estancos (bureaux de tabac LICENCIÉS) d'une commune (V2-77b), vérifiés par recherche
    web. Retourne ({ESTANCO_FACT_TYPE: {"estancos": [...]}}, méta coût). Adresse EXIGÉE (un
    estanco sans adresse n'est pas plaçable). Preuve ou rien ; liste vide valide."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client, _ESTANCO_PROMPT.format(city=city, country_code=country_code, today=today),
        city=city, country_code=country_code,
        max_searches=settings.estanco_max_searches,
        max_tokens=settings.estanco_max_tokens)
    clean, raw = _clean_web_places(data, "estancos", require_address=True, today=today)
    return {ESTANCO_FACT_TYPE: {"estancos": clean, "raw": raw,
                                "v": ESTANCO_SCHEMA_V}}, meta


def fetch_shisha_bars(city: str, country_code: str, client: anthropic.Anthropic,
                      today: str | None = None) -> tuple[dict, dict]:
    """Bars à CHICHA d'une commune (V2-77b), vérifiés par recherche web. Retourne
    ({SHISHA_FACT_TYPE: {"bars": [...]}}, méta coût). L'adresse est FACULTATIVE : le premier
    usage est d'apparier le nom contre les bars DÉJÀ moissonnés (cascade V2-73g) pour y
    poser la puce — un lieu sans adresse et sans jumeau moissonné sera simplement écarté.
    Preuve ou rien ; liste vide valide."""
    today = today or _dt.date.today().isoformat()
    data, meta = _ask_web_search_json(
        client, _SHISHA_PROMPT.format(city=city, country_code=country_code, today=today),
        city=city, country_code=country_code,
        max_searches=settings.shisha_max_searches,
        max_tokens=settings.shisha_max_tokens)
    clean, raw = _clean_web_places(data, "bars", require_address=False, today=today)
    return {SHISHA_FACT_TYPE: {"bars": clean, "raw": raw,
                               "v": SHISHA_SCHEMA_V}}, meta


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
        max_searches=settings.market_max_searches,
        # Une commune riche peut compter des dizaines de marchés → réponse LONGUE :
        # plafond de sortie relevé pour éviter la troncature (constat prod 12/08).
        max_tokens=settings.market_max_tokens)
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


# ── Activités du secteur : « que fait-on ici ? » (V2-71) ──────────────────────
# Les activités SANS lieu propre (surf, plongée, randonnée, vélo, kayak, via ferrata…)
# sont le cœur des vacances mais n'apparaissent nulle part (ni OSM ni carte). Passe web
# dédiée, mutualisée par commune (area_fact), preuve ou rien comme la réputation.
ACTIVITIES_FACT_TYPE = "activities"

# Version du SCHÉMA du fait 'activities'. Stampée dès qu'une passe de POSITIONNEMENT a été
# TENTÉE (cascade stricte V2-56b, pipeline). Elle distingue « pas de position parce que
# jamais tenté » (v périmé → à rattraper) de « pas de position parce que non géocodable »
# (v courant → une activité diffuse — les 16 circuits, une route vicinale — ne se
# re-géocode pas indéfiniment).
#   v2 (V2-73b) : 1er positionnement, MAIS géocodait la phrase `where` entière → Nominatim
#                 échouait sur « Plage du Gurp, … (env. 10 km), côte atlantique » (placed:0).
#   v3 (V2-73c) : géocodage sur les champs STRUCTURÉS place_name/place_city (ou dérivés de
#                 `where` au rattrapage). Bump → les faits v2 sont RE-POSITIONNÉS (passe de
#                 géocodage seule, aucun web/LLM), pas re-collectés.
#   v4 (V2-73d) : géocodage de l'ADRESSE POSTALE d'abord (`place_address`, la mieux résolue
#                 par Nominatim), puis nom/commune, puis REPLI OSM PAR TAG (Overpass : la
#                 « Plage du Gurp » est un natural=beach qu'aucune recherche textuelle ne
#                 trouve). Bump → les faits v3 sont re-positionnés (le repli OSM place les
#                 lieux naturels sans re-collecte ; place_address ne sert qu'aux collectes
#                 neuves).
#   v5 (V2-73e) : passe ANTI-EMPILEMENT — deux activités au même point (< 50 m) = adresse
#                 empruntée ; seule la NOMMÉE garde le marqueur. Bump → les faits v4 sont
#                 dé-empilés (les diffuses qui partageaient une épingle la perdent).
#   v6 (V2-73g) : appariement à la MOISSON (POI déjà collectés, position VÉRIFIÉE) AVANT le
#                 géocodage, souple sur les lieux naturels (« Plage du Gurp » ↔ « Le Gurp »).
#                 Bump → les positions APPROXIMATIVES des faits v5 sont ré-évaluées (une
#                 position exacte de POI apparié est conservée). `exact` marque le POI apparié
#                 (pas de cercle d'approximation).
ACTIVITIES_SCHEMA_V = 6

_ACTIVITIES_PROMPT = """\
Tu prépares l'encart « Activités du secteur » du guide d'un lieu de vacances situé à
{city} ({country_code}). Rôle : lister les ACTIVITÉS de plein air / sportives / nature
qu'on vient y PRATIQUER — surtout celles SANS établissement propre (surf, plongée,
randonnée, VTT, kayak, paddle, via ferrata, ski, escalade, plongée en apnée, voile…).

MÉTHODE : recherche web. Ne retiens QUE ce qui a une PREUVE en ligne récente (office de
tourisme, guide, prestataire local, presse). Pour chaque activité :
- `activity` : le nom de l'activité, EN {lang_name} (« Surf », « Randonnée », « Plongée ») ;
- `where` : OÙ on la pratique, en {lang_name}, phrase LISIBLE affichée au voyageur (« Plage
  du Gurp, Grayan-et-l'Hôpital, côte atlantique du Médoc ») ;
- `place_name` : le NOM SEUL du lieu à cartographier, sans commune ni commentaire (« Plage
  du Gurp », « Massif du Montgó », « Calanque de Sormiou »). **VIDE "" si l'activité est
  DIFFUSE** (réseau de sentiers balisés, routes vicinales, marais/réserve étendue, « tout le
  secteur ») — mieux vaut aucun point qu'un point faux ;
- `place_address` : l'ADRESSE POSTALE **PROPRE À CE LIEU**, telle qu'elle figure sur la
  source (rue + code postal + commune, ex. « Le Gurp, route de l'océan, 33590 Grayan-et-
  l'Hôpital »), "" si la source n'en donne pas. C'est le champ que le géocodeur résout le
  MIEUX — donne-le dès que la source le mentionne ;
- **N'EMPRUNTE JAMAIS l'adresse NI le nom d'une AUTRE entrée** : chaque activité a SON
  propre lieu. Une activité DIFFUSE (sans lieu unique) a `place_name` ET `place_address`
  VIDES — ne lui recopie pas le point d'une plage/d'un site voisin (un seul marqueur pour
  N activités serait un mensonge) ;
- `place_city` : la COMMUNE de ce lieu (« Grayan-et-l'Hôpital »), "" si inconnue. Sert
  UNIQUEMENT à lever l'ambiguïté du géocodage — ne mets ni distance, ni parenthèse, ni
  région ;
- `season` : la saison/période si pertinente, en {lang_name} (« toute l'année », « été »,
  « décembre à avril ») sinon "" ;
- `source_url` : l'URL de la preuve, en https, la PLUS UTILE au voyageur — de préférence
  l'office de tourisme, le site officiel du site, ou une fédération/prestataire local ;
  ÉVITE les blogs agrégateurs. Donne la page DÉDIÉE à l'activité (pas la page d'accueil
  générique du domaine si une page précise existe). `verified_on` : « {today} ».

RÈGLES STRICTES :
- PREUVE OU RIEN : pas de source https vérifiable → écartée. N'invente JAMAIS.
- Une liste VIDE est un résultat parfaitement valide.
- Reste dans le secteur de {city} et ses environs immédiats. Pas de prose de remplissage.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "activities": [
    {{"activity": "Surf", "where": "Plage du Gurp, Grayan-et-l'Hôpital",
      "place_name": "Plage du Gurp",
      "place_address": "Le Gurp, route de l'océan, 33590 Grayan-et-l'Hôpital",
      "place_city": "Grayan-et-l'Hôpital",
      "season": "toute l'année", "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def fetch_activities(city: str, country_code: str, client: anthropic.Anthropic,
                     today: str | None = None, lang: str = "fr") -> tuple[dict, dict]:
    """Activités du secteur (V2-71), vérifiées par recherche web. Retourne
    ({ACTIVITIES_FACT_TYPE: {"activities": [...], "v": N}}, méta coût). Chaque activité
    retenue porte `activity`, `where` (phrase lisible), `place_address`/`place_name`/
    `place_city` (champs STRUCTURÉS pour le géocodage, V2-73c/d — vides si diffuse),
    `season`, `source_url`, `verified_on`. **PREUVE OU RIEN** (sans source → écartée).
    **Liste vide valide.** Réponse malformée → ValueError."""
    today = today or _dt.date.today().isoformat()
    lang_name = _REPUTED_LANG_NAMES.get(lang, "français")
    data, meta = _ask_web_search_json(
        client, _ACTIVITIES_PROMPT.format(city=city, country_code=country_code,
                                          today=today, lang_name=lang_name),
        city=city, country_code=country_code,
        max_searches=settings.activities_max_searches,
        max_tokens=settings.activities_max_tokens)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    activities = data.get("activities")
    if not isinstance(activities, list):
        raise ValueError("Réponse IA invalide : 'activities' doit être une liste.")
    clean: list[dict] = []
    for a in activities:
        if not isinstance(a, dict):
            continue
        def _s(key: str) -> str:
            v = a.get(key)
            return v.strip() if isinstance(v, str) else ""
        name = _s("activity")
        source_url = _s("source_url")
        if not (name and source_url):
            continue  # preuve ou rien
        # V2-73c/V2-73d : champs STRUCTURÉS pour le géocodage (adresse postale d'abord —
        # la mieux résolue —, puis nom/commune), distincts de la phrase lisible `where`.
        clean.append({"activity": name, "where": _s("where"), "season": _s("season"),
                      "place_name": _s("place_name"), "place_address": _s("place_address"),
                      "place_city": _s("place_city"),
                      "source_url": source_url, "verified_on": _s("verified_on") or today})
    # V2-73b : le fait porte sa version de schéma ; le positionnement (cascade stricte)
    # est tenté juste après par le pipeline → « v présent » = « positions tentées ».
    return {ACTIVITIES_FACT_TYPE: {"activities": clean, "v": ACTIVITIES_SCHEMA_V}}, meta


# ── Commerces & services de village (V2-74) ──────────────────────────────────
# Quand OSM est MUET sur une commune (constat Bégadan : ni épicerie ni pharmacie dans la
# bbox), le guide sert la ville voisine à 10 km comme « le plus proche » — faux, et dangereux
# pour une pharmacie. Passe web MUTUALISÉE par commune (mairie, pages jaunes locales, office
# de tourisme, presse), matérialisée en POI par logement (patron des marchés). Preuve ou rien.
LOCAL_COMMERCE_FACT_TYPE = "local_commerces"
# Catégories ESSENTIELLES éligibles : ce qu'un habitant cherche d'abord au village. Les codes
# correspondent aux `category_code` du seed (materialisés tels quels en POI).
LOCAL_COMMERCE_CATEGORIES = ("pharmacy", "supermarket", "bakery", "doctor", "post_office",
                             "tobacco")
_LOCAL_COMMERCE_LABELS = {"pharmacy": "pharmacie", "supermarket": "épicerie / supérette",
                          "bakery": "boulangerie", "doctor": "médecin / cabinet médical",
                          "post_office": "bureau de poste / point poste",
                          "tobacco": "bureau de tabac / estanco / tabaccheria"}

# V2-77 — le tabac ne se CHERCHE sur le web que là où le réseau est LICENCIÉ, donc
# RECENSÉ publiquement : estanco (ES), tabaccheria (IT), bureau de tabac (FR), tabacaria
# (PT), Trafik (AT). Ailleurs le tabac se vend en supérette ou en supermarché sans
# annuaire dédié : demander « les estancos de <commune> » ne pourrait rien rendre, et la
# règle « preuve ou rien » se contenterait de renvoyer une liste vide — au prix d'un appel
# web par commune. On n'engage donc pas la dépense. La catégorie, elle, reste moissonnée
# par OSM PARTOUT (`shop=tobacco` existe dans tous les pays) : c'est la seule DÉCOUVERTE
# WEB qui est bornée. Table ajustable — pas un quota, une réalité institutionnelle.
TOBACCO_LICENSED_COUNTRIES = frozenset({"ES", "IT", "FR", "PT", "AT"})


def local_commerce_categories_for(country_code: str | None) -> tuple[str, ...]:
    """Catégories éligibles à la DÉCOUVERTE WEB pour ce pays (V2-77).

    Le tabac en est retiré hors des pays à réseau licencié : y chercher un annuaire
    d'estancos serait une dépense sans objet. PURE (aucune E/S)."""
    cc = (country_code or "").upper()
    if cc in TOBACCO_LICENSED_COUNTRIES:
        return LOCAL_COMMERCE_CATEGORIES
    return tuple(c for c in LOCAL_COMMERCE_CATEGORIES if c != "tobacco")

_LOCAL_COMMERCE_PROMPT = """\
Tu prépares la liste des COMMERCES & SERVICES ESSENTIELS de la commune de {city} ({country_code})
— ceux qu'un habitant a près de chez lui. OpenStreetMap ignore souvent les petits villages :
ton rôle est de retrouver ce qui existe VRAIMENT sur place.

Catégories recherchées (renvoie `category` avec EXACTEMENT l'un de ces codes) :
{cats}

MÉTHODE : recherche web. Sources fiables : site de la MAIRIE, office de tourisme, pages jaunes
locales, presse/annuaire local. Pour chaque commerce :
- `name` : le nom de l'établissement (« Pharmacie du Centre », « Boulangerie Martin », « Vival ») ;
- `category` : le code EXACT parmi la liste ci-dessus ;
- `place_address` : l'ADRESSE POSTALE telle qu'elle figure sur la source (rue + code postal +
  commune) — INDISPENSABLE pour le placer sur la carte ; sans adresse précise, écarte l'entrée ;
- `phone` : le téléphone si disponible, sinon "" ;
- `source_url` : l'URL de la preuve (https), `verified_on` : « {today} ».

RÈGLES STRICTES :
- PREUVE OU RIEN : pas d'adresse précise + pas de source https → écartée. N'invente JAMAIS.
- CHAQUE établissement a SON ADRESSE PROPRE. N'emprunte JAMAIS l'adresse (ni le nom) d'un
  autre lieu de la liste : deux établissements distincts au même point sont un MENSONGE sur
  la carte. Si tu n'as pas l'adresse propre d'un lieu, laisse `place_address` VIDE plutôt
  que d'en recopier une autre — il sera rattaché autrement, ou écarté.
- Reste DANS la commune de {city} (ou un hameau qui en dépend), pas la ville voisine.
- Pour `tobacco`, cherche le réseau LICENCIÉ du pays sous SON nom local (« estanco » /
  « expendeduría de tabaco y timbre » en Espagne, « tabaccheria » en Italie, « bureau de
  tabac » en France) — c'est ainsi qu'il est recensé. N'invente pas un estanco : s'il n'y
  en a pas dans la commune, n'en renvoie aucun.
- Une liste VIDE est un résultat parfaitement valide (le village n'a peut-être rien).

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire :
{{
  "commerces": [
    {{"name": "Pharmacie du Centre", "category": "pharmacy",
      "place_address": "3 place de l'Église, 33340 {city}", "phone": "+33 5 …",
      "source_url": "https://...", "verified_on": "{today}"}}
  ]
}}
"""


def fetch_local_commerces(city: str, country_code: str, client: anthropic.Anthropic,
                          today: str | None = None, lang: str = "fr") -> tuple[dict, dict]:
    """Commerces & services ESSENTIELS d'une commune (V2-74), vérifiés par recherche web.
    Retourne ({LOCAL_COMMERCE_FACT_TYPE: {"commerces": [...]}}, méta coût). Chaque entrée
    retenue porte `name`, `category` (∈ LOCAL_COMMERCE_CATEGORIES), `place_address` (NON
    VIDE — sans adresse un commerce n'est pas plaçable), `phone`, `source_url`, `verified_on`.
    **PREUVE OU RIEN** (ni adresse ni source → écartée). **Liste vide valide.** Réponse
    malformée → ValueError (aucune écriture)."""
    today = today or _dt.date.today().isoformat()
    cats = "\n".join(f"- `{c}` : {_LOCAL_COMMERCE_LABELS[c]}" for c in LOCAL_COMMERCE_CATEGORIES)
    data, meta = _ask_web_search_json(
        client, _LOCAL_COMMERCE_PROMPT.format(city=city, country_code=country_code,
                                              today=today, cats=cats),
        city=city, country_code=country_code,
        max_searches=settings.local_commerce_max_searches,
        max_tokens=settings.local_commerce_max_tokens)
    if not isinstance(data, dict):
        raise ValueError("Réponse IA invalide : objet JSON attendu.")
    commerces = data.get("commerces")
    if not isinstance(commerces, list):
        raise ValueError("Réponse IA invalide : 'commerces' doit être une liste.")
    clean: list[dict] = []
    for c in commerces:
        if not isinstance(c, dict):
            continue
        def _s(key: str) -> str:
            v = c.get(key)
            return v.strip() if isinstance(v, str) else ""
        name, cat, addr = _s("name"), _s("category"), _s("place_address")
        source_url = _s("source_url")
        # PREUVE OU RIEN + catégorie connue + adresse (sinon non plaçable).
        if not (name and addr and source_url and cat in LOCAL_COMMERCE_CATEGORIES):
            continue
        clean.append({"name": name, "category": cat, "place_address": addr,
                      "phone": _s("phone") or None, "source_url": source_url,
                      "verified_on": _s("verified_on") or today})
    return {LOCAL_COMMERCE_FACT_TYPE: {"commerces": clean}}, meta


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
