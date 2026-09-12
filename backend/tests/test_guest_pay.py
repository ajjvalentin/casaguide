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
