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
                  "website": "https://lamarejada.example",
                  "addr:city": "Torrevieja",   # V2-38 : commune voisine (≠ logement)
                  "cuisine": "Seafood;spanish"}},  # M-16 : multi-valué à normaliser
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

def _web_reply(text, *, searches=2):
    """Réponse à surface SDK RÉELLE d'un appel avec recherche web (OPS-1b) : blocs
    text + `usage.server_tool_use.web_search_requests`."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=200,
                              server_tool_use=SimpleNamespace(
                                  web_search_requests=searches)))


class FakeMessages:
    # Le bouchon `food_delivery` renvoie une réponse malformée sur demande (test de
    # rejet sans écriture) et compte ses appels (test de mutualisation). Le bouchon
    # baby-sitting (V2-07 volet 2) renvoie par défaut un service crédible.
    def __init__(self, food_delivery_malformed=False, babysitter_services=None,
                 service_completions=None, markets=None, markets_malformed=False,
                 describe_malformed=False, rentals=None, service_qualifications=None,
                 reputed_places=None, activities=None, local_commerces=None):
        self._service_qualifications = service_qualifications
        # V2-74 : commerces de village découverts par le web (défaut : rien — proof or nothing).
        self.local_commerces = local_commerces or []
        self.local_commerce_calls = 0
        # V2-56 : sélection éditoriale « sorties » (réputés) — deux incontournables
        # par défaut (le cas La Zenia : absents d'OSM), avec contacts + raison.
        self.reputed_places = ([
            {"name": "Brown's Cocktail Bar", "category": "bar",
             "address": "Calle Brown 1, La Zenia",
             "reason": "Cocktails réputés, terrasse animée.",
             "phone": "+34 966 111 222", "website": "https://browns.example",
             "source_url": "https://guide.example/bares", "verified_on": "2026-09-13"},
            {"name": "Casa Manolo", "category": "restaurant",
             "address": "Avenida Manolo 2, La Zenia",
             "reason": "Institution locale pour les arroces.",
             "phone": "+34 966 333 444", "website": "https://casamanolo.example",
             "source_url": "https://guide.example/restos", "verified_on": "2026-09-13"}]
            if reputed_places is None else reputed_places)
        self.food_delivery_malformed = food_delivery_malformed
        self.markets_malformed = markets_malformed
        self.describe_malformed = describe_malformed
        self.food_delivery_calls = 0
        self.market_calls = 0
        self.activities_calls = 0
        # V2-71 : activités du secteur (une crédible avec preuve par défaut). V2-73c :
        # champs structurés place_name/place_city pour le géocodage.
        self.activities = ([{"activity": "Surf", "where": "plage de La Zenia",
                             "place_name": "Playa de La Zenia", "place_city": "Orihuela Costa",
                             "season": "toute l'année",
                             "source_url": "https://turismo.example/surf",
                             "verified_on": "2026-09-15"}]
                           if activities is None else activities)
        self.rental_calls = 0
        self.rentals_malformed = rentals == "malformed"   # V2-44 volet 2 : JSON malformé
        self.rentals = [] if self.rentals_malformed else (rentals or [])
        self.babysitter_services = (
            [{"name": "Canguros Costa", "phone": "+34 966 000 111",
              "website": "https://canguroscosta.example",
              "source_url": "https://canguroscosta.example",
              "verified_on": "2026-08-11"}]
            if babysitter_services is None else babysitter_services)
        self.service_completions = service_completions or {}
        self.service_qualifications = self._service_qualifications   # V2-50 (places JSON)
        # V2-07 volet 3 : un marché crédible AVEC coordonnées (pas de géocodage).
        self.markets = ([{"name": "Mercadillo de La Zenia", "weekday": 6,
                          "hours": "8h00–14h00", "character": "fruits, vêtements",
                          "address": "Plaza de La Zenia", "lat": 37.930, "lon": -0.750,
                          "source_url": "https://orihuela.es/mercadillos",
                          "verified_on": "2026-08-12", "doubtful": False}]
                        if markets is None else markets)

    def create(self, *, model, max_tokens, messages, tools=None, **kwargs):
        prompt = messages[0]["content"]
        if "DÉTECTEUR DE BRUIT" in prompt:  # juge IA du flux POI (V2-54)
            import re
            verdicts = []
            for m in re.finditer(r'- id "([^"]+)" : ([^\n]+)', prompt):
                vid, rest = m.group(1), m.group(2)
                if "Lidl" in rest:   # rejet FRANC (≥ seuil) → écarté d'office
                    verdicts.append({"id": vid, "verdict": "reject",
                                     "confidence": 0.95, "reason": "doublon proche"})
                else:
                    verdicts.append({"id": vid, "verdict": "keep",
                                     "confidence": 0.80, "reason": "ok"})
            return SimpleNamespace(
                content=[SimpleNamespace(type="text",
                                         text=json.dumps({"verdicts": verdicts}))],
                usage=SimpleNamespace(input_tokens=500, output_tokens=200),
                stop_reason="end_turn")
        if "RÉPUTÉES" in prompt:  # sélection éditoriale « sorties » (V2-56)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"places": self.reputed_places}))
        if "BABY-SITTING" in prompt:  # création baby-sitting (V2-07 volet 2)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"services": self.babysitter_services}))
        if "LOUEURS" in prompt:  # découverte web des loueurs (V2-44 volet 2)
            self.rental_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            if self.rentals_malformed:            # JSON tronqué/malformé (robustesse V2-37)
                return _web_reply("désolé, réponse tronquée…")
            return _web_reply(json.dumps({"rentals": self.rentals}))
        if "QUALIFIES des lieux" in prompt:   # règles de service (V2-50)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"places": self.service_qualifications or []}))
        if "MARCHÉS HEBDOMADAIRES" in prompt:  # découverte marchés (V2-07 volet 3)
            self.market_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            if self.markets_malformed:            # JSON tronqué/malformé (volet 3bis)
                return _web_reply("désolé, réponse tronquée…")
            return _web_reply(json.dumps({"markets": self.markets}))
        if "LIEUX DE SERVICE" in prompt:  # complétion tel/site/horaires (volet 2)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps(self.service_completions))
        if "ACTIVITÉS de plein air" in prompt:  # activités du secteur (V2-71)
            self.activities_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"activities": self.activities}))
        if "COMMERCES & SERVICES ESSENTIELS" in prompt:  # commerces de village (V2-74)
            self.local_commerce_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"commerces": self.local_commerces}))
        if '"platforms"' in prompt:  # prompt livraison de repas (recherche web)
            self.food_delivery_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            text = ("désolé, indisponible" if self.food_delivery_malformed
                    else json.dumps({"platforms": [
                        {"name": "Glovo", "url": "https://glovoapp.com/es",
                         "verified_on": "2026-08-11"}], "note": ""}))
            return _web_reply(text)
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
        else:  # prompt descriptions POI (chemin _ask_json, sans web)
            if self.describe_malformed:               # V2-37 1bis : réponse vide tronquée
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="")],
                    usage=SimpleNamespace(input_tokens=800, output_tokens=1),
                    stop_reason="max_tokens")
            payload = {"node/333": "Restaurant de poissons face à la plage, "
                                   "apprécié pour ses arroces."}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=800, output_tokens=350),
            stop_reason="end_turn",
        )


class FakeAnthropic:
    def __init__(self, food_delivery_malformed=False, babysitter_services=None,
                 service_completions=None, markets=None, markets_malformed=False,
                 describe_malformed=False, rentals=None, service_qualifications=None,
                 reputed_places=None, local_commerces=None):
        self.messages = FakeMessages(food_delivery_malformed, babysitter_services,
                                     service_completions, markets, markets_malformed,
                                     describe_malformed, rentals,
                                     service_qualifications=service_qualifications,
                                     reputed_places=reputed_places,
                                     local_commerces=local_commerces)


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
def guest_property_id():
    """Fiche GUIDE VOYAGEUR (V2-54) : guest_guide=TRUE, déjà positionnée (le point est
    ajusté dans le tunnel → le pipeline saute le géocodage)."""
    pid, oid = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        # V2-56c : la mémoire de secteur est CUMULATIVE → repartir vierge pour l'isolation.
        conn.execute("DELETE FROM editorial_picks WHERE country_code = 'ES'")
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'Sys')",
                     (oid, f"{oid}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city,
                                       country_code, guest_guide, geom,
                                       geocode_source, geocode_accuracy)
               VALUES (%s, %s, 'Guide — Orihuela Costa', 'Calle Ejemplo 1',
                       'Orihuela Costa', 'ES', TRUE,
                       ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'manual', 'manual')""",
            (pid, oid, PROP_LON, PROP_LAT))
        conn.commit()
    yield pid
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM owners WHERE id = %s", (oid,))
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        conn.execute("DELETE FROM editorial_picks WHERE country_code = 'ES'")
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

        # POI en 'suggested', avec distances pré-calculées et traçabilité. Les POI
        # OSM (moisson) sont distincts du baby-sitting créé par Claude (V2-07 volet 2).
        pois = conn.execute(
            "SELECT * FROM pois WHERE property_id=%s ORDER BY name",
            (property_id,)).fetchall()
        assert {p["status"] for p in pois} == {"suggested"}
        osm = [p for p in pois if p["category_code"] not in ("babysitter", "market")]
        assert len(osm) == 4
        assert all(p["walk_min"] and p["drive_min"] for p in osm)
        assert all(p["source"] == "osm" and p["source_ref"] for p in osm)

        # Baby-sitting CRÉÉ par Claude + recherche web (V2-07 volet 2) : source
        # 'claude', status 'suggested' (validation propriétaire), téléphone + preuve.
        sitter = next(p for p in pois if p["category_code"] == "babysitter")
        assert sitter["source"] == "claude" and sitter["status"] == "suggested"
        assert sitter["name"] == "Canguros Costa" and sitter["phone"]
        assert sitter["completion_meta"]["_created"]["source_url"].startswith("http")

        # Marché CRÉÉ par Claude + recherche web (V2-07 volet 3) : source 'claude',
        # 'suggested', JOUR (weekday) + note (horaires « indicatifs ») + preuve +
        # position réelle + distances calculées.
        market = next(p for p in pois if p["category_code"] == "market")
        assert market["source"] == "claude" and market["status"] == "suggested"
        assert market["weekday"] == 6 and "Horaires indicatifs" in market["weekday_note"]
        assert market["walk_min"] and market["completion_meta"]["_market"]["source_url"]

        # Description IA appliquée au restaurant uniquement
        resto = next(p for p in pois if p["name"] == "La Marejada")
        assert "arroces" in resto["description_md"]
        # Cuisine récoltée et normalisée (premier terme, minuscules) — M-16
        assert resto["cuisine"] == "seafood"

        # area_facts : 3 lignes historiques + 1 livraison de repas (V2-07),
        # mutualisées ES / Orihuela Costa
        rows = conn.execute("""SELECT fact_type, content FROM area_facts
                            WHERE country_code='ES' AND admin_area='Orihuela Costa'"""
                            ).fetchall()
        facts = {r["fact_type"]: r["content"] for r in rows}
        # + 'markets' : la DÉCOUVERTE des marchés est mutualisée par commune (V2-07
        # volet 3), mise en cache area_facts comme la livraison de repas.
        assert set(facts) == {"emergency_numbers", "waste_rules", "noise_rules",
                              "food_delivery", "markets", "activities"}
        assert facts["markets"]["markets"][0]["weekday"] == 6
        # V2-71 : activités du secteur découvertes (surf) — area_fact mutualisé.
        assert facts["activities"]["activities"][0]["activity"] == "Surf"
        # Plateformes de livraison résolues par zone (nom de marque local + preuve).
        assert facts["food_delivery"]["platforms"][0]["name"] == "Glovo"
        assert facts["food_delivery"]["platforms"][0]["url"].startswith("https://")

        # Job 'done' avec toutes les étapes ok, coûts comptabilisés
        job = conn.execute("SELECT * FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done"
        # OPS-4 Pièce 2 : `steps` est le JOURNAL DE VÉRITÉ — chaque étape IA y figure
        # avec ok + compteurs + coût (plus seulement geocode/overpass/distances/claude).
        steps = job["steps"]
        assert all(steps[s]["ok"] for s in
                   ("geocode", "overpass", "distances", "area_facts", "describe_pois",
                    "food_delivery", "babysitter", "markets", "activities", "claude"))
        assert steps["activities"]["activities"] == 1            # compteur (V2-71)
        assert steps["food_delivery"]["platforms"] == 1          # compteur
        assert steps["babysitter"]["created"] == 1               # compteur
        assert steps["markets"]["discovered"] == 1 and steps["markets"]["created"] == 1
        assert "cost_cts" in steps["food_delivery"]              # coût par étape
        assert steps["overpass"]["failed"] == {}                 # 0 échec (mock)
        # area_facts + describe_pois + food_delivery (recherche web) comptabilisés
        ops = {r["operation"] for r in conn.execute(
            "SELECT operation FROM api_costs WHERE job_id=%s", (result["job_id"],))}
        # + baby-sitting + marchés (volets 2/3). Pas de 'service_complete' : sur un
        # run neuf tous les POI sont 'suggested', la complétion ne vise que les retenus.
        assert ops == {"area_facts", "describe_pois", "food_delivery", "babysitter",
                       "markets", "activities"}


def test_guest_guide_is_auto_judged_and_published(guest_property_id, http_client):
    """Offre Guide Voyageur (V2-54) : le juge arbitre TOUS les POI moissonnés (rejet
    ≥ seuil → 'rejected' motif tracé ; sinon 'approved'), puis la fiche est PUBLIÉE."""
    result = pipeline.run(
        guest_property_id, use_claude=True, trigger="guest",
        only_categories={"hospital", "supermarket", "restaurant"},
        http_client=http_client,
        # reputed_places=[] : ce test isole le JUGE (la passe éditoriale V2-56 a son
        # propre test) — sinon un pick réputé restaurant gonflerait les compteurs.
        anthropic_client=FakeAnthropic(reputed_places=[]))

    assert result["judge_rejected"] == 1     # Lidl (verdict fake ≥ 0,90)
    # hôpital + Mercadona + resto + baby-sitting + marché (tous les 'suggested' arbitrés)
    assert result["judge_approved"] == 5

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        prop = conn.execute("SELECT status FROM properties WHERE id=%s",
                            (guest_property_id,)).fetchone()
        assert prop["status"] == "published"   # auto-publication en fin de pipeline

        pois = conn.execute(
            "SELECT name, status, completion_meta FROM pois WHERE property_id=%s "
            "AND category_code NOT IN ('babysitter','market')",
            (guest_property_id,)).fetchall()
        by_name = {p["name"]: p for p in pois}
        # Aucun POI ne reste 'suggested' (arbitrage automatique, pas de triage humain).
        assert {p["status"] for p in pois} == {"approved", "rejected"}
        lidl = by_name["Lidl"]
        assert lidl["status"] == "rejected"
        # Motif tracé dans completion_meta._judge (patron V2-07, aucun champ de schéma).
        assert lidl["completion_meta"]["_judge"]["verdict"] == "reject"
        assert lidl["completion_meta"]["_judge"]["confidence"] >= 0.90
        assert by_name["Mercadona"]["status"] == "approved"

        # L'étape « judge » est tracée dans le journal du job + coût comptabilisé.
        steps = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (result["job_id"],)).fetchone()["steps"]
        assert steps["judge"]["rejected"] == 1 and steps["judge"]["approved"] == 5
        ops = {r["operation"] for r in conn.execute(
            "SELECT operation FROM api_costs WHERE job_id=%s", (result["job_id"],))}
        assert "judge" in ops


def test_guest_guide_editorial_sorties_adds_reputed_places(guest_property_id,
                                                           http_client):
    """V2-56 : pour un GUIDE VOYAGEUR, la passe éditoriale ajoute les adresses
    RÉPUTÉES « sorties » (bar/resto absents d'OSM), géocodées, avec contacts + raison,
    marquées « réputé » (completion_meta._editorial), et arbitrées par le juge."""
    result = pipeline.run(
        guest_property_id, use_claude=True, trigger="guest",
        only_categories={"restaurant", "bar", "hospital"},
        http_client=http_client, anthropic_client=FakeAnthropic())
    assert result["editorial_found"] == 2 and result["editorial_added"] == 2

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        pois = {p["name"]: p for p in conn.execute(
            "SELECT name, category_code, phone, website, owner_comment, "
            "completion_meta, source, status FROM pois WHERE property_id=%s",
            (guest_property_id,)).fetchall()}
        # Les incontournables sont présents (le cas La Zenia : Brown's + Casa Manolo).
        assert "Brown's Cocktail Bar" in pois and "Casa Manolo" in pois
        brown = pois["Brown's Cocktail Bar"]
        assert brown["category_code"] == "bar" and brown["source"] == "web"
        assert brown["phone"] and brown["website"]         # contacts (web)
        assert brown["owner_comment"] == "Cocktails réputés, terrasse animée."
        assert brown["completion_meta"]["_editorial"]["source_url"]
        assert brown["status"] == "approved"               # jugé + publié (guest)
        # La sélection cohabite avec le socle de proximité (La Marejada, OSM).
        assert pois["Casa Manolo"]["category_code"] == "restaurant"
        assert "La Marejada" in pois

        # Étape tracée dans le journal du job.
        steps = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (result["job_id"],)).fetchone()["steps"]
        assert steps["reputed_sorties"]["discovered"] == 2


def test_position_pick_cascade_never_centroid(monkeypatch):
    """V2-56b : cascade de POSITIONNEMENT d'un pick — (1) appariement par NOM contre
    OSM/Overture SANS distance (position de la base), (2) géocodage de rue STRICT,
    (3) sinon None (jamais le centroïde communal)."""
    prop = {"city": "Orihuela Costa", "country_code": "ES"}
    origin = (37.90, -0.75)
    osm = [{"name": "Casa Manolo", "lat": 37.905, "lon": -0.752,
            "phone": "+34 111", "website": None}]

    def fake_geocode(**kw):
        if "Y" in (kw.get("street") or ""):   # centroïde communal refusé
            return {"lat": 37.9375, "lon": -0.7453, "accuracy": "city",
                    "locality": None}
        return {"lat": 37.906, "lon": -0.753, "accuracy": "rooftop",
                "locality": "La Zenia"}
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))

    # (1) name-match OSM → position + contacts de la base (+ nom local, None ici : V2-66b).
    assert pipeline._position_pick({"name": "Casa Manolo", "address": "Av X"},
                                   osm, None, prop, origin, None) == \
        (37.905, -0.752, None, "+34 111", None, None)
    # (2) sans appariement, géocode 'city' → None (JAMAIS le centroïde).
    assert pipeline._position_pick({"name": "Bar Centroïde", "address": "Calle Y"},
                                   osm, None, prop, origin, None) is None
    # (3) sans appariement, rue rooftop → position géocodée.
    pos = pipeline._position_pick({"name": "Bien Placé", "address": "Calle Z 5"},
                                  osm, None, prop, origin, None)
    assert pos[:3] == (37.906, -0.753, "La Zenia")
    # (4) V2-66b cas (a) : la fiche OSM appariée porte un nom LOCAL → il remonte (6e champ).
    osm_jp = [{"name": "Kyubey", "lat": 35.67, "lon": 139.76,
               "completion_meta": {"_name_local": "久兵衛"}}]
    assert pipeline._position_pick({"name": "Kyubey", "address": "Ginza"},
                                   osm_jp, None, prop, origin, None)[5] == "久兵衛"


def test_activity_place_prefers_structured_then_derives_from_where():
    """V2-73c : le LIEU à géocoder vient des champs STRUCTURÉS (place_name/place_city) ;
    à défaut (faits d'avant V2-73c), il est DÉRIVÉ de la phrase `where` (1re partie = lieu,
    2e = commune, parenthèse/commentaire retirés). Diffuse (ni l'un ni l'autre) → ("","")."""
    ap = pipeline._activity_place
    # champs structurés fournis → tels quels.
    assert ap({"place_name": "Plage du Gurp", "place_city": "Grayan-et-l'Hôpital",
               "where": "peu importe"}) == ("Plage du Gurp", "Grayan-et-l'Hôpital")
    # dérivation de la phrase (cas réel Bégadan : commentaire de distance à retirer).
    assert ap({"where": "Plage du Gurp, Grayan-et-l'Hôpital (env. 10 km de Bégadan), "
                        "côte atlantique du Médoc"}) == ("Plage du Gurp", "Grayan-et-l'Hôpital")
    # un seul segment → lieu sans commune.
    assert ap({"where": "Sierra Escalona"}) == ("Sierra Escalona", "")
    # diffuse (aucun lieu nommé) → rien.
    assert ap({"place_name": "", "where": ""}) == ("", "")


def test_activity_soft_name_match_natural_places():
    """V2-73g : appariement SOUPLE des lieux naturels — mots de catégorie (plage/beach/…) et
    articles retirés avant comparaison. « Plage du Gurp » accroche « Le Gurp · Plage » ; deux
    plages DISTINCTES ne matchent jamais ; un nom = catégorie seule n'accroche rien."""
    assert pipeline._place_core("Plage du Gurp") == "gurp"
    assert pipeline._place_core("Le Gurp · Plage") == "gurp"
    assert pipeline._place_core("Plage") == ""                 # catégorie seule → vide
    cands = [{"name": "Le Gurp · Plage", "lat": 1, "lon": 1, "category": "beach"},
             {"name": "Plage de Montalivet", "lat": 2, "lon": 2, "category": "beach"}]
    m = pipeline._activity_name_match("Plage du Gurp", cands)
    assert m is not None and m["name"] == "Le Gurp · Plage"    # bon lieu, pas l'autre plage
    assert pipeline._activity_name_match("Plage", cands) is None   # cœur vide → aucune accroche
    assert pipeline._activity_name_match("Plage du Gurp", None) is None   # None toléré


def test_position_activity_cascade_never_centroid(monkeypatch):
    """V2-73/c/d/g : placement STRICT — (1) nom contre la MOISSON (exact), (2) ADRESSE
    POSTALE, (3) « nom, commune », (4) repli OSM par tag, (5) None. Le garde n'accepte jamais
    un centroïde ; une plage en lieu naturel EST acceptée. Renvoie `(lat, lon, exact, poi)`."""
    prop = {"city": "Bégadan", "country_code": "FR"}
    origin = (45.30, -0.86)
    # V2-73g : la moisson connaît le POI « Le Gurp · Plage » (position OSM vérifiée).
    harvested = [{"name": "Le Gurp · Plage", "lat": 45.36, "lon": -1.15, "category": "beach"}]
    osm_places = [{"name": "Plage du Gurp", "lat": 45.36, "lon": -1.15}]

    def fake_geocode(**kw):
        q = kw.get("address") or ""
        assert "env." not in q and "côte atlantique" not in q, f"phrase géocodée : {q!r}"
        if "route de l'océan" in q:                # ADRESSE POSTALE (tier 2) → résolue
            return {"lat": 45.42, "lon": -1.11, "accuracy": "street",
                    "osm_class": "highway", "osm_type": "residential"}
        if "Plage propre" in q:                    # plage résolue en LIEU NATUREL → ACCEPTÉE
            return {"lat": 45.39, "lon": -1.12, "accuracy": "city",
                    "osm_class": "natural", "osm_type": "beach"}
        if "Centre" in q:                          # CENTROÏDE communal → refusé
            return {"lat": 45.33, "lon": -0.87, "accuracy": "city",
                    "osm_class": "place", "osm_type": "village"}
        if "Loin" in q:                            # position aberrante (> 25 km) → refusée
            return {"lat": 46.50, "lon": -0.90, "accuracy": "street",
                    "osm_class": "highway", "osm_type": "residential"}
        raise pipeline.geocode.GeocodeError("introuvable")
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))

    def pos(a):
        return pipeline._position_activity(a, harvested, prop, origin, None, osm_places)

    # (1-point 1/2) APPARIEMENT SOUPLE à la moisson : « Plage du Gurp » s'accroche à
    #   « Le Gurp · Plage » (cœur « gurp ») → position VÉRIFIÉE, exact=True, poi renvoyé.
    r = pos({"place_name": "Plage du Gurp"})
    assert r[:2] == (45.36, -1.15) and r[2] is True and r[3]["category"] == "beach"
    # (2) ADRESSE POSTALE géocodée quand aucun POI n'apparie → APPROCHÉ (exact=False).
    r = pos({"place_name": "Spot isolé", "place_city": "Grayan",
             "place_address": "Le Truc, route de l'océan, 33590 Grayan"})
    assert r[:2] == (45.42, -1.11) and r[2] is False and r[3] is None
    # (3) plage résolue en lieu naturel (class=natural) → acceptée, APPROCHÉ.
    assert pos({"place_name": "Plage propre", "place_city": "Grayan"})[:3] == (45.39, -1.12, False)
    # centroïde administratif → None ; aberrant → None ; diffuse → None.
    assert pos({"place_name": "Centre de x"}) is None
    assert pos({"place_name": "Loin de x"}) is None
    assert pos({"place_name": "", "where": ""}) is None

    # (4) REPLI OSM par tag — isolé : moisson vide, géocodage en échec, lieu connu d'OSM →
    #     placé APPROCHÉ ; sans repli OSM → None (candidats None toléré, pas de crash).
    r = pipeline._position_activity({"place_name": "Plage du Gurp"}, [], prop, origin,
                                    None, osm_places)
    assert r[:3] == (45.36, -1.15, False)
    assert pipeline._position_activity({"place_name": "Plage du Gurp"}, [], prop, origin,
                                       None, None) is None


def test_place_activities_marks_placeable_and_is_idempotent(monkeypatch):
    """V2-73 : `_place_activities` pose lat/lon en place et n'écrase/re-géocode jamais une
    activité déjà positionnée. Le repli OSM par tag est un SEUL appel Overpass mutualisé."""
    prop = {"city": "Bégadan", "country_code": "FR"}
    origin = (45.30, -0.86)
    calls = {"geo": 0, "osm": 0}

    def fake_geocode(**kw):
        calls["geo"] += 1
        q = kw.get("address") or ""          # points DISTINCTS (pas d'empilement ici)
        lon = -0.90 if "falaise" in q else -0.95
        return {"lat": 45.31, "lon": lon, "accuracy": "street",
                "osm_class": "leisure", "osm_type": "sports_centre"}
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))

    def fake_natural(*a, **k):
        calls["osm"] += 1                    # doit rester à 1 (mutualisé par secteur)
        return []
    monkeypatch.setattr(pipeline.overpass, "fetch_natural_places", fake_natural)

    acts = [
        {"activity": "Surf", "where": "spot", "lat": 45.4, "lon": -1.1},   # déjà placé
        {"activity": "Escalade", "where": "falaise"},                       # à géocoder
        {"activity": "Rando", "where": "sentier"},                          # à géocoder
        {"activity": "Sans lieu", "where": ""},                            # non plaçable
    ]
    placed = pipeline._place_activities(acts, [], prop, origin, None)
    assert placed == 3                       # le déjà-placé + escalade + rando
    assert calls["geo"] == 2                 # le déjà-placé n'est PAS re-géocodé
    assert calls["osm"] == 1                 # UN seul appel Overpass pour tout le lot
    assert acts[0]["lat"] == 45.4            # position d'origine intacte
    assert acts[1]["lat"] == 45.31 and acts[1]["lon"] == -0.90
    assert "lat" not in acts[3]             # sans lieu → reste sans marqueur


def test_dedup_activity_positions_keeps_named_drops_borrowed():
    """V2-73e : plusieurs activités au MÊME point (< 50 m) → seule la NOMMÉE (place_name
    explicite) garde le marqueur ; les diffuses (place_name vide, adresse empruntée) le
    perdent. Une activité à un AUTRE point garde le sien. Idempotent."""
    lat, lon = 45.4286, -1.1165                 # plage du Gurp (cas réel Bégadan)
    acts = [
        {"activity": "Sentiers balisés", "place_name": "", "lat": lat, "lon": lon},       # emprunt
        {"activity": "Surf", "place_name": "Plage du Gurp", "lat": lat + 0.0001, "lon": lon},  # NOMMÉE
        {"activity": "Marais Natura 2000", "place_name": "", "lat": lat, "lon": lon + 0.0001},  # emprunt
        {"activity": "Kayak", "place_name": "Lac d'Hourtin", "lat": 45.20, "lon": -1.05},  # autre point
    ]
    pipeline._dedup_activity_positions(acts)
    assert acts[1].get("lat") is not None                     # Surf (nommée) gardée
    assert "lat" not in acts[0] and "lat" not in acts[2]      # emprunts évincés
    assert acts[3].get("lat") == 45.20                        # autre point : intact
    pipeline._dedup_activity_positions(acts)                  # re-passage : rien ne bouge
    assert acts[1].get("lat") is not None and "lat" not in acts[0]


def test_backfill_activity_positions_upgrades_pre_v2_73_fact(monkeypatch):
    """V2-73b/c/d : un fait 'activities' MÉMORISÉ d'un schéma périmé est mis à niveau —
    positions posées par la SEULE passe de cascade (aucun web/LLM de collecte), version
    stampée — puis JAMAIS re-tenté. Cas RÉEL Bégadan (V2-73d) : la « Plage du Gurp » n'est
    PAS indexée par Nominatim en texte (géocodage en échec), mais OSM la connaît par TAG
    (natural=beach) → le REPLI OSM la place (≈ 45.36 / -1.15) ; la diffuse reste sans marqueur."""
    from enrich import db as edb, claude_enrich as ce
    CC, CITY = "FR", "TestBackfill73d"
    origin = (45.30, -0.86)
    prop = {"country_code": CC, "city": CITY}
    job_id = str(uuid.uuid4())   # aucun job réel requis (UPDATE ... WHERE id : 0 ligne)
    queries: list[str] = []

    def fake_geocode(**kw):
        q = kw.get("address") or ""
        queries.append(q)
        # La cascade NE géocode JAMAIS la phrase entière (le bug V2-73b).
        assert "env." not in q and "côte atlantique" not in q, f"phrase géocodée : {q!r}"
        raise pipeline.geocode.GeocodeError("introuvable")   # Nominatim n'indexe pas le Gurp
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))

    osm_calls = {"n": 0}

    def fake_natural(lat, lon, client=None, **k):
        osm_calls["n"] += 1
        return [{"name": "Plage du Gurp", "lat": 45.36, "lon": -1.15}]  # OSM connaît par tag
    monkeypatch.setattr(pipeline.overpass, "fetch_natural_places", fake_natural)

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DELETE FROM area_facts WHERE country_code=%s AND admin_area=%s",
                     (CC, CITY))
        # Fait v3 (V2-73c) : phrase `where` entière, aucune position, aucun place_name/address.
        edb.upsert_area_facts(conn, CC, CITY, {ce.ACTIVITIES_FACT_TYPE: {"v": 3, "activities": [
            {"activity": "Surf", "source_url": "https://x", "where":
             "Plage du Gurp, Grayan-et-l'Hôpital (env. 10 km de Bégadan), côte atlantique"},
            {"activity": "16 circuits VTT", "source_url": "https://y",
             "where": "tout le secteur du Médoc"},
        ]}}, source="seed")
        conn.commit()

        pipeline._backfill_activity_positions(conn, prop, [], origin, None, job_id, {})
        fact = edb.get_area_fact(conn, CC, CITY, ce.ACTIVITIES_FACT_TYPE)
        assert fact["v"] == ce.ACTIVITIES_SCHEMA_V == 6      # v3 → v6 (schéma courant)
        acts = fact["activities"]
        assert acts[0].get("lat") == 45.36 and acts[0].get("lon") == -1.15  # plage placée (repli OSM)
        assert "lat" not in acts[1]                          # diffuse : reste sans marqueur
        # Le géocodage textuel a bien été tenté sur le LIEU dérivé (jamais la phrase)…
        assert any(q.startswith("Plage du Gurp, Grayan") for q in queries)
        assert osm_calls["n"] == 1                           # …et le repli OSM, UN seul appel

        # Re-passage : le fait est au schéma courant (v6) → AUCUNE nouvelle tentative.
        n_before = len(queries)
        pipeline._backfill_activity_positions(conn, prop, [], origin, None, job_id, {})
        assert len(queries) == n_before and osm_calls["n"] == 1

        conn.execute("DELETE FROM area_facts WHERE country_code=%s AND admin_area=%s",
                     (CC, CITY))
        conn.commit()


def test_backfill_repositions_approximate_to_harvested_poi(monkeypatch):
    """V2-73g : à la montée de schéma, une position APPROXIMATIVE (géocodée, `exact` absent)
    est ré-évaluée — « Plage du Gurp » s'accroche au POI moissonné « Le Gurp · Plage »
    (position OSM vérifiée, 1,5 km plus juste), devient exact + poi_cat ; une position déjà
    EXACTE est conservée telle quelle, sans re-géocodage."""
    from enrich import db as edb, claude_enrich as ce
    CC, CITY = "FR", "TestReposition73g"
    origin = (45.30, -0.86)
    prop = {"country_code": CC, "city": CITY}
    job_id = str(uuid.uuid4())
    harvested = [{"name": "Le Gurp · Plage", "lat": 45.36, "lon": -1.15, "category": "beach"}]
    geo_calls = {"n": 0}

    def fake_geocode(**kw):
        geo_calls["n"] += 1
        raise pipeline.geocode.GeocodeError("ne devrait pas être appelé")
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))
    monkeypatch.setattr(pipeline.overpass, "fetch_natural_places", lambda *a, **k: [])

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DELETE FROM area_facts WHERE country_code=%s AND admin_area=%s", (CC, CITY))
        # Fait v5 : Surf placé APPROXIMATIVEMENT (géocodage d'adresse, ~1,5 km off, exact absent) ;
        # Kayak déjà EXACT (POI apparié) → à conserver.
        edb.upsert_area_facts(conn, CC, CITY, {ce.ACTIVITIES_FACT_TYPE: {"v": 5, "activities": [
            {"activity": "Surf", "place_name": "Plage du Gurp", "source_url": "https://x",
             "lat": 45.42, "lon": -1.11},
            {"activity": "Kayak", "place_name": "Lac", "source_url": "https://y",
             "lat": 45.20, "lon": -1.05, "exact": True},
        ]}}, source="seed")
        conn.commit()

        pipeline._backfill_activity_positions(conn, prop, harvested, origin, None, job_id, {})
        acts = edb.get_area_fact(conn, CC, CITY, ce.ACTIVITIES_FACT_TYPE)["activities"]
        # Surf ré-accroché au POI moissonné (position vérifiée), exact + lien vers la fiche.
        assert acts[0]["lat"] == 45.36 and acts[0]["lon"] == -1.15
        assert acts[0]["exact"] is True and acts[0]["poi_cat"] == "beach"
        # Kayak (déjà exact) conservé tel quel ; aucun géocodage (la moisson a suffi).
        assert acts[1]["lat"] == 45.20 and acts[1].get("exact") is True
        assert geo_calls["n"] == 0

        conn.execute("DELETE FROM area_facts WHERE country_code=%s AND admin_area=%s", (CC, CITY))
        conn.commit()


# ── V2-74 : commerces de village (vide rural) ────────────────────────────────

def test_void_essentials_only_wanted_and_far():
    """V2-74 : une catégorie essentielle est VIDE si DEMANDÉE mais sans POI dans le rayon.
    Une catégorie non demandée n'est jamais « vide » ; un POI proche la couvre ; un POI
    lointain (> rayon) ne la couvre pas."""
    origin = (45.30, -0.86)
    harv = [{"name": "Supérette", "lat": 45.301, "lon": -0.861, "category": "supermarket"},
            {"name": "Pharmacie Lesparre", "lat": 45.40, "lon": -1.15, "category": "pharmacy"}]
    wanted = {"pharmacy", "supermarket", "bakery", "restaurant"}
    void = pipeline._void_essentials(harv, wanted, origin, 5000)
    assert "supermarket" not in void          # POI proche → couverte
    assert "pharmacy" in void                 # seule à ~24 km (hors rayon) → vide
    assert "bakery" in void                   # demandée, aucun POI → vide
    assert "doctor" not in void and "post_office" not in void  # non demandées → jamais


def test_geocode_local_commerce_guard_escalation_fallback(monkeypatch):
    """V2-74b : garde de cohérence (position PRÉCISE > 2 km du centre de la commune → rejetée,
    cas « 59 min »), ESCALADE sur la rue sans numéro (numéros ruraux absents d'OSM), REPLI au
    centre marqué approximatif."""
    prop = {"city": "Bégadan", "country_code": "FR"}
    center = (45.33, -0.86)                     # centre de la commune

    def fake_geocode(**kw):
        street = (kw.get("street") or "").strip()
        if street.startswith("1 ") and "Saint-Saturnin" in street:
            return {"lat": 45.37, "lon": -0.86, "accuracy": "street",   # ~4,4 km : LOIN du centre
                    "locality": "Bégadan"}
        if "Saint-Saturnin" in street:          # rue SANS numéro → près du centre
            return {"lat": 45.331, "lon": -0.861, "accuracy": "street", "locality": "Bégadan"}
        raise pipeline.geocode.GeocodeError("introuvable")
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda **kw: fake_geocode(**kw))

    # (1+2) numéro rural → géocodage LOIN, rejeté par la garde ; escalade rue seule → près du
    #       centre, position PRÉCISE retenue (non approximative).
    r = pipeline._geocode_local_commerce(
        {"place_address": "1 route de Saint-Saturnin, 33340 Bégadan"}, prop, center, None)
    assert r[:2] == (45.331, -0.861) and r[3] is False
    # (repli) adresse introuvable → centre de la commune, marqué APPROXIMATIF.
    r2 = pipeline._geocode_local_commerce(
        {"place_address": "5 impasse Inconnue, 33340 Bégadan"}, prop, center, None)
    assert r2[:2] == center and r2[3] is True


def test_fetch_local_commerces_proof_or_nothing():
    """V2-74 : PREUVE OU RIEN — sans adresse précise, sans source, ou catégorie inconnue,
    l'entrée est écartée. Liste vide valide."""
    from enrich import claude_enrich as ce
    ai = FakeAnthropic(local_commerces=[
        {"name": "Pharmacie du Centre", "category": "pharmacy",
         "place_address": "3 place de l'Église, 33340 Bégadan", "phone": "+33 5 56 00 00 00",
         "source_url": "https://mairie.example/commerces", "verified_on": "2026-09-15"},
        {"name": "Sans adresse", "category": "pharmacy", "source_url": "https://x"},  # écartée
        {"name": "Cinéma", "category": "cinema",                                       # catégorie hors liste
         "place_address": "1 rue X, Bégadan", "source_url": "https://y"},
    ])
    fact, meta = ce.fetch_local_commerces("Bégadan", "FR", ai, today="2026-09-15")
    items = fact[ce.LOCAL_COMMERCE_FACT_TYPE]["commerces"]
    assert [c["name"] for c in items] == ["Pharmacie du Centre"]   # preuve ou rien
    assert items[0]["category"] == "pharmacy" and items[0]["place_address"]
    assert meta["cost_cts"] >= 0


def test_local_commerce_fills_rural_void(monkeypatch):
    """V2-74 bout en bout : une commune où OSM ne connaît PAS la pharmacie (catégorie vide)
    déclenche la découverte web → la pharmacie de village est matérialisée en POI 'suggested'
    positionné (adresse géocodée), avec `locality` (honnêteté de la distance). La catégorie
    couverte par OSM (supermarket) n'est PAS re-découverte."""
    from enrich import db as edb, claude_enrich as ce
    pid, oid = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM area_facts WHERE country_code='ES'")
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s,%s,'T')",
                     (oid, f"{oid}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city, country_code)
               VALUES (%s,%s,'Villa Void','Calle 1','Orihuela Costa','ES')""", (pid, oid))
        conn.commit()
    ai = FakeAnthropic(local_commerces=[
        {"name": "Farmacia del Pueblo", "category": "pharmacy",
         "place_address": "Plaza Mayor 1, Orihuela Costa", "phone": "+34 966 00 00 00",
         "source_url": "https://ayto.example/farmacias", "verified_on": "2026-09-15"}])
    try:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            settings.politeness_delay_s = 0
            result = pipeline.run(pid, use_claude=True, trigger="initial",
                                  only_categories={"pharmacy", "supermarket"},
                                  http_client=client, anthropic_client=ai)
        assert result["local_commerces_created"] == 1
        with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
            poi = conn.execute(
                """SELECT name, category_code, locality, status, source, walk_min, drive_min
                   FROM pois WHERE property_id=%s AND category_code='pharmacy'""",
                (pid,)).fetchone()
            assert poi and poi["name"] == "Farmacia del Pueblo"
            assert poi["source"] == "claude" and poi["status"] == "suggested"
            assert poi["locality"]                                   # commune posée (honnêteté)
            assert poi["walk_min"] is not None or poi["drive_min"] is not None  # distances calculées
            job = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                               (result["job_id"],)).fetchone()
            lc = job["steps"]["local_commerces"]
            assert lc["ok"] and lc["created"] == 1 and "pharmacy" in lc["void"]
            # La commune a bien un area_fact mutualisé 'local_commerces'.
            assert edb.get_area_fact(conn, "ES", "Orihuela Costa",
                                     ce.LOCAL_COMMERCE_FACT_TYPE) is not None
    finally:
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute("DELETE FROM owners WHERE id=%s", (oid,))
            conn.execute("DELETE FROM area_facts WHERE country_code='ES'")
            conn.commit()


def test_sector_editorial_memory_accumulates_and_dedups():
    """V2-56c : la mémoire de secteur accumule et déduplique (upsert par nom normalisé),
    et rafraîchit position/contacts/raison au re-passage."""
    from enrich import db as edb
    SECT = "testsector56c"
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DELETE FROM editorial_picks WHERE city_norm=%s", (SECT,))
        edb.upsert_editorial_pick(
            conn, country_code="ES", city="La Zenia", city_norm=SECT,
            name="Casa Manolo", name_norm="casamanolo", category="restaurant",
            reason="Arroces", source_url="u1", verified_on="2026-09-13",
            lat=37.92, lon=-0.73, phone="+34 1", website=None, locality="La Zenia")
        edb.upsert_editorial_pick(   # re-vu : rafraîchit (pas de doublon)
            conn, country_code="ES", city="La Zenia", city_norm=SECT,
            name="Casa Manolo", name_norm="casamanolo", category="restaurant",
            reason="Arroces (maj)", source_url="u1b", verified_on="2026-09-14",
            lat=37.921, lon=-0.731, phone="+34 1", website="https://cm.example",
            locality="La Zenia")
        edb.upsert_editorial_pick(   # autre élu du secteur
            conn, country_code="ES", city="La Zenia", city_norm=SECT,
            name="Brown's Cocktail Bar", name_norm="brownscocktailbar", category="bar",
            reason="Cocktails", source_url="u2", verified_on="2026-09-14",
            lat=37.93, lon=-0.74, phone=None, website="https://browns-cocktailbar.com",
            locality="La Zenia")
        conn.commit()
        resto = edb.sector_editorial_picks(conn, "ES", SECT, "restaurant", 90)
        bars = edb.sector_editorial_picks(conn, "ES", SECT, "bar", 90)
        conn.execute("DELETE FROM editorial_picks WHERE city_norm=%s", (SECT,))
        conn.commit()
    assert [r["name"] for r in resto] == ["Casa Manolo"]       # dédup (un seul)
    assert resto[0]["website"] == "https://cm.example"          # contacts rafraîchis
    assert resto[0]["reason"] == "Arroces (maj)"                # raison rafraîchie
    assert resto[0]["lat"] == pytest.approx(37.921)            # position rafraîchie
    assert [r["name"] for r in bars] == ["Brown's Cocktail Bar"]


def test_editorial_pick_local_name_round_trip_and_poi(monkeypatch):
    """V2-66b cas (a) : le nom LOCAL d'un pick éditorial est mémorisé (editorial_picks)
    puis reporté en `completion_meta._name_local` du POI matérialisé — restaurants/bars
    japonais éditoriaux portent leur nom d'origine (et donc leur 🔊)."""
    from enrich import db as edb
    SECT = "testsector66b"
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("DELETE FROM editorial_picks WHERE city_norm=%s", (SECT,))
        edb.upsert_editorial_pick(
            conn, country_code="JP", city="Tokyo", city_norm=SECT,
            name="Kyubey", name_norm="kyubey", category="restaurant",
            reason="Sushi d'exception au Ginza", source_url="u", verified_on="2026-09-15",
            lat=35.671, lon=139.763, phone=None, website=None, locality="Ginza",
            name_local="銀座 久兵衛")
        conn.commit()
        mem = edb.sector_editorial_picks(conn, "JP", SECT, "restaurant", 90)
        conn.execute("DELETE FROM editorial_picks WHERE city_norm=%s", (SECT,))
        conn.commit()
    assert mem and mem[0]["name_local"] == "銀座 久兵衛"        # round-trip DB
    # Matérialisation en POI : le nom latin reste le nom affiché, l'original va en meta.
    poi = pipeline._build_editorial_poi(mem[0], "restaurant", mem[0]["lat"], mem[0]["lon"],
                                        mem[0].get("locality"), (35.68, 139.76), "sector_memory")
    assert poi["name"] == "Kyubey"
    assert poi["completion_meta"]["_name_local"] == "銀座 久兵衛"
    assert poi["completion_meta"]["_name_script"] == "cjk"
    # Pays latin : aucun nom local même si un name_local latin traînait (aucune régression).
    poi_es = pipeline._build_editorial_poi(
        {"name": "Casa Manolo", "name_local": "Casa Manolo", "address": None},
        "restaurant", 37.9, -0.7, None, (37.9, -0.7), "sector_memory")
    assert "_name_local" not in poi_es["completion_meta"]


def test_second_run_inherits_first_run_editorial_picks(http_client):
    """V2-56c : deux générations SUCCESSIVES du même secteur (logements distincts) — la
    seconde contient AU MOINS les élus positionnés de la première + ses frais, MÊME si
    le jury web du 2e run ne les redécouvre pas (Casa Manolo stable)."""
    oid = str(uuid.uuid4())
    pid1, pid2 = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM editorial_picks WHERE country_code='ES'")
        conn.execute("DELETE FROM area_facts WHERE country_code='ES'")
        conn.execute("INSERT INTO owners (id,email,full_name) VALUES (%s,%s,'S')",
                     (oid, f"{oid}@test.local"))
        for pid in (pid1, pid2):
            conn.execute(
                """INSERT INTO properties (id,owner_id,name,address_line1,city,
                       country_code,guest_guide,geom,geocode_source,geocode_accuracy)
                   VALUES (%s,%s,'G','Rue','Orihuela Costa','ES',TRUE,
                       ST_SetSRID(ST_MakePoint(%s,%s),4326),'manual','manual')""",
                (pid, oid, PROP_LON, PROP_LAT))
        conn.commit()
    try:
        # Run 1 : le jury web renvoie Casa Manolo.
        pipeline.run(pid1, use_claude=True, trigger="guest",
                     only_categories={"restaurant"}, http_client=http_client,
                     anthropic_client=FakeAnthropic(reputed_places=[
                         {"name": "Casa Manolo", "category": "restaurant",
                          "address": "Av Manolo, La Zenia", "reason": "Arroces",
                          "source_url": "u1", "phone": "+34 1",
                          "website": "https://cm.example"}]))
        # Run 2 : le jury web renvoie Brown's SEULEMENT (Casa Manolo absent du jury).
        pipeline.run(pid2, use_claude=True, trigger="guest",
                     only_categories={"restaurant"}, http_client=http_client,
                     anthropic_client=FakeAnthropic(reputed_places=[
                         {"name": "Brown's", "category": "restaurant",
                          "address": "Calle Brown, La Zenia", "reason": "Cocktails",
                          "source_url": "u2", "phone": "+34 2",
                          "website": "https://browns-cocktailbar.com"}]))
        with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
            names2 = {r["name"] for r in conn.execute(
                "SELECT name FROM pois WHERE property_id=%s AND category_code='restaurant'",
                (pid2,)).fetchall()}
        # Le 2e guide hérite de Casa Manolo (mémoire du secteur) ET a Brown's (frais).
        assert "Casa Manolo" in names2 and "Brown's" in names2
    finally:
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute("DELETE FROM properties WHERE id = ANY(%s)", ([pid1, pid2],))
            conn.execute("DELETE FROM owners WHERE id=%s", (oid,))
            conn.execute("DELETE FROM editorial_picks WHERE country_code='ES'")
            conn.execute("DELETE FROM area_facts WHERE country_code='ES'")
            conn.commit()


def test_owner_guide_has_no_editorial_pass(property_id, http_client):
    """La passe éditoriale est réservée aux guides voyageur : un guide propriétaire
    ne reçoit AUCUN pick réputé (curation humaine) — invariant du périmètre V2-56."""
    result = pipeline.run(
        property_id, use_claude=True, trigger="initial",
        only_categories={"restaurant", "bar", "hospital"},
        http_client=http_client, anthropic_client=FakeAnthropic())
    assert result["editorial_found"] == 0 and result["editorial_added"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s", (property_id,)).fetchall()}
        assert "Brown's Cocktail Bar" not in names and "Casa Manolo" not in names
        # Les POI OSM restent 'suggested' (pas de juge sur un guide propriétaire).
        assert {r["status"] for r in conn.execute(
            "SELECT status FROM pois WHERE property_id=%s AND source='osm'",
            (property_id,)).fetchall()} == {"suggested"}


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


# ── V2-07 volet 2 : complétion des fiches de service (tel/site/horaires) ──────

def test_service_completion_fills_kept_pois_and_never_overwrites(property_id, http_client):
    """Complète (avec preuve) les fiches RETENUES : Lidl (approved, sans horaires)
    gagne des horaires sourcés + mention « Horaires indicatifs » ; son site saisi
    PAR LE PROPRIÉTAIRE n'est jamais écrasé (il n'est même pas demandé) ; Mercadona
    resté 'suggested' n'est JAMAIS touché ; ni le status ni le source ne changent."""
    pipeline.run(property_id, use_claude=False, only_categories={"supermarket"},
                 http_client=http_client, anthropic_client=FakeAnthropic())

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # Le propriétaire retient Lidl (sans horaires) et lui saisit un SITE.
        conn.execute("""UPDATE pois SET status='approved',
                        website='https://owner-lidl.example'
                        WHERE property_id=%s AND name='Lidl'""", (property_id,))
        conn.commit()
        lidl = conn.execute("SELECT id FROM pois WHERE property_id=%s AND name='Lidl'",
                            (property_id,)).fetchone()
        lidl_id = str(lidl["id"])

    # La recherche web « renvoie » des horaires ET un site pour Lidl — mais le site
    # ne sera pas demandé (déjà saisi) donc jamais retenu.
    completions = {lidl_id: {
        "opening_hours": "Lun–Sam 8h–22h",
        "website": "https://claude-lidl.example",   # doit être ignoré (site saisi)
        "source_url": "https://lidl.es/tiendas/orihuela", "verified_on": "2026-08-12"}}
    pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                 http_client=http_client,
                 anthropic_client=FakeAnthropic(service_completions=completions))

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        lidl = conn.execute("SELECT * FROM pois WHERE id=%s", (lidl_id,)).fetchone()
        # Horaires complétés + mention (donnée périssable).
        assert lidl["opening_hours"] == "Lun–Sam 8h–22h · Horaires indicatifs"
        # Site du PROPRIÉTAIRE intact (jamais écrasé, ni même demandé).
        assert lidl["website"] == "https://owner-lidl.example"
        # Status/source inchangés : la complétion n'est pas une édition propriétaire.
        assert lidl["status"] == "approved" and lidl["source"] == "osm"
        # Provenance traçable par champ + marqueur de re-vérification.
        meta = lidl["completion_meta"]
        assert meta["opening_hours"]["source_url"].startswith("https://")
        assert meta["opening_hours"]["verified_on"] == "2026-08-12"
        assert meta["_checked_on"]

        # Mercadona resté 'suggested' n'est jamais touché par la complétion.
        merca = conn.execute("SELECT * FROM pois WHERE property_id=%s AND name='Mercadona'",
                             (property_id,)).fetchone()
        assert merca["status"] == "suggested" and merca["completion_meta"] is None

        # Coût comptabilisé sous une opération dédiée (unités web incluses).
        ops = {r["operation"] for r in conn.execute(
            "SELECT operation FROM api_costs WHERE property_id=%s", (property_id,))}
        assert "service_complete" in ops
        # OPS-4 Pièce 2 : l'étape 4c figure au journal (dernier job) avec compteur
        # PAR catégorie + coût.
        job = conn.execute(
            "SELECT steps FROM enrichment_jobs WHERE property_id=%s "
            "ORDER BY started_at DESC LIMIT 1", (property_id,)).fetchone()
        sc = job["steps"]["service_complete"]
        assert sc["ok"] is True and sc["by_category"].get("supermarket") == 1
        assert sc["completed"] == 1 and "cost_cts" in sc


def test_apply_completion_never_overwrites_and_guards_status(property_id):
    """Garde-fou unitaire de `db.apply_poi_completion` : COALESCE (jamais d'écrasement)
    et refus des POI non retenus (suggested/rejected)."""
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # POI approuvé avec un téléphone DÉJÀ saisi + une catégorie du périmètre.
        appr = conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, phone,
                                 source, source_ref, status)
               VALUES (%s, 'taxi', 'Taxi A',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       '+34 111', 'osm', 'n1', 'approved') RETURNING id""",
            (property_id,)).fetchone()
        sugg = conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom,
                                 source, source_ref, status)
               VALUES (%s, 'taxi', 'Taxi B',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       'osm', 'n2', 'suggested') RETURNING id""",
            (property_id,)).fetchone()
        conn.commit()

        # Tente d'écraser le téléphone d'un POI approuvé → COALESCE le protège.
        n = db.apply_poi_completion(conn, str(appr["id"]),
                                    {"phone": "+34 999"}, "https://x", "2026-08-12",
                                    "2026-08-12")
        # Une fiche 'suggested' n'est jamais complétée (garde de status).
        n2 = db.apply_poi_completion(conn, str(sugg["id"]),
                                     {"phone": "+34 888"}, "https://x", "2026-08-12",
                                     "2026-08-12")
        conn.commit()
        assert n == 1 and n2 == 0
        got = conn.execute("SELECT phone, completion_meta FROM pois WHERE id=%s",
                           (appr["id"],)).fetchone()
        assert got["phone"] == "+34 111"                 # jamais écrasé
        assert got["completion_meta"]["_checked_on"]     # provenance/marqueur posés
        got2 = conn.execute("SELECT phone FROM pois WHERE id=%s",
                            (sugg["id"],)).fetchone()
        assert got2["phone"] is None                     # 'suggested' intact


# ── V2-37 volet 2 : requalification de catégorie (non-réversion + périmètre) ──

def test_edited_category_survives_reenrichment_and_enters_completion(property_id,
                                                                     http_client):
    """Invariant critique : une catégorie REQUALIFIÉE par le propriétaire n'est jamais
    annulée par un ré-enrichissement (les POI edited sont hors upsert — WHERE
    status='suggested'). Effet automatique constaté : la fiche requalifiée entre dans
    le périmètre de complétion de sa NOUVELLE catégorie au prochain run."""
    from api import repo as api_repo
    from enrich import claude_enrich as ce

    # 1er run : La Marejada arrive en 'suggested' restaurant.
    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        poi = conn.execute("SELECT id, category_code, status FROM pois "
                           "WHERE property_id=%s AND name='La Marejada'",
                           (property_id,)).fetchone()
        assert poi["category_code"] == "restaurant" and poi["status"] == "suggested"
        # Le propriétaire requalifie restaurant → bar (l'édition force 'edited') et
        # garnit la fiche : commune corrigée à la main, coup de cœur, description.
        api_repo.edit_poi(conn, property_id, str(poi["id"]),
                          {"category_code": "bar", "locality": "Vétroz",
                           "owner_comment": "Notre apéro du soir",
                           "description_md": "Terrasse au calme."})
        conn.commit()

    # Ré-enrichissement Overpass : node/333 revient sous 'restaurant' (addr:city Torrevieja).
    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        got = conn.execute("SELECT category_code, status, locality, owner_comment, "
                           "description_md FROM pois "
                           "WHERE property_id=%s AND name='La Marejada'",
                           (property_id,)).fetchone()
        assert got["category_code"] == "bar"      # NON réverti par le re-run
        assert got["status"] == "edited"          # choix propriétaire conservé
        # V2-38bis : une localité SAISIE n'est JAMAIS écrasée (COALESCE l'interdit) —
        # « Vétroz » survit malgré l'addr:city 'Torrevieja' du flux OSM.
        assert got["locality"] == "Vétroz"
        # Tout le reste du contenu propriétaire reste STRICTEMENT hors upsert.
        assert got["owner_comment"] == "Notre apéro du soir"
        assert got["description_md"] == "Terrasse au calme."
        # Effet automatique (V2-37 vol 1 + 2) : la fiche 'bar' (edited) entre dans le
        # périmètre tél/site de sa nouvelle catégorie au prochain run de complétion.
        todo = db.pois_needing_completion(conn, property_id, "bar",
                                          ce.service_fields("bar"), 30)
        assert any(t["name"] == "La Marejada" for t in todo)


# ── V2-38 : localité (commune) d'un POI — stockage, COALESCE, non-réversion ──
# V2-38bis : la localité (et elle SEULE) traverse le statut — une fiche RETENUE la
# gagne au re-run (sinon le guide, qui n'affiche que les retenues, resterait NULL à
# jamais), mais tout le reste de son contenu demeure strictement hors upsert.

def test_locality_stored_by_enrichment_and_never_wiped(property_id, http_client):
    """La localité (addr:city) est POSÉE par l'enrichissement (end-to-end) et n'est
    JAMAIS effacée par un re-run où OSM ne la fournirait plus (COALESCE, motif du
    volet 2 : compléter le NULL, jamais écraser)."""
    from enrich import db as edb

    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute("SELECT id, locality, source_ref FROM pois "
                           "WHERE property_id=%s AND name='La Marejada'",
                           (property_id,)).fetchone()
        assert row["locality"] == "Torrevieja"        # posée end-to-end (addr:city)
        # Re-upsert du MÊME POI suggéré sans localité (OSM ne la renvoie plus).
        edb.upsert_pois(conn, property_id, "restaurant", [{
            "name": "La Marejada", "lat": 37.929, "lon": -0.747,
            "source": "osm", "source_ref": row["source_ref"], "locality": None}])
        conn.commit()
        kept = conn.execute("SELECT locality FROM pois WHERE id=%s",
                            (row["id"],)).fetchone()["locality"]
        assert kept == "Torrevieja"                   # COALESCE : jamais effacée


def test_edited_locality_survives_reenrichment(property_id, http_client):
    """Une localité SAISIE/CORRIGÉE par le propriétaire (statut 'edited') n'est jamais
    révertie par un ré-enrichissement (edited hors upsert — motif du test catégorie)."""
    from api import repo as api_repo

    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        poi = conn.execute("SELECT id FROM pois WHERE property_id=%s "
                           "AND name='La Marejada'", (property_id,)).fetchone()
        # Le propriétaire corrige la commune (Street View : c'est Vétroz, pas Ardon).
        api_repo.edit_poi(conn, property_id, str(poi["id"]), {"locality": "Vétroz"})
        conn.commit()

    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        got = conn.execute("SELECT locality, status FROM pois WHERE property_id=%s "
                           "AND name='La Marejada'", (property_id,)).fetchone()
        assert got["locality"] == "Vétroz"            # NON réverti par le re-run
        assert got["status"] == "edited"


def test_retained_fiche_gains_null_locality_but_all_else_untouched(property_id,
                                                                   http_client):
    """V2-38bis (le CŒUR) : une fiche RETENUE (approved) à locality NULL — cas d'une
    fiche arbitrée avant V2-38 — GAGNE sa commune au re-run (le guide n'affiche que les
    retenues : sinon elle resterait NULL à jamais). MAIS tout son contenu propriétaire
    reste STRICTEMENT hors upsert (invariant 1), y compris un champ (website) que OSM
    fournit et qui serait écrasé sans le garde-fou."""
    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        poi = conn.execute("SELECT id FROM pois WHERE property_id=%s "
                           "AND name='La Marejada'", (property_id,)).fetchone()
        pid_poi = str(poi["id"])
        # Fiche RETENUE + localité VIDÉE (comme une fiche approuvée avant V2-38) et
        # garnie de contenu propriétaire — dont un website DIFFÉRENT de celui d'OSM.
        conn.execute(
            """UPDATE pois SET status='approved', locality=NULL,
                   description_md='Cuisine de la mer, vue sur le port.',
                   phone='+34 111 222 333', website='https://chez-nous.example',
                   owner_comment='Notre cantine !' WHERE id=%s""", (pid_poi,))
        conn.commit()

    # Re-run : OSM renvoie La Marejada (addr:city Torrevieja, website lamarejada.example).
    pipeline.run(property_id, use_claude=False, only_categories={"restaurant"},
                 http_client=http_client, anthropic_client=FakeAnthropic())
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        got = conn.execute(
            "SELECT status, locality, description_md, phone, website, owner_comment, "
            "name, category_code FROM pois WHERE id=%s", (pid_poi,)).fetchone()
    assert got["locality"] == "Torrevieja"          # NULL comblée depuis OSM — LE FIX
    assert got["status"] == "approved"              # statut inchangé
    # Tout le reste STRICTEMENT intact (le contenu d'une fiche retenue n'est jamais
    # réenrichi) — website prouve la garde : sans elle, OSM l'écraserait.
    assert got["website"] == "https://chez-nous.example"
    assert got["description_md"] == "Cuisine de la mer, vue sur le port."
    assert got["phone"] == "+34 111 222 333"
    assert got["owner_comment"] == "Notre cantine !"
    assert got["name"] == "La Marejada" and got["category_code"] == "restaurant"


def test_upsert_merges_completion_meta_and_fills_retained(property_id):
    """V2-66 — completion_meta est FUSIONNÉ, jamais remplacé : (1) un suggested re-moissonné
    garde ce que le juge y a accumulé (_judge) tout en recevant le nom local ; (2) une fiche
    RETENUE (approved) à meta NULL GAGNE le nom local au re-run (le guide n'affiche que les
    retenues), le nom du champ `name` restant intouché (invariant 1)."""
    from enrich import db as edb
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # (1) moisson initiale : suggested + nom local.
        edb.upsert_pois(conn, property_id, "sight", [{
            "name": "Sensō-ji", "lat": 35.71, "lon": 139.79, "source": "osm",
            "source_ref": "node/loc1", "completion_meta": {"_name_local": "浅草寺"}}])
        # le juge accumule _judge PAR FUSION (comme en prod).
        conn.execute(
            "UPDATE pois SET completion_meta = completion_meta || "
            "'{\"_judge\": {\"verdict\": \"accept\"}}'::jsonb "
            "WHERE property_id=%s AND source_ref='node/loc1'", (property_id,))
        conn.commit()
        # re-moisson : le suggested garde _judge ET son nom local (fusion, pas écrasement).
        edb.upsert_pois(conn, property_id, "sight", [{
            "name": "Sensō-ji", "lat": 35.71, "lon": 139.79, "source": "osm",
            "source_ref": "node/loc1", "completion_meta": {"_name_local": "浅草寺"}}])
        conn.commit()
        row = conn.execute(
            "SELECT completion_meta FROM pois WHERE property_id=%s AND "
            "source_ref='node/loc1'", (property_id,)).fetchone()
        assert row["completion_meta"]["_name_local"] == "浅草寺"
        assert row["completion_meta"]["_judge"]["verdict"] == "accept"   # préservé

        # (2) fiche RETENUE (approved) à meta NULL → gagne le nom local au re-run.
        edb.upsert_pois(conn, property_id, "sight", [{
            "name": "Tokyo Tower", "lat": 35.65, "lon": 139.74, "source": "osm",
            "source_ref": "node/loc2"}])
        conn.execute("UPDATE pois SET status='approved', completion_meta=NULL "
                     "WHERE property_id=%s AND source_ref='node/loc2'", (property_id,))
        conn.commit()
        edb.upsert_pois(conn, property_id, "sight", [{
            "name": "Tokyo Tower JP", "lat": 35.65, "lon": 139.74, "source": "osm",
            "source_ref": "node/loc2",
            "completion_meta": {"_name_local": "東京タワー"}}])
        conn.commit()
        r2 = conn.execute(
            "SELECT name, status, completion_meta FROM pois WHERE property_id=%s AND "
            "source_ref='node/loc2'", (property_id,)).fetchone()
        assert r2["status"] == "approved"
        assert r2["completion_meta"]["_name_local"] == "東京タワー"   # RETENUE comblée
        assert r2["name"] == "Tokyo Tower"                            # `name` INTOUCHÉ (inv. 1)
        conn.execute("DELETE FROM pois WHERE property_id=%s AND source_ref IN "
                     "('node/loc1','node/loc2')", (property_id,))
        conn.commit()


# ── V2-35 : script ops de recensement des descriptions de remplissage ─────────

def test_ops_list_filler_descriptions_is_read_only(property_id):
    """Le script `ops/list_filler_descriptions.py` recense les descriptions de
    remplissage PAR LOGEMENT, en LECTURE SEULE (aucune écriture) — l'humain purge."""
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
    mod = importlib.import_module("list_filler_descriptions")

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        for name, cat, desc in [
            ("La Marquesa", "sight",
             "Site à visiter à Orihuela, accessible aux vacanciers."),   # remplissage
            ("Casa Pepe", "restaurant",
             "Restaurant de tapas réputé pour ses gambas al ajillo.")]:  # factuel
            conn.execute(
                """INSERT INTO pois (property_id, category_code, name, geom,
                                     description_md, source, source_ref, status)
                   VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                           %s, 'osm', %s, 'approved')""",
                (property_id, cat, name, desc, "n:" + name))
        conn.commit()
        groups = mod.find_filler_descriptions(conn, property_id)
        conn.commit()

    # Seul le remplissage est recensé (le factuel est épargné).
    assert len(groups) == 1
    assert {it["name"] for it in groups[0]["items"]} == {"La Marquesa"}
    # LECTURE SEULE : les deux descriptions restent INTACTES en base.
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        descs = {r["name"]: r["description_md"] for r in conn.execute(
            "SELECT name, description_md FROM pois WHERE property_id=%s", (property_id,))}
        assert descs["La Marquesa"].startswith("Site à visiter")
        assert descs["Casa Pepe"].startswith("Restaurant de tapas")


def test_ops_suspect_communes_flags_wrong_city_read_only(property_id):
    """V2-37 bonus : le script détecte une description qui affirme la commune du
    LOGEMENT alors que l'adresse OSM est ailleurs (cas Régence) — lecture seule."""
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
    mod = importlib.import_module("list_filler_descriptions")

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # Le logement est à « Orihuela Costa » (fixture) ; ce POI a une adresse à
        # Torrevieja mais sa description affirme « à Orihuela Costa » → SUSPECT.
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 description_md, source, source_ref, status)
               VALUES (%s, 'restaurant', 'Le Régence',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       '5 Calle Mayor, Torrevieja',
                       'Restaurant à Orihuela Costa réputé pour sa paella.',
                       'osm', 'n1', 'approved')""", (property_id,))
        # POI cohérent (adresse ET description à Orihuela Costa) → jamais suspect.
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 description_md, source, source_ref, status)
               VALUES (%s, 'restaurant', 'Casa Pepe',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       '2 Calle X, Orihuela Costa',
                       'Bar de tapas à Orihuela Costa.', 'osm', 'n2', 'approved')""",
            (property_id,))
        conn.commit()
        suspects = mod.find_suspect_communes(conn, property_id)
        conn.commit()

    assert len(suspects) == 1
    assert {it["name"] for it in suspects[0]["items"]} == {"Le Régence"}
    assert suspects[0]["items"][0]["address_city"] == "Torrevieja"
    # LECTURE SEULE : les descriptions restent intactes.
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                         "AND description_md IS NOT NULL", (property_id,)).fetchone()["c"]
        assert n == 2


def test_suspect_communes_prefers_locality_over_address_parsing(property_id):
    """V2-38 pièce 3 : la commune vient de `pois.locality` quand elle existe (donnée
    propre) — le faux positif « rue prise pour commune » (adresse sans virgule ni
    chiffre) disparaît. Repli parsing d'adresse pour l'existant sans locality."""
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
    mod = importlib.import_module("list_filler_descriptions")

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        # locality PROPRE = commune du logement → JAMAIS suspect, même si l'adresse
        # ('Route des Ecluses', sans virgule ni chiffre) serait prise pour une ville.
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 locality, description_md, source, source_ref, status)
               VALUES (%s, 'restaurant', 'Le Central',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       'Route des Ecluses', 'Orihuela Costa',
                       'Bistrot à Orihuela Costa, cuisine du marché.',
                       'osm', 'loc1', 'approved')""", (property_id,))
        # Sans locality, l'adresse ('5 Calle X, Torrevieja') fait foi (repli) → SUSPECT.
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, address,
                                 description_md, source, source_ref, status)
               VALUES (%s, 'restaurant', 'Le Régence',
                       ST_SetSRID(ST_MakePoint(-0.7, 37.9), 4326),
                       '5 Calle X, Torrevieja',
                       'Restaurant à Orihuela Costa réputé.', 'osm', 'loc2', 'approved')""",
            (property_id,))
        conn.commit()
        suspects = mod.find_suspect_communes(conn, property_id)
        conn.commit()

    flagged = {it["name"] for g in suspects for it in g["items"]}
    assert "Le Régence" in flagged        # repli adresse → détecté
    assert "Le Central" not in flagged    # locality propre = commune → jamais suspect


# ── V2-07 volet 3 : marchés hebdomadaires (découverte + matérialisation) ──────

def _insert_market(conn, property_id, name, weekday, lat, lon, status):
    conn.execute(
        """INSERT INTO pois (property_id, category_code, name, geom, weekday,
                             source, source_ref, status)
           VALUES (%s, 'market', %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s,
                   'owner', %s, %s)""",
        (property_id, name, lon, lat, weekday, "owner:" + name, status))


def test_markets_dedup_preserves_edited_and_rejected(property_id, http_client):
    """Le propriétaire a DÉJÀ un marché ÉDITÉ + un REJETÉ. La découverte, qui
    retrouve largement les mêmes, ne recrée NI l'un NI l'autre (jamais de
    résurrection d'un rejeté, jamais de doublon d'un édité) — seul un marché
    RÉELLEMENT nouveau est créé, en 'suggested'."""
    with psycopg.connect(settings.db_dsn) as conn:
        _insert_market(conn, property_id, "Mercadillo de La Zenia", 6, 37.930, -0.750,
                       "edited")
        _insert_market(conn, property_id, "Rastro de Los Dolses", 3, 37.940, -0.740,
                       "rejected")
        conn.commit()
    discovery = [
        {"name": "Mercado de la Zenia", "weekday": 6, "hours": "8h–14h",
         "address": "Zenia", "lat": 37.930, "lon": -0.750,
         "source_url": "https://x", "verified_on": "2026-08-12"},   # doublon (édité)
        {"name": "Rastro Los Dolses", "weekday": 3, "address": "Dolses",
         "lat": 37.9401, "lon": -0.7401, "source_url": "https://x",
         "verified_on": "2026-08-12"},                              # doublon (rejeté)
        {"name": "Mercadillo de Torrevieja", "weekday": 5, "hours": "9h–14h",
         "address": "Torrevieja", "lat": 37.980, "lon": -0.680,
         "source_url": "https://x", "verified_on": "2026-08-12"},   # RÉELLEMENT nouveau
    ]
    result = pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                          http_client=http_client,
                          anthropic_client=FakeAnthropic(markets=discovery))
    assert result["markets_created"] == 1
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        mkts = {m["name"]: m for m in conn.execute(
            "SELECT name, status, source FROM pois WHERE property_id=%s "
            "AND category_code='market'", (property_id,))}
        assert len(mkts) == 3                             # 2 propriétaire + 1 nouveau
        assert mkts["Mercadillo de La Zenia"]["status"] == "edited"      # intact
        assert mkts["Mercadillo de La Zenia"]["source"] == "owner"
        assert mkts["Rastro de Los Dolses"]["status"] == "rejected"      # jamais ressuscité
        assert mkts["Mercadillo de Torrevieja"]["status"] == "suggested"
        assert mkts["Mercadillo de Torrevieja"]["source"] == "claude"
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (result["job_id"],)).fetchone()["steps"]["markets"]
        assert step["created"] == 1 and step["skipped_duplicate"] == 2


def test_market_without_reliable_position_is_skipped(property_id, http_client):
    """Position non fiable (coordonnées aberrantes, aucune adresse géocodable) →
    marché SAUTÉ (jamais un marqueur ville) et journalisé (steps.skipped_position)."""
    discovery = [{"name": "Marché fantôme", "weekday": 4, "lat": 0.0, "lon": 0.0,
                  "source_url": "https://x", "verified_on": "2026-08-12"}]
    result = pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                          http_client=http_client,
                          anthropic_client=FakeAnthropic(markets=discovery))
    assert result["markets_created"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                         "AND category_code='market'", (property_id,)).fetchone()["c"]
        assert n == 0
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (result["job_id"],)).fetchone()["steps"]["markets"]
        assert step["skipped_position"] == 1 and step["created"] == 0


def test_market_discovery_is_mutualised_per_commune(property_id, http_client):
    """Deux logements d'une même commune PARTAGENT la découverte (cache area_facts) :
    le 2ᵉ logement ne déclenche AUCUN nouvel appel web de découverte."""
    fake = FakeAnthropic()   # PARTAGÉ entre les deux runs → compteur cumulatif
    pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                 http_client=http_client, anthropic_client=fake)
    assert fake.messages.market_calls == 1               # 1er logement : découverte

    oid2, pid2 = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'T')",
                     (oid2, f"{oid2}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city,
                                       country_code)
               VALUES (%s, %s, 'Villa B', 'Calle Ejemplo 2', 'Orihuela Costa', 'ES')""",
            (pid2, oid2))
        conn.commit()
    try:
        pipeline.run(pid2, use_claude=True, only_categories={"supermarket"},
                     http_client=http_client, anthropic_client=fake)
        assert fake.messages.market_calls == 1           # ZÉRO nouvel appel (mutualisé)
    finally:
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute("DELETE FROM owners WHERE id = %s", (oid2,))
            conn.commit()


def test_markets_double_malformed_counts_two_costs_and_writes_nothing(property_id,
                                                                      http_client):
    """V2-07 volet 3bis : deux essais malformés → ZÉRO écriture (ni area_fact ni POI)
    mais DEUX coûts comptabilisés (l'argent est dépensé à la réponse, pas au succès) ;
    l'étape marchés est journalisée en échec, le job reste 'done'."""
    result = pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                          http_client=http_client,
                          anthropic_client=FakeAnthropic(markets_malformed=True))
    assert result["markets_created"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                         "AND category_code='market'", (property_id,)).fetchone()["c"]
        assert n == 0                                    # aucune donnée écrite
        mk_fact = conn.execute(
            "SELECT 1 FROM area_facts WHERE country_code='ES' "
            "AND admin_area='Orihuela Costa' AND fact_type='markets'").fetchone()
        assert mk_fact is None                           # pas d'area_fact non plus
        mk_costs = conn.execute(
            "SELECT count(*) c FROM api_costs WHERE job_id=%s AND operation='markets'",
            (result["job_id"],)).fetchone()["c"]
        assert mk_costs == 2                             # essai + retry, tous deux payés
        job = conn.execute("SELECT status, steps FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done"
        assert job["steps"]["markets"]["ok"] is False and "cost_cts" in job["steps"]["markets"]


# ── M-18 : fiabilisation de la moisson (ré-essai + timeout aéroport) ──────────

from enrich import overpass  # noqa: E402

AIRPORT_EL = {"type": "node", "id": 999, "lat": 38.2822, "lon": -0.5581,
              "tags": {"name": "Aeropuerto de Alicante-Elche",
                       "aeroway": "aerodrome", "iata": "ALC"}}


class FlakyOverpass:
    """Overpass simulé : le palier aéroport (aeroway=aerodrome) échoue les
    `fail_airport_times` premières requêtes puis réussit. Compte les requêtes par
    sélecteur pour vérifier que le retry ne rejoue QUE les catégories manquantes."""

    def __init__(self, fail_airport_times=1):
        self.fail_airport_times = fail_airport_times
        self.airport_queries = 0
        self.supermarket_queries = 0

    def handler(self, request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            if '"aeroway"="aerodrome"' in body:
                self.airport_queries += 1
                if self.airport_queries <= self.fail_airport_times:
                    return httpx.Response(504)          # échec transitoire
                return httpx.Response(200, json={"elements": [AIRPORT_EL]})
            if '"shop"="supermarket"' in body:
                self.supermarket_queries += 1
            return httpx.Response(200, json=_overpass_payload(body))
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)


def _no_mirrors():
    """Contexte : un seul serveur Overpass → 1 POST par requête logique (mesure
    déterministe du nombre de requêtes dans les tests de retry)."""
    settings.politeness_delay_s = 0
    orig = settings.overpass_mirrors
    settings.overpass_mirrors = ()
    return orig


def test_dedup_merges_duplicate_airports_at_suggestion(property_id):
    """V2-40 bout-en-bout : OSM porte l'aéroport d'Alicante en DEUX éléments
    (« (ALC) » loin + « Miguel Hernández » proche) → UN SEUL POI suggéré (le plus
    proche survit, cas 52 vs 72), et le résumé + le journal comptent le doublon
    fusionné. Plus jamais deux Alicante à approuver."""
    orig = _no_mirrors()
    orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    miguel = {"type": "node", "id": 1001, "lat": 38.05, "lon": -0.66,   # proche
              "tags": {"name": "Aeropuerto de Alicante-Elche Miguel Hernández",
                       "aeroway": "aerodrome", "iata": "ALC"}}
    alc = {"type": "node", "id": 1002, "lat": 38.50, "lon": -0.55,      # loin
           "tags": {"name": "Aeropuerto de Alicante-Elche (ALC)",
                    "aeroway": "aerodrome", "iata": "ALC"}}

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            if '"aeroway"="aerodrome"' in body:
                return httpx.Response(200, json={"elements": [miguel, alc]})
            return httpx.Response(200, json={"elements": []})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=False,
                                   only_categories={"airport"},
                                   http_client=client,
                                   anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig
        settings.overpass_backoff_s = orig_backoff

    assert summary["duplicates_merged"] >= 1               # compté dans le résumé
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, drive_min FROM pois WHERE property_id=%s "
            "AND category_code='airport'", (property_id,)).fetchall()
        job = conn.execute(
            "SELECT steps FROM enrichment_jobs WHERE property_id=%s "
            "ORDER BY started_at DESC LIMIT 1", (property_id,)).fetchone()
    assert len(rows) == 1                                  # un seul aéroport (fusionné)
    assert "Miguel" in rows[0]["name"]                     # le plus proche a survécu
    assert job["steps"]["overpass"]["duplicates_merged"] >= 1   # journal de la passe


def test_bucket_timeout_airport_is_longer():
    """Le palier aéroport (100 km) utilise le timeout dédié plus long (M-18)."""
    assert overpass._bucket_timeout(100000) == settings.overpass_timeout_far_s
    assert overpass._bucket_timeout(2000) == settings.overpass_timeout_s
    # La requête du palier lointain embarque le timeout serveur long.
    q = overpass._build_query(['"aeroway"="aerodrome"'], 37.9, -0.7, 100000,
                              timeout_s=settings.overpass_timeout_far_s)
    assert f"[timeout:{settings.overpass_timeout_far_s}]" in q


def test_retry_recovers_airport_and_replays_only_missing(property_id):
    orig = _no_mirrors()
    orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0            # pas d'attente réelle en test (OPS-4)
    sleeps = []
    try:
        # OPS-4 : `_post_overpass` réessaie déjà `overpass_max_attempts` fois par run
        # (backoff sur 406/504…). Pour EXERCER le retry M-18 (au niveau pipeline), le
        # palier doit échouer TOUTES ces tentatives du 1er run → il retombe sur M-18.
        flaky = FlakyOverpass(fail_airport_times=settings.overpass_max_attempts)
        with httpx.Client(transport=httpx.MockTransport(flaky.handler)) as client:
            result = pipeline.run_with_retries(
                property_id, use_claude=True,
                only_categories={"airport", "supermarket"},
                http_client=client, anthropic_client=FakeAnthropic(),
                retry_delay_s=0, sleep=lambda s: sleeps.append(s))
    finally:
        settings.overpass_mirrors = orig
        settings.overpass_backoff_s = orig_backoff

    assert result["retries"] == 1 and not result["failed_categories"]
    assert sleeps == [0]                       # une attente avant le seul retry M-18
    # Aéroport : `max_attempts` requêtes au 1er run (toutes en échec, backoff interne)
    # + 1 au retry M-18 (succès). L'aéroport a max_radius_m = default → jamais d'escalade
    # V2-44 (pas de 2e passe). Le retry ne rejoue que les catégories manquantes.
    assert flaky.airport_queries == settings.overpass_max_attempts + 1
    # Supermarché : le 1er run le trouve mais SOUS le minimum (2 < MIN_RESULTS) → escalade
    # V2-44 au rayon max (passe 2) → 2 requêtes au 1er run ; JAMAIS rejoué au retry (il
    # n'était pas en échec). 2, pas 1.
    assert flaky.supermarket_queries == 2

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        cats = {r["category_code"] for r in conn.execute(
            "SELECT category_code FROM pois WHERE property_id=%s", (property_id,)).fetchall()}
        assert "airport" in cats and "supermarket" in cats
        job = conn.execute("SELECT steps, status FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done"          # le job reste 'done'
        assert job["steps"]["retry_1"]["ok"] is True
        assert "airport" in job["steps"]["retry_1"]["resolved"]


# ── OPS-4 : en-têtes Overpass, backoff 406/429, journal complet ───────────────

def test_overpass_sends_wildcard_accept_and_identifying_ua():
    """Pièce 1 : l'en-tête est `Accept: */*` (surtout PAS `application/json`, qui
    déclenche le 406 mod_negotiation d'overpass-api.de) et le User-Agent identifiant
    est présent sur chaque requête."""
    seen = {}

    def handler(request):
        seen["accept"] = request.headers.get("accept")
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"elements": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        overpass._post_overpass(client, "[out:json];out;")
    assert seen["accept"] == "*/*"
    assert seen["ua"] == settings.user_agent and seen["ua"]


def test_overpass_406_is_retryable_cycles_mirror_and_logs_full_body(caplog):
    """Pièce 1 : un 406 transitoire n'échoue pas le palier — on bascule sur le
    miroir suivant ; le CORPS COMPLET du 406 est journalisé (non tronqué)."""
    orig_mirrors = settings.overpass_mirrors
    orig_backoff = settings.overpass_backoff_s
    settings.overpass_mirrors = ("https://mirror.example/api/interpreter",)
    settings.overpass_backoff_s = 0
    body406 = ("<html><body><h1>Not Acceptable</h1><p>An appropriate representation "
               "could not be found. For more information about this error…</p></body></html>")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:  # le 1er serveur 406, le miroir répond
            return httpx.Response(406, text=body406)
        return httpx.Response(200, json={"elements": [{"type": "node", "id": 1}]})

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                caplog.at_level("WARNING"):
            els = overpass._post_overpass(client, "[out:json];out;")
    finally:
        settings.overpass_mirrors = orig_mirrors
        settings.overpass_backoff_s = orig_backoff

    assert els == [{"type": "node", "id": 1}]        # a basculé sur le miroir
    assert len(calls) == 2
    # Corps COMPLET côté logs (jamais tronqué à « For more informatio »).
    assert any("For more information about this error" in r.getMessage()
               for r in caplog.records)


def test_overpass_400_raises_short_error_without_cycling():
    """Pièce 1 : un 4xx NON transitoire (400 requête invalide) lève tout de suite,
    avec un message COURT pour `steps` (pas de cyclage inutile des miroirs)."""
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="line 1: parse error: bad query " * 20)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError) as ei:
            overpass._post_overpass(client, "bad")
    assert ei.value.status == 400 and len(calls) == 1
    assert str(ei.value).startswith("HTTP 400 de") and len(str(ei.value)) < 60


def test_short_truncation_is_word_clean():
    """La troncature pour `steps` coupe sur un mot et suffixe « … » (fini le
    « For more informatio » à cru)."""
    s = overpass._short("For more information about this particular error condition",
                        limit=20)
    assert s.endswith("…") and len(s) <= 21
    assert not s[:-1].endswith(" ") and "informatio…" not in s


def test_pipeline_closes_internally_created_anthropic_client(property_id, http_client,
                                                             monkeypatch):
    """Pièce 4 (sortie propre) : le client Anthropic créé DANS le pipeline (chemin CLI,
    `anthropic_client=None`) est fermé EXPLICITEMENT → son pool httpx ne retient plus
    la sortie du process. Un client PASSÉ (API/tests) n'est jamais fermé par le
    pipeline (il appartient à l'appelant)."""
    closed: list[bool] = []

    class SpyAnthropic(FakeAnthropic):
        def close(self):
            closed.append(True)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(pipeline.anthropic, "Anthropic", lambda **kw: SpyAnthropic())
    # anthropic_client=None → le pipeline crée le client (et doit le fermer).
    pipeline.run(property_id, use_claude=True, only_categories={"supermarket"},
                 http_client=http_client)
    assert closed == [True]


def test_retry_gives_up_after_max_attempts(property_id):
    orig = _no_mirrors()
    sleeps = []
    try:
        flaky = FlakyOverpass(fail_airport_times=99)   # échoue toujours
        with httpx.Client(transport=httpx.MockTransport(flaky.handler)) as client:
            result = pipeline.run_with_retries(
                property_id, use_claude=False,
                only_categories={"airport", "supermarket"},
                http_client=client, anthropic_client=FakeAnthropic(),
                max_retries=3, retry_delay_s=0, sleep=lambda s: sleeps.append(s))
    finally:
        settings.overpass_mirrors = orig

    assert result["retries"] == 3 and len(sleeps) == 3
    assert "airport" in result["failed_categories"]
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT steps, status FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert {"retry_1", "retry_2", "retry_3"} <= set(job["steps"])
        assert job["status"] == "done"          # terminé normalement malgré l'échec


def test_retry_preserves_arbitrated_pois(property_id):
    """Invariant 1 : un POI arbitré n'est jamais écrasé par un retry."""
    orig = _no_mirrors()
    try:
        # POI aéroport DÉJÀ approuvé (même source_ref que renverra le retry).
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute(
                """INSERT INTO pois (property_id, category_code, name, geom,
                       source, source_ref, status)
                   VALUES (%s,'airport','Mon aéroport à moi',
                       ST_SetSRID(ST_MakePoint(-0.5581,38.2822),4326),
                       'osm','node/999','approved')""", (property_id,))
            conn.commit()
        flaky = FlakyOverpass(fail_airport_times=1)
        with httpx.Client(transport=httpx.MockTransport(flaky.handler)) as client:
            pipeline.run_with_retries(
                property_id, use_claude=False, only_categories={"airport"},
                http_client=client, anthropic_client=FakeAnthropic(),
                retry_delay_s=0, sleep=lambda _s: None)
    finally:
        settings.overpass_mirrors = orig

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, status FROM pois WHERE property_id=%s AND category_code='airport'",
            (property_id,)).fetchall()
    assert len(rows) == 1                         # aucun doublon
    assert rows[0]["status"] == "approved"        # choix conservé (invariant 1)
    assert rows[0]["name"] == "Mon aéroport à moi"  # non écrasé par le retry


# ── V2-07 volet 1 : livraison de repas par zone (mutualisation & rejet) ───────

def test_food_delivery_shared_across_properties_same_commune(http_client):
    """Mutualisation prouvée : deux logements d'une même commune partagent le
    résultat de livraison de repas — le 2ᵉ ne déclenche AUCUN nouvel appel (la
    fenêtre de fraîcheur, pilotée par fetched_at, coupe l'appel)."""
    fake = FakeAnthropic()
    oid = str(uuid.uuid4())
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s, %s, 'T')",
                     (oid, f"{oid}@test.local"))
        for pid in ids:
            conn.execute(
                """INSERT INTO properties (id, owner_id, name, address_line1, city,
                                           country_code)
                   VALUES (%s, %s, 'Villa', 'Calle Ejemplo 1', 'Orihuela Costa', 'ES')""",
                (pid, oid))
        conn.commit()
    try:
        for pid in ids:
            pipeline.run(pid, use_claude=True, trigger="initial",
                         only_categories={"supermarket"},
                         http_client=http_client, anthropic_client=fake)
        # Le résultat est calculé UNE fois pour la commune, réutilisé ensuite.
        assert fake.messages.food_delivery_calls == 1
        with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
            n = conn.execute(
                """SELECT count(*) n FROM area_facts WHERE country_code='ES'
                   AND admin_area='Orihuela Costa' AND fact_type='food_delivery'"""
            ).fetchone()["n"]
        assert n == 1
    finally:
        with psycopg.connect(settings.db_dsn) as conn:
            conn.execute("DELETE FROM owners WHERE id = %s", (oid,))
            conn.execute("DELETE FROM area_facts WHERE country_code = 'ES'")
            conn.commit()


def test_food_delivery_malformed_rejected_without_write(property_id, http_client):
    """JSON malformé → rejeté SANS écriture de données, et le job réussit quand même
    (best-effort). MAIS le coût des essais est comptabilisé (V2-07 volet 3bis : l'argent
    est dépensé à la réponse) : DEUX lignes 'food_delivery' (essai + retry régénéré),
    tandis qu'AUCUN area_fact 'food_delivery' n'est écrit."""
    result = pipeline.run(
        property_id, use_claude=True, trigger="initial",
        only_categories={"supermarket"}, http_client=http_client,
        anthropic_client=FakeAnthropic(food_delivery_malformed=True))
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT status, steps FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done" and job["steps"]["claude"]["ok"]
        assert job["steps"]["food_delivery"]["ok"] is False   # étape en échec, journalisée
        facts = {r["fact_type"] for r in conn.execute(
            "SELECT fact_type FROM area_facts WHERE country_code='ES' "
            "AND admin_area='Orihuela Costa'")}
        # 'markets'/'activities' présents (mutualisés) ; PAS de 'food_delivery' (rejeté).
        assert facts == {"emergency_numbers", "waste_rules", "noise_rules",
                         "markets", "activities"}
        fd_costs = conn.execute(
            "SELECT count(*) c FROM api_costs WHERE job_id=%s AND operation='food_delivery'",
            (result["job_id"],)).fetchone()["c"]
        assert fd_costs == 2                                   # essai + retry, tous deux payés


def test_describe_failure_is_best_effort_job_stays_done(property_id, http_client):
    """V2-37 1bis : une réponse descriptions NON parsable ne tue plus le JOB (elle le
    tuait — Ardon 16/08). L'étape est journalisée en échec (compteur + raison avec
    stop_reason), le coût des DEUX essais est comptabilisé, les AUTRES étapes IA
    (baby-sitting, marchés) s'exécutent, et le job finit 'done'. Un manque de
    description n'est pas une corruption."""
    result = pipeline.run(
        property_id, use_claude=True, only_categories={"restaurant", "supermarket"},
        http_client=http_client,
        anthropic_client=FakeAnthropic(describe_malformed=True))
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT status, steps FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
        assert job["status"] == "done"                        # le job N'EST PAS tué
        dp = job["steps"]["describe_pois"]
        assert dp["ok"] is False and dp["described"] == 0     # échec journalisé + compteur
        assert "max_tokens" in dp["error"]                    # raison (stop_reason gravé)
        # Les autres étapes IA se sont exécutées malgré l'échec descriptions.
        assert job["steps"]["babysitter"]["ok"] and job["steps"]["markets"]["ok"]
        assert job["steps"]["claude"]["ok"]
        # Coût des DEUX essais describe comptabilisé (l'argent est dépensé à la réponse).
        n = conn.execute(
            "SELECT count(*) c FROM api_costs WHERE job_id=%s AND operation='describe_pois'",
            (result["job_id"],)).fetchone()["c"]
        assert n == 2
        # Le restaurant existe mais SANS description (un manque, pas une corruption).
        resto = conn.execute("SELECT description_md FROM pois WHERE property_id=%s "
                             "AND name='La Marejada'", (property_id,)).fetchone()
        assert resto["description_md"] is None


# ── V2-44 : aéroports civils, capés aux 3 plus proches en temps de trajet ─────

def test_airport_capped_to_three_nearest_civil(property_id):
    """5 aéroports CIVILS (avec IATA) + 1 base militaire → la base est exclue et on
    ne garde que les 3 civils les plus proches EN TEMPS DE TRAJET (benchmark : 7
    aéroports dont Ostende à 132 min)."""
    orig = _no_mirrors()
    orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0

    def apt(id_, name, dlat, **extra):
        return {"type": "node", "id": id_, "lat": PROP_LAT + dlat, "lon": PROP_LON,
                "tags": {"name": name, "aeroway": "aerodrome", **extra}}

    # Croissants en distance (≈ 10, 20, 30, 40, 50 km). La base est plus PROCHE (5 km)
    # mais militaire → exclue quand même.
    civils = [apt(1, "Alpha", 0.090, iata="AAA"), apt(2, "Bravo", 0.180, iata="BBB"),
              apt(3, "Charlie", 0.270, iata="CCC"), apt(4, "Delta", 0.360, iata="DDD"),
              apt(5, "Echo", 0.450, iata="EEE")]
    military = apt(6, "Vliegbasis Woensdrecht", 0.050, military="airfield")
    els = civils + [military]

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            return httpx.Response(200, json={
                "elements": els if '"aeroway"="aerodrome"' in body else []})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=False,
                                   only_categories={"airport"}, http_client=client,
                                   anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig
        settings.overpass_backoff_s = orig_backoff

    assert summary["pois"] == 3
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='airport' "
            "ORDER BY drive_min", (property_id,)).fetchall()]
    # Militaire exclu ; les 3 civils les plus proches seulement.
    assert names == ["Alpha", "Bravo", "Charlie"]


# ── V2-44 : catégories sans résultat signalées (résumé + steps) ──────────────

def test_empty_categories_reported_in_steps_and_summary(property_id):
    """Une catégorie où l'on ne trouve RIEN (pas une erreur) est nommée dans le résumé
    du run ET dans enrichment_jobs.steps.overpass.empty."""
    orig = _no_mirrors()
    orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            els = (OVERPASS_BY_CATEGORY["supermarket"]
                   if '"shop"="supermarket"' in body else [])   # laverie & plage vides
            return httpx.Response(200, json={"elements": els})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(
                property_id, use_claude=False,
                only_categories={"supermarket", "laundry", "beach"},
                http_client=client, anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig
        settings.overpass_backoff_s = orig_backoff

    assert set(summary["empty_categories"]) == {"laundry", "beach"}
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT steps FROM enrichment_jobs WHERE property_id=%s "
                           "ORDER BY started_at DESC LIMIT 1", (property_id,)).fetchone()
    assert set(job["steps"]["overpass"]["empty"]) == {"laundry", "beach"}
    assert job["steps"]["overpass"]["failed"] == {}    # vide ≠ échec


# ── V2-44 volet 2 : location par découverte web avec preuve ──────────────────

_KASSTEELE = {"name": "Kassteele Tweewielers", "address": "Kloosterweg 44, Noordgouwe",
              "phone": "+31 111 22 33", "website": "https://kassteele.nl",
              "source_url": "https://kassteele.nl/verhuur", "verified_on": "2026-08-31"}
_RENTER_DLAT = 0.0135   # ~1,5 km au nord du logement


def _rental_handler(*, renter_coords, osm_rental=None, renter_key="Kloosterweg"):
    """MockTransport : nominatim renvoie les coords du logement, sauf pour l'adresse du
    loueur (clé) → coords du loueur avec addressdetails (locality). Overpass renvoie
    `osm_rental` (défaut : aucun loueur OSM). OSRM = payload standard."""
    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            dec = urllib.parse.unquote_plus(url)
            if "Orihuela" in dec or "Calle Ejemplo" in dec:   # requête du LOGEMENT
                return httpx.Response(200, json=NOMINATIM)
            for key, (lat, lon, addr) in renter_coords.items():   # requête d'un LOUEUR
                if key in dec:
                    return httpx.Response(200, json=[{
                        "lat": str(lat), "lon": str(lon), "type": "house",
                        "class": "building", "display_name": key, "address": addr}])
            return httpx.Response(200, json=[])   # loueur non géocodable → écarté
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            els = (osm_rental or []) if ('"amenity"="bicycle_rental"' in body
                                         or '"amenity"="car_rental"' in body) else []
            return httpx.Response(200, json={"elements": els})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)
    return handler


def test_web_rental_discovered_geocoded_and_suggested(property_id):
    """Cas d'or Kassteele : un loueur ABSENT d'OSM est trouvé par le web, géocodé par
    son adresse (~1,5 km), et proposé avec TOUS ses champs, sa source 'web', sa preuve
    et sa localité (V2-38)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    rlat, rlon = PROP_LAT + _RENTER_DLAT, PROP_LON
    handler = _rental_handler(renter_coords={
        "Kloosterweg": (rlat, rlon, {"village": "Noordgouwe",
                                     "municipality": "Schouwen-Duiveland"})})
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental"}, http_client=client,
                                   anthropic_client=FakeAnthropic(rentals=[_KASSTEELE]))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff

    assert summary["rental_web_kept"] == 1
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        r = conn.execute(
            "SELECT name, source, status, phone, website, address, locality, walk_min,"
            " drive_min, completion_meta, ST_Y(geom) lat, ST_X(geom) lon "
            "FROM pois WHERE property_id=%s AND category_code='rental'",
            (property_id,)).fetchone()
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (summary["job_id"],)).fetchone()["steps"]["rental_web"]
    assert r["name"] == "Kassteele Tweewielers" and r["source"] == "web"
    assert r["status"] == "suggested" and r["phone"] and r["website"]
    assert r["address"] == "Kloosterweg 44, Noordgouwe"
    assert r["locality"] == "Noordgouwe"                 # V2-38 depuis le géocodage
    assert r["completion_meta"]["_web"]["source_url"].startswith("http")
    assert r["walk_min"] and r["drive_min"]              # distances OSRM calculées
    assert r["lat"] == pytest.approx(rlat) and r["lon"] == pytest.approx(rlon)
    assert 1000 < overpass.haversine_m(PROP_LAT, PROP_LON, r["lat"], r["lon"]) < 2000
    assert step["ok"] and step["kept"] == 1 and step["skipped_geocode"] == 0


def test_web_rental_discarded_when_address_not_geocodable(property_id):
    """Adresse non géocodable → loueur ÉCARTÉ (jamais de POI sans position), journalisé."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    ghost = {**_KASSTEELE, "name": "Loueur Fantôme", "address": "Rue Introuvable 99"}
    handler = _rental_handler(renter_coords={})   # aucune adresse ne matche → []
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental"}, http_client=client,
                                   anthropic_client=FakeAnthropic(rentals=[ghost]))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    assert summary["rental_web_kept"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                        "AND category_code='rental'", (property_id,)).fetchone()["c"]
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (summary["job_id"],)).fetchone()["steps"]["rental_web"]
    assert n == 0 and step["kept"] == 0 and step["skipped_geocode"] == 1


def test_osm_and_web_rental_merge_web_wins(property_id):
    """Doublon OSM/web du même loueur → UNE fiche (V2-40) : le web (tél+site) gagne
    sur l'OSM (nom seul)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    rlat, rlon = PROP_LAT + _RENTER_DLAT, PROP_LON
    osm_el = {"type": "node", "id": 700, "lat": rlat, "lon": rlon,
              "tags": {"name": "Kassteele Tweewielers", "amenity": "bicycle_rental"}}
    handler = _rental_handler(
        renter_coords={"Kloosterweg": (rlat, rlon, {"village": "Noordgouwe"})},
        osm_rental=[osm_el])
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental"}, http_client=client,
                                   anthropic_client=FakeAnthropic(rentals=[_KASSTEELE]))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, source, phone, website FROM pois WHERE property_id=%s "
            "AND category_code='rental'", (property_id,)).fetchall()
    assert len(rows) == 1                              # une seule fiche (fusion V2-40)
    assert rows[0]["source"] == "web"                  # le mieux renseigné a gagné
    assert rows[0]["phone"] and rows[0]["website"]
    assert summary["duplicates_merged"] >= 1


def test_web_rentals_capped_to_three_nearest(property_id):
    """5 loueurs web → seuls les 3 plus proches sont retenus (plafond)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    # Noms DISTINCTS (sinon la passe V2-40 les fusionnerait par similarité de nom) et
    # distances croissantes ; les 3 plus proches doivent survivre au plafond.
    labels = ["Fietsen Anna", "Bike Bob", "Cycles Carla", "Boten Dirk", "Ski Eva"]
    renters = [{**_KASSTEELE, "name": labels[i - 1], "address": f"Straat {i}, Dorp",
                "website": f"https://l{i}.nl", "source_url": f"https://l{i}.nl"}
               for i in range(1, 6)]
    coords = {f"Straat {i},": (PROP_LAT + 0.01 * i, PROP_LON, {"village": "Dorp"})
              for i in range(1, 6)}
    handler = _rental_handler(renter_coords=coords)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental"}, http_client=client,
                                   anthropic_client=FakeAnthropic(rentals=renters))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    assert summary["rental_web_kept"] == 3
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='rental'",
            (property_id,)).fetchall()}
    assert names == {"Fietsen Anna", "Bike Bob", "Cycles Carla"}   # les 3 plus proches


def test_web_rental_never_touches_arbitrated(property_id):
    """Une fiche loueur ARBITRÉE (approved) n'est jamais touchée par la découverte web
    (V2-40 filter_against_existing)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    rlat, rlon = PROP_LAT + _RENTER_DLAT, PROP_LON
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom, source,
                   source_ref, status, phone)
               VALUES (%s,'rental','Kassteele Tweewielers',
                   ST_SetSRID(ST_MakePoint(%s,%s),4326),'owner','owner:1','approved',
                   '+31 999')""", (property_id, rlon, rlat))
        conn.commit()
    handler = _rental_handler(
        renter_coords={"Kloosterweg": (rlat, rlon, {"village": "Noordgouwe"})})
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            pipeline.run(property_id, use_claude=True, only_categories={"rental"},
                         http_client=client,
                         anthropic_client=FakeAnthropic(rentals=[_KASSTEELE]))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, source, status, phone FROM pois WHERE property_id=%s "
            "AND category_code='rental'", (property_id,)).fetchall()
    assert len(rows) == 1                       # le web n'a pas re-proposé l'arbitrée
    assert rows[0]["status"] == "approved" and rows[0]["source"] == "owner"
    assert rows[0]["phone"] == "+31 999"        # non écrasée (invariant 1)


def test_web_rental_malformed_is_best_effort_and_costs_per_attempt(property_id):
    """JSON loueurs malformé → best-effort : aucune fiche, job 'done', et le coût des
    DEUX essais (régénération V2-37) comptabilisé (« coût compté par essai »)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    handler = _rental_handler(renter_coords={})
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental"}, http_client=client,
                                   anthropic_client=FakeAnthropic(rentals="malformed"))
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    assert summary["rental_web_kept"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        job = conn.execute("SELECT status, steps FROM enrichment_jobs WHERE id=%s",
                           (summary["job_id"],)).fetchone()
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                        "AND category_code='rental'", (property_id,)).fetchone()["c"]
        costs = conn.execute(
            "SELECT count(*) c FROM api_costs WHERE job_id=%s AND operation='rental_web'",
            (summary["job_id"],)).fetchone()["c"]
    assert job["status"] == "done"                       # best-effort, pas tué
    assert job["steps"]["rental_web"]["ok"] is False
    assert n == 0 and costs == 2                          # essai + retry, tous deux payés


# ── V2-44 volet 3 : minimums par catégorie + plafond de pertinence ───────────

def _police_handler(elements, osrm_drive_s):
    """MockTransport : nominatim (logement) + overpass (police = `elements`) + OSRM
    renvoyant `osrm_drive_s` secondes par destination (pour piloter drive_min)."""
    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            els = elements if '"amenity"="police"' in body else []
            return httpx.Response(200, json={"elements": els})
        if "/table/v1/" in url:
            coords = url.split("/table/v1/", 1)[1].split("/", 1)[1].split("?")[0]
            n = coords.count(";")
            return httpx.Response(200, json={
                "code": "Ok", "durations": [[0.0] + [float(osrm_drive_s)] * n],
                "distances": [[0.0] + [22000.0] * n]})
        return httpx.Response(404)
    return handler


def test_drive_cap_drops_far_escalated_result(property_id):
    """Aucun commissariat dans le rayon de préférence ; le seul (à ~22 km / 31 min)
    est amené par l'escalade mais retiré par le plafond (20 min) → catégorie vide
    signalée, plutôt qu'un résultat lointain et trompeur."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    far = {"type": "node", "id": 1, "lat": PROP_LAT + 0.20, "lon": PROP_LON,  # ~22 km
           "tags": {"name": "Politie Lointaine", "amenity": "police"}}
    handler = _police_handler([far], osrm_drive_s=1860)   # 31 min de route
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=False,
                                   only_categories={"police"}, http_client=client,
                                   anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    assert summary["hard_cap_dropped"] >= 1
    assert "police" in summary["empty_categories"]          # vidée par le plafond, signalée
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s "
                        "AND category_code='police'", (property_id,)).fetchone()["c"]
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                            (summary["job_id"],)).fetchone()["steps"]["overpass"]
    assert n == 0
    assert step["hard_cap_dropped"] >= 1 and "police" in step["empty"]


def test_near_police_within_cap_is_kept(property_id):
    """Un commissariat proche (~4 km / 6 min, DANS la préférence) est retenu :
    le plafond ne vise que l'escalade."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    near = {"type": "node", "id": 1, "lat": PROP_LAT + 0.036, "lon": PROP_LON,  # ~4 km
            "tags": {"name": "Politie Zierikzee", "amenity": "police"}}
    handler = _police_handler([near], osrm_drive_s=360)     # 6 min
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=False,
                                   only_categories={"police"}, http_client=client,
                                   anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    assert summary["hard_cap_dropped"] == 0
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='police'",
            (property_id,)).fetchall()]
    assert names == ["Politie Zierikzee"]


def test_min_one_suppresses_far_police_when_near_exists(property_id):
    """Recette : quand un commissariat existe à moins de 10 km, la catégorie police
    (min_results=1) ne propose PLUS de candidat lointain — le lointain n'est même pas
    moissonné (pas d'escalade)."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    near = {"type": "node", "id": 1, "lat": PROP_LAT + 0.036, "lon": PROP_LON,  # ~4 km
            "tags": {"name": "Politie Proche", "amenity": "police"}}
    far = {"type": "node", "id": 2, "lat": PROP_LAT + 0.20, "lon": PROP_LON,    # ~22 km
           "tags": {"name": "Politie Lointaine", "amenity": "police"}}
    handler = _police_handler([near, far], osrm_drive_s=360)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            pipeline.run(property_id, use_claude=False, only_categories={"police"},
                         http_client=client, anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='police'",
            (property_id,)).fetchall()}
    assert names == {"Politie Proche"}          # le lointain n'est pas proposé


# ── V2-46 : géocodage incohérent → le pipeline s'arrête, aucune moisson ───────

def test_pipeline_aborts_on_geocode_mismatch_and_harvests_nothing(property_id):
    """Un géocodage frais qui résout une rue homonyme dans une AUTRE commune (cas CASA
    MURCIA) doit STOPPER le job (échec motivé) sans moissonner 132 POI hors sujet ; la
    position est enregistrée en 'mismatch' pour l'ajustement propriétaire."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    # Le logement est saisi « Orihuela Costa » ; Nominatim renvoie Torre-Pacheco.
    mismatched = [{"lat": "37.74", "lon": "-0.95", "type": "house", "class": "building",
                   "display_name": "Rue homonyme",
                   "address": {"town": "Torre-Pacheco", "municipality": "Torre-Pacheco",
                               "county": "Murcia", "postcode": "30700"}}]

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=mismatched)
        if "overpass" in url:            # ne devrait JAMAIS être appelé
            return httpx.Response(200, json={"elements": [
                {"type": "node", "id": 1, "lat": 37.74, "lon": -0.95,
                 "tags": {"name": "Ne devrait pas apparaître", "amenity": "police"}}]})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(Exception):
                pipeline.run(property_id, use_claude=False,
                             only_categories={"police"}, http_client=client,
                             anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        n = conn.execute("SELECT count(*) c FROM pois WHERE property_id=%s",
                        (property_id,)).fetchone()["c"]
        prop = conn.execute("SELECT geocode_accuracy FROM properties WHERE id=%s",
                           (property_id,)).fetchone()
        job = conn.execute("SELECT status, steps FROM enrichment_jobs WHERE property_id=%s "
                          "ORDER BY started_at DESC LIMIT 1", (property_id,)).fetchone()
    assert n == 0                                        # AUCUNE moisson
    assert prop["geocode_accuracy"] == "mismatch"        # position marquée pour ajustement
    assert job["status"] == "failed"                     # job échoué proprement
    assert job["steps"]["geocode"]["ok"] is False and job["steps"]["geocode"]["reason"]


# ── V2-47 : cantonner OSM — banques en distributeur, réseau vélo réduit (bout-en-bout) ─

def test_v247_reductions_end_to_end(property_id):
    """Un run réel : une AGENCE bancaire entre comme distributeur, 4 stations MUyBICI se
    réduisent à une seule, et le journal compte la réduction réseau."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    # Banque et crypto ESPACÉS de > 150 m pour ne pas déclencher la fusion V2-40
    # (distance) — on teste ici l'admission des banques, pas la fusion de proximité.
    bank = {"type": "node", "id": 10, "lat": PROP_LAT + 0.006, "lon": PROP_LON,
            "tags": {"name": "Banco Santander", "amenity": "bank"}}
    crypto = {"type": "node", "id": 11, "lat": PROP_LAT + 0.001, "lon": PROP_LON,
              "tags": {"name": "BitBase", "amenity": "atm"}}
    muybici = [{"type": "node", "id": 20 + i, "lat": PROP_LAT + 0.001 * i,
                "lon": PROP_LON, "tags": {"name": f"MUyBICI: Estación {i}",
                                          "amenity": "bicycle_rental",
                                          "operator": "MUyBICI"}} for i in range(1, 5)]

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            els = []
            if '"amenity"="bank"' in body or '"amenity"="atm"' in body:
                els += [bank, crypto]
            if '"amenity"="bicycle_rental"' in body or '"amenity"="car_rental"' in body:
                els += muybici
            return httpx.Response(200, json={"elements": els})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=False,
                                   only_categories={"atm", "rental"},
                                   http_client=client, anthropic_client=FakeAnthropic())
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff

    # ≥ 3 : le réseau MUyBICI est réduit (chaque passe de moisson réduit la sienne —
    # l'escalade rurale re-moissonne rental, restée sous son minimum après collapse).
    assert summary["network_dropped"] >= 3
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        atms = [r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='atm' "
            "ORDER BY drive_min", (property_id,)).fetchall()]
        rentals = [(r["name"], r["completion_meta"]) for r in conn.execute(
            "SELECT name, completion_meta FROM pois WHERE property_id=%s "
            "AND category_code='rental'", (property_id,)).fetchall()]
        step = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                           (summary["job_id"],)).fetchone()["steps"]["overpass"]
    # La banque est ADMISE comme distributeur (le fix central) ; le crypto reste (déprio,
    # pas exclu). L'ordre de sélection banque-avant-crypto est couvert par le test unité.
    assert "Banco Santander" in atms and "BitBase" in atms
    # V2-51b : une seule station, nom NU + marqueur (suffixe localisé à l'affichage).
    assert [n for n, _ in rentals] == ["MUyBICI"]
    assert rentals[0][1]["_nearest_of_network"] is True
    assert step["network_dropped"] >= 3


# ── V2-50 : règles de service — contactabilité + qualification (bout-en-bout) ─

def test_v250_service_rules_qualify_and_drop(property_id):
    """Un loueur qualifié par le web (Gregorio → « fourgonnettes/camions ») est GARDÉ et
    qualifié ; un loueur sans contact ni sous-type est RETIRÉ ; un loueur déjà contactable
    reste. Non-régression : la boulangerie (hors périmètre service) n'est jamais touchée."""
    orig = _no_mirrors(); orig_backoff = settings.overpass_backoff_s
    settings.overpass_backoff_s = 0
    # Espacés > 150 m pour ne pas déclencher la fusion V2-40 (on teste ici les règles
    # de service, pas la dédup de proximité).
    rentals = [
        {"type": "node", "id": 40, "lat": PROP_LAT + 0.001, "lon": PROP_LON,
         "tags": {"name": "Alquiler Furgonetas Gregorio", "amenity": "car_rental",
                  "phone": "968 850 081"}},          # contactable, à qualifier
        {"type": "node", "id": 41, "lat": PROP_LAT + 0.006, "lon": PROP_LON,
         "tags": {"name": "Loueur Fantôme", "amenity": "car_rental"}},   # rien → retiré
    ]
    bakery = [{"type": "node", "id": 50, "lat": PROP_LAT + 0.001, "lon": PROP_LON,
               "tags": {"name": "Boulangerie Sans Tel", "shop": "bakery"}}]  # hors périmètre

    def handler(request):
        url = str(request.url)
        if "nominatim" in url:
            return httpx.Response(200, json=NOMINATIM)
        if "overpass" in url:
            body = urllib.parse.unquote_plus(request.read().decode())
            els = []
            if '"amenity"="car_rental"' in body or '"amenity"="bicycle_rental"' in body:
                els += rentals
            if '"shop"="bakery"' in body:
                els += bakery
            return httpx.Response(200, json={"elements": els})
        if "/table/v1/" in url:
            return httpx.Response(200, json=_osrm_payload(url))
        return httpx.Response(404)

    qual = [{"name": "Alquiler Furgonetas Gregorio", "phone": "968 850 081",
             "subtype": "fourgonnettes et camions", "source_url": "https://greg.example"}]
    fake = FakeAnthropic(rentals=[], service_qualifications=qual)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary = pipeline.run(property_id, use_claude=True,
                                   only_categories={"rental", "bakery"},
                                   http_client=client, anthropic_client=fake)
    finally:
        settings.overpass_mirrors = orig; settings.overpass_backoff_s = orig_backoff

    assert summary["service_dropped"] >= 1 and summary["service_qualified"] >= 1
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rentals_db = {r["name"]: r for r in conn.execute(
            "SELECT name, description_md, completion_meta FROM pois WHERE property_id=%s "
            "AND category_code='rental'", (property_id,)).fetchall()}
        bakery_db = [r["name"] for r in conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='bakery'",
            (property_id,)).fetchall()]
    assert "Alquiler Furgonetas Gregorio" in rentals_db      # qualifié → gardé
    assert "Loueur Fantôme" not in rentals_db                # non contactable ni qualifiable
    greg = rentals_db["Alquiler Furgonetas Gregorio"]
    assert greg["description_md"] == "fourgonnettes et camions"   # sous-type dans la fiche
    assert greg["completion_meta"]["_qualification"]["subtype"] == "fourgonnettes et camions"
    # Non-régression : la boulangerie (commerce de passage) n'est jamais retirée.
    assert bakery_db == ["Boulangerie Sans Tel"]


# ── V2-52 volet 1 : fusion de sources Overture ────────────────────────────────
# Overture est un flux réseau (DuckDB/S3) → INJECTÉ ici (aucun réseau). Le flag
# `overture_enabled` reste OFF (conftest) : passer un fetcher l'emporte sur le flag.

def _ovt_place(name, category, lat, lon, **f):
    """Un lieu Overture à la surface du fetcher de production (`enrich.overture`)."""
    return {"name": name, "lat": lat, "lon": lon, "category": category,
            "phone": f.get("phone"), "website": f.get("website"),
            "source_ref": f.get("ref", f"gers:{name}")}


def test_overture_fills_empty_atm_with_banks(property_id, http_client):
    """Gains 1+2 : OSM ne moissonne aucun distributeur (mock overpass sans 'atm') →
    Overture comble avec des banques NOMMÉES (téléphone présent), source 'overture'."""
    def fetch(lat, lon, radius):
        # Espacés > 150 m (sinon la dédup V2-40 les prendrait pour le même lieu).
        return [_ovt_place("Banco Santander", "bank_credit_union",
                           PROP_LAT + 0.001, PROP_LON, phone="+34 900 111", ref="gers:s"),
                _ovt_place("CaixaBank", "bank_credit_union",
                           PROP_LAT + 0.003, PROP_LON + 0.002, phone="+34 900 222",
                           ref="gers:c")]

    result = pipeline.run(property_id, use_claude=False, only_categories={"atm"},
                          http_client=http_client, anthropic_client=FakeAnthropic(),
                          overture_fetch=fetch)
    assert result["overture_added"] == 2

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, source, source_ref, phone, status FROM pois "
            "WHERE property_id=%s AND category_code='atm' ORDER BY name",
            (property_id,)).fetchall()
        job = conn.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
    assert {r["name"] for r in rows} == {"Banco Santander", "CaixaBank"}
    assert all(r["source"] == "overture" and r["source_ref"].startswith("gers:")
               for r in rows)
    assert all(r["phone"] and r["status"] == "suggested" for r in rows)
    # Étape Overture tracée dans le journal du job.
    assert job["steps"]["overture"]["ok"] is True
    assert job["steps"]["overture"]["mapped"] == 2


def test_overture_enriches_osm_contact_without_adding(property_id, http_client):
    """Gain 3 : un POI OSM retenu reçoit tél/site d'Overture par appariement sûr —
    source reste 'osm', provenance tracée, AUCUN POI ajouté (le lieu apparié est
    consommé, il ne repart pas en comblement)."""
    def fetch(lat, lon, radius):
        return [_ovt_place("Mercadona", "grocery_store", 37.9310, -0.7510,
                           phone="+34 966 000 000", website="https://mercadona.es",
                           ref="gers:merca")]

    result = pipeline.run(property_id, use_claude=False, only_categories={"supermarket"},
                          http_client=http_client, anthropic_client=FakeAnthropic(),
                          overture_fetch=fetch)
    assert result["overture_contacts"] == 1 and result["overture_added"] == 0

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, source, phone, website, completion_meta FROM pois "
            "WHERE property_id=%s AND category_code='supermarket' ORDER BY name",
            (property_id,)).fetchall()
    assert len(rows) == 2                                  # aucun POI ajouté
    merca = next(r for r in rows if r["name"] == "Mercadona")
    lidl = next(r for r in rows if r["name"] == "Lidl")
    assert merca["source"] == "osm"                        # jamais requalifié
    assert merca["phone"] == "+34 966 000 000"
    assert merca["website"] == "https://mercadona.es"
    assert merca["completion_meta"]["_overture"]["source_ref"] == "gers:merca"
    assert set(merca["completion_meta"]["_overture"]["fields"]) == {"phone", "website"}
    assert lidl["phone"] is None                           # non apparié → intact


def test_overture_failure_degrades_to_osm_only(property_id, http_client):
    """Dégradation douce : un fetch Overture qui ÉCHOUE est tracé et le job termine
    sur OSM seul (jamais un job cassé par la source secondaire)."""
    def boom(lat, lon, radius):
        raise RuntimeError("S3 indisponible")

    result = pipeline.run(property_id, use_claude=False, only_categories={"supermarket"},
                          http_client=http_client, anthropic_client=FakeAnthropic(),
                          overture_fetch=boom)
    assert result["overture_added"] == 0 and result["overture_contacts"] == 0

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name FROM pois WHERE property_id=%s AND category_code='supermarket'",
            (property_id,)).fetchall()
        job = conn.execute("SELECT steps, status FROM enrichment_jobs WHERE id=%s",
                           (result["job_id"],)).fetchone()
    assert job["status"] == "done"                         # job intact
    assert job["steps"]["overture"]["ok"] is False
    assert "S3" in job["steps"]["overture"]["error"]
    assert {r["name"] for r in rows} == {"Mercadona", "Lidl"}   # OSM seul


def test_overture_respects_arbitrated_pois(property_id, http_client):
    """Invariant 1 + dédup inter-sources : un distributeur DÉJÀ approuvé n'est jamais
    touché ; un candidat Overture qui le DOUBLE est écarté (dédup contre l'arbitré) ;
    un candidat distinct est bien ajouté."""
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            "INSERT INTO pois (property_id, category_code, name, geom, source, "
            "source_ref, status) VALUES (%s,'atm','Banco Santander', "
            "ST_SetSRID(ST_MakePoint(%s,%s),4326),'owner','owner:1','approved')",
            (property_id, PROP_LON, PROP_LAT))
        conn.commit()

    def fetch(lat, lon, radius):
        return [_ovt_place("Banco Santander", "bank_credit_union",
                           PROP_LAT + 0.0001, PROP_LON, phone="+34 1", ref="gers:s"),
                _ovt_place("CaixaBank", "bank_credit_union",
                           PROP_LAT + 0.003, PROP_LON + 0.003, phone="+34 2", ref="gers:c")]

    result = pipeline.run(property_id, use_claude=False, only_categories={"atm"},
                          http_client=http_client, anthropic_client=FakeAnthropic(),
                          overture_fetch=fetch)
    assert result["job_id"]

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT name, source, status FROM pois WHERE property_id=%s "
            "AND category_code='atm' ORDER BY name", (property_id,)).fetchall()
    assert {r["name"] for r in rows} == {"Banco Santander", "CaixaBank"}
    santander = next(r for r in rows if r["name"] == "Banco Santander")
    assert santander["status"] == "approved" and santander["source"] == "owner"
    caixa = next(r for r in rows if r["name"] == "CaixaBank")
    assert caixa["source"] == "overture" and caixa["status"] == "suggested"


# ── V2-70 : granularité de l'échec de traduction (langues réussies livrées) ────

def test_translate_run_delivers_succeeded_langs_when_one_lang_fails():
    """V2-70 pièce 4 : une langue en échec n'emporte plus les autres (fin de
    l'all-or-nothing). Les langues RÉUSSIES sont publiées ; la fautive est omise."""
    from enrich import translate
    from api import repo
    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        prop = repo.create_guest_property(conn, name="T70", city="Tokyo",
                                          country_code="JP", lat=35.7, lon=139.7)
        pid = str(prop["id"])
        conn.execute(
            """INSERT INTO pois (property_id, category_code, name, geom,
                                 description_md, source, source_ref, status)
               VALUES (%s, 'restaurant', 'R', ST_SetSRID(ST_MakePoint(139.7, 35.7), 4326),
                       'Bonjour le monde', 'osm', 'n:r70', 'approved')""", (pid,))
        conn.commit()

    class _FailDe:
        def translate(self, texts, *, target_lang, source_lang):
            if target_lang == "de":
                raise RuntimeError("boom de")   # une seule langue tombe
            return ({k: f"[{target_lang}] {v}" for k, v in texts.items()},
                    {"units": 1, "cost_cts": 0.0})

    summary = translate.run(pid, target_langs=["en", "de", "es"], translator=_FailDe())

    with psycopg.connect(settings.db_dsn, row_factory=psycopg.rows.dict_row) as conn:
        pub = conn.execute("SELECT published_langs FROM properties WHERE id=%s",
                           (pid,)).fetchone()["published_langs"] or []
        conn.execute("DELETE FROM properties WHERE id=%s", (pid,))
        conn.commit()
    assert set(pub) == {"en", "es"}                     # de omis, en/es LIVRÉS
    assert summary["langs"]["en"] == 1 and summary["langs"]["es"] == 1
    assert str(summary["langs"]["de"]).startswith("failed")


# ── V2-77 : le tabac se cherche sur le web LÀ OÙ le réseau est licencié ───────

def test_tobacco_web_discovery_only_in_licensed_countries():
    """V2-77 : `shop=tobacco` est moissonné par OSM PARTOUT, mais la DÉCOUVERTE WEB du
    tabac n'a de sens que là où le réseau est licencié, donc recensé (estanco ES,
    tabaccheria IT, bureau de tabac FR…). Ailleurs, chercher « les estancos de X » ne
    pourrait rien rendre : on n'engage pas l'appel. La catégorie, elle, reste demandée."""
    from enrich import claude_enrich as ce
    f = ce.local_commerce_categories_for
    for cc in ("ES", "es", "IT", "FR", "PT", "AT"):
        assert "tobacco" in f(cc), cc
    for cc in ("NL", "DE", "GB", "JP", "XK", "", None):
        assert "tobacco" not in f(cc), cc
    # Les cinq essentielles historiques ne bougent JAMAIS, quel que soit le pays.
    for cc in ("ES", "NL", None):
        assert {"pharmacy", "supermarket", "bakery", "doctor", "post_office"} <= set(f(cc))


def test_void_essentials_honours_the_country_gate():
    """La garde pays passe par `_void_essentials` : en Espagne un tabac absent du rayon
    est « vide » (donc à découvrir) ; aux Pays-Bas il ne l'est jamais — aucun appel web
    ne sera déclenché pour lui. Un estanco PROCHE couvre la catégorie, comme les autres."""
    origin = (28.09, -16.74)
    wanted = {"pharmacy", "tobacco"}
    assert "tobacco" in pipeline._void_essentials([], wanted, origin, 5000, "ES")
    assert "tobacco" not in pipeline._void_essentials([], wanted, origin, 5000, "NL")
    # Sans pays précisé : prudence — pas de dépense web pour le tabac.
    assert "tobacco" not in pipeline._void_essentials([], wanted, origin, 5000, None)
    # Un estanco dans le rayon couvre la catégorie (règle commune V2-74).
    harv = [{"name": "Estanco nº 12", "lat": 28.091, "lon": -16.741, "category": "tobacco"}]
    assert "tobacco" not in pipeline._void_essentials(harv, wanted, origin, 5000, "ES")


def test_editorial_shisha_flows_to_subtype_both_paths():
    """V2-77 : la chicha relevée par la passe éditoriale « sorties » traverse jusqu'à
    `pois.subtype` — sur un POI CRÉÉ comme sur une fiche OSM APPARIÉE. Elle ne remplace
    JAMAIS un sous-type déjà tagué par OSM (esprit V2-71 : OSM fait foi quand il parle)."""
    pk = {"name": "Cairo Lounge", "subtype": "shisha", "reason": "Terrasse à narguilés",
          "source_url": "https://ex.test/a", "verified_on": "2026-09-18"}
    created = pipeline._build_editorial_poi(pk, "bar", 28.1, -16.7, "Adeje",
                                            (28.1, -16.7), "web")
    assert created["subtype"] == "shisha"
    matched = {"name": "Cairo Lounge", "subtype": None}
    pipeline._mark_editorial(matched, pk, "web")
    assert matched["subtype"] == "shisha"
    tagged = {"name": "Padel Club", "subtype": "padel"}
    pipeline._mark_editorial(tagged, pk, "web")
    assert tagged["subtype"] == "padel"          # OSM préservé
    # Un pick ordinaire (sans chicha) ne pose aucun sous-type.
    plain = pipeline._build_editorial_poi({"name": "Casa Manolo", "reason": "r",
                                           "source_url": "u", "verified_on": "d"},
                                          "restaurant", 28.1, -16.7, None,
                                          (28.1, -16.7), "web")
    assert plain["subtype"] is None


def test_tobacco_category_is_seeded_short_radius_and_hosted():
    """V2-77 : la catégorie existe EN BASE (le seed est la source de vérité), au chapitre
    « Vie pratique », avec un rayon COURT (2 km, comme la boulangerie) — on n'envoie
    personne à 20 km acheter un timbre. Et elle est RATTACHÉE à la section qui la
    promettait déjà en toutes lettres (« Distributeur de billets, bureau de poste,
    tabac… »), sans quoi elle ne serait jamais demandée à la moisson."""
    with psycopg.connect(settings.db_dsn) as c:
        row = c.execute("SELECT chapter, icon, default_radius_m, name_i18n "
                        "FROM poi_categories WHERE code = 'tobacco'").fetchone()
        hosted = c.execute("SELECT field_schema->'poi_categories' FROM section_templates "
                           "WHERE code = 'C_shops'").fetchone()[0]
    assert row is not None, "catégorie 'tobacco' absente du seed"
    chapter, icon, radius, labels = row
    assert chapter == "C" and icon == "cigarette" and radius == 2000
    # Chaque langue nomme l'institution avec SON mot — jamais un mot étranger.
    assert labels["fr"] == "Tabac" and labels["en"] == "Tobacconist"
    assert labels["es"] == "Estanco"
    assert "tobacco" in hosted, "catégorie non rattachée à une section → jamais demandée"


# ── V2-77b : la FAUSSE plénitude — estancos & chicha par le web ──────────────

class _FakeWebAI:
    """Client Claude simulé pour les passes web V2-77b : rend la charge voulue, compte
    les appels. Surface minimale du SDK réellement utilisée par `_ask_web_search_json`."""

    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []


def _stub_web_pass(monkeypatch, estancos=None, bars=None):
    """Remplace les DEUX passes web par des fakes purs (aucun réseau) et compte les appels
    — c'est ainsi qu'on éprouve la garde pays sans clé API."""
    from enrich import claude_enrich as ce
    seen = {"estancos": 0, "shisha": 0}

    def fake_estancos(city, cc, client, today=None):
        seen["estancos"] += 1
        return {ce.ESTANCO_FACT_TYPE: {"estancos": estancos or []}}, {"cost_cts": 0.0,
                                                                      "attempts": []}

    def fake_shisha(city, cc, client, today=None):
        seen["shisha"] += 1
        return {ce.SHISHA_FACT_TYPE: {"bars": bars or []}}, {"cost_cts": 0.0,
                                                             "attempts": []}
    monkeypatch.setattr(ce, "fetch_estancos", fake_estancos)
    monkeypatch.setattr(ce, "fetch_shisha_bars", fake_shisha)
    return seen


def test_estanco_pass_fires_even_when_the_category_is_full(monkeypatch, property_id):
    """LE CŒUR DE V2-77b. À Adeje la rubrique tabac était PLEINE (« Radikas », « La Cava
    La Cubana ») et pourtant sans un seul ESTANCO : le système croyait avoir trouvé. La
    passe se déclenche donc SANS condition de vide — contrairement à V2-74 — et
    s'ACCROCHE au lieu déjà moissonné (V2-73g) au lieu d'en créer un second."""
    from enrich import db as edb, claude_enrich as ce
    seen = _stub_web_pass(monkeypatch, estancos=[
        {"name": "Estanco nº 12", "place_address": "12 calle Grande",
         "source_url": "https://ex.test/e", "verified_on": "2026-09-18"}])
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        # La moisson a déjà rendu DEUX tabacs — aucun n'est un estanco.
        for name, sub in (("Radikas", None), ("La Cava La Cubana", "cigar")):
            c.execute("""INSERT INTO pois (property_id, category_code, name, geom, source,
                                           status, subtype)
                         VALUES (%s,'tobacco',%s,ST_SetSRID(ST_MakePoint(-16.74,28.09),4326),
                                 'osm','approved',%s)""", (property_id, name, sub))
        c.commit()
        harvested = [{"name": "Radikas", "lat": 28.09, "lon": -16.74, "category": "tobacco"},
                     {"name": "La Cava La Cubana", "lat": 28.09, "lon": -16.74,
                      "category": "tobacco"}]
        prop = {"id": property_id, "country_code": "ES", "city": "Adeje"}
        summary = {"cost_cts": 0.0}
        jid = str(edb.start_job(c, property_id, "manual")) if hasattr(edb, "start_job") else None
        pipeline._discover_estancos(c, prop, object(), jid, summary, None,
                                    (28.09, -16.74), harvested)
        c.commit()
        assert seen["estancos"] == 1, "la passe ne s'est pas déclenchée sur une rubrique PLEINE"
        # Aucun jumeau : l'estanco n'existait pas dans la moisson → il est créé.
        row = c.execute("SELECT subtype FROM pois WHERE property_id=%s AND name='Estanco nº 12'",
                        (property_id,)).fetchone()
        assert row is not None and row["subtype"] == "estanco"
        # Les fiches arbitrées ne sont PAS touchées dans leur contenu.
        keep = c.execute("SELECT subtype FROM pois WHERE property_id=%s AND name="
                         "'La Cava La Cubana'", (property_id,)).fetchone()
        assert keep["subtype"] == "cigar"


def test_estanco_pass_marks_the_harvested_twin_instead_of_duplicating(monkeypatch,
                                                                     property_id):
    """Cascade V2-73g : si le web nomme un lieu DÉJÀ moissonné, on pose la puce sur lui —
    jamais un doublon. Et c'est bien la fiche `approved` qui est qualifiée : sans cette
    exception étroite (fill-NULL-only, régime `locality` V2-38bis), un guide déjà arbitré
    — le cas d'Adeje — n'afficherait jamais la puce."""
    from enrich import db as edb
    _stub_web_pass(monkeypatch, estancos=[
        {"name": "Estanco Tabacos Pérez", "place_address": "x",
         "source_url": "https://ex.test/e", "verified_on": "2026-09-18"}])
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        c.execute("""INSERT INTO pois (property_id, category_code, name, geom, source, status)
                     VALUES (%s,'tobacco','Tabacos Pérez',
                             ST_SetSRID(ST_MakePoint(-16.74,28.09),4326),'osm','approved')""",
                  (property_id,))
        c.commit()
        harvested = [{"name": "Tabacos Pérez", "lat": 28.09, "lon": -16.74,
                      "category": "tobacco"}]
        pipeline._discover_estancos(c, {"id": property_id, "country_code": "ES",
                                        "city": "Adeje"}, object(), None,
                                    {"cost_cts": 0.0}, None, (28.09, -16.74), harvested)
        c.commit()
        rows = c.execute("SELECT name, subtype, completion_meta FROM pois "
                         "WHERE property_id=%s AND category_code='tobacco'",
                         (property_id,)).fetchall()
        assert len(rows) == 1, f"doublon créé : {[r['name'] for r in rows]}"
        assert rows[0]["subtype"] == "estanco"
        assert rows[0]["completion_meta"]["_estanco"]["source_url"] == "https://ex.test/e"


def test_estanco_pass_never_fires_outside_licensed_countries(monkeypatch, property_id):
    """Point 4 de la mission : AUCUN appel web hors des pays à réseau licencié. Il n'y a
    pas d'annuaire d'estancos aux Pays-Bas — la dépense serait sans objet."""
    from enrich import db as edb
    seen = _stub_web_pass(monkeypatch, estancos=[{"name": "X", "place_address": "y",
                                                  "source_url": "https://e.test/z"}])
    with edb.connect() as c:
        for cc in ("NL", "DE", "GB", "XK"):
            pipeline._discover_estancos(c, {"id": property_id, "country_code": cc,
                                            "city": "Ville"}, object(), None,
                                        {"cost_cts": 0.0}, None, (0.0, 0.0), [])
        assert seen["estancos"] == 0
        pipeline._discover_estancos(c, {"id": property_id, "country_code": "ES",
                                        "city": "Adeje"}, object(), None,
                                    {"cost_cts": 0.0}, None, (28.09, -16.74), [])
        assert seen["estancos"] == 1     # l'Espagne, elle, déclenche


def test_shisha_pass_marks_the_matched_bar(monkeypatch, property_id):
    """La chicha est une PUCE qu'on pose, pas un lieu qu'on ajoute : l'appariement au bar
    déjà moissonné est le cas nominal. Un lieu sans jumeau ET sans adresse est écarté —
    jamais un bar inventé."""
    from enrich import db as edb
    _stub_web_pass(monkeypatch, bars=[
        {"name": "Backyard Lounge", "place_address": "",
         "source_url": "https://ex.test/s", "verified_on": "2026-09-18"},
        {"name": "Fantôme sans adresse", "place_address": "",
         "source_url": "https://ex.test/f", "verified_on": "2026-09-18"}])
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        c.execute("""INSERT INTO pois (property_id, category_code, name, geom, source, status)
                     VALUES (%s,'bar','Backyard Lounge',
                             ST_SetSRID(ST_MakePoint(-16.74,28.09),4326),'osm','approved')""",
                  (property_id,))
        c.commit()
        harvested = [{"name": "Backyard Lounge", "lat": 28.09, "lon": -16.74,
                      "category": "bar"}]
        pipeline._discover_shisha_bars(c, {"id": property_id, "country_code": "ES",
                                           "city": "Adeje"}, object(), None,
                                       {"cost_cts": 0.0}, None, (28.09, -16.74), harvested)
        c.commit()
        rows = c.execute("SELECT name, subtype FROM pois WHERE property_id=%s AND "
                         "category_code='bar'", (property_id,)).fetchall()
        assert len(rows) == 1, "le fantôme sans adresse ne doit PAS entrer"
        assert rows[0]["subtype"] == "shisha"


def test_web_passes_are_mutualised_per_commune(monkeypatch, property_id):
    """Point 3 : UN appel par secteur, réutilisé par tous les guides. Le second passage
    sur la même commune lit l'`area_fact` et ne rappelle pas le web (même régime que les
    marchés, les activités et les commerces de village)."""
    from enrich import db as edb
    seen = _stub_web_pass(monkeypatch, estancos=[
        {"name": "Estanco nº 12", "place_address": "12 calle Grande",
         "source_url": "https://ex.test/e", "verified_on": "2026-09-18"}])
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        c.execute("""INSERT INTO pois (property_id, category_code, name, geom, source, status)
                     VALUES (%s,'tobacco','Estanco nº 12',
                             ST_SetSRID(ST_MakePoint(-16.74,28.09),4326),'osm','suggested')""",
                  (property_id,))
        c.commit()
        prop = {"id": property_id, "country_code": "ES", "city": "Adeje"}
        harv = [{"name": "Estanco nº 12", "lat": 28.09, "lon": -16.74, "category": "tobacco"}]
        for _ in range(3):
            pipeline._discover_estancos(c, prop, object(), None, {"cost_cts": 0.0}, None,
                                        (28.09, -16.74), harv)
            c.commit()
    assert seen["estancos"] == 1, f"{seen['estancos']} appels web au lieu d'un seul"


# ── V2-77c : les lieux découverts ne s'empilent pas ──────────────────────────

def test_web_creations_never_stack_on_the_same_point(monkeypatch, property_id):
    """V2-77c point 1 — TROISIÈME occurrence du motif (picks V2-56b, activités V2-73e,
    lieux web ici). Recette Adeje : Ayune, Hayal et Kalani, trois établissements DISTINCTS,
    sortis au MÊME point. La cause n'est pas une adresse empruntée mais le REPLI AU CENTRE
    DE LA COMMUNE (V2-74b) : trois adresses irrésolues → trois fois le même centroïde. Le
    repli est juste pour l'épicerie d'un village, mensonger dès qu'il empile."""
    from enrich import db as edb
    _stub_web_pass(monkeypatch, bars=[
        {"name": "Ayune", "place_address": "a", "source_url": "https://ex.test/1"},
        {"name": "Hayal", "place_address": "b", "source_url": "https://ex.test/2"},
        {"name": "Kalani", "place_address": "c", "source_url": "https://ex.test/3"}])
    # Les trois adresses sont irrésolues → repli au centre de la commune, point IDENTIQUE.
    monkeypatch.setattr(pipeline, "_commune_center", lambda *a, **k: (28.09, -16.74))
    monkeypatch.setattr(pipeline, "_geocode_local_commerce",
                        lambda *a, **k: (28.09, -16.74, "Adeje", True))
    monkeypatch.setattr(pipeline.distance, "compute_distances", lambda *a, **k: None)
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        c.commit()
        pipeline._discover_shisha_bars(c, {"id": property_id, "country_code": "ES",
                                           "city": "Adeje"}, object(), None,
                                       {"cost_cts": 0.0}, None, (28.09, -16.74), [])
        c.commit()
        rows = c.execute("SELECT name, category_code FROM pois WHERE property_id=%s",
                         (property_id,)).fetchall()
    assert len(rows) == 1, f"empilement : {[r['name'] for r in rows]}"
    # …et la création va dans la rubrique DÉDIÉE, pas dans « bar ».
    assert rows[0]["category_code"] == "shisha"


def test_web_pass_step_distinguishes_nothing_found_from_everything_dropped(monkeypatch,
                                                                          property_id):
    """V2-77c point 3 — le journal doit permettre le DIAGNOSTIC. « 0 estanco » ne disait
    pas si le web n'avait rien rendu ou si « preuve ou rien » avait tout écarté : deux
    causes, deux correctifs opposés. `steps.estancos` porte désormais `raw` et `kept`."""
    from enrich import db as edb, claude_enrich as ce
    # Le modèle rend DEUX estancos, mais aucun n'a de source https → tous écartés.
    monkeypatch.setattr(ce, "fetch_estancos", lambda city, cc, client, today=None: (
        {ce.ESTANCO_FACT_TYPE: {"estancos": [], "raw": 2}}, {"cost_cts": 0.0, "attempts": []}))
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        jid = str(uuid.uuid4())
        c.execute("INSERT INTO enrichment_jobs (id, property_id, trigger, status) "
                  "VALUES (%s,%s,'manual','running')", (jid, property_id))
        c.commit()
        pipeline._discover_estancos(c, {"id": property_id, "country_code": "ES",
                                        "city": "Adeje"}, object(), jid,
                                    {"cost_cts": 0.0}, None, (28.09, -16.74), [])
        c.commit()
        steps = c.execute("SELECT steps FROM enrichment_jobs WHERE id=%s",
                          (jid,)).fetchone()["steps"]
    st = steps["estancos"]
    assert st["raw"] == 2 and st["kept"] == 0 and st["dropped_unproven"] == 2


# ── V2-77e : la passe chicha ratissait trop étroit ───────────────────────────

def test_shisha_prompt_sweeps_by_quarter_and_asks_for_contacts():
    """V2-77e — vérification manuelle d'André : sur SIX shisha bars d'Adeje (Kalani,
    Lateral Club, Ayune, Voodoo, Hades, Hayal), CINQ ont un site. Le web ouvert les
    connaît ; la passe n'en rendait que trois. Ce n'est pas une limite de la donnée
    (leçon V2-73d) mais une requête trop étroite. Le prompt reprend donc le motif V2-56b
    qui avait débloqué Brown's à La Zenia : vocabulaire RÉEL des lieux, ratissage PAR
    QUARTIER, cible large, coordonnées exigées."""
    from enrich import claude_enrich as ce
    pr = ce._SHISHA_PROMPT
    # 1) Le vocabulaire qu'ils emploient EUX-MÊMES (beaucoup ne disent jamais « chicha »).
    for mot in ("shisha lounge", "hookah lounge", "lounge bar", "gastrobar", "cachimbas",
                "narguile"):
        assert mot in pr.lower(), mot
    # 2) Ratissage par quartier (le déblocage de V2-56b).
    assert "RATISSE PAR QUARTIER" in pr and "URBANIZACIONES" in pr
    # 3) Cible large — le positionnement strict élague ensuite.
    assert "10 À 12" in pr and "élaguera" in pr
    # 4) Coordonnées publiées : site officiel, téléphone, adresse.
    assert "`website`" in pr and "site OFFICIEL" in pr and "`phone`" in pr
    assert "tripadvisor" in pr.lower() and "instagram" in pr.lower()
    # 5) Acquis V2-77c conservés : preuve ou rien, pas d'adresse empruntée.
    assert "PREUVE OU RIEN" in pr and "N'emprunte JAMAIS" in pr


def test_shisha_contacts_reach_the_poi_created_and_matched(monkeypatch, property_id):
    """V2-77e point 3 — un nom sans site ni téléphone ne sert à rien : le voyageur ne peut
    ni réserver ni vérifier les horaires. Les coordonnées publiées atteignent le POI dans
    les DEUX chemins : création (rubrique dédiée) et appariement (fill-NULL-only, régime
    V2-38bis — une valeur déjà présente n'est jamais touchée)."""
    from enrich import db as edb
    _stub_web_pass(monkeypatch, bars=[
        {"name": "Kalani Lounge", "place_address": "av. Kalani, Costa Adeje",
         "website": "https://kalani.test", "phone": "+34 111",
         "source_url": "https://ex.test/k"},
        {"name": "Mogu", "place_address": "", "website": "https://mogu.test",
         "phone": "+34 222", "source_url": "https://ex.test/m"}])
    monkeypatch.setattr(pipeline, "_commune_center", lambda *a, **k: (28.12, -16.72))
    monkeypatch.setattr(pipeline, "_geocode_local_commerce",
                        lambda *a, **k: (28.12, -16.72, "Adeje", False))
    monkeypatch.setattr(pipeline.distance, "compute_distances", lambda *a, **k: None)
    with edb.connect() as c:
        c.execute("DELETE FROM area_facts WHERE country_code='ES'")
        # Un bar DÉJÀ connu, sans coordonnées… et un téléphone déjà renseigné ailleurs.
        c.execute("""INSERT INTO pois (property_id, category_code, name, geom, source, status,
                                       phone)
                     VALUES (%s,'bar','Mogu',ST_SetSRID(ST_MakePoint(-16.74,28.09),4326),
                             'osm','approved','+34 DEJA')""", (property_id,))
        c.commit()
        harv = [{"name": "Mogu", "lat": 28.09, "lon": -16.74, "category": "bar"}]
        pipeline._discover_shisha_bars(c, {"id": property_id, "country_code": "ES",
                                           "city": "Adeje"}, object(), None,
                                       {"cost_cts": 0.0}, None, (28.09, -16.74), harv)
        c.commit()
        rows = {r["name"]: r for r in c.execute(
            "SELECT name, category_code, phone, website, subtype FROM pois "
            "WHERE property_id=%s", (property_id,)).fetchall()}
    # Créé dans la rubrique dédiée, AVEC son site et son téléphone.
    k = rows["Kalani Lounge"]
    assert k["category_code"] == "shisha" and k["website"] == "https://kalani.test"
    assert k["phone"] == "+34 111"
    # Apparié : le site MANQUANT est comblé, le téléphone DÉJÀ PRÉSENT n'est pas écrasé.
    m = rows["Mogu"]
    assert m["category_code"] == "bar" and m["subtype"] == "shisha"
    assert m["website"] == "https://mogu.test" and m["phone"] == "+34 DEJA"


def test_geocode_guard_anchors_on_the_property_not_only_the_town_hall(monkeypatch):
    """V2-77e — LE GARDE V2-74b MENTAIT SUR UNE GRANDE COMMUNE. Mesuré à Adeje (18/09) :
    le centre administratif est à 5,2 km du logement de Costa Adeje ; « Avenida de España,
    Costa Adeje » géocode en ROOFTOP à 6,7 km de ce centre — donc rejetée par la garde des
    2 km — mais à 1,5 km du LOGEMENT. Six lounges parfaitement adressés retombaient tous au
    centre-ville, puis cinq étaient écartés par l'anti-empilement. Le logement ancre le
    guide : une position précise et proche de LUI est cohérente."""
    prop = {"city": "Adeje", "country_code": "ES"}
    centre, home = (28.1394, -16.7395), (28.0925, -16.7400)   # 5,2 km d'écart réel
    monkeypatch.setattr(pipeline.geocode, "geocode",
                        lambda **kw: {"lat": 28.1060, "lon": -16.7290,
                                      "accuracy": "rooftop", "locality": "Costa Adeje"})
    lat, lon, loc, approx = pipeline._geocode_local_commerce(
        {"place_address": "Avenida de España 5"}, prop, centre, None, home)
    assert (lat, lon) == (28.1060, -16.7290) and approx is False, "position précise rejetée"
    # Sans le logement (appelant historique), l'ancien comportement est INTACT : hors des
    # 2 km du centre → repli marqué approximatif.
    lat2, lon2, _, approx2 = pipeline._geocode_local_commerce(
        {"place_address": "Avenida de España 5"}, prop, centre, None)
    assert (lat2, lon2) == centre and approx2 is True


def test_geocode_prefers_an_imprecise_nearby_point_over_the_town_centre(monkeypatch):
    """V2-77e, 2e tier — « Calle París 3 » et « Avenida Bruselas 4 » géocodent en `city`
    (Nominatim n'a pas le numéro) mais à 0,6 et 0,3 km DU LOGEMENT. Les rejeter pour
    retomber sur le centre à 5,2 km, c'est jeter un point à 300 m au profit d'un point à
    5 km. On les retient, MARQUÉS approximatifs (cercle + mention) : la carte ne ment pas.
    Garde : un résultat qui EST le centre de la commune ne se promeut pas lui-même."""
    prop = {"city": "Adeje", "country_code": "ES"}
    centre, home = (28.1394, -16.7395), (28.0925, -16.7400)
    monkeypatch.setattr(pipeline.geocode, "geocode",
                        lambda **kw: {"lat": 28.0900, "lon": -16.7380,
                                      "accuracy": "city", "locality": "Adeje"})
    lat, lon, _, approx = pipeline._geocode_local_commerce(
        {"place_address": "Calle París 3"}, prop, centre, None, home)
    assert (lat, lon) == (28.0900, -16.7380) and approx is True
    # Le centroïde de la commune lui-même → PAS plus proche que le centre → repli normal.
    monkeypatch.setattr(pipeline.geocode, "geocode",
                        lambda **kw: {"lat": centre[0], "lon": centre[1],
                                      "accuracy": "city", "locality": "Adeje"})
    lat3, lon3, _, approx3 = pipeline._geocode_local_commerce(
        {"place_address": "Adeje"}, prop, centre, None, home)
    assert (lat3, lon3) == centre and approx3 is True
