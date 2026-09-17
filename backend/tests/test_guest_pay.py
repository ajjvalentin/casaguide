"""Paiement one-shot « Guide Voyageur » (V2-54 Mission B) — webhook source de vérité.

Aucun réseau : la passerelle Stripe est une fausse (signature webhook RÉELLE, héritée),
la génération (`guest_guides.generate_guest_guide`) est remplacée par un stub qui crée
une vraie fiche guest publiée (FK réelle), le mailer est un ConsoleMailer inspectable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from api import billing_stripe, guest_guides, repo  # noqa: E402
from api.routers import guest_pay  # noqa: E402
from api.config import settings as api_settings  # noqa: E402
from api.deps import get_mailer, get_stripe  # noqa: E402
from api.main import app  # noqa: E402
from api.mailer import ConsoleMailer  # noqa: E402
from enrich import db as edb  # noqa: E402
from enrich.settings import settings  # noqa: E402

WEBHOOK_SECRET = "whsec_test_guest"


# ── Faux Stripe (signature réelle, opérations réseau simulées) ────────────────

class FakeGuestGateway(billing_stripe.LiveStripeGateway):
    def __init__(self):
        super().__init__(api_key="sk_test_x", webhook_secret=WEBHOOK_SECRET)
        self.checkout_calls: list[dict] = []

    def create_guest_checkout_session(self, *, amount_cts, currency, product_name,
                                      email, success_url, cancel_url, metadata):
        self.checkout_calls.append({
            "amount_cts": amount_cts, "currency": currency, "email": email,
            "metadata": metadata, "success_url": success_url})
        sid = f"cs_test_{metadata['order_id'][:8]}"
        return sid, f"https://checkout.stripe.test/pay/{sid}"


# ── Stub de génération : crée une VRAIE fiche guest publiée (FK réelle) ────────

_CREATED_PROPS: list[str] = []


def _stub_generate(**kw):
    with edb.connect() as conn:
        prop = repo.create_guest_property(
            conn, name=f"Guide — {kw['city']}", city=kw["city"],
            country_code=kw["country_code"],
            lat=kw.get("lat") or 38.35, lon=kw.get("lon") or -0.48)
        edb.publish_property(conn, str(prop["id"]))
        conn.commit()
    _CREATED_PROPS.append(str(prop["id"]))
    return {"property": prop, "cached": False,
            "summary": {"pois": 3, "judge_approved": 3, "judge_rejected": 0,
                        "cost_cts": 1.0}}


@pytest.fixture()
def pay(monkeypatch):
    gateway = FakeGuestGateway()
    mailer = ConsoleMailer(from_addr="Holaguia <no-reply@holaguia.test>")
    app.dependency_overrides[get_stripe] = lambda: gateway
    app.dependency_overrides[get_mailer] = lambda: mailer
    prev = api_settings.stripe_webhook_secret
    api_settings.stripe_webhook_secret = WEBHOOK_SECRET
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _stub_generate)
    _CREATED_PROPS.clear()
    _purge()                       # départ propre (résilient à un crash précédent)
    client = TestClient(app)
    try:
        yield client, gateway, mailer
    finally:
        app.dependency_overrides.pop(get_stripe, None)
        app.dependency_overrides.pop(get_mailer, None)
        api_settings.stripe_webhook_secret = prev
        _purge()
        with psycopg.connect(settings.db_dsn) as conn:
            for pid in _CREATED_PROPS:
                conn.execute("DELETE FROM properties WHERE id=%s", (pid,))
            conn.commit()


def _purge():
    """Nettoie les commandes de test et les event.id de test (les event.id
    persistent pour l'idempotence Stripe → un rejeu inter-run se déclarerait
    « duplicate » et fausserait le test)."""
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("DELETE FROM guest_guide_orders WHERE email LIKE %s",
                     ("%@paytest.com",))
        conn.execute("DELETE FROM stripe_events WHERE id IN "
                     "('evt_g1','evt_unpaid','evt_fail','evt_resend')")
        conn.commit()


def _webhook(client, event: dict, *, valid=True):
    payload = json.dumps(event).encode()
    if valid:
        ts = int(time.time())
        sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload,
                       hashlib.sha256).hexdigest()
        header = f"t={ts},v1={sig}"
    else:
        header = "t=1,v1=deadbeef"
    return client.post("/api/stripe/webhook", content=payload,
                       headers={"stripe-signature": header,
                                "content-type": "application/json"})


def _completed_event(evt_id: str, session_id: str, order_id: str, *,
                     payment_status="paid"):
    return {"id": evt_id, "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {
                "object": "checkout.session", "id": session_id, "mode": "payment",
                "payment_status": payment_status,
                "metadata": {"kind": "guest_guide", "order_id": order_id}}}}


def _order_by_token(token: str) -> dict:
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
        return repo.get_guest_order_by_token(conn, token)


def _checkout(client, email: str, **over) -> dict:
    body = {"email": email, "city": "Alicante", "country_code": "ES",
            "lat": 38.35, "lon": -0.48, "lang": "fr"}
    body.update(over)
    r = client.post("/api/guest-guides/checkout", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_checkout_creates_pending_order_with_config_price(pay):
    client, gateway, _ = pay
    out = _checkout(client, "buy@paytest.com")
    assert out["url"].startswith("https://checkout.stripe.test/")
    # Montant lu de la config (jamais codé en dur).
    assert gateway.checkout_calls[0]["amount_cts"] == api_settings.guest_guide_price_cts
    assert gateway.checkout_calls[0]["metadata"]["kind"] == "guest_guide"
    order = _order_by_token(out["token"])
    assert order["status"] == "pending" and order["stripe_session_id"]


def test_webhook_paid_generates_once_and_delivers(pay):
    client, _, mailer = pay
    out = _checkout(client, "deliver@paytest.com")
    order = _order_by_token(out["token"])
    evt = _completed_event("evt_g1", order["stripe_session_id"], str(order["id"]))

    r = _webhook(client, evt)
    assert r.status_code == 200 and r.json() == {"received": True}
    # Génération faite (tâche de fond exécutée par le TestClient) + livraison.
    done = _order_by_token(out["token"])
    assert done["status"] == "done" and done["guide_token"] and done["property_id"]
    assert len(mailer.sent) == 1
    to, email = mailer.sent[0]
    assert to == "deliver@paytest.com"
    assert done["guide_token"] in email.text and "Guide Voyageur" in email.subject

    # REJEU du même événement (idempotence Stripe) → aucun retraitement, une seule
    # génération, un seul e-mail.
    r2 = _webhook(client, evt)
    assert r2.json().get("duplicate") is True
    assert len(mailer.sent) == 1
    assert len(_CREATED_PROPS) == 1


def test_quality_notes_recorded_on_order(pay, monkeypatch):
    """V2-57 : le récapitulatif de qualité (ce qui manque au guide servi) est écrit
    sur la commande — plus jamais d'échec muet."""
    client, _, _ = pay

    def _degraded(**kw):
        res = _stub_generate(**kw)
        res["quality"] = {"notes": "catégories non moissonnées (échec réseau) : atm, "
                                   "cafe ; traduction de/nl échouée : Claude 529"}
        return res
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _degraded)

    out = _checkout(client, "quality@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_g1", order["stripe_session_id"],
                                      str(order["id"])))
    done = _order_by_token(out["token"])
    assert done["status"] == "done"
    assert "atm, cafe" in done["quality_notes"]
    assert "traduction de/nl échouée" in done["quality_notes"]


def test_no_generation_without_confirmed_payment(pay):
    client, _, mailer = pay
    out = _checkout(client, "unpaid@paytest.com")
    order = _order_by_token(out["token"])
    # payment_status != 'paid' → aucune génération, la commande reste 'pending'.
    r = _webhook(client, _completed_event("evt_unpaid", order["stripe_session_id"],
                                          str(order["id"]), payment_status="unpaid"))
    assert r.status_code == 200
    assert _order_by_token(out["token"])["status"] == "pending"
    assert mailer.sent == []
    # L'endpoint de reprise refuse une commande non payée (409 not_paid).
    rr = client.post(f"/api/guest-guides/orders/{out['token']}/retry", json={})
    assert rr.status_code == 409 and rr.json()["detail"]["code"] == "not_paid"


def test_failed_generation_sends_retry_email_no_repayment(pay, monkeypatch):
    client, _, mailer = pay

    def _boom(**kw):
        raise guest_guides.GuestGuideMismatch("commune incohérente")
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _boom)

    out = _checkout(client, "fail@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_fail", order["stripe_session_id"],
                                      str(order["id"])))
    failed = _order_by_token(out["token"])
    assert failed["status"] == "failed" and failed["error"]
    # Un seul e-mail : la REPRISE (ajuster l'adresse), pas de re-paiement.
    assert len(mailer.sent) == 1
    assert "reprise" in mailer.sent[0][1].text.lower() or \
           "ajuster" in mailer.sent[0][1].text.lower()
    assert out["token"] in mailer.sent[0][1].text   # lien de reprise porte le token


def test_retry_after_failure_regenerates_without_payment(pay):
    client, _, mailer = pay
    out = _checkout(client, "retry@paytest.com")
    order = _order_by_token(out["token"])
    # Marque la commande 'failed' comme après un premier échec.
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("UPDATE guest_guide_orders SET status='paid', paid_at=now() "
                     "WHERE id=%s", (order["id"],))
        conn.execute("UPDATE guest_guide_orders SET status='failed' WHERE id=%s",
                     (order["id"],))
        conn.commit()
    r = client.post(f"/api/guest-guides/orders/{out['token']}/retry",
                    json={"lat": 38.36, "lon": -0.49})
    assert r.status_code == 200
    done = _order_by_token(out["token"])
    assert done["status"] == "done" and done["guide_token"]
    assert len(mailer.sent) == 1   # livraison


# ── Chien de garde des commandes orphelines (V2-64) ──────────────────────────

def _set_status(order_id: str, status: str, *, updated_ago_s: int = 0) -> None:
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute(
            "UPDATE guest_guide_orders SET status=%s, paid_at=now(), "
            "updated_at=now() - make_interval(secs => %s) WHERE id=%s",
            (status, updated_ago_s, order_id))
        conn.commit()


def _recover(mailer, *, older_than_s: int, spawn=lambda fn: fn()) -> int:
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
        return guest_guides.recover_stuck_orders(
            conn, mailer=mailer, base_url="https://holaguia.test",
            older_than_s=older_than_s, spawn=spawn)


def test_vital_floor_fails_urban_guide_without_vitals(pay, monkeypatch):
    """V2-68 p4 : en zone URBAINE (dense) sans hôpital/pharmacie/police, on ne livre pas
    un guide creux → commande 'failed' + e-mail de reprise (jamais 'done')."""
    client, _, mailer = pay

    def _hollow(**kw):
        res = _stub_generate(**kw)
        res["summary"] = {"pois": 40, "dense": True,
                          "categories": {"restaurant": 8, "bar": 8, "cafe": 8}}
        return res
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _hollow)

    out = _checkout(client, "hollow@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_g1", order["stripe_session_id"],
                                      str(order["id"])))
    failed = _order_by_token(out["token"])
    assert failed["status"] == "failed"                      # jamais 'done'
    assert len(mailer.sent) == 1                             # e-mail de REPRISE
    assert "reprise" in mailer.sent[0][1].text.lower() or \
           "ajuster" in mailer.sent[0][1].text.lower()


def test_vital_floor_ok_when_one_vital_present(pay, monkeypatch):
    """V2-68 p4 : une seule catégorie vitale présente suffit → livraison normale."""
    client, _, mailer = pay

    def _with_pharmacy(**kw):
        res = _stub_generate(**kw)
        res["summary"] = {"pois": 40, "dense": True,
                          "categories": {"restaurant": 8, "pharmacy": 2}}
        return res
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _with_pharmacy)

    out = _checkout(client, "hasvital@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_g1", order["stripe_session_id"],
                                      str(order["id"])))
    assert _order_by_token(out["token"])["status"] == "done"


def test_vital_floor_not_applied_in_rural(pay, monkeypatch):
    """V2-68 p4 : hors zone dense (rural), l'absence de vitaux ne bloque JAMAIS (le plus
    proche peut être loin ; dégradation douce, V2-57)."""
    client, _, _ = pay

    def _rural(**kw):
        res = _stub_generate(**kw)
        res["summary"] = {"pois": 5, "dense": False, "categories": {"restaurant": 2}}
        return res
    monkeypatch.setattr(guest_guides, "generate_guest_guide", _rural)

    out = _checkout(client, "rural@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_g1", order["stripe_session_id"],
                                      str(order["id"])))
    assert _order_by_token(out["token"])["status"] == "done"


def test_watchdog_recovers_stuck_generating_order(pay):
    """V2-64 — une commande PAYÉE figée en 'generating' (tâche de fond tuée, ex. restart
    de déploiement en pleine génération) est reprise par le chien de garde : génération
    relancée, guide livré par e-mail. Le client ne reste jamais sur une roue éternelle."""
    client, _, mailer = pay
    out = _checkout(client, "stuck@paytest.com")
    _set_status(str(_order_by_token(out["token"])["id"]),
                "generating", updated_ago_s=7200)      # figée depuis 2 h
    n = _recover(mailer, older_than_s=2700)             # backstop périodique (45 min)
    assert n == 1
    done = _order_by_token(out["token"])
    assert done["status"] == "done" and done["guide_token"]
    assert len(mailer.sent) == 1 and mailer.sent[0][0] == "stuck@paytest.com"


def test_watchdog_ignores_fresh_generating_order(pay):
    """V2-64 — une commande VRAIMENT en cours (updated_at récent grâce au battement de
    cœur) n'est PAS reprise par le backstop périodique : « lente » ≠ « morte »."""
    client, _, mailer = pay
    out = _checkout(client, "fresh@paytest.com")
    _set_status(str(_order_by_token(out["token"])["id"]),
                "generating", updated_ago_s=60)         # battement récent
    assert _recover(mailer, older_than_s=2700) == 0
    assert _order_by_token(out["token"])["status"] == "generating"   # intacte
    assert mailer.sent == []


def test_watchdog_startup_recovers_all_generating_regardless_of_age(pay):
    """V2-64 — au DÉMARRAGE (seuil 0), toute commande 'generating' est orpheline (aucune
    tâche de fond ne survit à un redémarrage) → reprise même récente. Idem une commande
    'paid' jamais partie (tâche perdue entre le commit et l'enqueue)."""
    client, _, mailer = pay
    out = _checkout(client, "boot@paytest.com")
    _set_status(str(_order_by_token(out["token"])["id"]),
                "generating", updated_ago_s=30)
    assert _recover(mailer, older_than_s=0) == 1
    assert _order_by_token(out["token"])["status"] == "done"


def test_watchdog_double_spawn_generates_once(pay):
    """V2-64 — le verrou atomique de génération empêche toute double génération, même si
    la reprise est lancée deux fois (app au démarrage ET timer périodique en même temps)."""
    client, _, mailer = pay
    out = _checkout(client, "once@paytest.com")
    _set_status(str(_order_by_token(out["token"])["id"]),
                "generating", updated_ago_s=7200)
    _recover(mailer, older_than_s=2700, spawn=lambda fn: (fn(), fn()))   # double appel
    assert _order_by_token(out["token"])["status"] == "done"
    assert len(_CREATED_PROPS) == 1     # UNE seule génération (le verrou tranche)
    assert len(mailer.sent) == 1


def test_watchdog_leaves_done_and_failed_untouched(pay):
    """V2-64 — le chien de garde ne touche jamais une commande livrée ('done') ni une
    déjà en reprise assistée ('failed') : il ne reprend que les orphelines payées."""
    client, _, mailer = pay
    out = _checkout(client, "done@paytest.com")
    _set_status(str(_order_by_token(out["token"])["id"]),
                "done", updated_ago_s=7200)
    assert _recover(mailer, older_than_s=0) == 0
    assert _order_by_token(out["token"])["status"] == "done"
    assert mailer.sent == []


def test_heartbeat_touches_only_generating(pay):
    """V2-64 — le battement de cœur (`touch_guest_order_generating`) rafraîchit updated_at
    UNIQUEMENT pour une commande 'generating' : une commande finie/en attente n'est jamais
    ré-horodatée à tort (sinon on masquerait une orpheline)."""
    client, _, _ = pay
    oid = str(_order_by_token(_checkout(client, "beat@paytest.com")["token"])["id"])
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
        # 'paid' → intact (statut ≠ generating).
        _set_status(oid, "paid", updated_ago_s=3600)
        before = repo.get_guest_order(conn, oid)["updated_at"]
        repo.touch_guest_order_generating(conn, oid)
        conn.commit()
        assert repo.get_guest_order(conn, oid)["updated_at"] == before
        # 'generating' → rafraîchi.
        _set_status(oid, "generating", updated_ago_s=3600)
        old = repo.get_guest_order(conn, oid)["updated_at"]
        repo.touch_guest_order_generating(conn, oid)
        conn.commit()
        assert repo.get_guest_order(conn, oid)["updated_at"] > old


def test_resend_delivers_then_rate_limits(pay):
    client, _, mailer = pay
    # Prépare une commande LIVRÉE.
    out = _checkout(client, "resend@paytest.com")
    order = _order_by_token(out["token"])
    _webhook(client, _completed_event("evt_resend", order["stripe_session_id"],
                                      str(order["id"])))
    assert len(mailer.sent) == 1
    # La livraison vient de poser delivered_at → un renvoi immédiat serait throttlé
    # (comportement voulu : « vous venez de le recevoir »). On recule delivered_at
    # pour éprouver le renvoi PUIS la cadence.
    with psycopg.connect(settings.db_dsn) as conn:
        conn.execute("UPDATE guest_guide_orders SET delivered_at = now() - "
                     "interval '1 hour' WHERE id=%s", (order["id"],))
        conn.commit()
    # Renvoi : un e-mail de plus.
    r = client.post("/api/guest-guides/resend", json={"email": "resend@paytest.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(mailer.sent) == 2 and "renvoi" in mailer.sent[1][1].subject.lower()
    # Renvoi immédiat → bloqué par la cadence (200 constant, aucun e-mail de plus).
    r2 = client.post("/api/guest-guides/resend", json={"email": "resend@paytest.com"})
    assert r2.status_code == 200 and len(mailer.sent) == 2
    # E-mail inconnu → 200 constant, rien envoyé (anti-énumération).
    r3 = client.post("/api/guest-guides/resend", json={"email": "ghost@paytest.com"})
    assert r3.status_code == 200 and len(mailer.sent) == 2


def test_offer_price_from_config(pay):
    """V2-54 C : le prix de l'offre vient de la config (jamais codé en dur côté front)."""
    client, _, _ = pay
    r = client.get("/api/guest-guides/offer")
    assert r.status_code == 200
    j = r.json()
    assert j["price_cts"] == api_settings.guest_guide_price_cts and j["currency"]


def test_demo_endpoint_shape(pay):
    """V2-58 : l'endpoint démo répond toujours (token ou null si non générée)."""
    client, _, _ = pay
    r = client.get("/api/guest-guides/demo")
    assert r.status_code == 200 and "token" in r.json()


def test_public_geocode_endpoint(pay, monkeypatch):
    """V2-54 C : géocodage PUBLIC pré-checkout — found/mismatch/introuvable, throttle
    neutralisé en test."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay, "_GEO_MIN_INTERVAL_S", 0)   # pas d'attente en test
    monkeypatch.setattr(guest_pay._geocode, "geocode",
                        lambda **kw: {"lat": 37.9, "lon": -0.7, "accuracy": "rooftop"})
    r = client.post("/api/guest-guides/geocode",
                    json={"city": "La Zenia", "country_code": "ES",
                          "address_line1": "Calle X"})
    assert r.status_code == 200 and r.json()["found"] is True
    assert r.json()["mismatch"] is False and r.json()["lat"] == 37.9
    # Mismatch V2-46 → drapeau.
    monkeypatch.setattr(guest_pay._geocode, "geocode",
                        lambda **kw: {"lat": 38.0, "lon": -0.9, "accuracy": "mismatch"})
    assert client.post("/api/guest-guides/geocode",
                       json={"city": "X", "country_code": "ES"}).json()["mismatch"] is True
    # Introuvable → found=False (placement manuel côté tunnel).
    monkeypatch.setattr(
        guest_pay._geocode, "geocode",
        lambda **kw: (_ for _ in ()).throw(guest_pay._geocode.GeocodeError("nope")))
    monkeypatch.setattr(guest_pay._geocode, "coarse_locate", lambda **kw: None)
    assert client.post("/api/guest-guides/geocode",
                       json={"city": "X", "country_code": "ES"}).json()["found"] is False


def test_geocode_failure_still_serves_a_starting_landmark(pay, monkeypatch):
    """V2-68c p1/p3 : une adresse introuvable OUVRE le placement manuel — `found=False`
    part AVEC un repère de départ (commune → code postal → pays) pour centrer la carte.
    Cas réel : « Rrugë Skënderbeu 307, Xërxë, XK »."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay, "_GEO_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(
        guest_pay._geocode, "geocode",
        lambda **kw: (_ for _ in ()).throw(guest_pay._geocode.GeocodeError("nope")))
    monkeypatch.setattr(guest_pay._geocode, "coarse_locate",
                        lambda **kw: {"lat": 42.6, "lon": 20.9, "level": "country"})
    body = client.post("/api/guest-guides/geocode",
                       json={"city": "Xërxë", "country_code": "XK",
                             "address_line1": "Rrugë Skënderbeu 307"}).json()
    assert body["found"] is False
    assert (body["lat"], body["lon"]) == (42.6, 20.9)
    assert body["hint_level"] == "country"


def test_geocode_never_500s_on_an_unexpected_failure(pay, monkeypatch):
    """V2-68c : une panne de géocodage (réseau, HTTP, quota Nominatim) se traite comme
    une adresse introuvable — repère + placement manuel, JAMAIS un 500 qui fermerait le
    parcours."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay, "_GEO_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(
        guest_pay._geocode, "geocode",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("nominatim 503")))
    monkeypatch.setattr(guest_pay._geocode, "coarse_locate",
                        lambda **kw: {"lat": 44.9, "lon": -0.7, "level": "postal"})
    r = client.post("/api/guest-guides/geocode",
                    json={"city": "Bégadan", "postal_code": "33340",
                          "country_code": "FR"})
    assert r.status_code == 200
    assert r.json()["found"] is False and r.json()["hint_level"] == "postal"


def test_checkout_refuses_imprecise_location_without_point(pay, monkeypatch):
    """V2-68 p1 : sans point ajusté, un ancrage trop vague (accuracy 'city') est REFUSÉ
    (422 imprecise_location) — jamais de paiement pour un guide invendable."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay._geocode, "geocode",
                        lambda **kw: {"lat": 35.6, "lon": 139.7, "accuracy": "city"})
    r = client.post("/api/guest-guides/checkout",
                    json={"email": "vague@paytest.com", "city": "Tokyo",
                          "country_code": "JP", "lang": "fr"})   # PAS de lat/lon
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "imprecise_location"


def test_checkout_allows_precise_location_without_point(pay, monkeypatch):
    """V2-68 p1 : une rue précise (accuracy 'street') passe sans point manuel."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay._geocode, "geocode",
                        lambda **kw: {"lat": 35.66, "lon": 139.70, "accuracy": "street"})
    r = client.post("/api/guest-guides/checkout",
                    json={"email": "precise@paytest.com", "city": "Tokyo",
                          "country_code": "JP", "address_line1": "1-2-3 Shibuya",
                          "lang": "fr"})
    assert r.status_code == 200 and r.json()["url"]


def test_checkout_with_adjusted_point_skips_precision_garde(pay):
    """V2-68 p1 : un point ajusté (lat/lon fournis) est toujours accepté (le tunnel a
    fait le travail) — la garde ne re-géocode pas."""
    client, _, _ = pay
    out = _checkout(client, "adjusted@paytest.com")   # _checkout envoie lat/lon
    assert out["token"]


def test_neighborhoods_endpoint(pay, monkeypatch):
    """V2-68 p2 : quartiers d'une grande ville, servis pour le choix d'ancrage."""
    client, _, _ = pay
    monkeypatch.setattr(guest_pay, "_GEO_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(guest_pay._geocode, "geocode",
                        lambda **kw: {"lat": 35.68, "lon": 139.76, "accuracy": "city"})
    from enrich import overpass
    monkeypatch.setattr(overpass, "nearby_neighborhoods",
                        lambda lat, lon, **kw: [{"name": "Shibuya", "lat": 35.66, "lon": 139.70},
                                                {"name": "Ginza", "lat": 35.67, "lon": 139.76}])
    r = client.post("/api/guest-guides/neighborhoods",
                    json={"city": "Tokyo", "country_code": "JP"})
    assert r.status_code == 200
    assert [h["name"] for h in r.json()] == ["Shibuya", "Ginza"]
    # Ville introuvable → liste vide (le tunnel retombe sur l'ajustement du point).
    monkeypatch.setattr(
        guest_pay._geocode, "geocode",
        lambda **kw: (_ for _ in ()).throw(guest_pay._geocode.GeocodeError("nope")))
    assert client.post("/api/guest-guides/neighborhoods",
                       json={"city": "Nulle", "country_code": "JP"}).json() == []


def test_checkout_success_url_is_merci_page(pay):
    """V2-54 C : le success_url pointe vers /#/voyageur/merci/{token} (corrige le défaut
    recette B : l'acheteur atterrissait sur l'écran de connexion propriétaire)."""
    client, gateway, _ = pay
    out = _checkout(client, "url@paytest.com")
    call = gateway.checkout_calls[0]
    assert f"/#/voyageur/merci/{out['token']}" in call["success_url"]


def test_checkout_503_without_stripe(pay):
    client, _, _ = pay
    app.dependency_overrides[get_stripe] = lambda: None
    try:
        r = client.post("/api/guest-guides/checkout",
                        json={"email": "x@paytest.com", "city": "Alicante",
                              "country_code": "ES"})
        assert r.status_code == 503
    finally:
        app.dependency_overrides[get_stripe] = lambda: FakeGuestGateway()
