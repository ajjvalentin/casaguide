"""V2-79b — géocodage des picks : adresse découpée, file d'attente Nominatim, 429 repris.

Constat réel (Jávea, job 98cdff38, --refresh-sector) : 11 picks sur 11 sautés « position
non fiable » (adresse entière passée dans `street`), `q=<commune>` rejoué pour chacun,
puis 429 → job PAYÉ mort. Aucun réseau ici : MockTransport + horloge injectée.
"""
from __future__ import annotations

import urllib.parse

import httpx
import pytest

from enrich import geocode
from enrich.settings import settings

JAVEA = {"lat": "38.79", "lon": "0.17", "category": "highway", "type": "secondary",
         "place_rank": 26, "display_name": "x",
         "address": {"town": "Xàbia / Jávea", "postcode": "03730"}}


@pytest.mark.parametrize("addr, expected", [
    # Les vraies formes du journal de Jávea.
    ("Carretera de Benitachell, 100, 03730 Jávea, Alicante",
     {"street": "Carretera de Benitachell 100", "postalcode": "03730", "city": "Jávea"}),
    ("Av. del Mediterráneo, 1 (Platja de l'Arenal), 03730 Xàbia",
     {"street": "Av. del Mediterráneo 1", "postalcode": "03730", "city": "Xàbia"}),
    ("Urbanización El Tosalet, Calle Pinos 5, 03730 Jávea",
     {"street": "Calle Pinos 5", "postalcode": "03730", "city": "Jávea"}),
    ("Passeig Marítim s/n, 03590 Altea, Alicante, España",
     {"street": "Passeig Marítim", "postalcode": "03590", "city": "Altea"}),
    # Sans CP ni ville : la commune du logement sert de repli.
    ("Calle Mayor 5", {"street": "Calle Mayor 5", "postalcode": None, "city": "Jávea"}),
    # Forme rurale valencienne AVEC numéro : c'est une adresse, gardée.
    ("Partida Pla de Lluca 22, Jávea",
     {"street": "Partida Pla de Lluca 22", "postalcode": None, "city": "Jávea"}),
    # Rien d'autre que la commune : aucune rue.
    ("Jávea", {"street": None, "postalcode": None, "city": "Jávea"}),
])
def test_split_address(addr, expected):
    assert geocode.split_address(addr, "Jávea") == expected


def _recording(handler_status=None):
    """Client MockTransport qui enregistre les paramètres de chaque requête."""
    seen: list[dict] = []
    statuses = list(handler_status or [])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.url.query.decode())))
        if statuses:
            st = statuses.pop(0)
            if isinstance(st, tuple):
                return httpx.Response(st[0], headers=st[1])
            if st != 200:
                return httpx.Response(st)
        return httpx.Response(200, json=[JAVEA])
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_pick_query_is_structured_and_never_asks_the_commune_centroid():
    client, seen = _recording()
    with client:
        geo = geocode.geocode_place("Carretera de Benitachell, 100, 03730 Jávea, Alicante",
                                    "Jávea", "ES", client)
    assert geo["accuracy"] == "street"
    assert seen[0]["street"] == "Carretera de Benitachell 100"
    assert seen[0]["postalcode"] == "03730" and seen[0]["city"] == "Jávea"
    assert all("q" not in p for p in seen)          # jamais `q=<commune>`


def test_unresolvable_pick_costs_no_centroid_request():
    """Rue introuvable : le pick tombe SANS aucune requête de zone (avant : `q=Jávea`
    rejoué une fois par pick)."""
    def handler(request):
        return httpx.Response(200, json=[])
    seen = []
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: (seen.append(r.url.query.decode()), handler(r))[1])) as client:
        with pytest.raises(geocode.GeocodeError):
            geocode.geocode_place("Calle Inexistente 9, 03730 Jávea", "Jávea", "ES", client)
    assert seen and not any("q=" in q.split("&")[0] for q in seen)
    assert all("street=" in q for q in seen)


def test_identical_requests_are_cached_within_a_job_only():
    client, seen = _recording()
    with client:
        with geocode.request_cache():
            for _ in range(11):
                geocode.geocode(city="Jávea", country_code="ES", client=client)
        assert len(seen) == 1                       # 11 picks → UNE requête
        geocode.geocode(city="Jávea", country_code="ES", client=client)
        assert len(seen) == 2                       # hors job : pas de cache


class _Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(geocode, "_now", c.now)
    monkeypatch.setattr(geocode, "_sleep", c.sleep)
    monkeypatch.setitem(geocode._state, "last", 0.0)
    monkeypatch.setitem(geocode._state, "blocked_until", 0.0)
    return c


def test_requests_are_spaced_by_the_shared_queue(clock, monkeypatch):
    monkeypatch.setattr(settings, "nominatim_min_interval_s", 1.1)
    client, seen = _recording()
    with client:
        for _ in range(3):
            geocode.geocode(city="Jávea", country_code="ES", client=client)
    assert len(seen) == 3 and clock.slept == [1.1, 1.1]


def test_429_waits_and_resumes_instead_of_failing(clock, monkeypatch):
    monkeypatch.setattr(settings, "nominatim_backoff_s", 5.0)
    client, seen = _recording([(429, {"Retry-After": "7"}), 429, 200])
    with client:
        geo = geocode.geocode(street="Calle Mayor 5", city="Jávea", country_code="ES",
                              client=client, area_fallback=False)
    assert geo["accuracy"] == "street" and len(seen) == 3
    assert clock.slept == [7.0, 10.0]               # Retry-After, puis backoff 5×2


def test_persistent_429_raises_a_geocode_error(clock, monkeypatch):
    monkeypatch.setattr(settings, "nominatim_max_attempts", 3)
    client, seen = _recording([429, 429, 429])
    with client, pytest.raises(geocode.GeocodeRateLimited) as exc:
        geocode.geocode(street="Calle Mayor 5", city="Jávea", country_code="ES",
                        client=client, area_fallback=False)
    assert isinstance(exc.value, geocode.GeocodeError)   # absorbé par le pipeline
    assert len(seen) == 3
