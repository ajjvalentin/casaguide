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
    "atm":             [("amenity", "atm")],
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

# Catégories capées aux N plus proches EN TEMPS DE TRAJET (V2-44), après calcul des
# distances (dans le pipeline). Un aéroport de vacances utile est l'un des rares
# hubs les plus proches — pas les 8 aérodromes du rayon (benchmark : 7 aéroports,
# dont Ostende à 132 min). NULL/absent = pas de cap dédié (plafond général de 8).
NEAREST_BY_TRAVEL: dict[str, int] = {"airport": 3}

# Noms GÉNÉRIQUES de type (V2-44) : un élément dont le nom N'EST QU'un mot de type
# (« Speeltuin », « Aire de jeux », « Trampoline ») n'a pas de nom propre → aucune
# valeur dans le guide (benchmark Op de Boerderie : « Speelweide », « Ballenbad »…).
# Liste multilingue, comparée NORMALISÉE (casse/accents) sur le nom ENTIER : « Trampoline
# Park Zeeland » (nom propre) n'y figure pas et est CONSERVÉ.
_GENERIC_NAMES: frozenset[str] = frozenset({
    # anglais
    "playground", "play area", "play ground", "sports field", "sports ground",
    # néerlandais
    "speeltuin", "speeltuintje", "speelweide", "speelplaats", "speelplek",
    "trampoline", "ballenbad", "glijbaan", "zandbak",
    # français
    "aire de jeux", "aire de jeu", "jeux pour enfants", "terrain de jeux",
    # espagnol
    "parque infantil", "zona de juegos", "area de juegos", "columpios",
    # allemand
    "spielplatz", "spielwiese", "bolzplatz", "trampolin",
    # italien
    "parco giochi", "area giochi",
})


def _norm_generic(name: str | None) -> str:
    """Nom normalisé pour le test générique : sans accents, minuscule, ponctuation →
    espace, espaces compactés (mêmes règles que dedup._norm)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return " ".join(s.split())


def is_generic_name(name: str | None) -> bool:
    """Vrai si le nom n'est QU'UN nom générique de type (V2-44). Comparaison sur le
    nom ENTIER normalisé → un nom propre qui CONTIENT un mot générique (« Trampoline
    Park Zeeland ») n'est jamais rejeté."""
    return _norm_generic(name) in _GENERIC_NAMES


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


def _dedup_sort(pois: list[dict]) -> list[dict]:
    """Dédoublonne (même nom à < 100 m) et trie par distance. Ne plafonne PAS et ne
    retire PAS les tags — base commune de `_finalize` et `_select_adaptive`."""
    seen: dict[str, dict] = {}
    for p in sorted(pois, key=lambda p: p["crow_m"]):
        key = p["name"].lower()
        if key not in seen or p["crow_m"] < seen[key]["crow_m"] - 100:
            seen.setdefault(key, p)
    return sorted(seen.values(), key=lambda p: p["crow_m"])


def _strip_tags(pois: list[dict]) -> list[dict]:
    """Retire les tags internes (`_tags`) — copies propres prêtes pour l'upsert."""
    return [{k: v for k, v in p.items() if k != "_tags"} for p in pois]


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
        return _finalize(matched, settings.max_pois_per_category)
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
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    try:
        # ── Passe 1 : rayon de PRÉFÉRENCE (historique) ──────────────────────
        m1, failures, generic = _run_buckets(client, codes, pref_of, lat, lon)
        for code in codes:
            results[code] = _finalize(m1.get(code, []), settings.max_pois_per_category)

        # ── Passe 2 : ESCALADE ciblée (rural) ───────────────────────────────
        deficient = [c for c in codes
                     if c not in failures
                     and max_of[c] > pref_of[c]
                     and len(results[c]) < settings.min_results_per_category]
        if deficient:
            m2, f2, g2 = _run_buckets(client, deficient, max_of, lat, lon)
            generic += g2
            for code in deficient:
                if code in f2:
                    continue  # escalade en échec : on garde le résultat de la passe 1
                results[code] = _select_adaptive(
                    m2.get(code, []), pref_of[code],
                    settings.min_results_per_category, settings.max_pois_per_category)

        _dedup_health_categories(results)
        empty = [c for c in codes if not results.get(c) and c not in failures]
        return results, failures, {"generic_dropped": generic, "empty": empty}
    finally:
        if own_client:
            client.close()
