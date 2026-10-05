"""Géocodage d'une adresse via Nominatim (OpenStreetMap).

Étape 1 du pipeline (§5.1 du CdC). Essaie plusieurs stratégies, de la plus
précise à la plus grossière (échelle de repli), car les adresses résidentielles
ne sont pas toujours cartographiées dans OSM :

  1. recherche structurée rue + numéro + code postal + ville  -> rooftop/street
  2. recherche structurée rue sans numéro                     -> street
  3. code postal + ville                                      -> city
  4. ville seule                                              -> city

Une précision 'city' signifie que le propriétaire devra positionner le point
sur la carte dans le back-office (prévu au CdC, champ geocode_accuracy).
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

import httpx

from .settings import settings

log = logging.getLogger(__name__)

_ACCURACY = {
    "building": "rooftop", "house": "rooftop", "residential": "rooftop",
    "road": "street", "street": "street",
    # Types de LIEU précis (V2-07 volet 3) : une place/un marché géocodé à ce niveau
    # est exploitable pour un marqueur (≠ « city », qui est le repli imprécis).
    "square": "street", "marketplace": "street", "pedestrian": "street",
}


# Précisions ACCEPTABLES pour ancrer un guide voyageur (V2-68 p1). Une précision
# « city » (centroïde administratif) ou « mismatch » (commune incohérente) produit un
# guide vague (POI jusqu'à 60 km, catégories vitales en échec de palier à Tokyo) → le
# tunnel/API exige alors une rue ou un point ajusté. `manual` = point posé à la main.
_PRECISE_ACCURACY = frozenset({"rooftop", "street", "manual"})


def is_precise_enough(accuracy: str | None) -> bool:
    """Vrai si la précision de géocodage suffit à ancrer un guide (rue/quartier/point
    manuel). Faux pour « city »/« mismatch »/None → il faut préciser (V2-68 p1)."""
    return accuracy in _PRECISE_ACCURACY


class GeocodeError(Exception):
    pass


class GeocodeRateLimited(GeocodeError):
    """Nominatim refuse encore (429/503) après les essais (V2-79b). Un GeocodeError :
    les appelants du pipeline l'absorbent (le lieu tombe, le job continue)."""


# ── V2-46 : contrôle de cohérence post-géocodage ──────────────────────────────
#
# CASA MURCIA (« Príncipe de Asturias 38, 30007, MURCIA ») : Nominatim a résolu une
# rue HOMONYME à Torre-Pacheco (30700, ~40 km) et l'a étiquetée « précis » → 132 POI
# hors sujet sur une fiche publiée, sans aucune alerte. Les odonymes homonymes sont
# très fréquents en Espagne. On compare donc la COMMUNE (et le code postal) du résultat
# à la saisie : tout écart interdit l'étiquette « précis » et lève une alerte.
#
# Niveaux d'adresse Nominatim comparés à la commune saisie — municipalité et EN DESSOUS
# UNIQUEMENT. On EXCLUT délibérément province/région/état (`state`, `county`,
# `province`) : Torre-Pacheco est DANS la région de Murcie, si bien qu'inclure la
# province ferait « matcher » la saisie « Murcia » et MANQUERAIT le défaut. Comparer à
# TOUS ces niveaux (pas seulement le premier) évite le faux positif village↔municipalité
# (« Noordgouwe » dans « Schouwen-Duiveland » : la saisie matche le niveau `village`).
_MUNICIPAL_KEYS = ("city", "town", "village", "hamlet", "municipality",
                   "suburb", "city_district", "borough", "quarter")


@dataclass(frozen=True)
class Mismatch:
    """Écart détecté entre la saisie et le résultat de géocodage (V2-46)."""
    input_city: str | None
    input_postcode: str | None
    result_locality: str | None
    result_postcode: str | None

    def message_fr(self) -> str:
        """Phrase d'alerte prête pour l'UI propriétaire."""
        def _fmt(place, pc):
            place = place or "un lieu inconnu"
            return f"{place} ({pc})" if pc else place
        return (f"L'adresse a été localisée à {_fmt(self.result_locality, self.result_postcode)}, "
                f"mais vous avez saisi {_fmt(self.input_city, self.input_postcode)} — "
                f"vérifiez le point sur la carte.")


def _norm_place(s: str | None) -> str:
    """Normalise un nom de commune pour comparaison : sans accents, minuscule,
    ponctuation ET tirets → espaces, espaces compactés. « Schouwen-Duiveland » →
    « schouwen duiveland », « Málaga » → « malaga »."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def _norm_postcode(s: str | None) -> str:
    """Code postal normalisé : alphanumérique majuscule, sans espaces ni tirets."""
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _place_matches(a: str | None, b: str | None) -> bool:
    """Deux noms de commune désignent-ils le même lieu ? Égalité normalisée OU
    sous-ensemble de tokens (un nom de village peut être un mot d'un libellé composé),
    jamais un simple chevauchement partiel (« Murcia » ≠ « Torre-Pacheco »)."""
    na, nb = _norm_place(a), _norm_place(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta


def check_geocode_consistency(input_city: str | None, input_postcode: str | None,
                              result_address: dict | None) -> Mismatch | None:
    """Compare la commune (et le code postal) saisies au résultat Nominatim (bloc
    `address`, `addressdetails=1`). Retourne un `Mismatch` en cas d'écart, sinon None.
    PUR (aucune E/S).

    Règle (conservatrice — priorité à ZÉRO faux positif sur les fiches correctes) : le
    déclencheur est la COMMUNE. Un écart n'est retenu que si le résultat FOURNIT une
    commune qui CONTREDIT la saisie ; l'ABSENCE de détail d'adresse n'est JAMAIS un
    écart (un résultat sans `address` ne prouve rien — cas des géocodages bruts / mocks).
    Le code postal ne déclenche seul QUE lorsqu'aucune commune n'est saisie (sinon un CP
    légèrement faux sur une grande ville à plusieurs codes postaux ferait un faux
    positif) ; il enrichit toujours le message."""
    result_address = result_address or {}
    city_in = (input_city or "").strip()
    levels = [v for v in (result_address.get(k) for k in _MUNICIPAL_KEYS) if v]
    result_locality = levels[0] if levels else None
    result_pc = result_address.get("postcode")
    pc_in, pc_res = _norm_postcode(input_postcode), _norm_postcode(result_pc)

    # La commune saisie contredit-elle le résultat ? Seulement si le résultat FOURNIT
    # au moins un niveau municipal et qu'AUCUN ne correspond à la saisie.
    city_conflict = bool(city_in) and bool(levels) and not any(
        _place_matches(city_in, v) for v in levels)
    pc_conflict = bool(pc_in and pc_res and pc_in != pc_res)

    if city_conflict or (not city_in and pc_conflict):
        return Mismatch(input_city=city_in or None,
                        input_postcode=(input_postcode or "").strip() or None,
                        result_locality=result_locality, result_postcode=result_pc)
    return None


def _strip_house_number(street: str) -> str:
    """'Calle San Ignacio 23' -> 'Calle San Ignacio' ; '23 Rue X' -> 'Rue X'."""
    s = re.sub(r"[,\s]+\d+[a-zA-Z]?\s*$", "", street)
    s = re.sub(r"^\s*\d+[a-zA-Z]?[,\s]+", "", s)
    return s.strip() or street


# ── V2-79b : file d'attente Nominatim partagée + reprise sur 429 + cache par job ──
#
# Constat (Jávea, job 98cdff38) : 11 picks géocodés à la suite, chacun avec son échelle de
# repli, dont `q=<commune>` rejoué 11 fois → 429 Too Many Requests → `raise_for_status`
# non rattrapé → job PAYÉ mort. Politique d'usage OSM : 1 req/s, une seule file par IP.
_NOMINATIM_LOCK = threading.Lock()
_state = {"last": 0.0, "blocked_until": 0.0}
_sleep = time.sleep            # injectables (tests : aucun vrai sommeil)
_now = time.monotonic
_RETRYABLE = frozenset({429, 503})
# Cache des requêtes IDENTIQUES le temps d'un job (`request_cache()`) : la même question
# reçoit la même réponse — le centroïde d'une commune ne bouge pas pendant un run.
_JOB_CACHE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "nominatim_job_cache", default=None)


@contextlib.contextmanager
def request_cache():
    """Active, pour la durée du bloc (un job du pipeline), le cache des requêtes
    Nominatim identiques. Imbriquable : un bloc interne réutilise le cache externe."""
    if _JOB_CACHE.get() is not None:
        yield
        return
    token = _JOB_CACHE.set({})
    try:
        yield
    finally:
        _JOB_CACHE.reset(token)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _nominatim_get(url: str, params: dict, client: httpx.Client):
    """GET Nominatim POLI : file partagée par le processus (un appel à la fois, espacés
    de `nominatim_min_interval_s`), reprise sur 429/503 (Retry-After, sinon backoff
    exponentiel plafonné — et TOUT le processus attend, l'IP est bannie pour tous), cache
    par job. Lève `GeocodeRateLimited` si le service refuse encore après les essais."""
    key = (url, tuple(sorted((k, str(v)) for k, v in params.items())))
    cache = _JOB_CACHE.get()
    if cache is not None and key in cache:
        return cache[key]
    attempts = max(1, settings.nominatim_max_attempts)
    for attempt in range(1, attempts + 1):
        with _NOMINATIM_LOCK:
            wait = max(settings.nominatim_min_interval_s - (_now() - _state["last"]),
                       _state["blocked_until"] - _now())
            if wait > 0:
                _sleep(wait)
            try:
                resp = client.get(url, params=params,
                                  headers={"User-Agent": settings.user_agent})
            finally:
                _state["last"] = _now()
            if resp.status_code in _RETRYABLE:
                delay = _retry_after(resp)
                if delay is None:
                    delay = settings.nominatim_backoff_s * 2 ** (attempt - 1)
                delay = min(delay, settings.nominatim_backoff_max_s)
                _state["blocked_until"] = _now() + delay
        if resp.status_code in _RETRYABLE:
            if attempt == attempts:
                raise GeocodeRateLimited(
                    f"Nominatim {resp.status_code} après {attempts} essai(s)")
            log.warning("Nominatim %s — attente %.0f s puis reprise (essai %d/%d)",
                        resp.status_code, delay, attempt + 1, attempts)
            continue
        resp.raise_for_status()
        data = resp.json()
        if cache is not None:
            cache[key] = data
        return data
    raise GeocodeRateLimited("Nominatim indisponible")   # inatteignable


def _search(params: dict, country_code: str, client: httpx.Client) -> dict | None:
    # addressdetails=1 : Nominatim renvoie la ventilation d'adresse (ville/commune) —
    # sert à remplir `locality` (V2-38, servie sur la carte du guide). Sans coût
    # supplémentaire pour les appels existants (le champ est simplement présent).
    results = _nominatim_get(
        settings.nominatim_url,
        {**params, "countrycodes": country_code.lower(),
         "format": "jsonv2", "limit": 1, "addressdetails": 1},
        client)
    return results[0] if results else None


# ── V2-79b : découpage d'une adresse libre en composants structurés ───────────
#
# La recherche structurée de Nominatim attend `street` = numéro + rue SEULEMENT. Les picks
# éditoriaux portent l'adresse ENTIÈRE (« Carretera de Benitachell, 100, 03730 Jávea,
# Alicante ») : passée telle quelle dans `street`, zéro résultat → repli centroïde → rejet.
_POSTCODE_RE = re.compile(r"\b(\d{4,5}(?:-\d{3,4})?)\b")
_NUMBER_RE = re.compile(r"^(?:n[º°o]\.?\s*)?\d+[a-zA-Z]?(?:\s*[-/]\s*\d+[a-zA-Z]?)?$|^s/?n$",
                        re.IGNORECASE)
# Segments qui ne sont PAS une rue : lieux-dits, lotissements, mentions de zone.
_NOISE_RE = re.compile(
    r"^(urbanizaci[oó]n|urb\.?|urbanitzaci[oó]|partida|pda\.?|edificio|edif\.?|"
    r"residencial|local|bajo|planta|piso|puerta|lieu-dit|zone|zona|pol[ií]gono)\b",
    re.IGNORECASE)


def split_address(address: str | None, city: str | None = None) -> dict:
    """Découpe une adresse libre en `{"street", "postalcode", "city"}` (PUR).

    - mentions entre parenthèses retirées (« (Platja de l'Arenal) ») ;
    - segment de zone SANS numéro (« urbanización … ») ignoré ;
    - le CODE POSTAL va dans `postalcode`, la ville qui l'accompagne dans `city` ;
    - un numéro isolé (« 100 », « s/n ») est rattaché à la rue qui le précède ;
    - tout ce qui suit la ville (province, pays) est ignoré.
    `city` (celle du logement) sert de repli quand l'adresse n'en porte pas."""
    text = re.sub(r"\([^)]*\)", " ", address or "")
    segs = [re.sub(r"\s+", " ", x).strip(" .") for x in text.split(",")]
    # Zone SANS numéro (« Urbanización El Tosalet ») = pas une rue ; « Partida Pla 22 »,
    # forme d'adresse rurale valencienne, porte un numéro → gardée.
    segs = [x for x in segs if x and not (_NOISE_RE.match(x) and not re.search(r"\d", x))]
    street, postal, town = None, None, None
    city_n = _norm_place(city) if city else ""
    for i, seg in enumerate(segs):
        m = _POSTCODE_RE.search(seg)
        if m and not _NUMBER_RE.match(seg):
            postal = m.group(1)
            rest = (seg[:m.start()] + seg[m.end():]).strip(" -")
            town = rest or (segs[i + 1] if i + 1 < len(segs) else None)
            break
        if city_n and _norm_place(seg) == city_n:
            town = seg
            break
        if street is None:
            street = seg
        elif _NUMBER_RE.match(seg):
            street = f"{street} {seg}" if not seg.lower().startswith("s") else street
        else:
            # Deuxième segment non numérique avant le CP/la ville : quartier, lieu-dit…
            # On garde la rue, on ignore le reste.
            continue
    if street:
        street = re.sub(r"\s+s/?n$", "", street, flags=re.IGNORECASE).strip() or None
    if street and city_n and _norm_place(street) == city_n:
        street = None
    return {"street": street or None, "postalcode": postal, "city": town or city}


def _osm_class(r: dict) -> str:
    """Classe OSM d'un résultat. V2-79 : en `format=jsonv2` (notre format depuis le
    01/09), Nominatim la renvoie sous la clé **`category`** — `class` n'existe qu'en
    `format=json`. Lire `class` seul rendait TOUJOURS « » : la table `_ACCURACY` ne
    voyait jamais la classe, et la garde anti-centroïde des activités était morte."""
    return r.get("category") or r.get("class") or ""


# Rangs Nominatim : 30 = bâtiment/POI (le restaurant lui-même), 26-27 = rue. En dessous,
# quartier/commune/région — le « centroïde » qu'on refuse.
_RANK_ROOFTOP, _RANK_STREET = 30, 26


def _accuracy_of(r: dict) -> str:
    """Précision d'un résultat Nominatim. Le TYPE d'abord (table historique), puis la
    CLASSE (`highway` = une rue, `building` = un bâtiment), puis le RANG (`place_rank`).
    V2-79 — constat réel (Altea, 05/10) : « Calle Mayor 5, Altea » renvoie le restaurant
    Oustau (`amenity/restaurant`, rang 30), « Carrer La Mar 127 » une route
    (`highway/secondary`, rang 26) ; aucun de ces types n'était dans la table → « city »
    → chaque pick éditorial rejeté comme centroïde, 15 sur 15. Un type inconnu ne vaut
    « city » que si son rang est celui d'une zone, jamais d'une adresse."""
    acc = _ACCURACY.get(r.get("type", "")) or _ACCURACY.get(_osm_class(r))
    if acc:
        return acc
    cls = _osm_class(r)
    if cls == "highway":
        return "street"
    if cls in ("place", "boundary"):
        return "city"
    try:
        rank = int(r.get("place_rank"))
    except (TypeError, ValueError):
        return "city"
    if rank >= _RANK_ROOFTOP:
        return "rooftop"
    if rank >= _RANK_STREET:
        return "street"
    return "city"


def _locality_of(r: dict) -> str | None:
    """Commune/localité d'un résultat Nominatim (addressdetails) — même ordre de
    préférence que le proxy de recherche de POI (`poi_search._candidate`)."""
    addr = r.get("address") or {}
    return (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or None)


def geocode(address: str | None = None, country_code: str = "ES",
            client: httpx.Client | None = None, *,
            street: str | None = None, postalcode: str | None = None,
            city: str | None = None, area_fallback: bool = True) -> dict:
    """Retourne {"lat", "lon", "accuracy", "display_name", "locality", "source",
    "mismatch"}.

    Passer de préférence les composants (street/postalcode/city) pour activer
    l'échelle de repli ; `address` libre reste accepté (rétro-compatibilité).

    V2-46 : la commune (et le code postal) du résultat sont comparés à la saisie. En
    cas d'écart (rue homonyme résolue dans une autre commune), `accuracy` vaut
    **`'mismatch'`** (jamais « rooftop »/« street ») et `mismatch` porte les détails
    de l'alerte propriétaire.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        attempts: list[tuple[dict, str | None]] = []
        if street and city:
            full = {"street": street, "city": city}
            if postalcode:
                full["postalcode"] = postalcode
            attempts.append((full, None))                       # 1. précis
            no_num = _strip_house_number(street)
            if no_num != street:
                attempts.append(({"street": no_num, "city": city}, "street"))  # 2.
        # 3-4. Replis de ZONE (centroïde). `area_fallback=False` (V2-79b) : l'appelant
        # refuse de toute façon un centroïde (pick éditorial) — inutile de le demander.
        if area_fallback and postalcode and city:
            attempts.append(({"q": f"{postalcode} {city}"}, "city"))           # 3.
        if area_fallback and city:
            attempts.append(({"q": city}, "city"))                             # 4.
        if address:
            attempts.insert(0, ({"q": address}, None))          # requête libre d'abord

        for params, forced_accuracy in attempts:
            r = _search(params, country_code, client)
            if not r:
                continue
            accuracy = forced_accuracy or _accuracy_of(r)
            # V2-46 : contrôle de cohérence commune/CP. Un écart (rue homonyme dans une
            # autre commune) force `accuracy='mismatch'` — jamais « précis ».
            mismatch = check_geocode_consistency(city, postalcode, r.get("address"))
            if mismatch is not None:
                accuracy = "mismatch"
            return {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "accuracy": accuracy,
                "display_name": r.get("display_name", ""),
                "locality": _locality_of(r),   # V2-38 : commune, servie sur la carte
                "source": "nominatim",
                "mismatch": mismatch,          # Mismatch | None (V2-46)
                # Classe/type OSM bruts (V2-73) : permettent à l'appelant de distinguer
                # un LIEU précis (plage, massif, leisure…) d'un CENTROÏDE administratif
                # (boundary / place=city|town|village) que `accuracy` ne sépare pas —
                # un type non cartographié retombe sur « city » alors que sa position,
                # elle, est spécifique. Le placement strict des activités s'en sert.
                "osm_class": _osm_class(r),
                "osm_type": r.get("type", ""),
            }

        tried = address or f"{street}, {postalcode}, {city}"
        raise GeocodeError(f"Adresse introuvable (toutes stratégies) : {tried!r}")
    finally:
        if own_client:
            client.close()


def geocode_place(address: str | None, city: str | None, country_code: str,
                  client: httpx.Client | None = None) -> dict:
    """Géocode l'adresse LIBRE d'un lieu (pick éditorial, marché, commerce web) — V2-79b.

    Découpe d'abord l'adresse (`split_address` : rue+numéro / code postal / ville, sans
    province ni mention entre parenthèses) puis interroge la recherche STRUCTURÉE, SANS
    repli de zone : ces appelants refusent un centroïde de toute façon, le demander ne
    ferait que charger Nominatim (le `q=<commune>` rejoué 11 fois de Jávea). Lève
    GeocodeError si la rue ne se résout pas."""
    parts = split_address(address, city)
    if not parts["street"]:
        raise GeocodeError(f"Adresse sans rue exploitable : {address!r}")
    return geocode(street=parts["street"], postalcode=parts["postalcode"],
                   city=parts["city"], country_code=country_code, client=client,
                   area_fallback=False)


# ── V2-68c : repère de départ quand l'adresse est introuvable ─────────────────
#
# Un géocodage infructueux doit OUVRIR le placement manuel, jamais fermer le parcours
# (constat Kosovo, « Rrugë Skënderbeu 307, Xërxë, XK » : le tunnel disait « adresse
# introuvable » et s'arrêtait, alors que la carte d'ajustement existe et que le client,
# lui, connaît sa position). On rend donc le MEILLEUR repère disponible pour centrer la
# carte — du plus fin au plus large — le code pays étant toujours fourni par la saisie.
#
# Ce n'est JAMAIS une position de guide : `coarse_locate` ne sert qu'à cadrer la carte
# de placement. L'ancrage réel reste le point posé à la main (accuracy 'manual'), et la
# garde de précision (`is_precise_enough`) n'est pas touchée.
_COARSE_LEVELS = ("city", "postal", "country")


def coarse_locate(*, city: str | None = None, postalcode: str | None = None,
                  country_code: str = "ES",
                  client: httpx.Client | None = None) -> dict | None:
    """Meilleur REPÈRE de départ pour une adresse introuvable (V2-68c).

    Essaie, du plus fin au plus large : commune → code postal → pays. Retourne
    `{"lat", "lon", "level"}` (`level` ∈ `city` | `postal` | `country`) ou `None`
    si même le pays est introuvable. Un échec d'un barreau (réseau, HTTP, pays
    inconnu de Nominatim) n'interrompt jamais la descente : on essaie le suivant.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        attempts: list[tuple[dict, str]] = []
        if city:
            attempts.append(({"q": city}, "city"))
        if postalcode:
            attempts.append(({"q": postalcode}, "postal"))
        # Le pays est TOUJOURS saisi dans le tunnel → dernier repère garanti.
        attempts.append(({"country": country_code}, "country"))

        for params, level in attempts:
            try:
                r = _search(params, country_code, client)
            except Exception:  # noqa: BLE001 — un barreau qui casse n'arrête pas l'échelle
                log.info("Repère '%s' non résolu pour %s/%s.", level, city, country_code,
                         exc_info=True)
                continue
            if r:
                return {"lat": float(r["lat"]), "lon": float(r["lon"]), "level": level}
        return None
    finally:
        if own_client:
            client.close()


def reverse(lat: float, lon: float, country_code: str | None = None,
            client: httpx.Client | None = None) -> dict | None:
    """Géocodage INVERSE : commune/CP réels d'un point stocké (V2-46, audit rétroactif).
    Retourne le bloc `address` de Nominatim (`{city|town|village…, postcode, …}`) ou
    None. Sert à confronter la position enregistrée d'un logement à sa saisie — sans
    re-lancer le forward géocodage (qui reproduirait le même défaut d'homonymie)."""
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        url = settings.nominatim_url.replace("/search", "/reverse")
        data = _nominatim_get(url, {"lat": lat, "lon": lon, "format": "jsonv2",
                                    "addressdetails": 1, "zoom": 18}, client)
        return data.get("address") if isinstance(data, dict) else None
    finally:
        if own_client:
            client.close()
