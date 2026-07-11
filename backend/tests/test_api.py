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

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import crypto, repo  # noqa: E402
from api.deps import get_enrichment_runner  # noqa: E402
from api.main import app  # noqa: E402
from enrich import pipeline  # noqa: E402
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


def _test_runner(property_id: str, trigger: str, job_id: str) -> None:
    """Exécuteur d'enrichissement sans réseau, injecté à la place du vrai pipeline."""
    settings.politeness_delay_s = 0
    with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as c:
        pipeline.run(property_id, use_claude=True, trigger=trigger, job_id=job_id,
                     only_categories={"hospital", "supermarket", "restaurant"},
                     http_client=c, anthropic_client=FakeAnthropic())


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    app.dependency_overrides[get_enrichment_runner] = lambda: _test_runner
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


# ── Guide public (invariants 4 et 5) ─────────────────────────────────────────

def test_public_guide(client):
    owner = register(client)
    prop = make_property(client, owner["headers"])
    pid, token = prop["id"], prop["guide_token"]

    # Un guide non publié n'est pas servi (on ne révèle pas son existence)
    assert client.get(f"/g/{token}").status_code == 404

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

    # Secrets renseignés — ne doivent JAMAIS apparaître dans le guide public
    client.put(f"/api/properties/{pid}/secrets", headers=owner["headers"],
               json={"wifi_ssid": "VillaMarAzul", "wifi_pass": "MotDePasseUltraSecret"})

    # Publication
    pub = client.patch(f"/api/properties/{pid}", headers=owner["headers"],
                       json={"status": "published"})
    assert pub.status_code == 200

    # Guide public
    g = client.get(f"/g/{token}")
    assert g.status_code == 200
    assert g.headers["X-Robots-Tag"].startswith("noindex")
    assert "max-age" in g.headers.get("Cache-Control", "")
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

    g = client.get(f"/g/{token}")
    assert g.status_code == 200
    assert "�" not in g.text            # aucun caractère de remplacement
    desc = g.json()["pois"][0]["description_md"]
    assert "encadrée" in desc and "�" not in desc
