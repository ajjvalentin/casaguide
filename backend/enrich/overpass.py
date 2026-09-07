"""Recherche de POI via l'API Overpass (OpenStreetMap).

Étape 2 du pipeline (§5.1). Pour chaque catégorie du seed (poi_categories),
on cherche les POI dans le rayon `default_radius_m` autour du logement.

Trois garde-fous qualité, tirés du premier test réel (M-01) :
  * cohérence catégorie/tags : un POI dont les tags contredisent la catégorie
    demandée est rejeté (agence immobilière taggée marketplace, bureau, etc.) ;
  * aéroports : seuls les aérodromes publics/IATA sont gardés (pas les bases
    militaires ni les aéroclubs) ;
  * santé : dédoublonnage entre `doctor` et `veterinary`.

Performance : `fetch_grouped` regroupe les catégories par palier de rayon en
une seule requête Overpass par palier (union de sélecteurs, résultats
re-ventilés par catégorie via leurs tags), puis re-filtre chaque catégorie à
son rayon exact du seed. On passe ainsi d'environ 25 requêtes à ~5.

Collecte adaptée à la ruralité (V2-44) : `default_radius_m` est le rayon de
PRÉFÉRENCE. Une passe 1 interroge à ce rayon (comportement historique) ; si une
catégorie n'atteint pas MIN_RESULTS et qu'elle a un `max_radius_m` plus large, une
passe 2 CIBLÉE escalade jusqu'au rayon maximal et complète avec les plus proches
au-delà de la préférence. En zone dense, la préférence est déjà pleine → aucune
escalade (sortie identique). Les requêtes lourdes ne partent donc que là où la
donnée est rare : coût nul en dense (cas courant), borné en rural.

Deux catégories n'ont pas de tags OSM fiables et sont traitées par l'étape
Claude (recherche web) : food_delivery, babysitter.
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx

from .settings import settings

log = logging.getLogger("casaguide.overpass")

# Codes HTTP transitoires : on réessaie (miroir suivant puis backoff). Le 406 en
# fait partie : overpass-api.de le renvoie par intermittence sous charge (voir
# `_post_overpass`). Un 400 (requête invalide) n'y est PAS : insister est inutile.
_RETRYABLE_STATUS = frozenset({406, 408, 425, 429, 500, 502, 503, 504})


class OverpassError(RuntimeError):
    """Refus HTTP d'un serveur Overpass. `str()` reste COURT (journal `steps` :
    « HTTP 406 de overpass-api.de ») ; le CORPS complet de la réponse (Overpass y
    explique parfois son refus) est journalisé à part par `_post_overpass`."""

    def __init__(self, url: str, status: int, body: str):
        self.url, self.status, self.body = url, status, body
        host = url.split("//", 1)[-1].split("/", 1)[0]
        super().__init__(f"HTTP {status} de {host}")


def _short(msg: str, limit: int = 160) -> str:
    """Troncature LISIBLE pour `enrichment_jobs.steps` : coupe à la limite mais sur
    une frontière de mot (jamais « For more informatio ») et suffixe « … »."""
    msg = " ".join((msg or "").split())  # normalise espaces/retours à la ligne
    if len(msg) <= limit:
        return msg
    cut = msg[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (cut or msg[:limit]) + "…"

# Catégorie CasaGuide -> tags OSM positifs (clé, valeur). Source de vérité unique :
# les sélecteurs de requête en sont dérivés, et le contrôle de cohérence s'appuie
# dessus pour re-ventiler les résultats d'une requête groupée.
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "hospital":        [("amenity", "hospital")],
    "pharmacy":        [("amenity", "pharmacy")],
    "doctor":          [("amenity", "doctors"), ("amenity", "dentist")],
    "police":          [("amenity", "police")],
    "veterinary":      [("amenity", "veterinary")],
    "supermarket":     [("shop", "supermarket")],
    "market":          [("amenity", "marketplace")],
    "bakery":          [("shop", "bakery")],
    # V2-47 : toute agence BANCAIRE a un distributeur. En centre-ville, OSM ne tague
    # souvent que les ATM crypto (« Bitcoin ATM », « BitBase ») → on élargit à
    # `amenity=bank` (l'agence entre comme « distributeur » sous son nom), et les ATM
    # crypto sont DÉPRIORISÉS (pas exclus) derrière les banques au tri.
    "atm":             [("amenity", "atm"), ("amenity", "bank")],
    "post_office":     [("amenity", "post_office")],
    "mall":            [("shop", "mall")],
    "laundry":         [("shop", "laundry"), ("shop", "dry_cleaning")],
    "restaurant":      [("amenity", "restaurant")],
    "bar":             [("amenity", "bar"), ("amenity", "pub")],
    "cafe":            [("amenity", "cafe")],
    "beach":           [("natural", "beach")],
    "sight":           [("tourism", "attraction"), ("tourism", "museum")],
    "family_activity": [("leisure", "water_park"), ("tourism", "theme_park"),
                        ("leisure", "playground")],
    "sport":           [("leisure", "sports_centre"), ("leisure", "golf_course")],
    "taxi":            [("amenity", "taxi")],
    "bus_stop":        [("highway", "bus_stop")],
    "bus_station":     [("amenity", "bus_station")],
    "train_station":   [("railway", "station")],
    "airport":         [("aeroway", "aerodrome")],
    "parking":         [("amenity", "parking")],
    "rental":          [("amenity", "bicycle_rental"), ("amenity", "car_rental")],
    "fuel":            [("amenity", "fuel")],               # M-30 : station-service
    "charging_station": [("amenity", "charging_station")],  # M-30 : borne de recharge
}

# Sélecteurs Overpass dérivés des tags positifs (ex. '"amenity"="hospital"').
CATEGORY_SELECTORS: dict[str, list[str]] = {
    code: [f'"{k}"="{v}"' for k, v in tags] for code, tags in CATEGORY_TAGS.items()
}

# Catégories sans tags OSM exploitables, traitées AILLEURS. La liste est désormais
# VIDE (V2-07) : `food_delivery` (volet 1) est résolue par zone (Claude + recherche
# web → area_facts) et `babysitter` (volet 2) est CRÉÉE par Claude + recherche web
# (`claude_enrich.fetch_babysitters`, POI source='claude'). Aucune des deux n'a de
# tag dans CATEGORY_TAGS → l'Overpass les saute de toute façon par la 2ᵉ condition
# (`code not in CATEGORY_TAGS`). On garde le nom (référencé par le pipeline) comme
# ensemble vide plutôt que de disperser des `if` : le contrat reste lisible.
CLAUDE_ONLY_CATEGORIES: set[str] = set()

# Paliers de rayon (m) : chaque catégorie est requêtée au plus petit palier
# >= à son rayon du seed, puis re-filtrée à son rayon exact. Regrouper par palier
# réduit fortement le nombre de requêtes Overpass.
_RADIUS_BUCKETS = (2000, 5000, 10000, 25000, 100000)

# Tags qui disqualifient un POI quelle que soit la catégorie demandée.
_DISQUALIFYING_TAGS: list[tuple[str, str]] = [
    ("shop", "estate_agent"),   # agence immobilière (constatée taggée marketplace)
]

# V2-47 — PROTECTION CIVILE mal classée en « police » (benchmark Murcie : « Protección
# Civil », base DIEM). Discriminateur : tags de protection civile / secours NON policiers,
# ou nom explicite. Une VRAIE police est `amenity=police` sans ces marqueurs.
_CIVIL_PROTECTION_RE = re.compile(
    r"protecc?i[oó]n\s+civil|protection\s+civile|civil\s+protection|"
    r"\bdiem\b|\bdya\b|cruz\s+roja|croix[- ]rouge|red\s+cross|bomber",
    re.IGNORECASE)

# V2-47 — COURSIERS / MESSAGERIES mal classés en « poste ». On EXCLUT le coursier
# (Ecomensajeros) mais on GARDE bureaux de poste ET points relais (post_partner).
_COURIER_RE = re.compile(r"mensajer|coursier|courier|\bglovo\b|\bstuart\b", re.IGNORECASE)

# Catégories capées aux N plus proches EN TEMPS DE TRAJET (V2-44), après calcul des
# distances (dans le pipeline). Un aéroport de vacances utile est l'un des rares
# hubs les plus proches — pas les 8 aérodromes du rayon (benchmark : 7 aéroports,
# dont Ostende à 132 min). NULL/absent = pas de cap dédié (plafond général de 8).
NEAREST_BY_TRAVEL: dict[str, int] = {"airport": 3}


# ── V2-44 volet 3 : minimum de résultats ET plafond de pertinence PAR catégorie ──
#
# Le volet 1 escaladait le rayon jusqu'à MIN_RESULTS=3, UNIFORME. Or trois
# commissariats à 30-32 min (benchmark Op de Boerderie) valent moins que le seul
# poste à 4 km : le quota uniforme fabrique des résultats lointains et trompeurs dans
# les catégories naturellement clairsemées. On règle DEUX curseurs par catégorie :
#   - `min_results`         : combien de lieux viser avant d'arrêter l'escalade ;
#   - `hard_cap_drive_min`  : temps de route au-delà duquel un résultat AMENÉ PAR
#     L'ESCALADE (hors rayon de préférence) n'est PAS retenu pour combler le quota —
#     mieux vaut une catégorie honnête, voire vide (signalée, volet 1), que remplie
#     de lieux inutiles. Un lieu DANS le rayon de préférence est toujours gardé.
#
# Le cap ne s'applique qu'à l'escalade (« pour satisfaire le quota ») : il exige le
# `drive_min` (OSRM), donc il est posé dans le PIPELINE après le calcul des distances
# (`apply_drive_cap`), tandis que `min_results` gouverne l'escalade dans `fetch_grouped`.
#
# Plafonds par chapitre (justif. scribe) : 20 min pour le quotidien (C, D hors
# hôpital, F=restauration, E listé, arrêt de bus) ; 45 min pour hôpital/aéroport/gare
# (hubs d'arrivée, trajet accepté) ; aucun pour G (excursions légitimes). Une
# catégorie ABSENTE de la table retombe sur le comportement du volet 1
# (`settings.min_results_per_category`, aucun cap) — voir `target_for`.
@dataclass(frozen=True)
class CategoryTarget:
    min_results: int
    hard_cap_drive_min: int | None = None


CATEGORY_TARGETS: dict[str, CategoryTarget] = {
    # Minimum 1 — « le plus proche suffit »
    "police":          CategoryTarget(1, 20),
    "hospital":        CategoryTarget(1, 45),
    "post_office":     CategoryTarget(1, 20),
    "train_station":   CategoryTarget(1, 45),
    "airport":         CategoryTarget(1, 45),
    "veterinary":      CategoryTarget(1, 20),
    # Minimum 2
    "pharmacy":        CategoryTarget(2, 20),
    "doctor":          CategoryTarget(2, 20),
    "atm":             CategoryTarget(2, 20),
    "bakery":          CategoryTarget(2, 20),
    "market":          CategoryTarget(2, 20),
    "mall":            CategoryTarget(2, 20),
    "laundry":         CategoryTarget(2, 20),
    "taxi":            CategoryTarget(2, 20),
    "bus_stop":        CategoryTarget(2, 20),
    # Minimum 3
    "supermarket":     CategoryTarget(3, 20),
    "beach":           CategoryTarget(3, None),   # G : excursions, pas de plafond
    "sight":           CategoryTarget(3, None),
    "family_activity": CategoryTarget(3, None),
    "sport":           CategoryTarget(3, None),
    "bar":             CategoryTarget(3, 20),
    "cafe":            CategoryTarget(3, 20),
    "rental":          CategoryTarget(3, 20),
    # Minimum 5
    "restaurant":      CategoryTarget(5, 20),
}


def target_for(code: str) -> CategoryTarget:
    """Cible (min_results + plafond) d'une catégorie. REPLI (catégorie absente de la
    table, ex. bus_station/parking/fuel, ou catégorie ajoutée plus tard) : comportement
    volet 1 — `settings.min_results_per_category` et AUCUN plafond, pour ne rien casser."""
    t = CATEGORY_TARGETS.get(code)
    if t is not None:
        return t
    return CategoryTarget(settings.min_results_per_category, None)


def apply_drive_cap(pois: list[dict], preferred_m: int,
                    hard_cap_drive_min: int | None) -> tuple[list[dict], int]:
    """Retire les POI amenés PAR L'ESCALADE (crow au-delà du rayon de préférence) dont
    le temps de route dépasse le plafond de pertinence (V2-44 volet 3). Renvoie
    `(gardés, n_retirés)`. Les POI DANS le rayon de préférence sont toujours gardés (ils
    sont réellement proches) ; sans plafond (None) ou sans `drive_min`/`crow_m` connu →
    inchangé. En zone dense (tout dans la préférence), ne retire jamais rien."""
    if not hard_cap_drive_min:
        return pois, 0
    kept: list[dict] = []
    dropped = 0
    for p in pois:
        crow = p.get("crow_m")
        drive = p.get("drive_min")
        beyond_preferred = crow is not None and crow > preferred_m
        if beyond_preferred and drive is not None and drive > hard_cap_drive_min:
            dropped += 1
        else:
            kept.append(p)
    return kept, dropped


# Noms GÉNÉRIQUES de type (V2-44, structuré par langue V2-47) : un élément dont le nom
# N'EST QU'un mot de type (« Speeltuin », « Aire de jeux », « Zona Infantil ») n'a pas de
# nom propre → aucune valeur dans le guide. Structuré PAR LANGUE pour les extensions
# futures (V2-47 : espagnol enrichi). Comparé NORMALISÉ (casse/accents/ponctuation) sur le
# nom ENTIER : « Trampoline Park Zeeland » (nom propre) n'y figure pas et est CONSERVÉ.
_GENERIC_NAMES_BY_LANG: dict[str, set[str]] = {
    "en": {"playground", "play area", "play ground", "sports field", "sports ground",
           "laundrette", "launderette"},
    "nl": {"speeltuin", "speeltuintje", "speelweide", "speelplaats", "speelplek",
           "trampoline", "ballenbad", "glijbaan", "zandbak", "wasserette", "wasserij",
           "was"},
    "fr": {"aire de jeux", "aire de jeu", "jeux pour enfants", "terrain de jeux",
           "laverie"},
    # Espagnol ENRICHI (V2-47, benchmark Murcie : « Zona Infantil », « Columpios, tobogán »
    # non filtrés — le volet 1 était calibré sur le néerlandais). Formes normalisées :
    # « Columpios, tobogán » → « columpios tobogan ».
    "es": {"parque infantil", "zona de juegos", "area de juegos", "columpios",
           "zona infantil", "columpios tobogan", "tobogan", "juegos infantiles",
           "area infantil", "zona de juegos infantiles", "lavanderia"},
    "de": {"spielplatz", "spielwiese", "bolzplatz", "trampolin"},
    "it": {"parco giochi", "area giochi"},
}
_GENERIC_NAMES: frozenset[str] = frozenset(
    n for names in _GENERIC_NAMES_BY_LANG.values() for n in names)


def _norm_name(name: str | None) -> str:
    """Nom normalisé : sans accents, minuscule, ponctuation → espace, espaces compactés
    (mêmes règles que dedup._norm). Base commune des filtres génériques et des dédups."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return " ".join(s.split())


# Compat : ancien nom interne conservé (référencé par des tests).
_norm_generic = _norm_name


def is_generic_name(name: str | None) -> bool:
    """Vrai si le nom n'est QU'UN nom générique de type (V2-44). Comparaison sur le
    nom ENTIER normalisé → un nom propre qui CONTIENT un mot générique (« Trampoline
    Park Zeeland ») n'est jamais rejeté."""
    return _norm_name(name) in _GENERIC_NAMES


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Distance à vol d'oiseau en mètres (utile pour trier et pour le fallback)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


# ── Contrôle de cohérence catégorie / tags (M-01) ────────────────────────────

def _is_public_airport(tags: dict) -> bool:
    """Vrai pour un aéroport CIVIL COMMERCIAL (V2-44, resserré). Proxy retenu :
    présence d'un tag `iata` (code commercial). On EXCLUT d'abord tout signe
    MILITAIRE (`military=*`, `aerodrome:type=military`, `landuse=military`) — une
    base à usage mixte peut porter un IATA mais n'a rien à faire dans un guide de
    vacances. Un aérodrome sans IATA (aéroclub, altiport, base) est écarté. Motif :
    benchmark Op de Boerderie (7 aéroports dont 2 bases militaires, Ostende à 132 min)."""
    if (tags.get("military")
            or tags.get("aerodrome:type") == "military"
            or tags.get("landuse") == "military"):
        return False
    return bool(tags.get("iata"))


def _is_disqualified(category: str, tags: dict) -> bool:
    """Tags qui rendent un POI incohérent avec la catégorie demandée."""
    # Bureaux / administratif : jamais un POI pertinent pour un voyageur.
    if "office" in tags:
        return True
    for k, v in _DISQUALIFYING_TAGS:
        if tags.get(k) == v:
            return True
    # Un vétérinaire n'est ni un médecin ni un dentiste (et inversement).
    if category != "veterinary" and tags.get("amenity") == "veterinary":
        return True
    # Un vrai marché hebdomadaire n'a pas de tag `shop` (minimarket, commerce…).
    if category == "market" and "shop" in tags:
        return True
    # Gare de TOURISME / PATRIMOINE (tram-musée, ligne préservée) : pas une gare de
    # transport réelle (V2-44 — cas « Middelplaat Haven (RTM) » du benchmark, tram
    # historique taggé railway=station).
    if category == "train_station" and (
            tags.get("usage") == "tourism"
            or tags.get("railway:historic")
            or tags.get("railway:preserved") == "yes"
            or tags.get("tourism") in {"attraction", "museum"}):
        return True
    # V2-47 — PROTECTION CIVILE / secours non policier classé « police » : les tags
    # `government=civil_protection`, `office=emergency`, un `emergency=*` non policier,
    # ou un nom explicite (Protección Civil, DIEM, Cruz Roja…) disqualifient.
    if category == "police":
        if (tags.get("government") in {"civil_protection", "emergency"}
                or tags.get("office") == "emergency"
                or (tags.get("emergency") and tags.get("emergency") != "police")
                or _CIVIL_PROTECTION_RE.search(tags.get("name") or "")
                or _CIVIL_PROTECTION_RE.search(tags.get("operator") or "")):
            return True
    # V2-47 — COURSIER / MESSAGERIE classé « poste » : exclu, SAUF un point relais
    # (`post_office=post_partner`) qu'on garde. Discriminateur : tag `office=courier`
    # (déjà couvert par l'exclusion `office` globale ci-dessus) ou nom de messagerie.
    if category == "post_office" and tags.get("post_office") != "post_partner":
        if (tags.get("post_office") == "courier"
                or tags.get("courier")
                or _COURIER_RE.search(tags.get("name") or "")):
            return True
    return False


def category_matches(category: str, tags: dict) -> bool:
    """Vrai si les tags OSM correspondent réellement à la catégorie demandée.

    1. au moins un tag positif de la catégorie ;
    2. aucun tag disqualifiant ;
    3. cas particulier des aéroports (publics/IATA uniquement)."""
    positives = CATEGORY_TAGS.get(category, [])
    if not any(tags.get(k) == v for k, v in positives):
        return False
    if _is_disqualified(category, tags):
        return False
    if category == "airport" and not _is_public_airport(tags):
        return False
    return True


def _dedup_health_categories(results: dict[str, list[dict]]) -> None:
    """Un même établissement ne doit pas figurer à la fois en doctor et
    veterinary : on le retire de `doctor` (priorité au vétérinaire, plus
    spécifique). Comparaison par source_ref ET par nom."""
    docs, vets = results.get("doctor"), results.get("veterinary")
    if not docs or not vets:
        return
    vet_keys = {p["source_ref"] for p in vets} | {p["name"].lower() for p in vets}
    results["doctor"] = [
        p for p in docs
        if p["source_ref"] not in vet_keys and p["name"].lower() not in vet_keys
    ]


# ── V2-47 : réductions PAR catégorie (source & granularité OSM) ───────────────
#
# Correctifs que ni rayon ni plafond ne traitent : dédup des systèmes en RÉSEAU (une
# station de vélos en libre-service comptée N fois), dédup node/way (même lieu en deux
# éléments à noms imbriqués), dépriorisation des ATM crypto derrière les banques. Ces
# passes opèrent sur les POI encore porteurs de leurs `_tags`, AVANT `_finalize`.

_CRYPTO_ATM_RE = re.compile(r"bitcoin|crypto|shitcoin|bitbase|coinstar", re.IGNORECASE)
# Catégories où la dédup par OPÉRATEUR/réseau est SÛRE : un système de vélos en
# libre-service (MUyBICI ×8) doit se réduire à sa station la plus proche. On l'exclut
# des commerces (les succursales d'une CHAÎNE — Mercadona, Lidl — sont des lieux
# distincts utiles, jamais à fusionner).
_NETWORK_DEDUP_CATEGORIES = frozenset({"rental"})
_NETWORK_DEDUP_MIN = 3            # à partir de 3 occurrences d'un même réseau
_NODE_WAY_DIST_M = 50.0          # deux éléments du même lieu (node + way)


def _is_crypto_atm(tags: dict) -> bool:
    """Vrai pour un distributeur de CRYPTOMONNAIE (V2-47) : tag `currency:XBT=yes`
    (ou autre crypto) ou nom/opérateur évocateur (« Bitcoin ATM », « BitBase »)."""
    for k, v in tags.items():
        if k.startswith("currency:") and k not in ("currency:EUR",) and v == "yes":
            return True
    return bool(_CRYPTO_ATM_RE.search(tags.get("name") or "")
                or _CRYPTO_ATM_RE.search(tags.get("operator") or ""))


# Mots GÉNÉRIQUES d'un système en réseau (V2-50) : ignorés comme clé de groupement (ils
# ne distinguent pas un système d'un autre — « estación », « servicio », « alquiler »…).
_NETWORK_STOP = frozenset({
    "estacion", "estacions", "station", "stations", "servicio", "servei", "public",
    "publico", "publica", "parada", "point", "punto", "sistema", "system", "ute",
    "bici", "bicis", "bike", "bikes", "bicicleta", "bicicletas", "rental", "alquiler",
    "lloguer", "location", "rent", "coche", "coches", "cars", "electrico", "electrica",
    "municipal", "aparcamiento", "sarl",
})


def _distinctive_tokens(p: dict) -> set[str]:
    """Tokens DISTINCTIFS (≥ 4 lettres, non génériques, non numériques) du nom/opérateur —
    servent à rapprocher un même système écrit différemment (V2-50 : « MUyBICI: * » et
    « UTE MuyBici servicio público… » partagent « muybici »)."""
    toks: set[str] = set()
    t = p.get("_tags", {})
    for src in (t.get("network"), t.get("operator"), p.get("name")):
        for tok in _norm_name(src).split():
            if len(tok) >= 4 and not tok.isdigit() and tok not in _NETWORK_STOP:
                toks.add(tok)
    return toks


def _group_keys(p: dict) -> set[str]:
    """Clés candidates de groupement RÉSEAU d'un POI (V2-50, généralisé) : préfixe avant
    « : » (V2-47), opérateur/réseau entier, ET tokens distinctifs (tête de nom comprise,
    « BiciCampus * »). Un POI peut porter plusieurs clés ; le regroupement est glouton."""
    keys = set(_distinctive_tokens(p))
    t = p.get("_tags", {})
    op = (t.get("network") or t.get("operator") or "").strip()
    if op:
        keys.add(_norm_name(op).replace(" ", ""))
    name = p.get("name") or ""
    if ":" in name:
        pre = _norm_name(name.split(":", 1)[0]).replace(" ", "")
        if len(pre) >= 3:
            keys.add(pre)
    return keys


def _group_label(members: list[dict]) -> str:
    """Libellé propre d'un groupe réseau (V2-50) : dérivé du membre au nom le plus COURT
    (souvent le nom de système le plus net) — opérateur, sinon préfixe avant « : », sinon
    premier token du nom."""
    src = min(members, key=lambda p: len(p.get("name") or "~" * 99))
    t = src.get("_tags", {})
    op = (t.get("network") or t.get("operator") or "").strip()
    if op:
        return op
    name = (src.get("name") or "").strip()
    if ":" in name:
        return name.split(":", 1)[0].strip()
    return name.split()[0] if name.split() else name


def _dedup_by_operator(pois: list[dict]) -> tuple[list[dict], int]:
    """Réduit les systèmes en réseau (V2-47, GÉNÉRALISÉ V2-50) : ≥ `_NETWORK_DEDUP_MIN`
    POI partageant une clé de groupement (préfixe « : », opérateur, OU token distinctif de
    tête) → on ne garde que le PLUS PROCHE, renommé « X (station la plus proche) ».
    Regroupement GLOUTON (le plus grand groupe d'abord ; chaque POI assigné une fois) →
    « BiciCampus * » et « MUyBICI: * »/« UTE MuyBici… » sont enfin réduits. Ordre stable."""
    key_members: dict[str, list[int]] = {}
    for i, p in enumerate(pois):
        for k in _group_keys(p):
            key_members.setdefault(k, []).append(i)
    # Clés candidates à réduire, les plus grosses d'abord (assignation gloutonne).
    candidates = sorted((k for k, m in key_members.items() if len(m) >= _NETWORK_DEDUP_MIN),
                        key=lambda k: -len(key_members[k]))
    assigned: dict[int, str] = {}
    for k in candidates:
        members = [i for i in key_members[k] if i not in assigned]
        if len(members) >= _NETWORK_DEDUP_MIN:
            for i in members:
                assigned[i] = k
    if not assigned:
        return pois, 0
    groups: dict[str, list[int]] = {}
    for i, k in assigned.items():
        groups.setdefault(k, []).append(i)
    survivor: dict[str, int] = {
        k: min(idxs, key=lambda i: pois[i]["crow_m"]) for k, idxs in groups.items()}
    labels = {k: _group_label([pois[i] for i in idxs]) for k, idxs in groups.items()}
    out: list[dict] = []
    dropped = 0
    for i, p in enumerate(pois):
        k = assigned.get(i)
        if k is None:
            out.append(p)
        elif survivor[k] == i:
            keep = dict(p)
            keep["name"] = f"{labels[k]} (station la plus proche)"
            out.append(keep)
        else:
            dropped += 1
    return out, dropped


def _nonempty(v) -> bool:
    return bool(v.strip()) if isinstance(v, str) else v is not None


def _completeness(p: dict) -> tuple[int, int]:
    """Score de complétude d'un POI (V2-47, dédup node/way) : nb de champs renseignés
    puis longueur du nom — le SURVIVANT est le plus complet (« Tintorería Greco » >
    « Greco »)."""
    fields = sum(1 for f in ("phone", "website", "opening_hours", "cuisine", "address")
                 if _nonempty(p.get(f)))
    return fields, len(p.get("name") or "")


def _name_nested(a: str, b: str) -> bool:
    """Vrai si le nom normalisé le plus court est un SOUS-ENSEMBLE de tokens de l'autre
    (« greco » ⊂ « tintoreria greco »). Jamais un simple chevauchement partiel."""
    ta, tb = set(_norm_name(a).split()), set(_norm_name(b).split())
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _dedup_node_way(pois: list[dict]) -> tuple[list[dict], int]:
    """Dédup node/way (V2-47) : deux POI à < 50 m dont l'un des noms est imbriqué dans
    l'autre → garder le plus COMPLET. Renvoie (liste, n_retirés). Ordre stable (le
    survivant garde la place du premier rencontré)."""
    keep: list[dict] = []
    dropped = 0
    for p in pois:
        dup_idx = None
        for i, s in enumerate(keep):
            if (haversine_m(p["lat"], p["lon"], s["lat"], s["lon"]) <= _NODE_WAY_DIST_M
                    and _name_nested(p.get("name") or "", s.get("name") or "")):
                dup_idx = i
                break
        if dup_idx is None:
            keep.append(p)
        else:
            dropped += 1
            if _completeness(p) > _completeness(keep[dup_idx]):
                keep[dup_idx] = p     # remplace en place par le plus complet
    return keep, dropped


def _reduce_category(code: str, pois: list[dict]) -> tuple[list[dict], int]:
    """Applique les réductions V2-47 propres à une catégorie (avant `_finalize`).
    Renvoie (liste réduite, n_retirés). Ordre : réseau (rental) → node/way → priorité
    crypto (atm). Les POI conservent leurs `_tags` (l'appelant finalise ensuite)."""
    dropped = 0
    if code in _NETWORK_DEDUP_CATEGORIES:
        pois, n = _dedup_by_operator(pois)
        dropped += n
    pois, n = _dedup_node_way(pois)
    dropped += n
    if code == "atm":
        for p in pois:               # crypto DÉPRIORISÉ (pas exclu) derrière les banques
            if _is_crypto_atm(p.get("_tags", {})):
                p["_priority"] = 1
    return pois, dropped


# ── Requête et parsing ───────────────────────────────────────────────────────

def _bucket_timeout(bucket_m: int) -> int:
    """Timeout Overpass adapté au rayon (M-18) : le palier aéroport (≥ 50 km,
    typiquement 100 km) est bien plus lourd → timeout dédié plus long."""
    return (settings.overpass_timeout_far_s
            if bucket_m >= settings.overpass_far_bucket_m
            else settings.overpass_timeout_s)


def _build_query(selectors: list[str], lat: float, lon: float, radius_m: int,
                 timeout_s: int | None = None) -> str:
    clauses = "".join(
        f'nwr[{sel}](around:{radius_m},{lat},{lon});' for sel in selectors
    )
    return (
        f"[out:json][timeout:{timeout_s or settings.overpass_timeout_s}];"
        f"({clauses});out center tags;"
    )


def _post_overpass(client: httpx.Client, query: str,
                   timeout_s: int | None = None) -> list[dict]:
    """POST vers Overpass avec bascule sur les miroirs ET backoff sur 406/429.

    **Correctif OPS-4 (12/08).** Reproduit : la MÊME requête (celle que ce code
    construit) reçoit par intermittence un **406 Not Acceptable** d'overpass-api.de
    — corps = page Apache générique (`Server: Apache`, `text/html`), pas un message
    Overpass. A/B décisif : `Accept: application/json` → ~8/15 en 406 ;
    `Accept: */*` → **0/15**. Cause : la négociation de contenu Apache
    (mod_negotiation) de l'endpoint interpreter n'offre aucune variante
    `application/json` → 406. Le format de sortie est décidé par `[out:json]` DANS
    la requête, jamais par l'en-tête HTTP `Accept` → on envoie `*/*`.

    En plus : on essaie l'URL principale puis chaque miroir ; sur un statut
    **transitoire** (`_RETRYABLE_STATUS`, dont 406/429/503/504) on journalise le
    CORPS COMPLET (il explique parfois le refus), on passe au miroir suivant, puis on
    RÉESSAIE la liste avec un backoff croissant. Un 4xx non transitoire (400 :
    requête invalide) lève tout de suite. `timeout_s` (M-18) surcharge le timeout HTTP
    (palier aéroport). L'exception levée reste COURTE pour `steps` (corps déjà logué)."""
    headers = {"User-Agent": settings.user_agent, "Accept": "*/*"}
    post_kwargs: dict = {}
    if timeout_s is not None:
        post_kwargs["timeout"] = timeout_s + 5  # marge au-dessus du [timeout:] serveur
    urls = (settings.overpass_url, *settings.overpass_mirrors)
    last_error: Exception | None = None
    for attempt in range(1, settings.overpass_max_attempts + 1):
        for url in urls:
            try:
                resp = client.post(url, data={"data": query}, headers=headers, **post_kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Overpass %s : erreur réseau (essai %d) : %s",
                            url, attempt, exc)
                continue  # miroir suivant
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            body = (resp.text or "").strip()
            last_error = OverpassError(url, resp.status_code, body)
            # Journal COMPLET côté logs (le corps explique le refus) ; tronqué
            # PROPREMENT côté `steps` par l'appelant via `str(OverpassError)`.
            log.warning("Overpass %s : HTTP %d (essai %d) — corps :\n%s",
                        url, resp.status_code, attempt, body[:2000])
            if resp.status_code not in _RETRYABLE_STATUS:
                raise last_error  # 400… : insister sur les miroirs ne sert à rien
        if attempt < settings.overpass_max_attempts:
            time.sleep(settings.overpass_backoff_s * attempt)  # backoff croissant
    raise last_error or RuntimeError("Aucun serveur Overpass joignable")


def _element_to_poi(el: dict, lat0: float, lon0: float) -> dict | None:
    """Transforme un élément Overpass en POI. Conserve les tags (`_tags`) pour la
    re-ventilation par catégorie ; ils sont retirés par `_finalize`."""
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name:
        return None  # un POI sans nom n'a pas d'intérêt dans le guide
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    if lat is None or lon is None:
        return None
    addr = ", ".join(filter(None, [
        " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
        tags.get("addr:city"),
    ])) or None
    return {
        "name": name,
        "lat": float(lat),
        "lon": float(lon),
        "address": addr,
        # Localité RÉELLE du POI (V2-37) : passée telle quelle au prompt de description
        # (ne JAMAIS supposer la commune du logement). En mémoire seulement, non stockée.
        "locality": (tags.get("addr:city") or "").strip() or None,
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "opening_hours": tags.get("opening_hours"),
        "cuisine": _norm_cuisine(tags.get("cuisine")),  # M-16 : type de cuisine
        "source": "osm",
        "source_ref": f'{el.get("type", "node")}/{el.get("id")}',
        "crow_m": haversine_m(lat0, lon0, float(lat), float(lon)),
        "_tags": tags,
    }


def _norm_cuisine(raw: str | None) -> str | None:
    """Normalise le tag OSM `cuisine` (M-16 + validation de forme V2-35).

    Le tag OSM est multi-valué par `;` (`italian;pizza`) — on ne garde que le PREMIER
    terme, en minuscules. **Validation de forme (V2-35)** : un tag de cuisine fait 1 à
    3 mots, jamais une phrase — un mappeur OSM écrit parfois de la prose (« Modern,
    international cuisine and mixology », séparée par des virgules, pas par `;`). Au-delà
    de 3 mots → tag IGNORÉ (renvoie None) et NON tronqué : couper « Modern, … » à
    « modern » inventerait un tag que le lieu ne revendique pas. Renvoie None si vide."""
    if not raw:
        return None
    first = raw.split(";")[0].strip().lower()   # séparateur OSM standard : `;`
    if not first or len(first.split()) > 3:
        return None
    return first


def _sort_key(p: dict) -> tuple[int, int]:
    """Tri par (PRIORITÉ, distance) — `_priority` (défaut 0) déprioriser sans exclure
    (V2-47 : ATM crypto derrière les banques). 0 = normal, 1 = relégué."""
    return (p.get("_priority", 0), p["crow_m"])


def _dedup_sort(pois: list[dict]) -> list[dict]:
    """Dédoublonne (même nom à < 100 m) et trie par (priorité, distance). Ne plafonne
    PAS et ne retire PAS les tags — base commune de `_finalize` et `_select_adaptive`."""
    seen: dict[str, dict] = {}
    for p in sorted(pois, key=_sort_key):
        key = p["name"].lower()
        if key not in seen or p["crow_m"] < seen[key]["crow_m"] - 100:
            seen.setdefault(key, p)
    return sorted(seen.values(), key=_sort_key)


def _strip_tags(pois: list[dict]) -> list[dict]:
    """Retire les champs internes (`_tags`, `_priority`…) — copies propres prêtes pour
    l'upsert. Tout ce qui commence par « _ » est interne (jamais stocké)."""
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in pois]


def _finalize(pois: list[dict], limit: int) -> list[dict]:
    """Dédoublonne, trie par distance, plafonne, retire les tags internes."""
    return _strip_tags(_dedup_sort(pois)[:limit])


def _select_adaptive(matched: list[dict], preferred_m: int,
                     min_results: int, limit: int) -> list[dict]:
    """Sélection ADAPTÉE à la ruralité (V2-44). `matched` = POI déjà bornés au rayon
    MAXIMAL. On garde tout ce qui est dans le rayon de PRÉFÉRENCE ; si c'est moins que
    `min_results`, on complète avec les plus proches au-delà jusqu'à `min_results`.
    Plafond `limit` inchangé. En zone dense (préférence pleine), renvoie exactement le
    rayon de préférence → sortie identique à l'historique."""
    clean = _dedup_sort(matched)                       # trié par distance, dédoublonné
    within = [p for p in clean if p["crow_m"] <= preferred_m]
    chosen = within if len(within) >= min_results else clean[:min_results]
    return _strip_tags(chosen[:limit])


def _bucket_radius(radius_m: int) -> int:
    """Plus petit palier standard >= au rayon demandé (jamais inférieur, pour ne
    manquer aucun POI ; le re-filtrage au rayon exact se fait ensuite)."""
    for b in _RADIUS_BUCKETS:
        if radius_m <= b:
            return b
    return radius_m


# ── API publique ─────────────────────────────────────────────────────────────

def fetch_category(category: str, lat: float, lon: float, radius_m: int,
                   client: httpx.Client | None = None) -> list[dict]:
    """POI d'une catégorie, filtrés/cohérents, triés par distance, plafonnés.

    Conservé pour compat/tests ; le pipeline utilise `fetch_grouped`."""
    if category not in CATEGORY_TAGS:
        return []
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    try:
        query = _build_query(CATEGORY_SELECTORS[category], lat, lon, radius_m)
        elements = _post_overpass(client, query)
        parsed = (_element_to_poi(el, lat, lon) for el in elements)
        matched = [p for p in parsed
                   if p and not is_generic_name(p["name"])  # V2-44 : noms génériques
                   and category_matches(category, p["_tags"])]
        reduced, _ = _reduce_category(category, matched)     # V2-47 : réductions
        return _finalize(reduced, settings.max_pois_per_category)
    finally:
        if own_client:
            client.close()
        time.sleep(settings.politeness_delay_s)  # politesse envers les serveurs publics


def _run_buckets(client: httpx.Client, codes: list[str],
                 query_radius: dict[str, int], lat: float, lon: float,
                 ) -> tuple[dict[str, list[dict]], dict[str, str], int]:
    """Interroge Overpass pour `codes`, groupés par palier de rayon selon
    `query_radius[code]` (une requête par palier, union de sélecteurs). Renvoie
    (`{code: [POI matched, crow ≤ query_radius[code]]}`, `{code: msg}` des paliers en
    échec, n_generic) — n_generic = nb d'éléments REJETÉS à la moisson pour nom
    générique (V2-44). Les POI ne sont NI dédoublonnés NI plafonnés (l'appelant finalise)."""
    buckets: dict[int, list[str]] = {}
    for code in codes:
        buckets.setdefault(_bucket_radius(query_radius[code]), []).append(code)

    matched: dict[str, list[dict]] = {code: [] for code in codes}
    failures: dict[str, str] = {}
    generic_dropped = 0
    for bucket, bcodes in buckets.items():
        selectors: list[str] = []
        for code in bcodes:
            for sel in CATEGORY_SELECTORS[code]:
                if sel not in selectors:
                    selectors.append(sel)
        # M-18 : un palier lointain (≥ 50 km, aéroport) reçoit un timeout dédié plus long.
        timeout_s = _bucket_timeout(bucket)
        query = _build_query(selectors, lat, lon, bucket, timeout_s=timeout_s)
        try:
            elements = _post_overpass(client, query, timeout_s=timeout_s)
        except Exception as exc:  # tout le palier échoue -> catégories tracées
            # `str(OverpassError)` est déjà court ; le corps complet a été logué par
            # `_post_overpass`. Troncature PROPRE (jamais un mot coupé à cru) pour `steps`.
            msg = _short(f"{type(exc).__name__}: {exc}")
            log.warning("Palier %s m (%s) en échec : %s", bucket, ",".join(bcodes), msg)
            for code in bcodes:
                failures[code] = msg
            continue
        finally:
            time.sleep(settings.politeness_delay_s)  # politesse entre requêtes

        parsed: list[dict] = []
        for el in elements:
            p = _element_to_poi(el, lat, lon)
            if p is None:
                continue
            if is_generic_name(p["name"]):   # V2-44 : nom générique de type -> rejeté
                generic_dropped += 1
                continue
            parsed.append(p)
        for code in bcodes:
            matched[code] = [
                p for p in parsed
                if category_matches(code, p["_tags"])
                and p["crow_m"] <= query_radius[code]  # re-filtrage au rayon exact
            ]
    return matched, failures, generic_dropped


def fetch_grouped(categories: list[dict], lat: float, lon: float,
                  client: httpx.Client | None = None,
                  ) -> tuple[dict[str, list[dict]], dict[str, str], dict]:
    """Récupère les POI de plusieurs catégories, ADAPTÉ à la ruralité (V2-44).

    Deux passes AU PLUS, groupées par palier de rayon (une requête Overpass par palier) :
      1. au rayon de PRÉFÉRENCE (`default_radius_m`) — comportement historique ; en
         zone dense la préférence est déjà pleine et c'est terminé.
      2. ESCALADE au rayon MAXIMAL (`max_radius_m`), UNIQUEMENT pour les catégories qui
         n'ont pas atteint MIN_RESULTS et dont max > préférence — regroupées (~1 requête
         de plus). Les requêtes LOURDES (grand rayon) ne partent donc que là où la donnée
         est rare : coût nul en zone dense (cas courant), borné en rural.

    `categories` : dicts {code, default_radius_m, max_radius_m?}. Retourne
    (`{code: [pois]}`, `{code: message}` des paliers en échec, `stats`) où
    stats = {"generic_dropped": n, "empty": [codes sans résultat, hors échec]}. Les
    catégories Claude-only et inconnues sont ignorées.
    """
    pref_of: dict[str, int] = {}
    max_of: dict[str, int] = {}
    for cat in categories:
        code = cat["code"]
        if code in CLAUDE_ONLY_CATEGORIES or code not in CATEGORY_TAGS:
            continue
        pref_of[code] = cat["default_radius_m"]
        max_of[code] = cat.get("max_radius_m") or cat["default_radius_m"]
    codes = list(pref_of)

    results: dict[str, list[dict]] = {}
    network_dropped = 0
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    try:
        # ── Passe 1 : rayon de PRÉFÉRENCE (historique) ──────────────────────
        m1, failures, generic = _run_buckets(client, codes, pref_of, lat, lon)
        for code in codes:
            reduced, n = _reduce_category(code, m1.get(code, []))   # V2-47
            network_dropped += n
            results[code] = _finalize(reduced, settings.max_pois_per_category)

        # ── Passe 2 : ESCALADE ciblée (rural), MIN_RESULTS par catégorie (V2-44 v3) ─
        deficient = [c for c in codes
                     if c not in failures
                     and max_of[c] > pref_of[c]
                     and len(results[c]) < target_for(c).min_results]
        if deficient:
            m2, f2, g2 = _run_buckets(client, deficient, max_of, lat, lon)
            generic += g2
            for code in deficient:
                if code in f2:
                    continue  # escalade en échec : on garde le résultat de la passe 1
                reduced, n = _reduce_category(code, m2.get(code, []))   # V2-47
                network_dropped += n
                results[code] = _select_adaptive(
                    reduced, pref_of[code],
                    target_for(code).min_results, settings.max_pois_per_category)

        _dedup_health_categories(results)
        empty = [c for c in codes if not results.get(c) and c not in failures]
        return results, failures, {"generic_dropped": generic,
                                   "network_dropped": network_dropped, "empty": empty}
    finally:
        if own_client:
            client.close()
