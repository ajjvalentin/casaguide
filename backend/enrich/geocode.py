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

import re
import unicodedata
from dataclasses import dataclass

import httpx

from .settings import settings

_ACCURACY = {
    "building": "rooftop", "house": "rooftop", "residential": "rooftop",
    "road": "street", "street": "street",
    # Types de LIEU précis (V2-07 volet 3) : une place/un marché géocodé à ce niveau
    # est exploitable pour un marqueur (≠ « city », qui est le repli imprécis).
    "square": "street", "marketplace": "street", "pedestrian": "street",
}


class GeocodeError(Exception):
    pass


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


def _search(params: dict, country_code: str, client: httpx.Client) -> dict | None:
    # addressdetails=1 : Nominatim renvoie la ventilation d'adresse (ville/commune) —
    # sert à remplir `locality` (V2-38, servie sur la carte du guide). Sans coût
    # supplémentaire pour les appels existants (le champ est simplement présent).
    resp = client.get(
        settings.nominatim_url,
        params={**params, "countrycodes": country_code.lower(),
                "format": "jsonv2", "limit": 1, "addressdetails": 1},
        headers={"User-Agent": settings.user_agent},
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def _locality_of(r: dict) -> str | None:
    """Commune/localité d'un résultat Nominatim (addressdetails) — même ordre de
    préférence que le proxy de recherche de POI (`poi_search._candidate`)."""
    addr = r.get("address") or {}
    return (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or None)


def geocode(address: str | None = None, country_code: str = "ES",
            client: httpx.Client | None = None, *,
            street: str | None = None, postalcode: str | None = None,
            city: str | None = None) -> dict:
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
        if postalcode and city:
            attempts.append(({"q": f"{postalcode} {city}"}, "city"))           # 3.
        if city:
            attempts.append(({"q": city}, "city"))                             # 4.
        if address:
            attempts.insert(0, ({"q": address}, None))          # requête libre d'abord

        for params, forced_accuracy in attempts:
            r = _search(params, country_code, client)
            if not r:
                continue
            accuracy = forced_accuracy or _ACCURACY.get(
                r.get("type", ""), _ACCURACY.get(r.get("class", ""), "city"))
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
            }

        tried = address or f"{street}, {postalcode}, {city}"
        raise GeocodeError(f"Adresse introuvable (toutes stratégies) : {tried!r}")
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
        resp = client.get(
            url, params={"lat": lat, "lon": lon, "format": "jsonv2",
                         "addressdetails": 1, "zoom": 18},
            headers={"User-Agent": settings.user_agent})
        resp.raise_for_status()
        data = resp.json()
        return data.get("address") if isinstance(data, dict) else None
    finally:
        if own_client:
            client.close()
