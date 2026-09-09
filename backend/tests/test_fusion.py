"""Fusion inter-sources OSM ↔ Overture (V2-52 volet 1) — cœur PUR.

Aucun réseau, aucune base : le matcher, l'enrichissement de contacts et la
construction des candidats de comblement sont des fonctions pures testées sur fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from enrich import fusion  # noqa: E402

LAT, LON = 37.984, -1.128


def _ovt(name, dlat=0.0, dlon=0.0, **f):
    return {"name": name, "lat": LAT + dlat, "lon": LON + dlon,
            "phone": None, "website": None, "source_ref": f.pop("ref", "gers:X"), **f}


def _osm(name, dlat=0.0, dlon=0.0, **f):
    return {"name": name, "lat": LAT + dlat, "lon": LON + dlon,
            "phone": None, "website": None, **f}


# ── Appariement ───────────────────────────────────────────────────────────────

def test_same_place_distance_and_name():
    a = _osm("Banco Santander")
    assert fusion.same_place(a, _ovt("Banco Santander", dlat=0.0003))       # ~33 m
    assert not fusion.same_place(a, _ovt("Banco Santander", dlat=0.01))     # ~1,1 km
    assert not fusion.same_place(a, _ovt("Pizzería Roma", dlat=0.0003))     # proche, autre nom
    # Une coordonnée manquante → jamais un appariement.
    assert not fusion.same_place(a, {"name": "Banco Santander", "lat": None, "lon": LON})


def test_name_similarity_tolerates_source_spelling():
    # Seuil inter-sources 0,55 : une variante d'écriture apparie tout de même.
    assert fusion.name_similarity("CaixaBank", "Caixabank") >= fusion.NAME_SIM_THRESHOLD
    assert fusion.name_similarity("Mercadona", "Lidl") < fusion.NAME_SIM_THRESHOLD


# ── Gain 3 : enrichissement de contacts ──────────────────────────────────────

def test_enrich_fills_missing_contacts_and_marks_provenance():
    osm = [_osm("Banco Santander")]                    # sans tél ni site
    ovt = [_ovt("Banco Santander", dlat=0.0002, phone="+34 900", website="http://s",
                ref="gers:42")]
    pois, n, consumed = fusion.enrich_osm_contacts(osm, ovt, today="2026-09-09")
    assert n == 1 and consumed == {0}
    assert pois[0]["phone"] == "+34 900" and pois[0]["website"] == "http://s"
    mark = pois[0]["completion_meta"]["_overture"]
    assert mark["source_ref"] == "gers:42" and mark["verified_on"] == "2026-09-09"
    assert mark["fields"] == ["phone", "website"]


def test_enrich_never_overwrites_present_contact():
    osm = [_osm("Bar Sol", phone="+34 111")]           # tél DÉJÀ présent
    ovt = [_ovt("Bar Sol", dlat=0.0002, phone="+34 999", website="http://x")]
    pois, n, consumed = fusion.enrich_osm_contacts(osm, ovt, today="D")
    assert pois[0]["phone"] == "+34 111"               # OSM conservé (jamais écrasé)
    assert pois[0]["website"] == "http://x"            # seul le NULL comblé
    assert n == 1 and consumed == {0}


def test_enrich_consumes_match_even_without_new_data():
    # Même lieu mais Overture n'apporte rien de plus → consommé quand même (il ne doit
    # pas repartir en candidat de comblement, gain 2).
    osm = [_osm("Lidl", phone="+34 1", website="http://l")]
    ovt = [_ovt("Lidl", dlat=0.0002)]
    pois, n, consumed = fusion.enrich_osm_contacts(osm, ovt, today="D")
    assert n == 0 and consumed == {0}
    assert "completion_meta" not in pois[0] or not pois[0].get("completion_meta")


def test_enrich_picks_nearest_match_when_several():
    osm = [_osm("Mercadona")]
    ovt = [_ovt("Mercadona", dlat=0.0005, phone="+34 far"),     # ~55 m
           _ovt("Mercadona", dlat=0.0001, phone="+34 near")]    # ~11 m → gagne
    pois, n, consumed = fusion.enrich_osm_contacts(osm, ovt, today="D")
    assert pois[0]["phone"] == "+34 near" and consumed == {1}


# ── Gains 1 & 2 : candidats de comblement ────────────────────────────────────

def test_fill_candidates_shape_scope_and_exclusions():
    ovt = [_ovt("Banco A", dlat=0.001, phone="+34 A", ref="gers:a"),   # ~111 m
           _ovt("Banco B", dlat=0.05, ref="gers:b"),                   # hors rayon
           _ovt("Zona Infantil", dlat=0.0005, ref="gers:c"),          # générique → écarté
           _ovt("Consommé", dlat=0.0004, ref="gers:d")]               # index 3 consommé
    out = fusion.build_fill_candidates(
        ovt, consumed={3}, code="atm", lat0=LAT, lon0=LON,
        radius_m=2000, limit=8, today="2026-09-09")
    names = [p["name"] for p in out]
    assert names == ["Banco A"]                        # seul candidat valide et dans le rayon
    p = out[0]
    assert p["source"] == "overture" and p["source_ref"] == "gers:a"
    assert p["category"] == "atm" and p["phone"] == "+34 A" and p["crow_m"] > 0
    assert p["completion_meta"]["_overture"]["origin"] == "fill"


def test_fill_candidates_nearest_first_and_limit():
    ovt = [_ovt(f"Banco {i}", dlat=0.001 * (5 - i), ref=f"gers:{i}") for i in range(5)]
    out = fusion.build_fill_candidates(
        ovt, consumed=set(), code="atm", lat0=LAT, lon0=LON,
        radius_m=5000, limit=2, today="D")
    assert [p["name"] for p in out] == ["Banco 4", "Banco 3"]   # 2 plus proches


def test_fill_candidates_marks_crypto_atm_as_deprioritised():
    ovt = [_ovt("Bitcoin ATM - Shitcoins.club", dlat=0.0005, ref="gers:c"),
           _ovt("Banco Santander", dlat=0.0006, ref="gers:s")]
    out = fusion.build_fill_candidates(
        ovt, consumed=set(), code="atm", lat0=LAT, lon0=LON,
        radius_m=2000, limit=8, today="D")
    crypto = next(p for p in out if "Bitcoin" in p["name"])
    bank = next(p for p in out if "Santander" in p["name"])
    assert crypto.get("_priority") == 1 and bank.get("_priority", 0) == 0


def test_cap_after_fusion_ranks_priority_then_travel():
    pois = [
        {"name": "crypto proche", "_priority": 1, "drive_min": 1},
        {"name": "banque loin", "_priority": 0, "drive_min": 9},
        {"name": "banque proche", "_priority": 0, "drive_min": 2},
    ]
    kept = fusion.cap_after_fusion(pois, limit=2)
    # Priorité 0 d'abord (banques), puis trajet croissant → crypto tombe malgré sa proximité.
    assert [p["name"] for p in kept] == ["banque proche", "banque loin"]
