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
                 service_completions=None, markets=None, markets_malformed=False):
        self.food_delivery_malformed = food_delivery_malformed
        self.markets_malformed = markets_malformed
        self.food_delivery_calls = 0
        self.market_calls = 0
        self.babysitter_services = (
            [{"name": "Canguros Costa", "phone": "+34 966 000 111",
              "website": "https://canguroscosta.example",
              "source_url": "https://canguroscosta.example",
              "verified_on": "2026-08-11"}]
            if babysitter_services is None else babysitter_services)
        self.service_completions = service_completions or {}
        # V2-07 volet 3 : un marché crédible AVEC coordonnées (pas de géocodage).
        self.markets = ([{"name": "Mercadillo de La Zenia", "weekday": 6,
                          "hours": "8h00–14h00", "character": "fruits, vêtements",
                          "address": "Plaza de La Zenia", "lat": 37.930, "lon": -0.750,
                          "source_url": "https://orihuela.es/mercadillos",
                          "verified_on": "2026-08-12", "doubtful": False}]
                        if markets is None else markets)

    def create(self, *, model, max_tokens, messages, tools=None, **kwargs):
        prompt = messages[0]["content"]
        if "BABY-SITTING" in prompt:  # création baby-sitting (V2-07 volet 2)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps({"services": self.babysitter_services}))
        if "MARCHÉS HEBDOMADAIRES" in prompt:  # découverte marchés (V2-07 volet 3)
            self.market_calls += 1
            assert tools and tools[0]["type"] == "web_search_20250305"
            if self.markets_malformed:            # JSON tronqué/malformé (volet 3bis)
                return _web_reply("désolé, réponse tronquée…")
            return _web_reply(json.dumps({"markets": self.markets}))
        if "LIEUX DE SERVICE" in prompt:  # complétion tel/site/horaires (volet 2)
            assert tools and tools[0]["type"] == "web_search_20250305"
            return _web_reply(json.dumps(self.service_completions))
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
        else:  # prompt descriptions POI
            payload = {"node/333": "Restaurant de poissons face à la plage, "
                                   "apprécié pour ses arroces."}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=800, output_tokens=350),
        )


class FakeAnthropic:
    def __init__(self, food_delivery_malformed=False, babysitter_services=None,
                 service_completions=None, markets=None, markets_malformed=False):
        self.messages = FakeMessages(food_delivery_malformed, babysitter_services,
                                     service_completions, markets, markets_malformed)


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
                              "food_delivery", "markets"}
        assert facts["markets"]["markets"][0]["weekday"] == 6
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
                    "food_delivery", "babysitter", "markets", "claude"))
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
                       "markets"}


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
    # + 1 au retry M-18 (succès) ; supermarché 1 SEULE fois (le retry ne rejoue que
    # les catégories manquantes).
    assert flaky.airport_queries == settings.overpass_max_attempts + 1
    assert flaky.supermarket_queries == 1

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
        # 'markets' présent (mutualisé) ; PAS de 'food_delivery' (rejeté sans écriture).
        assert facts == {"emergency_numbers", "waste_rules", "noise_rules", "markets"}
        fd_costs = conn.execute(
            "SELECT count(*) c FROM api_costs WHERE job_id=%s AND operation='food_delivery'",
            (result["job_id"],)).fetchone()["c"]
        assert fd_costs == 2                                   # essai + retry, tous deux payés
