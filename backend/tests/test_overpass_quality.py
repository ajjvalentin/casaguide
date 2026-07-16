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
    aerodrome = ("aeroway", "aerodrome")
    # Aéroport international avec code IATA -> gardé
    assert overpass.category_matches("airport", {aerodrome[0]: aerodrome[1],
                                                 "iata": "ALC", "name": "Alicante"})
    # Type public explicite -> gardé
    assert overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                 "aerodrome:type": "regional"})
    # Base militaire (San Javier) -> exclue (ni IATA ni type public)
    assert not overpass.category_matches("airport", {"aeroway": "aerodrome",
                                                     "military": "airfield",
                                                     "name": "Base Aérea de San Javier"})
    # Aéroclub (Mar Menor) -> exclu
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
    results, failures = overpass.fetch_grouped(cats, LAT, LON, client=client)
    client.close()

    # Une seule requête Overpass pour les trois catégories
    assert len(calls) == 1
    assert failures == {}
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
