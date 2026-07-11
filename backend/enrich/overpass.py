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

Deux catégories n'ont pas de tags OSM fiables et sont traitées par l'étape
Claude (recherche web) : food_delivery, babysitter.
"""
from __future__ import annotations

import math
import time

import httpx

from .settings import settings

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
    "train_station":   [("railway", "station")],
    "airport":         [("aeroway", "aerodrome")],
    "parking":         [("amenity", "parking")],
    "rental":          [("amenity", "bicycle_rental"), ("amenity", "car_rental")],
}

# Sélecteurs Overpass dérivés des tags positifs (ex. '"amenity"="hospital"').
CATEGORY_SELECTORS: dict[str, list[str]] = {
    code: [f'"{k}"="{v}"' for k, v in tags] for code, tags in CATEGORY_TAGS.items()
}

# Catégories sans tags OSM exploitables -> enrichies par Claude uniquement
CLAUDE_ONLY_CATEGORIES = {"food_delivery", "babysitter"}

# Paliers de rayon (m) : chaque catégorie est requêtée au plus petit palier
# >= à son rayon du seed, puis re-filtrée à son rayon exact. Regrouper par palier
# réduit fortement le nombre de requêtes Overpass.
_RADIUS_BUCKETS = (2000, 5000, 10000, 25000, 100000)

# Tags qui disqualifient un POI quelle que soit la catégorie demandée.
_DISQUALIFYING_TAGS: list[tuple[str, str]] = [
    ("shop", "estate_agent"),   # agence immobilière (constatée taggée marketplace)
]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Distance à vol d'oiseau en mètres (utile pour trier et pour le fallback)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


# ── Contrôle de cohérence catégorie / tags (M-01) ────────────────────────────

def _is_public_airport(tags: dict) -> bool:
    """Vrai pour un aérodrome ouvert au public (IATA ou type international/
    régional/public). Exclut bases militaires et aéroclubs (ni IATA, ni type
    public)."""
    if tags.get("iata"):
        return True
    return tags.get("aerodrome:type") in {"international", "regional", "public"}


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

def _build_query(selectors: list[str], lat: float, lon: float, radius_m: int) -> str:
    clauses = "".join(
        f'nwr[{sel}](around:{radius_m},{lat},{lon});' for sel in selectors
    )
    return (
        f"[out:json][timeout:{settings.overpass_timeout_s}];"
        f"({clauses});out center tags;"
    )


def _post_overpass(client: httpx.Client, query: str) -> list[dict]:
    """POST vers Overpass avec bascule automatique sur les miroirs.

    Le serveur principal overpass-api.de renvoie des 406 aux clients automatisés
    depuis 2026 : on essaie l'URL principale puis chaque miroir dans l'ordre.
    Lève la dernière erreur si tous échouent (disjoncteur en amont côté appelant).
    """
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    last_error: Exception | None = None
    for url in (settings.overpass_url, *settings.overpass_mirrors):
        try:
            resp = client.post(url, data={"data": query}, headers=headers)
            resp.raise_for_status()
            return resp.json().get("elements", [])
        except httpx.HTTPError as exc:
            last_error = exc
            continue  # miroir suivant
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
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "opening_hours": tags.get("opening_hours"),
        "source": "osm",
        "source_ref": f'{el.get("type", "node")}/{el.get("id")}',
        "crow_m": haversine_m(lat0, lon0, float(lat), float(lon)),
        "_tags": tags,
    }


def _finalize(pois: list[dict], limit: int) -> list[dict]:
    """Dédoublonne (même nom à < 100 m), trie par distance, plafonne, et retire
    les tags internes. Retourne des copies propres."""
    seen: dict[str, dict] = {}
    for p in sorted(pois, key=lambda p: p["crow_m"]):
        key = p["name"].lower()
        if key not in seen or p["crow_m"] < seen[key]["crow_m"] - 100:
            seen.setdefault(key, p)
    out = sorted(seen.values(), key=lambda p: p["crow_m"])[:limit]
    return [{k: v for k, v in p.items() if k != "_tags"} for p in out]


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
        matched = [p for p in parsed if p and category_matches(category, p["_tags"])]
        return _finalize(matched, settings.max_pois_per_category)
    finally:
        if own_client:
            client.close()
        time.sleep(settings.politeness_delay_s)  # politesse envers les serveurs publics


def fetch_grouped(categories: list[dict], lat: float, lon: float,
                  client: httpx.Client | None = None,
                  ) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Récupère les POI de plusieurs catégories en groupant les requêtes par
    palier de rayon (une requête Overpass par palier).

    `categories` : itérable de dicts {code, default_radius_m}. Retourne
    (`{code: [pois]}`, `{code: message}` pour les paliers en échec). Les
    catégories Claude-only et inconnues sont ignorées.
    """
    radius_of: dict[str, int] = {}
    buckets: dict[int, list[str]] = {}
    for cat in categories:
        code = cat["code"]
        if code in CLAUDE_ONLY_CATEGORIES or code not in CATEGORY_TAGS:
            continue
        radius_of[code] = cat["default_radius_m"]
        buckets.setdefault(_bucket_radius(cat["default_radius_m"]), []).append(code)

    results: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    own_client = client is None
    client = client or httpx.Client(timeout=settings.overpass_timeout_s + 5)
    try:
        for bucket, codes in buckets.items():
            selectors: list[str] = []
            for code in codes:
                for sel in CATEGORY_SELECTORS[code]:
                    if sel not in selectors:
                        selectors.append(sel)
            query = _build_query(selectors, lat, lon, bucket)
            try:
                elements = _post_overpass(client, query)
            except Exception as exc:  # tout le palier échoue -> catégories tracées
                msg = f"{type(exc).__name__}: {exc}"[:120]
                for code in codes:
                    failures[code] = msg
                continue
            finally:
                time.sleep(settings.politeness_delay_s)  # politesse entre requêtes

            parsed = [p for el in elements if (p := _element_to_poi(el, lat, lon))]
            for code in codes:
                matched = [
                    p for p in parsed
                    if category_matches(code, p["_tags"])
                    and p["crow_m"] <= radius_of[code]  # re-filtrage au rayon exact
                ]
                results[code] = _finalize(matched, settings.max_pois_per_category)
        _dedup_health_categories(results)
        return results, failures
    finally:
        if own_client:
            client.close()
