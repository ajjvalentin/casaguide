"""Cohérence commune/CP post-géocodage (V2-46).

Le comparateur `check_geocode_consistency` est PUR → testable sans réseau. Le géocodage
`geocode()` et l'audit rétroactif sont vérifiés via un transport httpx simulé / une
fonction reverse injectée. Cas de reproduction exact : CASA MURCIA localisée à
Torre-Pacheco.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/
from enrich import geocode  # noqa: E402
from enrich.settings import settings  # noqa: E402

# Adresses Nominatim réalistes (bloc `address`, addressdetails=1).
TORRE_PACHECO = {"road": "Príncipe de Asturias", "town": "Torre-Pacheco",
                 "municipality": "Torre-Pacheco", "county": "Murcia",
                 "state": "Región de Murcia", "postcode": "30700", "country_code": "es"}
MURCIA_OK = {"road": "Príncipe de Asturias", "city": "Murcia",
             "municipality": "Murcia", "state": "Región de Murcia",
             "postcode": "30007", "country_code": "es"}
NOORDGOUWE = {"road": "Hanenweg", "village": "Noordgouwe",
              "municipality": "Schouwen-Duiveland", "state": "Zeeland",
              "postcode": "4317 NJ", "country_code": "nl"}


# ── Comparateur PUR ───────────────────────────────────────────────────────────

def test_repro_casa_murcia_is_a_mismatch():
    mm = geocode.check_geocode_consistency("MURCIA", "30007", TORRE_PACHECO)
    assert mm is not None
    assert mm.result_locality == "Torre-Pacheco" and mm.result_postcode == "30700"
    assert mm.input_city == "MURCIA" and mm.input_postcode == "30007"
    assert "Torre-Pacheco" in mm.message_fr() and "MURCIA" in mm.message_fr()


def test_correct_geocodes_never_flag():
    # Murcia bien localisée (contre-épreuve).
    assert geocode.check_geocode_consistency("Murcia", "30007", MURCIA_OK) is None
    # Op de Boerderie : village saisi = niveau `village` du résultat (municipalité
    # différente « Schouwen-Duiveland » n'y change rien).
    assert geocode.check_geocode_consistency("Noordgouwe", "4317 NJ", NOORDGOUWE) is None


def test_accents_case_and_dutch_dash_municipality():
    # Accents + casse : « Málaga » == « MALAGA ».
    assert geocode.check_geocode_consistency(
        "MÁLAGA", None, {"city": "Malaga", "postcode": "29001"}) is None
    # Arrondissement à tiret néerlandais saisi tel quel (municipalité).
    assert geocode.check_geocode_consistency(
        "Schouwen-Duiveland", None, NOORDGOUWE) is None
    # Le tiret ne crée pas de faux positif : « schouwen duiveland » normalisé des deux côtés.
    assert geocode.check_geocode_consistency(
        "schouwen duiveland", None, {"municipality": "Schouwen-Duiveland"}) is None


def test_province_never_counts_as_locality_match():
    # Torre-Pacheco EST dans la province « Murcia » : inclure la province masquerait
    # le défaut. La saisie « Murcia » ne doit PAS matcher via `county`/`state`.
    mm = geocode.check_geocode_consistency("Murcia", None, TORRE_PACHECO)
    assert mm is not None and mm.result_locality == "Torre-Pacheco"


def test_absent_address_detail_is_never_a_mismatch():
    # Un résultat sans bloc adresse (géocodage brut / mock) ne prouve rien.
    assert geocode.check_geocode_consistency("Murcia", "30007", None) is None
    assert geocode.check_geocode_consistency("Murcia", "30007", {}) is None
    # Un résultat avec seulement une route (pas de niveau municipal) non plus.
    assert geocode.check_geocode_consistency(
        "Murcia", "30007", {"road": "X", "state": "Murcia"}) is None


def test_postcode_conflict_only_triggers_without_a_city():
    # Sans commune saisie, un CP contradictoire déclenche.
    mm = geocode.check_geocode_consistency(None, "30007", TORRE_PACHECO)
    assert mm is not None and mm.result_postcode == "30700"
    # Avec commune saisie QUI CORRESPOND, un CP légèrement différent ne déclenche pas
    # (grande ville à plusieurs codes postaux — évite le faux positif).
    assert geocode.check_geocode_consistency(
        "Murcia", "30099", MURCIA_OK) is None


# ── geocode() force accuracy='mismatch' ──────────────────────────────────────

def _transport(address: dict, lat=37.74, lon=-0.95):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "lat": str(lat), "lon": str(lon), "type": "house", "class": "building",
            "display_name": "Príncipe de Asturias 38", "address": address}])
    return httpx.MockTransport(handler)


def test_geocode_sets_mismatch_accuracy_on_homonym():
    client = httpx.Client(transport=_transport(TORRE_PACHECO))
    res = geocode.geocode(street="Príncipe de Asturias 38", postalcode="30007",
                          city="MURCIA", country_code="ES", client=client)
    client.close()
    assert res["accuracy"] == "mismatch"          # jamais « rooftop »/« precise »
    assert res["mismatch"] is not None
    assert res["mismatch"].result_locality == "Torre-Pacheco"


def test_geocode_keeps_rooftop_when_consistent():
    client = httpx.Client(transport=_transport(MURCIA_OK))
    res = geocode.geocode(street="Príncipe de Asturias 38", postalcode="30007",
                          city="Murcia", country_code="ES", client=client)
    client.close()
    assert res["accuracy"] == "rooftop" and res["mismatch"] is None


# ── Audit rétroactif (reverse injecté) ───────────────────────────────────────

def test_audit_flags_only_the_incoherent_property():
    import audit_geocode as A  # noqa: PLC0415
    props = [
        {"id": "1", "name": "CASA MURCIA", "city": "Murcia", "postal_code": "30007",
         "lat": 37.74, "lon": -0.95, "geocode_accuracy": "rooftop",
         "geocode_source": "nominatim"},
        {"id": "2", "name": "Op de Boerderie", "city": "Noordgouwe",
         "postal_code": "4317 NJ", "lat": 51.71, "lon": 3.91,
         "geocode_accuracy": "rooftop", "geocode_source": "nominatim"},
    ]
    reverse_by_id = {("37.74", "-0.95"): TORRE_PACHECO,
                     ("51.71", "3.91"): NOORDGOUWE}

    def reverse(lat, lon):
        return reverse_by_id[(str(lat), str(lon))]

    findings = A.audit(props, reverse)
    assert [f["name"] for f in findings] == ["CASA MURCIA"]
    assert findings[0]["result_locality"] == "Torre-Pacheco"


# ── V2-68 pièce 1 : seuil de précision d'ancrage ──────────────────────────────

def test_is_precise_enough():
    """Rue/quartier/point manuel = assez précis pour ancrer un guide ; centroïde
    administratif (« city »), commune incohérente (« mismatch ») ou None = non."""
    for ok in ("rooftop", "street", "manual"):
        assert geocode.is_precise_enough(ok) is True
    for bad in ("city", "mismatch", None, ""):
        assert geocode.is_precise_enough(bad) is False


# ── V2-68c : repère de départ d'une adresse introuvable ───────────────────────

def _fake_search(found_for):
    """`_search` simulé : rend un résultat pour les jeux de paramètres nommés dans
    `found_for` (clé = 'city' | 'postal' | 'country'), None sinon. Aucun réseau."""
    seen = []

    def _search(params, country_code, client):
        level = ("country" if "country" in params
                 else "postal" if params.get("q", "").strip().isdigit()
                 else "city")
        seen.append(level)
        pt = found_for.get(level)
        return {"lat": str(pt[0]), "lon": str(pt[1])} if pt else None

    return _search, seen


def test_coarse_locate_prefers_the_commune(monkeypatch):
    """V2-68c p3 : le meilleur repère d'abord — la commune quand Nominatim la connaît."""
    search, seen = _fake_search({"city": (46.2, 7.26), "country": (46.8, 8.2)})
    monkeypatch.setattr(geocode, "_search", search)
    out = geocode.coarse_locate(city="Ardon", postalcode="1957", country_code="CH")
    assert out == {"lat": 46.2, "lon": 7.26, "level": "city"}
    assert seen == ["city"]                       # on ne descend pas plus bas que besoin


def test_coarse_locate_falls_back_to_postcode_then_country(monkeypatch):
    """Commune inconnue → code postal ; ni l'un ni l'autre → pays (TOUJOURS fourni par la
    saisie). Cas réel : « Rrugë Skënderbeu 307, Xërxë, XK »."""
    search, seen = _fake_search({"postal": (44.9, -0.7), "country": (42.6, 20.9)})
    monkeypatch.setattr(geocode, "_search", search)
    assert geocode.coarse_locate(city="Bégadan", postalcode="33340",
                                 country_code="FR")["level"] == "postal"

    search, seen = _fake_search({"country": (42.6, 20.9)})
    monkeypatch.setattr(geocode, "_search", search)
    out = geocode.coarse_locate(city="Xërxë", country_code="XK")
    assert out == {"lat": 42.6, "lon": 20.9, "level": "country"}
    assert seen == ["city", "country"]            # pas de CP saisi → barreau sauté


def test_coarse_locate_survives_a_broken_rung(monkeypatch):
    """Un barreau qui casse (réseau, HTTP, pays inconnu) n'interrompt jamais la descente
    — sinon le parcours se refermerait justement là où il doit s'ouvrir."""
    def search(params, country_code, client):
        if "country" not in params:
            raise httpx.ConnectError("boom")
        return {"lat": "42.6", "lon": "20.9"}

    monkeypatch.setattr(geocode, "_search", search)
    assert geocode.coarse_locate(city="Xërxë", country_code="XK")["level"] == "country"


def test_coarse_locate_returns_none_when_even_the_country_is_unknown(monkeypatch):
    """Aucun repère du tout → None (le tunnel ouvre alors la carte au large)."""
    search, _ = _fake_search({})
    monkeypatch.setattr(geocode, "_search", search)
    assert geocode.coarse_locate(city="Nulle part", country_code="ZZ") is None
