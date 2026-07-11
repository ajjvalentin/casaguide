"""Test d'intégration du pipeline contre le vrai PostgreSQL/PostGIS.

Les API externes (Nominatim, Overpass, OSRM) sont simulées par des réponses
réalistes via httpx.MockTransport ; l'API Claude est bouchonnée. Le reste —
orchestration, parsing, upserts, idempotence, suivi de job, coûts — est le
code de production, exécuté pour de vrai.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from enrich import db, pipeline  # noqa: E402
from enrich.settings import settings  # noqa: E402

PROP_LAT, PROP_LON = 37.9280, -0.7482  # Orihuela Costa

# ── Réponses simulées des API géo ────────────────────────────────────────────

NOMINATIM = [{"lat": str(PROP_LAT), "lon": str(PROP_LON),
              "type": "house", "class": "building",
              "display_name": "Calle Ejemplo 1, Orihuela Costa"}]

OVERPASS_BY_CATEGORY = {
    "hospital": [
        {"type": "way", "id": 111, "center": {"lat": 37.9950, "lon": -0.7130},
         "tags": {"name": "Hospital Universitario de Torrevieja",
                  "phone": "+34 965 72 12 00", "amenity": "hospital"}},
    ],
    "supermarket": [
        {"type": "node", "id": 222, "lat": 37.9310, "lon": -0.7510,
         "tags": {"name": "Mercadona", "shop": "supermarket",
                  "opening_hours": "Mo-Sa 09:00-21:30"}},
        {"type": "node", "id": 223, "lat": 37.9330, "lon": -0.7550,
         "tags": {"name": "Lidl", "shop": "supermarket"}},
        {"type": "node", "id": 224, "lat": 37.9331, "lon": -0.7551,
         "tags": {"shop": "supermarket"}},  # sans nom -> doit être ignoré
    ],
    "restaurant": [
        {"type": "node", "id": 333, "lat": 37.9290, "lon": -0.7470,
         "tags": {"name": "La Marejada", "amenity": "restaurant",
                  "website": "https://lamarejada.example"}},
    ],
}


def _overpass_payload(query: str) -> dict:
    # Requêtes groupées par palier de rayon : la réponse est l'UNION des
    # catégories dont un sélecteur figure dans la requête (re-ventilées par tags).
    elements: list[dict] = []
    for cat, sel in {"hospital": '"amenity"="hospital"',
                     "supermarket": '"shop"="supermarket"',
                     "restaurant": '"amenity"="restaurant"'}.items():
        if sel in query:
            elements.extend(OVERPASS_BY_CATEGORY[cat])
    return {"elements": elements}


def _osrm_payload(url: str) -> dict:
    n = url.split("/table/v1/driving/")[1].split("?")[0].count(";")
    return {"code": "Ok",
            "durations": [[0] + [540.0 + 60 * i for i in range(n)]],
            "distances": [[0] + [4200.0 + 500 * i for i in range(n)]]}


def _mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "nominatim" in url:
        return httpx.Response(200, json=NOMINATIM)
    if "overpass" in url:
        body = urllib.parse.unquote_plus(request.read().decode())
        return httpx.Response(200, json=_overpass_payload(body))
    if "/table/v1/" in url:
        return httpx.Response(200, json=_osrm_payload(url))
    return httpx.Response(404)


# ── Bouchon de l'API Claude ──────────────────────────────────────────────────

class FakeMessages:
    def create(self, *, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "emergency_numbers" in prompt:  # prompt area_facts
            payload = {
                "emergency_numbers": {"items": [
                    {"label": "Urgences (UE)", "number": "112"},
                    {"label": "Guardia Civil", "number": "062"}],
                    "notes": "Le 112 fonctionne dans toute l'Espagne."},
                "waste_rules": {"summary": "Tri par conteneurs de couleur.",
                                "containers": [
                                    {"color_or_type": "jaune", "accepts": "emballages"},
                                    {"color_or_type": "vert", "accepts": "verre"}]},
                "noise_rules": {"summary": "Bruit limité la nuit.",
                                "quiet_hours": "23h00-08h00"},
            }
        else:  # prompt descriptions POI
            payload = {"node/333": "Restaurant de poissons face à la plage, "
                                   "apprécié pour ses arroces."}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=800, output_tokens=350),
        )


class FakeAnthropic:
    messages = FakeMessages()


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def property_id():
    pid, oid = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as conn:
        # Isolation : repartir d'une zone ES vierge. Les area_facts sont
        # mutualisés par (pays, commune) ; d'éventuels vestiges (ex. données
        # d'un test réel) fausseraient le décompte des coûts (area_facts sauté).
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'Test')",
                     (oid, f"{oid}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city,
                                       country_code)
               VALUES (%s, %s, 'Villa Pipeline', 'Calle Ejemplo 1',
                       'Orihuela Costa', 'ES')""",
            (pid, oid))
        conn.commit()
    yield pid
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM owners WHERE id = %s", (oid,))
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        conn.commit()


@pytest.fixture()
def http_client():
    settings.politeness_delay_s = 0  # pas d'attente en test
    with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as c:
        yield c


# ── Tests ────────────────────────────────────────────────────────────────────

def test_full_pipeline(property_id, http_client):
    result = pipeline.run(property_id, use_claude=True, trigger="initial",
                          only_categories={"hospital", "supermarket", "restaurant"},
                          http_client=http_client,
                          anthropic_client=FakeAnthropic())

    assert result["pois"] == 4  # 1 hôpital + 2 supermarchés (le sans-nom exclu) + 1 resto
    assert result["cost_cts"] > 0

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # Géocodage écrit sur le logement, précision 'rooftop'
        prop = conn.execute(
            "SELECT ST_Y(geom) lat, geocode_accuracy FROM properties WHERE id=%s",
            (property_id,)).fetchone()
        assert prop["lat"] == pytest.approx(PROP_LAT)
        assert prop["geocode_accuracy"] == "rooftop"

        # POI en 'suggested', avec distances pré-calculées et traçabilité
        pois = conn.execute(
            "SELECT * FROM pois WHERE property_id=%s ORDER BY name",
            (property_id,)).fetchall()
        assert {p["status"] for p in pois} == {"suggested"}
        assert all(p["walk_min"] and p["drive_min"] for p in pois)
        assert all(p["source"] == "osm" and p["source_ref"] for p in pois)

        # Description IA appliquée au restaurant uniquement
        resto = next(p for p in pois if p["name"] == "La Marejada")
        assert "arroces" in resto["description_md"]

        # area_facts : 3 lignes mutualisées ES / Orihuela Costa
        n = conn.execute("""SELECT count(*) n FROM area_facts
                            WHERE country_code='ES' AND admin_area='Orihuela Costa'"""
                         ).fetchone()["n"]
        assert n == 3

        # Job 'done' avec toutes les étapes ok, coûts comptabilisés
        job = conn.execute("SELECT * FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done"
        assert all(job["steps"][s]["ok"] for s in
                   ("geocode", "overpass", "distances", "claude"))
        costs = conn.execute("SELECT count(*) n FROM api_costs WHERE job_id=%s",
                             (result["job_id"],)).fetchone()["n"]
        assert costs == 2  # area_facts + describe_pois


def test_rerun_is_idempotent_and_preserves_owner_choices(property_id, http_client):
    kw = dict(only_categories={"supermarket"}, http_client=http_client,
              anthropic_client=FakeAnthropic())
    pipeline.run(property_id, use_claude=False, **kw)

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # Le propriétaire approuve Mercadona et lui met un commentaire
        conn.execute("""UPDATE pois SET status='approved',
                        owner_comment='Le plus pratique'
                        WHERE property_id=%s AND name='Mercadona'""", (property_id,))
        conn.commit()

    pipeline.run(property_id, use_claude=False, **kw)  # ré-enrichissement

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        pois = conn.execute("SELECT name, status, owner_comment FROM pois "
                            "WHERE property_id=%s", (property_id,)).fetchall()
        assert len(pois) == 2  # aucun doublon créé
        merca = next(p for p in pois if p["name"] == "Mercadona")
        assert merca["status"] == "approved"          # choix conservé
        assert merca["owner_comment"] == "Le plus pratique"
