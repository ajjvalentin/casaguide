"""Tests d'intégration de l'API FastAPI contre le vrai PostgreSQL/PostGIS.

Sur le modèle de test_pipeline.py : la base est réelle (schéma + seed chargés),
les API externes du pipeline (Nominatim, Overpass, OSRM, Claude) sont simulées.
Le reste — routers, auth JWT, chiffrement des secrets, isolation multi-tenant,
tâche de fond d'enrichissement, validation des POI, guide public — est le code
de production exécuté pour de vrai.

Couvre notamment les invariants du CLAUDE.md :
  #1  un POI arbitré n'est pas réécrit (statuts approved/edited/rejected) ;
  #4  aucun appel externe côté voyageur (le guide ne lit que la base) ;
  #5  secrets chiffrés en base, jamais exposés sur l'endpoint public.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace

# Environnement AVANT import des modules api (config/crypto lisent l'env à l'import)
os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")
os.environ.setdefault("CASAGUIDE_JWT_SECRET",
                      "test-secret-not-for-prod-0123456789-abcdefghij")  # ≥ 32 o
os.environ.setdefault(
    "CASAGUIDE_SECRET_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")  # 32 o (hex)
os.environ.setdefault("CASAGUIDE_PBKDF2_ITER", "10000")  # tests plus rapides
# Racine des médias isolée pour les tests (M-12)
os.environ.setdefault("MEDIA_ROOT",
                      os.path.join(tempfile.gettempdir(), "casaguide-test-media"))

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import crypto, repo  # noqa: E402
from api.deps import (  # noqa: E402
    get_distance_computer, get_enrichment_runner, get_translation_runner)
from api.main import app  # noqa: E402
from enrich import pipeline, translate  # noqa: E402
from enrich.overpass import haversine_m  # noqa: E402
from enrich.settings import settings  # noqa: E402

PROP_LAT, PROP_LON = 37.9280, -0.7482  # Orihuela Costa

# ── Simulations réseau (reprises de test_pipeline) ───────────────────────────

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
    ],
    "restaurant": [
        {"type": "node", "id": 333, "lat": 37.9290, "lon": -0.7470,
         "tags": {"name": "La Marejada", "amenity": "restaurant",
                  "website": "https://lamarejada.example"}},
    ],
}


def _overpass_payload(query: str) -> dict:
    # Requêtes groupées par palier de rayon : union des catégories présentes.
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


class FakeMessages:
    def create(self, *, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "emergency_numbers" in prompt:
            payload = {
                "emergency_numbers": {"items": [
                    {"label": "Urgences (UE)", "number": "112"},
                    {"label": "Guardia Civil", "number": "062"}],
                    "notes": "Le 112 fonctionne dans toute l'Espagne."},
                "waste_rules": {"summary": "Tri par conteneurs de couleur.",
                                "containers": [
                                    {"color_or_type": "jaune", "accepts": "emballages"}]},
                "noise_rules": {"summary": "Bruit limité la nuit.",
                                "quiet_hours": "23h00-08h00"},
            }
        else:
            payload = {"node/333": "Restaurant de poissons face à la plage."}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=800, output_tokens=350),
        )


class FakeAnthropic:
    messages = FakeMessages()


def _test_distance_computer(origin, pois) -> None:
    """Recalcul de distances sans réseau (haversine × détour), pour M-05."""
    for p in pois:
        m = round(haversine_m(origin[0], origin[1], p["lat"], p["lon"]) * 1.3)
        p["dist_walk_m"] = m
        p["walk_min"] = max(1, round(m / 1000 / 4.8 * 60))
        p["dist_drive_m"] = m
        p["drive_min"] = max(1, round(m / 1000 / 40 * 60))


def _test_runner(property_id: str, trigger: str, job_id: str) -> None:
    """Exécuteur d'enrichissement sans réseau, injecté à la place du vrai pipeline."""
    settings.politeness_delay_s = 0
    with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as c:
        pipeline.run(property_id, use_claude=True, trigger=trigger, job_id=job_id,
                     only_categories={"hospital", "supermarket", "restaurant"},
                     http_client=c, anthropic_client=FakeAnthropic())


class FakeTranslator:
    """Traducteur sans réseau (M-09) : préfixe chaque valeur par sa langue cible,
    ce qui rend la traduction observable dans les tests. Compte les appels pour
    vérifier le ciblage (re-traduction du seul périmé)."""

    def __init__(self):
        self.calls: list[dict] = []

    def translate(self, texts, *, target_lang, source_lang):
        self.calls.append(dict(texts))
        out = {k: f"[{target_lang}] {v}" for k, v in texts.items()}
        # Méta plausible (comptabilisée dans api_costs, operation='translate')
        return out, {"units": 10 * len(texts) or 1, "cost_cts": 0.05}


# Traducteur partagé par le runner de test (inspectable dans les assertions).
LAST_TRANSLATOR = FakeTranslator()


def _test_translation_runner(property_id: str, job_id: str) -> None:
    """Exécuteur de traduction sans réseau, injecté à la place du vrai."""
    translate.run(property_id, job_id=job_id, translator=LAST_TRANSLATOR)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    app.dependency_overrides[get_enrichment_runner] = lambda: _test_runner
    app.dependency_overrides[get_distance_computer] = lambda: _test_distance_computer
    app.dependency_overrides[get_translation_runner] = lambda: _test_translation_runner
    emails: list[str] = []
    c = TestClient(app)
    c.created_emails = emails  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()
    with psycopg.connect(settings.db_dsn) as conn:
        for email in emails:
            conn.execute("DELETE FROM owners WHERE email = %s", (email,))
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        conn.commit()


def register(client, **over) -> dict:
    """Inscrit un propriétaire unique, renvoie {email, token, headers}."""
    email = over.pop("email", f"{uuid.uuid4()}@casaguide-test.com")
    body = {"email": email, "password": "password123", "full_name": "Prop Test"}
    body.update(over)
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    client.created_emails.append(email)
    token = r.json()["access_token"]
    return {"email": email, "token": token,
            "headers": {"Authorization": f"Bearer {token}"}}


def make_property(client, headers, **over) -> dict:
    body = {"name": "Villa Mar Azul", "address_line1": "Calle Ejemplo 1",
            "city": "Orihuela Costa", "country_code": "ES"}
    body.update(over)
    r = client.post("/api/properties", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_register_login_me(client):
    owner = register(client)
    # /me renvoie le profil + le plan d'essai attribué
    me = client.get("/api/auth/me", headers=owner["headers"])
    assert me.status_code == 200
    assert me.json()["email"] == owner["email"]
    assert me.json()["plan_id"] == "free"

    # Connexion avec les mêmes identifiants
    r = client.post("/api/auth/login",
                    json={"email": owner["email"], "password": "password123"})
    assert r.status_code == 200 and r.json()["access_token"]


def test_register_duplicate_and_bad_login(client):
    owner = register(client)
    dup = client.post("/api/auth/register", json={
        "email": owner["email"], "password": "password123", "full_name": "X"})
    assert dup.status_code == 409

    bad = client.post("/api/auth/login",
                      json={"email": owner["email"], "password": "wrong-pass"})
    assert bad.status_code == 401

    unknown = client.post("/api/auth/login",
                          json={"email": "nobody@casaguide-test.com",
                                "password": "whatever12"})
    assert unknown.status_code == 401


def test_requires_auth(client):
    assert client.get("/api/properties").status_code == 401
    assert client.get("/api/properties",
                      headers={"Authorization": "Bearer garbage"}).status_code == 401


# ── CRUD logements & isolation multi-tenant ──────────────────────────────────

def test_property_crud(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    assert prop["status"] == "draft"
    assert len(prop["guide_token"]) >= 32  # token secret ≥ 128 bits (hex)

    # Lecture
    got = client.get(f"/api/properties/{pid}", headers=owner["headers"])
    assert got.status_code == 200 and got.json()["name"] == "Villa Mar Azul"

    # Mise à jour partielle + placement manuel du point
    patch = client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                         json={"name": "Villa Renommée", "lat": PROP_LAT,
                               "lon": PROP_LON})
    assert patch.status_code == 200
    assert patch.json()["name"] == "Villa Renommée"
    assert patch.json()["lat"] == pytest.approx(PROP_LAT)
    assert patch.json()["geocode_source"] == "manual"

    # Liste
    lst = client.get("/api/properties", headers=owner["headers"])
    assert lst.status_code == 200 and len(lst.json()) == 1

    # Suppression
    assert client.delete(f"/api/properties/{pid}",
                         headers=owner["headers"]).status_code == 204
    assert client.get(f"/api/properties/{pid}",
                      headers=owner["headers"]).status_code == 404


def test_multitenant_isolation(client):
    alice = register(client)
    bob = register(client)
    prop = make_property(client, alice["headers"])
    pid = prop["id"]

    # Bob ne voit pas et n'atteint pas le logement d'Alice
    assert client.get("/api/properties", headers=bob["headers"]).json() == []
    assert client.get(f"/api/properties/{pid}",
                      headers=bob["headers"]).status_code == 404
    assert client.patch(f"/api/properties/{pid}", headers=bob["headers"],
                        json={"name": "Pirate"}).status_code == 404
    assert client.delete(f"/api/properties/{pid}",
                         headers=bob["headers"]).status_code == 404
    assert client.post(f"/api/properties/{pid}/enrich", headers=bob["headers"],
                       json={"trigger": "manual"}).status_code == 404


# ── Données sensibles chiffrées (invariant 5) ────────────────────────────────

def test_secrets_encrypted_and_owner_only(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]

    put = client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
                     json={"wifi_ssid": "VillaMarAzul",
                           "wifi_pass": "SuperSecret!42",
                           "keybox_code": "7391",
                           "keybox_notes": "À gauche de la porte"})
    assert put.status_code == 200

    # Le propriétaire récupère les secrets en clair
    got = client.get(f"/api/properties/{pid}/secrets", headers=owner["headers"])
    assert got.status_code == 200
    assert got.json()["wifi_pass"] == "SuperSecret!42"
    assert got.json()["keybox_code"] == "7391"

    # En base : colonnes BYTEA chiffrées, jamais le clair
    with psycopg.connect(settings.db_dsn) as conn:
        row = conn.execute(
            "SELECT wifi_pass_enc, keybox_code_enc FROM property_secrets "
            "WHERE property_id = %s", (pid,)).fetchone()
        wifi_enc = bytes(row[0])
        assert b"SuperSecret" not in wifi_enc
        assert crypto.decrypt(wifi_enc) == "SuperSecret!42"
        assert crypto.decrypt(bytes(row[1])) == "7391"

    # Un autre propriétaire ne peut pas lire les secrets
    intruder = register(client)
    assert client.get(f"/api/properties/{pid}/secrets",
                      headers=intruder["headers"]).status_code == 404


# ── Enrichissement en tâche de fond + validation des POI ─────────────────────

def _enrich_and_wait(client, headers, pid) -> None:
    r = client.post(f"/api/properties/{pid}/enrich", headers=headers,
                    json={"trigger": "initial"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    # Avec TestClient, la tâche de fond s'exécute avant le retour de la requête
    job = client.get(f"/api/properties/{pid}/jobs/{job_id}", headers=headers)
    assert job.status_code == 200
    assert job.json()["status"] == "done", job.json()
    for step in ("geocode", "overpass", "distances", "claude"):
        assert job.json()["steps"][step]["ok"]


def test_enrich_then_validate_pois(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]

    _enrich_and_wait(client, owner["headers"], pid)

    # 4 POI suggérés (hôpital + 2 supermarchés + restaurant), distances pré-calculées
    suggested = client.get(f"/api/properties/{pid}/pois?status=suggested",
                           headers=owner["headers"]).json()
    assert len(suggested) == 4
    assert all(p["walk_min"] and p["drive_min"] for p in suggested)
    assert all(p["source"] == "osm" for p in suggested)
    by_name = {p["name"]: p for p in suggested}

    # Le propriétaire arbitre : approuve, édite, rejette
    hosp = by_name["Hospital Universitario de Torrevieja"]
    assert client.post(f"/api/properties/{pid}/pois/{hosp['id']}/approve",
                       headers=owner["headers"]).json()["status"] == "approved"

    resto = by_name["La Marejada"]
    edited = client.patch(f"/api/properties/{pid}/pois/{resto['id']}",
                          headers=owner["headers"],
                          json={"owner_comment": "Notre cantine préférée"})
    assert edited.status_code == 200 and edited.json()["status"] == "edited"

    lidl = by_name["Lidl"]
    assert client.post(f"/api/properties/{pid}/pois/{lidl['id']}/reject",
                       headers=owner["headers"]).json()["status"] == "rejected"

    # POI d'un autre logement/propriétaire : 404
    intruder = register(client)
    assert client.post(f"/api/properties/{pid}/pois/{hosp['id']}/approve",
                       headers=intruder["headers"]).status_code == 404


def test_enrich_quota_enforced(client):
    """Le plan gratuit autorise 1 enrichissement/mois (§5.2)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    _enrich_and_wait(client, owner["headers"], pid)
    # Deuxième déclenchement le même mois -> 429
    second = client.post(f"/api/properties/{pid}/enrich", headers=owner["headers"],
                         json={"trigger": "refresh"})
    assert second.status_code == 429


# ── M-22 : ajout manuel de POI (recherche Nominatim + création owner) ─────────

def test_poi_search_maps_category_and_isolates(client):
    """La recherche mappe class/type OSM → category_code et reste étanche
    (un autre propriétaire n'y accède pas). Searcher injecté (aucun réseau)."""
    from api.deps import get_poi_searcher

    seen: list[tuple] = []

    def fake_searcher(q, lat, lon):
        seen.append((q, lat, lon))
        return [{"name": "El Meson de la Costa", "address": "Av. de la Costa, Torrevieja",
                 "lat": 37.978, "lon": -0.682, "category_code": "restaurant",
                 "phone": "+34 966 00 00 00", "website": None}]

    app.dependency_overrides[get_poi_searcher] = lambda: fake_searcher
    try:
        owner = register(client)
        prop = make_property(client, owner["headers"])
        pid = prop["id"]
        client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                     json={"lat": PROP_LAT, "lon": PROP_LON})

        r = client.get(f"/api/properties/{pid}/pois/search?q=Meson Costa",
                       headers=owner["headers"])
        assert r.status_code == 200
        cands = r.json()
        assert cands[0]["name"] == "El Meson de la Costa"
        assert cands[0]["category_code"] == "restaurant"
        # Le biais géographique reçoit la position du logement
        assert seen[-1] == ("Meson Costa", pytest.approx(PROP_LAT), pytest.approx(PROP_LON))

        # Étanchéité : un autre propriétaire ne peut pas chercher sur ce logement
        intruder = register(client)
        assert client.get(f"/api/properties/{pid}/pois/search?q=x",
                          headers=intruder["headers"]).status_code == 404
    finally:
        app.dependency_overrides.pop(get_poi_searcher, None)


def test_poi_search_function_biases_and_guesses(client):
    """Test unitaire de poi_search.search avec un transport httpx simulé : biais
    viewbox, User-Agent requis, mappage class/type, extratags → téléphone/site."""
    from api import poi_search

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json=[{
            "lat": "37.978", "lon": "-0.682", "class": "amenity", "type": "restaurant",
            "name": "El Meson de la Costa", "display_name": "El Meson…, Torrevieja",
            "extratags": {"contact:phone": "+34 966 11 22 33",
                          "website": "https://meson.example"}}])

    settings.politeness_delay_s = 0
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = poi_search.search("Meson", PROP_LAT, PROP_LON, client=c)
    assert "viewbox=" in captured["url"] and "extratags=1" in captured["url"]
    assert captured["ua"] == settings.user_agent          # politesse : UA obligatoire
    assert out[0]["category_code"] == "restaurant"
    assert out[0]["phone"] == "+34 966 11 22 33"
    assert out[0]["website"] == "https://meson.example"
    # Mappage direct : class/type inconnu → repli 'sight'
    assert poi_search.guess_category("tourism", "artwork") == "sight"
    assert poi_search.guess_category("aeroway", "aerodrome") == "airport"


def test_create_manual_poi_computes_distances_shows_in_guide_off_quota(client):
    """Création manuelle : distances calculées à l'insertion, source='owner',
    status='approved', visible dans le guide publié, et AUCUN job d'enrichissement
    créé (hors quota)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"lat": PROP_LAT, "lon": PROP_LON})

    body = {"category_code": "restaurant", "name": "El Meson de la Costa",
            "lat": 37.9290, "lon": -0.7470, "address": "Av. de la Costa, Torrevieja",
            "phone": "+34 966 00 00 00", "cuisine": "seafood",
            "owner_comment": "Notre cantine à Torrevieja"}
    r = client.post(f"/api/properties/{pid}/pois", headers=owner["headers"], json=body)
    assert r.status_code == 201, r.text
    poi = r.json()
    assert poi["source"] == "owner" and poi["status"] == "approved"
    assert poi["map_color"] == "#EF6C00"                  # catégorie jointe (chapitre F)
    # Distances calculées à l'insertion (computer de test = haversine × 1,3)
    expected = round(haversine_m(PROP_LAT, PROP_LON, 37.9290, -0.7470) * 1.3)
    assert poi["dist_walk_m"] == expected and poi["walk_min"] >= 1

    # Hors quota : la création manuelle n'ouvre AUCUN job d'enrichissement
    # (vérifié avant toute publication, qui elle enfile une traduction).
    with psycopg.connect(settings.db_dsn) as conn:
        n_jobs = conn.execute("SELECT count(*) FROM enrichment_jobs "
                              "WHERE property_id = %s", (pid,)).fetchone()[0]
    assert n_jobs == 0

    # Apparaît dans « Retenus » (approved) de l'écran de validation
    kept = client.get(f"/api/properties/{pid}/pois?status=approved",
                      headers=owner["headers"]).json()
    assert any(p["name"] == "El Meson de la Costa" for p in kept)

    # Publié → visible dans le guide voyageur comme les autres POI
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})
    data = client.get(f"/g/{token}/data").json()
    meson = next(p for p in data["pois"] if p["name"] == "El Meson de la Costa")
    assert meson["cuisine"] == "seafood" and meson["owner_comment"]


def test_undo_restores_previous_poi_status(client):
    """M-23 : l'annulation d'un Approuver/Rejeter restaure le statut précédent
    (y compris 'suggested') via POST /pois/{id}/status."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    _enrich_and_wait(client, owner["headers"], pid)
    poi = client.get(f"/api/properties/{pid}/pois?status=suggested",
                     headers=owner["headers"]).json()[0]
    pid_poi = poi["id"]

    # Approuver puis annuler → retour à 'suggested'
    assert client.post(f"/api/properties/{pid}/pois/{pid_poi}/approve",
                       headers=owner["headers"]).json()["status"] == "approved"
    back = client.post(f"/api/properties/{pid}/pois/{pid_poi}/status",
                       headers=owner["headers"], json={"status": "suggested"})
    assert back.status_code == 200 and back.json()["status"] == "suggested"

    # Statut invalide → 422 ; POI d'un autre propriétaire → 404
    assert client.post(f"/api/properties/{pid}/pois/{pid_poi}/status",
                       headers=owner["headers"], json={"status": "bogus"}).status_code == 422
    intruder = register(client)
    assert client.post(f"/api/properties/{pid}/pois/{pid_poi}/status",
                       headers=intruder["headers"],
                       json={"status": "suggested"}).status_code == 404


def test_create_manual_poi_rejects_unknown_category(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    r = client.post(f"/api/properties/{pid}/pois", headers=owner["headers"],
                    json={"category_code": "not_a_category", "name": "X",
                          "lat": 37.9, "lon": -0.7})
    assert r.status_code == 422


def test_create_manual_poi_without_property_position(client):
    """Sans position du logement, le POI est créé mais sans distances (jamais
    d'erreur : le propriétaire peut recalculer après avoir placé le logement)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]  # logement non positionné
    r = client.post(f"/api/properties/{pid}/pois", headers=owner["headers"],
                    json={"category_code": "beach", "name": "Playa de la Zenia",
                          "lat": 37.93, "lon": -0.735})
    assert r.status_code == 201
    assert r.json()["walk_min"] is None and r.json()["source"] == "owner"


# ── Guide public (invariants 4 et 5) ─────────────────────────────────────────

def test_public_guide(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]

    # Un guide non publié n'est pas servi (on ne révèle pas son existence).
    # La page HTML répond 404 « propre » (pas d'erreur JSON) ; /data aussi.
    draft = client.get(f"/g/{token}")
    assert draft.status_code == 404
    assert "text/html" in draft.headers["content-type"]
    assert client.get(f"/g/{token}/data").status_code == 404

    _enrich_and_wait(client, owner["headers"], pid)
    pois = client.get(f"/api/properties/{pid}/pois", headers=owner["headers"]).json()
    by_name = {p["name"]: p for p in pois}

    # Arbitrage : hôpital approuvé, restaurant édité, un supermarché rejeté,
    # l'autre reste 'suggested' (donc absent du guide)
    client.post(f"/api/properties/{pid}/pois/{by_name['Hospital Universitario de Torrevieja']['id']}/approve",
                headers=owner["headers"])
    client.patch(f"/api/properties/{pid}/pois/{by_name['La Marejada']['id']}",
                 headers=owner["headers"], json={"owner_comment": "Coup de cœur"})
    client.post(f"/api/properties/{pid}/pois/{by_name['Lidl']['id']}/reject",
                headers=owner["headers"])

    # Sections : une visible, une masquée
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "16:00"}, "body_md": "Arrivée dès 16h",
                     "is_visible": True, "completed": True})
    client.put(f"/api/properties/{pid}/sections/A_keybox", headers=owner["headers"],
               json={"content": {"location": "Sous le pot"}, "is_visible": False})
    # A_arrival visible → héberge l'adresse & le GPS copiables (M-19)
    client.put(f"/api/properties/{pid}/sections/A_arrival", headers=owner["headers"],
               json={"content": {"from_airport": "Prenez l'AP-7"}, "is_visible": True})

    # Secrets renseignés — ne doivent JAMAIS apparaître dans le guide public
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "VillaMarAzul", "wifi_pass": "MotDePasseUltraSecret"})

    # Publication
    pub = client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                       json={"status": "published"})
    assert pub.status_code == 200

    # ── Page HTML voyageur (M-08) ─────────────────────────────────────────────
    page = client.get(f"/g/{token}")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert page.headers["X-Robots-Tag"].startswith("noindex")
    assert "max-age" in page.headers.get("Cache-Control", "")
    assert "Villa Mar Azul" in page.text                 # nom du logement rendu
    assert "Check-in" in page.text                       # section visible rendue (SSR)
    assert "Sous le pot" not in page.text                # section A_keybox masquée : absente du HTML
    # Aucun secret dans la page HTML (déchiffrement réservé à /secrets)
    assert "MotDePasseUltraSecret" not in page.text
    # M-19 : adresse & GPS copiables rendus dans A_arrival (position géocodée)
    assert 'class="arrival-meta"' in page.text
    assert 'data-copy="Calle Ejemplo 1, Orihuela Costa"' in page.text
    assert 'data-copy="37.928000, -0.748200"' in page.text  # GPS 6 décimales

    # ── Guide JSON (/data) : charset explicite, sans secret ──────────────────
    g = client.get(f"/g/{token}/data")
    assert g.status_code == 200
    assert g.headers["content-type"] == "application/json; charset=utf-8"
    assert g.headers["X-Robots-Tag"].startswith("noindex")
    data = g.json()

    # Infos logement, sans secret
    assert data["property"]["name"] == "Villa Mar Azul"
    assert "MotDePasseUltraSecret" not in g.text
    assert "wifi_pass" not in g.text and "keybox_code" not in g.text

    # POI : approuvé + édité présents ; rejeté & suggéré absents
    names = {p["name"]: p for p in data["pois"]}
    assert set(names) == {"Hospital Universitario de Torrevieja", "La Marejada"}
    assert names["Hospital Universitario de Torrevieja"]["status"] == "approved"
    assert names["La Marejada"]["status"] == "edited"
    assert names["La Marejada"]["owner_comment"] == "Coup de cœur"
    # Chaque POI porte la catégorie (icône/couleur du seed) et ses distances
    assert names["Hospital Universitario de Torrevieja"]["map_color"] == "#C62828"
    assert names["La Marejada"]["walk_min"]

    # Sections : la visible seulement
    codes = {s["code"] for s in data["sections"]}
    assert "A_checkin" in codes and "A_keybox" not in codes

    # area_facts locaux présents (urgences, tri, bruit)
    assert set(data["area_facts"]) == {"emergency_numbers", "waste_rules", "noise_rules"}
    assert data["area_facts"]["emergency_numbers"]["items"][0]["number"] == "112"

    # ── Secrets (mode 'link') servis à la demande, jamais dans /data ni le HTML ─
    s = client.get(f"/g/{token}/secrets")
    assert s.status_code == 200
    assert s.headers["content-type"] == "application/json; charset=utf-8"
    assert "no-store" in s.headers.get("Cache-Control", "")   # jamais mis en cache HTTP partagé
    secrets = s.json()
    assert secrets["wifi_ssid"] == "VillaMarAzul"
    assert secrets["wifi_pass"] == "MotDePasseUltraSecret"


# ── Quota : les jobs en échec ne comptent pas (M-01, tâche 3) ────────────────

def test_quota_excludes_failed_jobs(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("INSERT INTO enrichment_jobs (property_id, trigger, status) "
                     "VALUES (%s, 'manual', 'failed')", (pid,))
        conn.execute("INSERT INTO enrichment_jobs (property_id, trigger, status) "
                     "VALUES (%s, 'manual', 'failed')", (pid,))
        conn.execute("INSERT INTO enrichment_jobs (property_id, trigger, status) "
                     "VALUES (%s, 'manual', 'done')", (pid,))
        conn.commit()
    # Seul le job 'done' est décompté ; les deux 'failed' sont ignorés
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        assert repo.count_jobs_current_month(conn, pid) == 1


# ── Requalification des jobs orphelins au démarrage (M-01, tâche 4) ──────────

def test_startup_requeues_orphan_running_jobs(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    with psycopg.connect(settings.db_dsn) as conn:
        row = conn.execute(
            "INSERT INTO enrichment_jobs (property_id, trigger, status, started_at) "
            "VALUES (%s, 'manual', 'running', now()) RETURNING id", (pid,)).fetchone()
        conn.commit()
    job_id = str(row[0])

    # Entrer dans le context manager déclenche l'événement de démarrage (lifespan)
    with TestClient(app):
        pass

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT status, error FROM enrichment_jobs WHERE id = %s",
                           (job_id,)).fetchone()
    assert job["status"] == "failed"
    assert job["error"] == "interrompu par redémarrage"


# ── Encodage : le guide public préserve l'UTF-8 (M-01, tâche 7) ──────────────

def test_public_guide_preserves_utf8(client):
    """Le stockage est en UTF-8 (vérifié en base) ; on garde une garde de
    non-régression sur le chemin d'export (l'endpoint public sert le JSON)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom,
                                 description_md, source, status)
               VALUES (%s, 'beach', 'Playa Cala Bosque',
                       ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                       'Petite plage de sable encadrée de rochers, idéale pour la baignade.',
                       'owner', 'approved')""",
            (pid, PROP_LON, PROP_LAT))
        conn.commit()
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    # Page HTML : UTF-8 préservé, charset déclaré
    page = client.get(f"/g/{token}")
    assert page.status_code == 200
    assert "charset=utf-8" in page.headers["content-type"]
    assert "encadrée" in page.text and "�" not in page.text

    # JSON public : charset explicite (mojibake Safari) et contenu intact
    g = client.get(f"/g/{token}/data")
    assert g.status_code == 200
    assert g.headers["content-type"] == "application/json; charset=utf-8"
    assert "�" not in g.text            # aucun caractère de remplacement
    desc = g.json()["pois"][0]["description_md"]
    assert "encadrée" in desc and "�" not in desc


# ── Indicateurs de complétude et de POI (back-office M-03/M-04) ───────────────

def test_property_stats(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]

    # Logement vierge : aucune section complétée, aucun POI
    s0 = client.get(f"/api/properties/{pid}/stats", headers=owner["headers"])
    assert s0.status_code == 200
    assert s0.json()["completion_pct"] == 0
    assert s0.json()["sections_total"] == 44  # catalogue complet du seed
    assert s0.json()["pois_total"] == 0

    # Deux sections complétées puis enrichissement (4 POI suggérés)
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "16:00"}, "completed": True})
    client.put(f"/api/properties/{pid}/sections/B_wifi", headers=owner["headers"],
               json={"content": {"router_location": "Salon"}, "completed": True})
    _enrich_and_wait(client, owner["headers"], pid)

    s1 = client.get(f"/api/properties/{pid}/stats", headers=owner["headers"]).json()
    assert s1["sections_done"] == 2
    assert s1["completion_pct"] == round(2 / 44 * 100)
    assert s1["pois_total"] == 4 and s1["pois_suggested"] == 4

    # Un autre propriétaire n'accède pas aux indicateurs (isolation)
    intruder = register(client)
    assert client.get(f"/api/properties/{pid}/stats",
                      headers=intruder["headers"]).status_code == 404


def test_list_pois_carries_category_metadata(client):
    """L'écran de validation reçoit libellé, icône et couleur de chaque POI."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    _enrich_and_wait(client, owner["headers"], pid)

    pois = client.get(f"/api/properties/{pid}/pois", headers=owner["headers"]).json()
    hosp = next(p for p in pois if p["category_code"] == "hospital")
    assert hosp["map_color"] == "#C62828"
    assert hosp["chapter"] == "D"
    assert hosp["category_name"]["fr"] == "Hôpital"
    assert hosp["category_icon"] == "cross"


# ── Recalcul des distances après repositionnement manuel (M-05) ──────────────

def test_recompute_distances_after_manual_move(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    _enrich_and_wait(client, owner["headers"], pid)

    # Le propriétaire déplace le point du logement (placement manuel)
    moved_lat, moved_lon = PROP_LAT + 0.05, PROP_LON + 0.05
    patch = client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                         json={"lat": moved_lat, "lon": moved_lon})
    assert patch.status_code == 200
    assert patch.json()["geocode_source"] == "manual"

    # Recalcul des distances de tous les POI depuis la nouvelle position
    rc = client.post(f"/api/properties/{pid}/recompute-distances",
                     headers=owner["headers"])
    assert rc.status_code == 200
    assert rc.json()["updated"] == 4  # les 4 POI suggérés

    # Les distances reflètent la nouvelle origine (recalcul hermétique haversine)
    pois = client.get(f"/api/properties/{pid}/pois", headers=owner["headers"]).json()
    hosp = next(p for p in pois if p["category_code"] == "hospital")
    expected = round(haversine_m(moved_lat, moved_lon, hosp["lat"], hosp["lon"]) * 1.3)
    assert hosp["dist_walk_m"] == expected

    # Sans position, le recalcul est refusé proprement ; isolation respectée
    intruder = register(client)
    assert client.post(f"/api/properties/{pid}/recompute-distances",
                       headers=intruder["headers"]).status_code == 404


def test_recompute_distances_without_position(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]  # jamais géocodé -> pas de geom
    rc = client.post(f"/api/properties/{pid}/recompute-distances",
                     headers=owner["headers"])
    assert rc.status_code == 409


# ── Médias par section : upload, service, isolation, visibilité (M-12) ────────

def _png_bytes(color=(200, 60, 60), size=(12, 12)) -> bytes:
    """Petit PNG valide (magic bytes corrects), généré via Pillow."""
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, headers, pid, *, data=None, filename="photo.png",
            content_type="image/png", section_code=None, caption=None):
    files = {"file": (filename, data if data is not None else _png_bytes(), content_type)}
    form = {}
    if section_code is not None:
        form["section_code"] = section_code
    if caption is not None:
        form["caption"] = caption
    return client.post(f"/api/properties/{pid}/media", headers=headers,
                       files=files, data=form)


def test_media_upload_list_and_serve(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]

    r = _upload(client, owner["headers"], pid, section_code="A_keybox",
                caption="Boîte à clés, à gauche de la porte")
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["kind"] == "photo" and m["section_code"] == "A_keybox"
    assert m["caption"].startswith("Boîte à clés")
    assert m["sort_order"] == 0

    # Liste filtrée par section
    lst = client.get(f"/api/properties/{pid}/media?section_code=A_keybox",
                     headers=owner["headers"]).json()
    assert len(lst) == 1 and lst[0]["id"] == m["id"]
    # Section sans média -> liste vide
    assert client.get(f"/api/properties/{pid}/media?section_code=A_checkin",
                      headers=owner["headers"]).json() == []

    # Service du fichier au propriétaire (image ré-encodée => toujours un PNG)
    f = client.get(m["url"], headers=owner["headers"])
    assert f.status_code == 200
    assert f.headers["content-type"] == "image/png"
    assert f.content[:8] == b"\x89PNG\r\n\x1a\n"

    # Mise à jour de la légende
    upd = client.patch(f"/api/properties/{pid}/media/{m['id']}",
                       headers=owner["headers"], json={"caption": "Nouvelle légende"})
    assert upd.status_code == 200 and upd.json()["caption"] == "Nouvelle légende"

    # Suppression
    assert client.delete(f"/api/properties/{pid}/media/{m['id']}",
                         headers=owner["headers"]).status_code == 204
    assert client.get(f"/api/properties/{pid}/media",
                      headers=owner["headers"]).json() == []


def test_media_rejects_bad_type(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    r = _upload(client, owner["headers"], pid, data=b"ceci n'est pas une image",
                filename="notes.txt", content_type="text/plain")
    assert r.status_code == 415, r.text
    # Un Content-Type mensonger ne suffit pas : le type réel est sniffé
    r2 = _upload(client, owner["headers"], pid, data=b"pas un vrai PNG",
                 filename="fake.png", content_type="image/png")
    assert r2.status_code == 415


def test_media_rejects_too_large(client, monkeypatch):
    from api.config import settings as api_settings
    monkeypatch.setattr(api_settings, "max_upload_bytes", 512)
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    big = _png_bytes(size=(200, 200))
    assert len(big) > 512
    r = _upload(client, owner["headers"], pid, data=big)
    assert r.status_code == 413, r.text


def test_media_isolation(client):
    alice = register(client)
    bob = register(client)
    prop = make_property(client, alice["headers"])
    pid = prop["id"]
    up = _upload(client, alice["headers"], pid, section_code="A_keybox")
    assert up.status_code == 201
    mid = up.json()["id"]

    # Bob n'atteint ni la liste, ni l'upload, ni le fichier, ni la suppression
    assert client.get(f"/api/properties/{pid}/media",
                      headers=bob["headers"]).status_code == 404
    assert _upload(client, bob["headers"], pid).status_code == 404
    assert client.get(f"/api/properties/{pid}/media/{mid}/file",
                      headers=bob["headers"]).status_code == 404
    assert client.delete(f"/api/properties/{pid}/media/{mid}",
                         headers=bob["headers"]).status_code == 404
    # Le média d'Alice est intact
    assert len(client.get(f"/api/properties/{pid}/media",
                          headers=alice["headers"]).json()) == 1


def test_media_reorder(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    ids = [_upload(client, owner["headers"], pid, section_code="A_keybox").json()["id"]
           for _ in range(3)]
    reordered = list(reversed(ids))
    r = client.post(f"/api/properties/{pid}/media/reorder", headers=owner["headers"],
                    json={"ids": reordered})
    assert r.status_code == 200
    order = {m["id"]: m["sort_order"] for m in r.json()}
    assert order[reordered[0]] == 0 and order[reordered[2]] == 2


def test_media_public_guide_visibility(client):
    """Un média n'apparaît dans le guide public que si le logement est publié et
    sa section visible ; jamais pour une section masquée."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]

    # Média sur une section visible, une masquée, et un média au niveau logement
    vis = _upload(client, owner["headers"], pid, section_code="A_checkin",
                  caption="Façade").json()
    hid = _upload(client, owner["headers"], pid, section_code="A_keybox").json()
    gen = _upload(client, owner["headers"], pid).json()  # sans section

    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "16:00"}, "is_visible": True,
                     "completed": True})
    client.put(f"/api/properties/{pid}/sections/A_keybox", headers=owner["headers"],
               json={"content": {"location": "Sous le pot"}, "is_visible": False})

    # Avant publication : le média public n'est pas servi
    assert client.get(f"/g/{token}/media/{vis['id']}").status_code == 404

    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    g = client.get(f"/g/{token}/data")
    assert g.status_code == 200
    data = g.json()
    # La section visible porte son média ; la masquée est absente des sections
    sec = {s["code"]: s for s in data["sections"]}
    assert "A_checkin" in sec and "A_keybox" not in sec
    assert [m["id"] for m in sec["A_checkin"]["media"]] == [vis["id"]]
    # Le média au niveau logement est exposé
    assert [m["id"] for m in data["media"]] == [gen["id"]]

    # Service public : visible OK, masqué 404 (on ne révèle rien)
    fv = client.get(f"/g/{token}/media/{vis['id']}")
    assert fv.status_code == 200 and fv.headers["content-type"] == "image/png"
    assert fv.headers["X-Robots-Tag"].startswith("noindex")
    assert client.get(f"/g/{token}/media/{hid['id']}").status_code == 404


# ── Guide voyageur PWA (M-08) ────────────────────────────────────────────────

def test_guide_page_404_is_clean_html(client):
    """Token inconnu → page 404 HTML propre (jamais une trace JSON brute)."""
    r = client.get("/g/tokeninexistant0000")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert r.headers["X-Robots-Tag"].startswith("noindex")
    assert "Guide introuvable" in r.text


def test_guide_secrets_only_link_mode(client):
    """Les secrets ne sont servis qu'en mode d'accès 'link' (MVP). En mode 'pin'
    (V2), l'endpoint renvoie un objet vide sans rien divulguer."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "Reseau", "wifi_pass": "secret-wifi-xyz",
                     "keybox_code": "4321"})
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    # Mode 'link' : secrets déchiffrés
    s = client.get(f"/g/{token}/secrets").json()
    assert s["wifi_pass"] == "secret-wifi-xyz" and s["keybox_code"] == "4321"

    # Bascule en mode 'pin' → plus aucun secret exposé publiquement
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"access_mode": "pin"})
    s2 = client.get(f"/g/{token}/secrets")
    assert s2.status_code == 200
    body = s2.json()
    assert body == {"wifi_networks": [], "wifi_ssid": None, "wifi_pass": None,
                    "keybox_code": None, "keybox_notes": None}
    assert "secret-wifi-xyz" not in s2.text

    # Guide non publié : 404 même en mode 'link'
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"access_mode": "link", "status": "draft"})
    # (le token reste valide mais le guide n'est plus publié)
    assert client.get(f"/g/{token}/secrets").json()["wifi_pass"] is None


# ── Multi-wifi (M-15) ────────────────────────────────────────────────────────

def test_multi_wifi_roundtrip_encrypted_and_legacy_mirror(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    nets = [{"label": "Maison", "ssid": "Villa-Interieur", "pass": "dedans-2026"},
            {"label": "Terrasse", "ssid": "Villa-Exterieur", "pass": "dehors-2026"}]
    r = client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
                   json={"wifi_networks": nets})
    assert r.status_code == 200
    out = r.json()
    # La liste revient telle quelle (clé « pass »).
    assert [(n["label"], n["ssid"], n["pass"]) for n in out["wifi_networks"]] == [
        ("Maison", "Villa-Interieur", "dedans-2026"),
        ("Terrasse", "Villa-Exterieur", "dehors-2026")]
    # Champs legacy alimentés depuis le réseau n°1 (rétrocompat).
    assert out["wifi_ssid"] == "Villa-Interieur" and out["wifi_pass"] == "dedans-2026"

    # GET renvoie la même liste.
    g = client.get(f"/api/properties/{pid}/secrets", headers=owner["headers"]).json()
    assert [n["label"] for n in g["wifi_networks"]] == ["Maison", "Terrasse"]
    assert g["wifi_pass"] == "dedans-2026"

    # Invariant 5 : rien en clair en base (ni pass, ni SSID chiffré via la liste).
    with psycopg.connect(settings.db_dsn) as conn:
        row = conn.execute(
            "SELECT wifi_networks_enc, wifi_pass_enc FROM property_secrets "
            "WHERE property_id = %s", (pid,)).fetchone()
    blob = bytes(row[0])
    assert blob and b"dedans-2026" not in blob and b"dehors-2026" not in blob
    assert bytes(row[1]) and b"dedans-2026" not in bytes(row[1])


def test_legacy_single_wifi_becomes_network_one(client):
    """Donnée pré-migration (colonnes legacy, wifi_networks_enc NULL) : l'ancien
    wifi devient le réseau n°1 « Wifi » sans re-saisie (M-15)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    # Simule une ligne écrite AVANT M-15 : seulement les colonnes historiques.
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            "INSERT INTO property_secrets (property_id, wifi_ssid, wifi_pass_enc) "
            "VALUES (%s, %s, %s)",
            (pid, "AncienReseau", crypto.encrypt("ancien-mdp-123")))
        conn.commit()

    g = client.get(f"/api/properties/{pid}/secrets", headers=owner["headers"]).json()
    assert g["wifi_networks"] == [
        {"label": "Wifi", "ssid": "AncienReseau", "pass": "ancien-mdp-123"}]
    assert g["wifi_ssid"] == "AncienReseau" and g["wifi_pass"] == "ancien-mdp-123"


def test_backward_compat_single_wifi_fields_still_accepted(client):
    """Un client qui n'envoie que wifi_ssid/wifi_pass (ancien format) fonctionne :
    le wifi devient le réseau n°1 (M-15, rétrocompat PUT)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "SoloNet", "wifi_pass": "solo-pass"})
    g = client.get(f"/api/properties/{pid}/secrets", headers=owner["headers"]).json()
    assert [n["label"] for n in g["wifi_networks"]] == ["Wifi"]
    assert g["wifi_networks"][0]["ssid"] == "SoloNet"
    assert g["wifi_pass"] == "solo-pass"


def test_guide_public_secrets_expose_wifi_networks(client):
    """Le guide voyageur (mode 'link') reçoit la liste multi-wifi ; jamais dans /data."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    nets = [{"label": "Maison", "ssid": "In", "pass": "in-pass-99"},
            {"label": "Jardin", "ssid": "Out", "pass": "out-pass-99"}]
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_networks": nets})
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    s = client.get(f"/g/{token}/secrets").json()
    assert [(n["label"], n["ssid"], n["pass"]) for n in s["wifi_networks"]] == [
        ("Maison", "In", "in-pass-99"), ("Jardin", "Out", "out-pass-99")]
    assert s["wifi_pass"] == "in-pass-99"  # rétrocompat : réseau n°1

    # Jamais dans le JSON public /data ni la page HTML.
    assert "in-pass-99" not in client.get(f"/g/{token}/data").text
    assert "out-pass-99" not in client.get(f"/g/{token}").text


def test_guide_manifest_is_per_guide(client):
    owner = register(client)
    prop = make_property(client, owner["headers"], name="Casa Bonita")
    pid, token = prop["id"], prop["guide_token"]
    assert client.get(f"/g/{token}/manifest.webmanifest").status_code == 404  # brouillon
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})
    m = client.get(f"/g/{token}/manifest.webmanifest")
    assert m.status_code == 200
    assert "application/manifest+json" in m.headers["content-type"]
    man = m.json()
    assert man["start_url"] == f"/g/{token}" and man["scope"] == f"/g/{token}"
    assert "Casa Bonita" in man["name"]
    assert any(ic["src"].startswith("/guide/icon") for ic in man["icons"])


def test_service_worker_scope_header(client):
    """Le service worker est servi avec Service-Worker-Allowed: / (portée '/g/…')."""
    r = client.get("/guide/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert "addEventListener" in r.text


# ── Versionnage des assets & cache-busting (M-11) ─────────────────────────────

def test_index_injects_asset_version(client, monkeypatch):
    """La page d'accueil du back-office porte ?v=<sha> sur ses assets locaux et
    n'est jamais mise en cache dur (revalidation)."""
    monkeypatch.setenv("CASAGUIDE_ASSET_VERSION", "abc1234")
    r = client.get("/")
    assert r.status_code == 200
    assert "css/app.css?v=abc1234" in r.text
    assert "js/app.js?v=abc1234" in r.text
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_static_assets_revalidate(client):
    """Les assets statiques du front sont servis en no-cache (revalidation ETag)
    → un module modifié est toujours re-téléchargé après déploiement."""
    r = client.get("/css/app.css")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_service_worker_carries_deploy_version(client, monkeypatch):
    """Le SHA du déploiement est injecté dans le nom des caches du service worker
    (le placeholder est toujours remplacé → busting auto à chaque déploiement)."""
    monkeypatch.setenv("CASAGUIDE_ASSET_VERSION", "deadbee")
    r = client.get("/guide/sw.js")
    assert r.status_code == 200
    assert "__ASSET_VERSION__" not in r.text
    assert "deadbee" in r.text


def test_guide_page_versions_local_assets(client):
    """La page guide voyageur (SSR) porte ?v=<sha> sur guide.css et app.js."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "16:00"}, "is_visible": True,
                     "completed": True})
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})
    r = client.get(f"/g/{token}")
    assert r.status_code == 200
    assert "/guide/guide.css?v=" in r.text
    assert "/guide/app.js?v=" in r.text


# ── Cahier « équipe d'entretien » + étanchéité guest/staff (M-13) ─────────────

def test_staff_and_guest_are_watertight_both_ways(client):
    """INVARIANT 7 : les sections 'staff' ne sortent JAMAIS sur /g ni /g/data ;
    les sections 'guest' ne sortent JAMAIS sur /s. Testé dans les deux sens."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, gtoken, staff_token = prop["id"], prop["guide_token"], prop["staff_token"]
    # Le staff_token est un second lien secret, distinct du lien voyageur
    assert len(staff_token) >= 32 and staff_token != gtoken

    # Contenu voyageur (guest) et contenu équipe d'entretien (staff)
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "16:00"}, "body_md": "Arrivée GUESTMARK",
                     "is_visible": True, "completed": True})
    client.put(f"/api/properties/{pid}/sections/S_checklist", headers=owner["headers"],
               json={"content": {"tasks": [{"task": "Nettoyer STAFFMARK", "details": "robot piscine"}]},
                     "is_visible": True, "completed": True})
    client.put(f"/api/properties/{pid}/sections/S_special", headers=owner["headers"],
               json={"content": {"wishes": "Arroser les plantes WISHMARK"}, "is_visible": True})

    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    # ── Sens 1 : /g et /g/data ne contiennent QUE du 'guest' ─────────────────
    page = client.get(f"/g/{gtoken}")
    assert page.status_code == 200
    assert "GUESTMARK" in page.text
    assert "STAFFMARK" not in page.text and "WISHMARK" not in page.text
    data = client.get(f"/g/{gtoken}/data")
    codes = {s["code"] for s in data.json()["sections"]}
    assert "A_checkin" in codes
    assert not any(c.startswith("S_") for c in codes)  # aucune section staff
    assert "STAFFMARK" not in data.text and "WISHMARK" not in data.text

    # ── Sens 2 : /s ne contient QUE du 'staff' (jamais guest, secrets, POI) ───
    s = client.get(f"/s/{staff_token}")
    assert s.status_code == 200
    assert "text/html" in s.headers["content-type"]
    assert s.headers["X-Robots-Tag"].startswith("noindex")
    assert "charset=utf-8" in s.headers["content-type"]
    assert "STAFFMARK" in s.text and "WISHMARK" in s.text
    assert "Arroser les plantes" in s.text          # accents/UTF-8 préservés
    assert "GUESTMARK" not in s.text                # aucune section voyageur
    assert 'id="map"' not in s.text                 # jamais de carte
    assert "secret-slot" not in s.text              # jamais d'emplacement secret


def test_staff_cahier_accessible_in_draft(client):
    """Le cahier /s est accessible AVANT publication (l'équipe prépare en amont),
    alors que le guide voyageur /g reste 404 tant que le logement est en brouillon."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, gtoken, staff_token = prop["id"], prop["guide_token"], prop["staff_token"]
    assert prop["status"] == "draft"

    client.put(f"/api/properties/{pid}/sections/S_checklist", headers=owner["headers"],
               json={"content": {"tasks": [{"task": "Tâche DRAFTMARK"}]}, "is_visible": True})

    s = client.get(f"/s/{staff_token}")
    assert s.status_code == 200 and "DRAFTMARK" in s.text
    # Le guide voyageur, lui, n'est pas encore servi
    assert client.get(f"/g/{gtoken}").status_code == 404

    # Token inconnu → 404 HTML propre (on ne révèle rien)
    nf = client.get("/s/inconnu00000000000000000000")
    assert nf.status_code == 404
    assert "text/html" in nf.headers["content-type"]
    assert "introuvable" in nf.text.lower()


def test_staff_cahier_never_exposes_secrets(client):
    """Même renseignés, wifi et code boîte à clés n'apparaissent jamais sur /s."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, staff_token = prop["id"], prop["staff_token"]
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "Reseau", "wifi_pass": "MotDePasseStaffXYZ",
                     "keybox_code": "9137"})
    client.put(f"/api/properties/{pid}/sections/S_checklist", headers=owner["headers"],
               json={"content": {"tasks": [{"task": "x"}]}, "is_visible": True})
    s = client.get(f"/s/{staff_token}")
    assert s.status_code == 200
    assert "MotDePasseStaffXYZ" not in s.text and "9137" not in s.text


def test_staff_media_watertight(client):
    """Un média de section 'staff' est servi sur /s mais JAMAIS sur /g, et
    n'apparaît pas dans /g/data (invariant 7 étendu aux médias, M-12)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, gtoken, staff_token = prop["id"], prop["guide_token"], prop["staff_token"]

    sm = _upload(client, owner["headers"], pid, section_code="S_welcome_pack",
                 caption="Panier de bienvenue").json()
    client.put(f"/api/properties/{pid}/sections/S_welcome_pack", headers=owner["headers"],
               json={"content": {"contents": "café + eau + petit mot"}, "is_visible": True})
    client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                 json={"status": "published"})

    # Servi sur le cahier staff
    fs = client.get(f"/s/{staff_token}/media/{sm['id']}")
    assert fs.status_code == 200 and fs.headers["content-type"] == "image/png"
    # Jamais servi sur le guide voyageur (média d'une section staff)
    assert client.get(f"/g/{gtoken}/media/{sm['id']}").status_code == 404
    # Absent des données publiques voyageur (ni section staff, ni média logement)
    data = client.get(f"/g/{gtoken}/data").json()
    assert not any(c.startswith("S_") for c in {s["code"] for s in data["sections"]})
    assert data["media"] == []


def test_property_stats_excludes_staff_sections(client):
    """La complétude du guide (dashboard) ne compte QUE les sections voyageur :
    compléter une section staff ne fait pas bouger le pourcentage voyageur."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    # 44 sections voyageur au catalogue ; les 5 sections staff n'y comptent pas
    s0 = client.get(f"/api/properties/{pid}/stats", headers=owner["headers"]).json()
    assert s0["sections_total"] == 44

    # Compléter une section staff ne change ni le total ni le pourcentage voyageur
    client.put(f"/api/properties/{pid}/sections/S_checklist", headers=owner["headers"],
               json={"content": {"tasks": [{"task": "x"}]}, "completed": True, "is_visible": True})
    s1 = client.get(f"/api/properties/{pid}/stats", headers=owner["headers"]).json()
    assert s1["sections_total"] == 44
    assert s1["sections_done"] == 0 and s1["completion_pct"] == 0


# ── Affiche « QR code à imprimer » (M-07) ────────────────────────────────────

def test_guide_poster_pdf(client):
    owner = register(client)
    prop = make_property(client, owner["headers"], name="Villa Mar Azul")
    pid = prop["id"]

    r = client.get(f"/api/properties/{pid}/guide-poster.pdf", headers=owner["headers"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content[:5] == b"%PDF-"

    # Variante A4
    r4 = client.get(f"/api/properties/{pid}/guide-poster.pdf?size=a4",
                    headers=owner["headers"])
    assert r4.status_code == 200 and r4.content[:5] == b"%PDF-"

    # Taille non prévue → 422 (validation Literal)
    assert client.get(f"/api/properties/{pid}/guide-poster.pdf?size=a3",
                      headers=owner["headers"]).status_code == 422

    # Réservé au propriétaire du logement (isolation multi-tenant) + auth requise
    intruder = register(client)
    assert client.get(f"/api/properties/{pid}/guide-poster.pdf",
                      headers=intruder["headers"]).status_code == 404
    assert client.get(f"/api/properties/{pid}/guide-poster.pdf").status_code == 401


def test_guide_poster_qr_encodes_public_guide_link(client, monkeypatch):
    """Le QR encode le lien PUBLIC du guide (/g/{token}) — jamais un secret."""
    from api.config import settings as api_settings
    monkeypatch.setattr(api_settings, "public_base_url", "https://casaguide.example")
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]

    # Secrets renseignés : ils ne doivent surtout pas finir dans le PDF
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "Reseau", "wifi_pass": "Poster-Secret-Wifi"})
    r = client.get(f"/api/properties/{pid}/guide-poster.pdf", headers=owner["headers"])
    assert r.status_code == 200
    assert b"Poster-Secret-Wifi" not in r.content     # aucun secret dans le PDF

    # Le QR décode exactement l'URL publique du guide (vérif OpenCV si dispo)
    try:
        import cv2  # noqa: F401
        import numpy as np
        from pdf2image import convert_from_bytes  # noqa: F401
    except Exception:
        return  # dépendances de vérif d'image absentes : on s'arrête au PDF
    _assert_pdf_qr_decodes(r.content, f"https://casaguide.example/g/{token}")


def _assert_pdf_qr_decodes(pdf_bytes: bytes, expected: str) -> None:  # pragma: no cover
    import cv2
    import numpy as np
    from pdf2image import convert_from_bytes
    img = convert_from_bytes(pdf_bytes, dpi=200)[0]
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    data, _pts, _ = cv2.QRCodeDetector().detectAndDecode(arr)
    assert data == expected, f"QR décodé={data!r}"


# ── Multilingue FR/EN/ES (M-09, §9) ──────────────────────────────────────────

def _publish_guide_with_content(client, headers, pid):
    """Enrichit, arbitre des POI, saisit une section visible, puis publie.
    Renvoie une fois la (re)traduction de fond terminée (TestClient synchrone)."""
    _enrich_and_wait(client, headers, pid)
    pois = client.get(f"/api/properties/{pid}/pois", headers=headers).json()
    by_name = {p["name"]: p for p in pois}
    client.post(f"/api/properties/{pid}/pois/{by_name['Hospital Universitario de Torrevieja']['id']}/approve",
                headers=headers)
    # La Marejada : POI 'edited' porteur d'une description IA + d'un coup de cœur
    client.patch(f"/api/properties/{pid}/pois/{by_name['La Marejada']['id']}",
                 headers=headers, json={"owner_comment": "Notre coup de cœur"})
    # Section visible avec texte libre + champ structuré (heure)
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=headers,
               json={"content": {"checkin_from": "16:00"},
                     "body_md": "Arrivée dès 16h, clés dans la boîte.",
                     "is_visible": True, "completed": True})
    pub = client.patch(f"/api/properties/{pid}", headers=headers,
                       json={"status": "published"})
    assert pub.status_code == 200
    return by_name


def test_publish_generates_translations_and_serves_es(client):
    LAST_TRANSLATOR.calls.clear()
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    _publish_guide_with_content(client, owner["headers"], pid)

    # La publication a rempli published_langs (pilote le sélecteur)
    p = client.get(f"/api/properties/{pid}", headers=owner["headers"]).json()
    assert set(p["published_langs"]) == {"en", "es"}

    # ── Page HTML espagnole ───────────────────────────────────────────────────
    es = client.get(f"/g/{token}?lang=es")
    assert es.status_code == 200
    assert 'lang="es"' in es.text
    assert "Tu guía de estancia" in es.text          # libellé fixe localisé
    assert "[es] Arrivée dès 16h, clés dans la boîte." in es.text  # body traduit
    assert "[es] Notre coup de cœur" in es.text       # coup de cœur POI traduit
    # Champ structuré (heure) inchangé — jamais traduit
    assert "16:00" in es.text
    assert "[es] 16:00" not in es.text

    # ── JSON /data espagnol ───────────────────────────────────────────────────
    data = client.get(f"/g/{token}/data?lang=es").json()
    assert data["lang"] == "es"
    checkin = next(s for s in data["sections"] if s["code"] == "A_checkin")
    assert checkin["body_md"] == "[es] Arrivée dès 16h, clés dans la boîte."
    assert checkin["content"]["checkin_from"] == "16:00"   # structuré : intact
    resto = next(x for x in data["pois"] if x["name"] == "La Marejada")
    assert resto["owner_comment"] == "[es] Notre coup de cœur"
    assert resto["description_md"].startswith("[es] ")     # description IA traduite

    # ── Repli élégant : langue non publiée / absente → français, jamais de trou ─
    fr = client.get(f"/g/{token}").text
    assert "Votre guide de séjour" in fr
    assert "Arrivée dès 16h, clés dans la boîte." in fr and "[es]" not in fr
    de = client.get(f"/g/{token}?lang=de").text        # 'de' non publié → fr
    assert 'lang="fr"' in de and "[es]" not in de


def test_translation_cost_recorded_and_no_secret_translated(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    # Secret wifi : ne doit jamais transiter par la traduction ni le guide
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "VillaMarAzul", "wifi_pass": "SecretWifi42"})
    _publish_guide_with_content(client, owner["headers"], pid)

    # Coûts comptabilisés (operation='translate') — invariant 6
    with psycopg.connect(settings.db_dsn) as conn:
        rows = conn.execute(
            "SELECT operation, count(*) AS n FROM api_costs "
            "WHERE property_id = %s GROUP BY operation", (pid,)).fetchall()
    ops = {op: n for op, n in rows}
    assert ops.get("translate", 0) >= 1

    # Aucun secret dans le guide traduit
    es = client.get(f"/g/{token}?lang=es").text
    assert "SecretWifi42" not in es
    # Le secret n'a jamais été soumis au traducteur
    for batch in LAST_TRANSLATOR.calls:
        assert not any("SecretWifi42" in v for v in batch.values())


def test_translation_is_stale_and_retranslation_is_targeted(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]
    _publish_guide_with_content(client, owner["headers"], pid)

    # Après publication : tout est à jour
    st = client.get(f"/api/properties/{pid}/translation-status",
                    headers=owner["headers"]).json()
    assert st["up_to_date"] is True and st["outdated"] == 0
    assert set(st["langs"]) == {"en", "es"}

    # Édition d'une SEULE section → ses traductions deviennent périmées
    client.put(f"/api/properties/{pid}/sections/A_checkin", headers=owner["headers"],
               json={"content": {"checkin_from": "17:00"},
                     "body_md": "Nouvel horaire : arrivée dès 17h.",
                     "is_visible": True, "completed": True})
    st2 = client.get(f"/api/properties/{pid}/translation-status",
                     headers=owner["headers"]).json()
    assert st2["outdated"] > 0
    # La langue es n'est plus totalement à jour (1 section périmée)
    assert st2["langs"]["es"]["stale"] >= 1

    # Le guide es retombe sur le français pour la section périmée (pas d'info obsolète)
    es_stale = client.get(f"/g/{token}?lang=es").text
    assert "Nouvel horaire : arrivée dès 17h." in es_stale   # source fr affichée
    assert "[es] Nouvel horaire" not in es_stale

    # Re-traduction ciblée : ne retraite QUE le périmé
    LAST_TRANSLATOR.calls.clear()
    r = client.post(f"/api/properties/{pid}/translate", headers=owner["headers"])
    assert r.status_code == 202
    # Deux appels au plus (en + es), chacun ne portant QUE le segment ré-édité
    assert len(LAST_TRANSLATOR.calls) <= 2
    for batch in LAST_TRANSLATOR.calls:
        assert all("Nouvel horaire" in v for v in batch.values())

    st3 = client.get(f"/api/properties/{pid}/translation-status",
                     headers=owner["headers"]).json()
    assert st3["up_to_date"] is True
    es_fresh = client.get(f"/g/{token}?lang=es").text
    assert "[es] Nouvel horaire : arrivée dès 17h." in es_fresh


def test_translate_excluded_from_enrich_quota(client):
    """Traduire ne consomme pas le quota d'enrichissement (M-09)."""
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid = prop["id"]
    _enrich_and_wait(client, owner["headers"], pid)          # 1er (et seul) enrichissement du plan free
    # Plusieurs traductions n'entament pas le quota
    for _ in range(3):
        assert client.post(f"/api/properties/{pid}/translate",
                           headers=owner["headers"]).status_code == 202
    # Un second enrichissement reste refusé (quota du plan free = 1)
    assert client.post(f"/api/properties/{pid}/enrich", headers=owner["headers"],
                       json={"trigger": "refresh"}).status_code == 429
