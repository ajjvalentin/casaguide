"""Tests unitaires des garde-fous qualité Overpass (M-01).

Sans base ni réseau réel : les filtres de cohérence sont des fonctions pures,
et le regroupement des requêtes est vérifié via un transport httpx simulé qui
compte les appels.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from enrich import overpass  # noqa: E402
from enrich.settings import settings  # noqa: E402

LAT, LON = 37.9280, -0.7482


# ── 1a. Filtre aéroports : publics/IATA seulement ────────────────────────────

def test_airport_keeps_public_excludes_military_and_aeroclub():
    # V2-44 : proxy « civil commercial » = présence d'un code IATA ; toute marque
    # militaire exclut d'emblée. Aéroport avec IATA -> gardé.
    assert overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                 "iata": "ALC", "name": "Alicante"})
    # Aérodrome régional SANS IATA -> exclu (aéroclub, altiport : pas de trafic
    # commercial de vacances).
    assert not overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                     "aerodrome:type": "regional"})
    # Base militaire (San Javier) -> exclue même AVEC un IATA d'usage mixte.
    assert not overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                     "military": "airfield",
                                                     "iata": "XXX",
                                                     "name": "Base Aérea de San Javier"})
    # aerodrome:type=military -> exclu (Woensdrecht, Gilze-Rijen du benchmark)
    assert not overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                     "aerodrome:type": "military",
                                                     "iata": "XXX",
                                                     "name": "Vliegbasis Woensdrecht"})
    # Aéroclub (Mar Menor) -> exclu (pas d'IATA)
    assert not overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                     "aerodrome:type": "airfield",
                                                     "name": "Aeroclub Mar Menor"})


# ── 1b. Cohérence catégorie / tags ───────────────────────────────────────────

def test_market_rejects_estate_agent_and_minimarket():
    # Vrai marché hebdomadaire -> gardé
    assert overpass.category_matches("market", {"amenity": "marketplace",
                                                "name": "Mercadillo"})
    # Agence immobilière taggée marketplace -> rejetée
    assert not overpass.category_matches("market", {"amenity": "marketplace",
                                                    "shop": "estate_agent"})
    # Minimarket (shop) taggé marketplace -> rejeté
    assert not overpass.category_matches("market", {"amenity": "marketplace",
                                                    "shop": "convenience"})
    # Bureau taggé marketplace -> rejeté
    assert not overpass.category_matches("market", {"amenity": "marketplace",
                                                    "office": "company"})


def test_veterinary_not_returned_as_doctor():
    # Un vétérinaire ne doit pas passer pour un médecin/dentiste
    assert not overpass.category_matches("doctor", {"amenity": "veterinary",
                                                    "name": "Clínica Veterinaria"})
    # …et reste valide dans sa propre catégorie
    assert overpass.category_matches("veterinary", {"amenity": "veterinary",
                                                    "name": "Clínica Veterinaria"})
    # Un vrai médecin reste accepté
    assert overpass.category_matches("doctor", {"amenity": "doctors",
                                                "name": "Centro de Salud"})


# ── 1c. Dédoublonnage santé doctor / veterinary ──────────────────────────────

def test_dedup_health_removes_shared_establishment():
    results = {
        "doctor": [
            {"source_ref": "node/1", "name": "Clínica Mar", "crow_m": 100},
            {"source_ref": "node/2", "name": "Dr. Pérez", "crow_m": 200},
        ],
        "veterinary": [
            {"source_ref": "node/1", "name": "Clínica Mar", "crow_m": 100},
        ],
    }
    overpass._dedup_health_categories(results)
    doctor_names = {p["name"] for p in results["doctor"]}
    assert doctor_names == {"Dr. Pérez"}          # l'établissement partagé retiré de doctor
    assert len(results["veterinary"]) == 1        # conservé côté vétérinaire


# ── 5. Regroupement des requêtes Overpass par palier de rayon ────────────────

def _grouping_handler(calls: list[str]):
    """Transport simulé : compte les requêtes et renvoie l'union par sélecteur."""
    market = {"type": "node", "id": 1, "lat": 37.9285, "lon": -0.7485,
              "tags": {"name": "Mercadona", "shop": "supermarket"}}
    market_office = {"type": "node", "id": 2, "lat": 37.9286, "lon": -0.7486,
                     "tags": {"name": "Bureau Immo", "shop": "supermarket",
                              "office": "estate_agent"}}  # incohérent -> exclu
    resto = {"type": "node", "id": 3, "lat": 37.9287, "lon": -0.7487,
             "tags": {"name": "La Marejada", "amenity": "restaurant"}}
    bar = {"type": "node", "id": 4, "lat": 37.9288, "lon": -0.7488,
           "tags": {"name": "Bar Central", "amenity": "bar"}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = urllib.parse.unquote_plus(request.read().decode())
        els = []
        if '"shop"="supermarket"' in body:
            els += [market, market_office]
        if '"amenity"="restaurant"' in body:
            els += [resto]
        if '"amenity"="bar"' in body or '"amenity"="pub"' in body:
            els += [bar]
        return httpx.Response(200, json={"elements": els})
    return handler


def test_fetch_grouped_single_request_per_radius_bucket():
    settings.politeness_delay_s = 0
    calls: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(_grouping_handler(calls)))
    # Trois catégories de même palier de rayon (3000 -> palier 5000)
    cats = [{"code": "supermarket", "default_radius_m": 3000},
            {"code": "restaurant", "default_radius_m": 3000},
            {"code": "bar", "default_radius_m": 3000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()

    # Une seule requête Overpass pour les trois catégories : les catégories du test ne
    # fournissent PAS de `max_radius_m` → max = préférence, aucune escalade (V2-44).
    assert len(calls) == 1
    assert failures == {}
    assert stats["generic_dropped"] == 0
    # Re-ventilation correcte par tags, POI incohérent (office) exclu
    assert {p["name"] for p in results["supermarket"]} == {"Mercadona"}
    assert {p["name"] for p in results["restaurant"]} == {"La Marejada"}
    assert {p["name"] for p in results["bar"]} == {"Bar Central"}


def test_fetch_grouped_reduces_request_count_on_full_catalogue():
    """Sur les 26 catégories interrogeables, le regroupement par palier tient
    la promesse « moins de 10 requêtes »."""
    settings.politeness_delay_s = 0
    calls: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(_grouping_handler(calls)))
    cats = [{"code": c, "default_radius_m": r} for c, r in {
        "parking": 1000, "supermarket": 3000, "market": 8000, "bakery": 2000,
        "atm": 2000, "post_office": 5000, "mall": 15000, "laundry": 5000,
        "hospital": 25000, "pharmacy": 3000, "doctor": 5000, "police": 10000,
        "veterinary": 10000, "taxi": 10000, "rental": 10000, "restaurant": 3000,
        "bar": 3000, "cafe": 2000, "beach": 10000, "sight": 20000,
        "family_activity": 15000, "sport": 10000, "bus_stop": 1000,
        "bus_station": 20000, "train_station": 15000, "airport": 100000,
    }.items()]
    overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert len(calls) < 10          # objectif M-01 (constaté : 5 paliers)


# ── M-21 : gare routière (bus_station) ───────────────────────────────────────

def test_bus_station_selector_and_bucket():
    """bus_station est interrogeable (amenity=bus_station) et son rayon 20 km
    tombe dans un palier NORMAL (25 km < 50 km), pas dans le palier lointain
    aéroport → timeout standard, aucune requête Overpass supplémentaire."""
    # Sélecteur OSM dérivé de CATEGORY_TAGS
    assert overpass.CATEGORY_TAGS["bus_station"] == [("amenity", "bus_station")]
    assert overpass.category_matches("bus_station",
                                     {"amenity": "bus_station", "name": "Estación de autobuses"})
    assert not overpass.category_matches("bus_station", {"highway": "bus_stop"})
    # Palier de rayon : 20 km → palier 25 km (< overpass_far_bucket_m = 50 km)
    bucket = overpass._bucket_radius(20000)
    assert bucket == 25000
    assert bucket < settings.overpass_far_bucket_m
    # Donc timeout standard, pas le timeout « far » de l'aéroport
    assert overpass._bucket_timeout(bucket) == settings.overpass_timeout_s


# ── M-16 : récolte et normalisation du tag OSM « cuisine » ───────────────────

def test_norm_cuisine_first_term_lowercased():
    assert overpass._norm_cuisine("italian") == "italian"
    assert overpass._norm_cuisine("Italian") == "italian"
    # Multi-valué -> premier terme seulement
    assert overpass._norm_cuisine("italian;pizza") == "italian"
    assert overpass._norm_cuisine("  Seafood ; Spanish ") == "seafood"
    # Vide / absent -> None
    assert overpass._norm_cuisine(None) is None
    assert overpass._norm_cuisine("") is None
    assert overpass._norm_cuisine("  ;  ") is None


def test_element_to_poi_carries_cuisine():
    el = {"type": "node", "id": 42, "lat": LAT, "lon": LON,
          "tags": {"name": "Trattoria", "amenity": "restaurant",
                   "cuisine": "Italian;pizza"}}
    poi = overpass._element_to_poi(el, LAT, LON)
    assert poi["cuisine"] == "italian"      # normalisé
    # Un POI sans tag cuisine porte cuisine=None (jamais de KeyError)
    el2 = {"type": "node", "id": 43, "lat": LAT, "lon": LON,
           "tags": {"name": "Bar Central", "amenity": "bar"}}
    assert overpass._element_to_poi(el2, LAT, LON)["cuisine"] is None


# ── V2-44 : rayons adaptatifs (ruralité) ─────────────────────────────────────

def _sm(name: str, dlat: float) -> dict:
    """Supermarché à `dlat` degrés de latitude au nord du logement (id stable)."""
    return {"type": "node", "id": 900000 + int(dlat * 1e5), "lat": LAT + dlat,
            "lon": LON, "tags": {"name": name, "shop": "supermarket"}}


def _one_selector_handler(elements: list[dict], selector: str, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = urllib.parse.unquote_plus(request.read().decode())
        return httpx.Response(200, json={"elements": elements if selector in body else []})
    return handler


def test_adaptive_radius_fills_min_results_in_rural_zone():
    """Rien dans le rayon de PRÉFÉRENCE (3 km), 5 lieux dans le rayon MAX (25 km) →
    MIN_RESULTS (3) retenus, les plus proches, avec leurs VRAIES distances."""
    settings.politeness_delay_s = 0
    els = [_sm("S8", 0.0719), _sm("S10", 0.0898), _sm("S12", 0.1078),
           _sm("S15", 0.1347), _sm("S20", 0.1797)]     # ~8,10,12,15,20 km (tous > 3 km)
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler(els, '"shop"="supermarket"', calls)))
    cats = [{"code": "supermarket", "default_radius_m": 3000, "max_radius_m": 25000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    got = results["supermarket"]
    assert [p["name"] for p in got] == ["S8", "S10", "S12"]   # les 3 plus proches
    assert all(p["crow_m"] > 3000 for p in got)               # aucun dans la préférence
    assert got[0]["crow_m"] < got[1]["crow_m"] < got[2]["crow_m"]
    assert len(calls) == 2 and failures == {}    # préférence (vide) PUIS escalade


def test_adaptive_radius_byte_identical_in_dense_zone():
    """5 lieux DANS le rayon de préférence : la préférence est pleine → ZÉRO escalade,
    sortie inchangée par rapport à l'historique (une seule requête)."""
    settings.politeness_delay_s = 0
    els = [_sm("D05", 0.0045), _sm("D10", 0.0090), _sm("D15", 0.0135),
           _sm("D20", 0.0180), _sm("D25", 0.0225)]      # ~0,5 à 2,5 km (tous < 3 km)
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler(els, '"shop"="supermarket"', calls)))
    cats = [{"code": "supermarket", "default_radius_m": 3000, "max_radius_m": 25000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert [p["name"] for p in results["supermarket"]] == \
        ["D05", "D10", "D15", "D20", "D25"]
    assert len(calls) == 1                        # préférence pleine → pas de 2e passe


def test_no_max_radius_means_no_escalation():
    """`max_radius_m` NULL (= default, ex. parking/aéroport) → jamais de 2e passe même
    sous le minimum."""
    settings.politeness_delay_s = 0
    els = [_sm("Loin", 0.05)]                      # 1 seul lieu, > préférence
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler(els, '"shop"="supermarket"', calls)))
    # max_radius_m absent → pas d'escalade ; le lieu hors préférence n'est pas retenu.
    cats = [{"code": "supermarket", "default_radius_m": 3000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert results["supermarket"] == [] and len(calls) == 1


# ── V2-44 : filtre des noms génériques ───────────────────────────────────────

def test_is_generic_name_multilingual_and_keeps_proper_names():
    for n in ["Speeltuin", "SPEELWEIDE", "Speeltuintje", "Trampoline", "Ballenbad",
              "Aire de jeux", "aire de jeu", "Parque infantil", "Área de juegos",
              "Spielplatz", "Playground", "Parco giochi",
              # laveries génériques (V2-44 volet 2)
              "Wasserette", "Was", "Laundrette", "Laverie", "Lavandería"]:
        assert overpass.is_generic_name(n), n
    # Un nom PROPRE qui contient un mot générique est CONSERVÉ.
    for n in ["Trampoline Park Zeeland", "Speeltuin De Boomhut",
              "Parque Warner Madrid", "Café De Speeltuin"]:
        assert not overpass.is_generic_name(n), n
    assert not overpass.is_generic_name(None)
    assert not overpass.is_generic_name("")


def test_fetch_grouped_drops_generic_named_and_counts():
    settings.politeness_delay_s = 0
    els = [
        {"type": "node", "id": 1, "lat": LAT + 0.001, "lon": LON,
         "tags": {"name": "Speeltuin", "leisure": "playground"}},          # générique
        {"type": "node", "id": 2, "lat": LAT + 0.0011, "lon": LON,
         "tags": {"name": "Trampoline Park Zeeland", "leisure": "playground"}},  # propre
    ]
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler(els, '"leisure"="playground"', calls)))
    cats = [{"code": "family_activity", "default_radius_m": 15000,
             "max_radius_m": 15000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert [p["name"] for p in results["family_activity"]] == ["Trampoline Park Zeeland"]
    assert stats["generic_dropped"] == 1


# ── V2-44 : pertinence gare (tram-musée exclu) ───────────────────────────────

def test_train_station_excludes_heritage_tram_museum():
    # Vraie gare -> gardée
    assert overpass.category_matches("train_station",
                                     {"railway": "station", "name": "Goes"})
    # Tram-musée / ligne préservée -> exclu (cas « Middelplaat Haven (RTM) » du benchmark)
    assert not overpass.category_matches("train_station",
                                         {"railway": "station", "usage": "tourism",
                                          "name": "Middelplaat Haven (RTM)"})
    assert not overpass.category_matches("train_station",
                                         {"railway": "station", "tourism": "attraction",
                                          "name": "Museumtram"})
    assert not overpass.category_matches("train_station",
                                         {"railway": "station",
                                          "railway:preserved": "yes",
                                          "name": "Stoomtrein"})


# ── V2-44 : catégories sans résultat signalées ───────────────────────────────

def test_fetch_grouped_reports_empty_categories():
    settings.politeness_delay_s = 0

    def handler(request: httpx.Request) -> httpx.Response:
        body = urllib.parse.unquote_plus(request.read().decode())
        els = ([{"type": "node", "id": 1, "lat": LAT + 0.001, "lon": LON,
                 "tags": {"name": "Jumbo", "shop": "supermarket"}}]
               if '"shop"="supermarket"' in body else [])
        return httpx.Response(200, json={"elements": els})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cats = [{"code": "supermarket", "default_radius_m": 3000, "max_radius_m": 3000},
            {"code": "laundry", "default_radius_m": 5000, "max_radius_m": 5000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert results["supermarket"] and not results["laundry"]
    assert stats["empty"] == ["laundry"] and failures == {}


# ── V2-44 volet 3 : minimums par catégorie + plafond de pertinence ───────────

def test_target_for_known_values_and_fallback():
    assert overpass.target_for("police") == overpass.CategoryTarget(1, 20)
    assert overpass.target_for("restaurant") == overpass.CategoryTarget(5, 20)
    assert overpass.target_for("hospital") == overpass.CategoryTarget(1, 45)
    assert overpass.target_for("beach").hard_cap_drive_min is None      # G : pas de plafond
    # Catégorie ABSENTE de la table → repli comportement volet 1 (min global, aucun cap).
    fb = overpass.target_for("bus_station")
    assert fb == overpass.CategoryTarget(settings.min_results_per_category, None)
    assert overpass.target_for("categorie_inexistante") == \
        overpass.CategoryTarget(settings.min_results_per_category, None)


def test_apply_drive_cap_drops_far_escalation_keeps_near():
    pref = 10000
    within = {"name": "Proche", "crow_m": 4000, "drive_min": 6}     # dans la préférence
    far_ok = {"name": "Escalade OK", "crow_m": 15000, "drive_min": 18}  # au-delà, sous le cap
    far_no = {"name": "Escalade loin", "crow_m": 22000, "drive_min": 31}  # au-delà, hors cap
    kept, dropped = overpass.apply_drive_cap([within, far_ok, far_no], pref, 20)
    assert [p["name"] for p in kept] == ["Proche", "Escalade OK"]
    assert dropped == 1


def test_apply_drive_cap_keeps_within_preferred_even_if_slow():
    # Un lieu DANS le rayon de préférence est toujours gardé (réellement proche),
    # même si sa route dépasse le plafond — le plafond ne vise QUE l'escalade.
    pref = 10000
    slow_near = {"name": "Proche lent", "crow_m": 8000, "drive_min": 25}
    kept, dropped = overpass.apply_drive_cap([slow_near], pref, 20)
    assert kept == [slow_near] and dropped == 0


def test_apply_drive_cap_no_cap_or_unknown_drive_unchanged():
    pois = [{"name": "X", "crow_m": 30000, "drive_min": 40}]
    assert overpass.apply_drive_cap(pois, 10000, None) == (pois, 0)     # pas de plafond
    nodrive = [{"name": "Y", "crow_m": 30000}]                          # drive inconnu
    assert overpass.apply_drive_cap(nodrive, 10000, 20) == (nodrive, 0)


def test_fetch_grouped_min_one_stops_escalation_when_one_found():
    """police a min_results=1 : un seul résultat DANS la préférence suffit → aucune
    escalade (une seule requête), le quota uniforme (3) ne fabrique plus de lointains."""
    settings.politeness_delay_s = 0
    near = {"type": "node", "id": 1, "lat": LAT + 0.01, "lon": LON,     # ~1,1 km
            "tags": {"name": "Politie Zierikzee", "amenity": "police"}}
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler([near], '"amenity"="police"', calls)))
    cats = [{"code": "police", "default_radius_m": 10000, "max_radius_m": 25000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert [p["name"] for p in results["police"]] == ["Politie Zierikzee"]
    assert len(calls) == 1          # min_results=1 atteint → pas de 2e passe


def test_fetch_grouped_fallback_category_still_escalates_to_three():
    """Une catégorie absente de la table (repli min 3) garde le comportement volet 1 :
    sous 3, elle escalade."""
    settings.politeness_delay_s = 0
    el = {"type": "node", "id": 1, "lat": LAT + 0.20, "lon": LON,       # ~22 km (hors pref)
          "tags": {"name": "Busstation", "amenity": "bus_station"}}
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler([el], '"amenity"="bus_station"', calls)))
    cats = [{"code": "bus_station", "default_radius_m": 20000, "max_radius_m": 40000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert len(calls) == 2          # 1 < 3 (repli) → escalade
    assert overpass.target_for("bus_station").min_results == 3


# ── V2-47 : cantonner OSM (source & granularité) ─────────────────────────────

def _poi(name, lat=LAT, lon=LON, tags=None, **fields):
    """POI interne (avec _tags) pour les tests de réduction."""
    t = {"name": name, **(tags or {})}
    return {"name": name, "lat": lat, "lon": lon, "_tags": t,
            "crow_m": overpass.haversine_m(LAT, LON, lat, lon), **fields}


def test_atm_extended_to_banks_and_crypto_deprioritised():
    # Une agence bancaire est acceptée comme distributeur (V2-47).
    assert overpass.category_matches("atm", {"amenity": "bank", "name": "Banco Santander"})
    assert overpass.category_matches("atm", {"amenity": "atm", "name": "ATM"})
    # ATM crypto détecté (nom ou tag currency:XBT), banque non.
    assert overpass._is_crypto_atm({"name": "Bitcoin ATM - Shitcoins.club"})
    assert overpass._is_crypto_atm({"name": "BitBase"})
    assert overpass._is_crypto_atm({"name": "ATM", "currency:XBT": "yes"})
    assert not overpass._is_crypto_atm({"name": "Banco Santander"})
    # Dépriorisation : la banque plus LOIN passe AVANT le crypto plus proche au tri.
    crypto = _poi("BitBase", lat=LAT + 0.001)            # ~110 m
    bank = _poi("Banco Sabadell", lat=LAT + 0.004,       # ~445 m
                tags={"amenity": "bank"})
    reduced, _ = overpass._reduce_category("atm", [crypto, bank])
    ordered = overpass._finalize(reduced, 8)
    assert [p["name"] for p in ordered] == ["Banco Sabadell", "BitBase"]


def test_operator_dedup_collapses_bike_share_network():
    # 8 stations MUyBICI (même operator) → une seule, la plus proche, renommée.
    stations = [_poi(f"MUyBICI: Estación {i}", lat=LAT + 0.001 * i,
                     tags={"amenity": "bicycle_rental", "operator": "MUyBICI"})
                for i in range(1, 9)]
    reduced, dropped = overpass._reduce_category("rental", stations)
    names = [p["name"] for p in reduced]
    assert dropped == 7 and len(names) == 1
    assert names[0] == "MUyBICI (station la plus proche)"
    # Sous le seuil (2 stations) → intactes.
    two = stations[:2]
    kept, d2 = overpass._reduce_category("rental", two)
    assert d2 == 0 and len(kept) == 2


def test_operator_dedup_never_collapses_shop_chains():
    # Une CHAÎNE de supermarchés (même operator) reste DISTINCTE (lieux utiles) :
    # la dédup réseau ne s'applique pas aux commerces.
    branches = [_poi(f"Mercadona {i}", lat=LAT + 0.001 * i,
                     tags={"shop": "supermarket", "operator": "Mercadona"})
                for i in range(1, 5)]
    reduced, dropped = overpass._reduce_category("supermarket", branches)
    assert dropped == 0 and len(reduced) == 4


def test_node_way_dedup_keeps_most_complete():
    # « Greco » (node) et « Tintorería Greco » (way) à < 50 m → une fiche, la + complète.
    node = _poi("Greco", lat=LAT)
    way = _poi("Tintorería Greco", lat=LAT + 0.0002,      # ~22 m
               tags={"shop": "dry_cleaning"}, phone="+34 968 000 000")
    reduced, dropped = overpass._reduce_category("laundry", [node, way])
    assert dropped == 1 and len(reduced) == 1
    assert reduced[0]["name"] == "Tintorería Greco"       # la plus complète survit
    # Deux lieux distincts (noms non imbriqués) ne fusionnent pas.
    a = _poi("Lavandería Sol", lat=LAT)
    b = _poi("Lavandería Luna", lat=LAT + 0.0002)
    kept, d = overpass._reduce_category("laundry", [a, b])
    assert d == 0 and len(kept) == 2


def test_spanish_generic_names_filtered_by_language():
    for n in ["Zona Infantil", "Columpios, tobogán", "Columpios", "Parque infantil",
              "Zona de juegos", "Tobogán"]:
        assert overpass.is_generic_name(n), n
    # Structure par langue exposée + nom propre conservé.
    assert "zona infantil" in overpass._GENERIC_NAMES_BY_LANG["es"]
    assert not overpass.is_generic_name("Parque Warner Madrid")
    assert not overpass.is_generic_name("Columpios del Rey")   # nom propre


def test_police_excludes_civil_protection():
    assert overpass.category_matches("police", {"amenity": "police", "name": "Comisaría"})
    # Protección Civil / DIEM / Cruz Roja mal classés police → exclus.
    assert not overpass.category_matches(
        "police", {"amenity": "police", "name": "Protección Civil - Base de la Fica"})
    assert not overpass.category_matches(
        "police", {"amenity": "police", "name": "DIEM I", "emergency": "yes"})
    assert not overpass.category_matches(
        "police", {"amenity": "police", "government": "civil_protection", "name": "PC"})


def test_post_office_excludes_couriers_keeps_relay_points():
    assert overpass.category_matches("post_office",
                                     {"amenity": "post_office", "name": "Correos"})
    # Coursier / messagerie → exclu.
    assert not overpass.category_matches(
        "post_office", {"amenity": "post_office", "name": "Ecomensajeros"})
    # Point relais (post_partner) → GARDÉ même si le nom évoque une enseigne.
    assert overpass.category_matches(
        "post_office", {"amenity": "post_office", "post_office": "post_partner",
                        "name": "Punto GLS"})


def test_fetch_grouped_reports_network_dropped():
    settings.politeness_delay_s = 0
    els = [{"type": "node", "id": i, "lat": LAT + 0.001 * i, "lon": LON,
            "tags": {"name": f"MUyBICI: {i}", "amenity": "bicycle_rental",
                     "operator": "MUyBICI"}} for i in range(1, 5)]
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(
        _one_selector_handler(els, '"amenity"="bicycle_rental"', calls)))
    cats = [{"code": "rental", "default_radius_m": 10000, "max_radius_m": 10000}]
    results, failures, stats = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()
    assert stats["network_dropped"] == 3
    assert [p["name"] for p in results["rental"]] == ["MUyBICI (station la plus proche)"]


# ── V2-50 : dédup opérateur GÉNÉRALISÉE (tokens de tête + inter-chaînes) ──────

def test_operator_dedup_groups_common_head_token():
    # « BiciCampus * » ×3 sans « : » ni operator → groupés par le token de tête (V2-47
    # les laissait passer). Le plus proche survit.
    stations = [_poi("BiciCampus Reina Sofía", lat=LAT + 0.001,
                     tags={"amenity": "bicycle_rental"}),
                _poi("BiciCampus Facultad", lat=LAT + 0.002,
                     tags={"amenity": "bicycle_rental"}),
                _poi("BiciCampus Aulario", lat=LAT + 0.003,
                     tags={"amenity": "bicycle_rental"})]
    reduced, dropped = overpass._reduce_category("rental", stations)
    assert dropped == 2 and len(reduced) == 1
    assert reduced[0]["name"] == "BiciCampus (station la plus proche)"


def test_operator_dedup_reconciles_cross_chain_same_system():
    # « MUyBICI: * » et « UTE MuyBici servicio público… » = le MÊME système écrit
    # différemment → réconciliés par le token distinctif « muybici » (survivaient en
    # double au run V2-47). Le libellé vient du nom le plus court (le plus net).
    mixed = [_poi("MUyBICI: Estación 5", lat=LAT + 0.002,
                  tags={"amenity": "bicycle_rental"}),
             _poi("MUyBICI: Estación 9", lat=LAT + 0.003,
                  tags={"amenity": "bicycle_rental"}),
             _poi("UTE MuyBici servicio público de bicicleta", lat=LAT + 0.001,
                  tags={"amenity": "bicycle_rental"})]
    reduced, dropped = overpass._reduce_category("rental", mixed)
    assert dropped == 2 and len(reduced) == 1
    assert reduced[0]["name"] == "MUyBICI (station la plus proche)"


def test_operator_dedup_spares_distinct_agencies_sharing_generic_words():
    # 3 agences DISTINCTES ne partageant que des mots génériques (« alquiler », « coches »)
    # ne sont PAS fusionnées (tokens génériques ignorés).
    ag = [_poi("Alquiler de Coches Sol", lat=LAT + 0.001, tags={"amenity": "car_rental"}),
          _poi("Alquiler de Coches Luna", lat=LAT + 0.002, tags={"amenity": "car_rental"}),
          _poi("Alquiler de Coches Mar", lat=LAT + 0.003, tags={"amenity": "car_rental"})]
    reduced, dropped = overpass._reduce_category("rental", ag)
    assert dropped == 0 and len(reduced) == 3
