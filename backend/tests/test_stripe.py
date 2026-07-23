"""Tests de la fondation Stripe (V2-05b, volet 1).

Deux volets, aucun appel réseau :
  - `sync_plans` : synchronisation plans→Stripe avec un **faux client** en
    mémoire (idempotence, archivage d'un ancien prix, écriture du stripe_price_id).
  - `LiveStripeGateway.construct_event` : vérification de signature webhook,
    exercée pour de vrai (pur calcul HMAC — la crypto de Stripe, sans réseau).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import billing_stripe  # noqa: E402
from enrich.settings import settings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))  # ops/
import stripe_sync_products as sync  # noqa: E402


# ── Faux client Stripe en mémoire (pour la synchronisation) ──────────────────

class _Collection:
    """Imite `client.v1.products` / `client.v1.prices` : list/create/modify."""

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._store: list[SimpleNamespace] = []
        self.creates = 0  # compteur pour asserter l'idempotence

    def list(self, params):
        active = params.get("active")
        product = params.get("product")
        data = [o for o in self._store
                if (active is None or o.active == active)
                and (product is None or getattr(o, "product", None) == product)]
        return SimpleNamespace(data=data)

    def create(self, params):
        self.creates += 1
        oid = f"{self._prefix}_{len(self._store) + 1}"
        obj = SimpleNamespace(id=oid, active=True, **params)
        # Metadata en dict simple (comme un StripeObject normalisé par dict()).
        obj.metadata = dict(params.get("metadata") or {})
        self._store.append(obj)
        return obj

    def modify(self, oid, params):
        for o in self._store:
            if o.id == oid:
                for k, v in params.items():
                    setattr(o, k, v)
                return o
        raise KeyError(oid)


class FakeStripeClient:
    def __init__(self):
        self.v1 = SimpleNamespace(products=_Collection("prod"),
                                  prices=_Collection("price"))


@pytest.fixture()
def conn():
    """Connexion réelle : les plans du seed (solo/pro) sont la source de vérité.
    On restaure les `stripe_price_id` en fin de test (non destructif)."""
    c = psycopg.connect(settings.db_dsn, row_factory=dict_row)
    snapshot = c.execute("SELECT id, stripe_price_id FROM plans").fetchall()
    try:
        yield c
    finally:
        c.rollback()
        for row in snapshot:
            c.execute("UPDATE plans SET stripe_price_id = %s WHERE id = %s",
                      (row["stripe_price_id"], row["id"]))
        c.commit()
        c.close()


# ── Synchronisation des plans ────────────────────────────────────────────────

def test_sync_creates_products_and_prices_and_stores_ids(conn):
    client = FakeStripeClient()
    result = sync.sync_plans(client, conn, log=lambda *_: None)

    # Un Price pour chaque plan payant (free ignoré : prix 0).
    paid = conn.execute(
        "SELECT id, price_month_cts FROM plans WHERE price_month_cts > 0"
    ).fetchall()
    assert set(result) == {p["id"] for p in paid}

    # stripe_price_id écrit en base pour chaque plan payant, jamais pour 'free'.
    for p in paid:
        stored = conn.execute("SELECT stripe_price_id FROM plans WHERE id = %s",
                              (p["id"],)).fetchone()["stripe_price_id"]
        assert stored == result[p["id"]]
    free = conn.execute(
        "SELECT stripe_price_id FROM plans WHERE id = 'free'").fetchone()
    assert free["stripe_price_id"] is None

    # Le montant du Price provient de la base (price_month_cts), en EUR mensuel.
    for pr in client.v1.prices._store:
        plan_id = pr.metadata["plan_id"]
        expected = next(p["price_month_cts"] for p in paid if p["id"] == plan_id)
        assert pr.unit_amount == expected
        assert pr.currency == "eur"
        assert pr.recurring["interval"] == "month"


def test_sync_is_idempotent(conn):
    client = FakeStripeClient()
    first = sync.sync_plans(client, conn, log=lambda *_: None)
    prod_creates = client.v1.products.creates
    price_creates = client.v1.prices.creates

    second = sync.sync_plans(client, conn, log=lambda *_: None)
    # Aucune création supplémentaire : mêmes Products, mêmes Prices.
    assert client.v1.products.creates == prod_creates
    assert client.v1.prices.creates == price_creates
    assert first == second


def test_sync_price_change_creates_new_and_archives_old(conn):
    client = FakeStripeClient()
    sync.sync_plans(client, conn, log=lambda *_: None)
    old_price_id = conn.execute(
        "SELECT stripe_price_id FROM plans WHERE id = 'solo'").fetchone()["stripe_price_id"]

    # Changement de prix du plan 'solo' → nouveau Price, ancien archivé.
    conn.execute("UPDATE plans SET price_month_cts = 990 WHERE id = 'solo'")
    sync.sync_plan(client, conn,
                   conn.execute("SELECT id, name, price_month_cts FROM plans "
                                "WHERE id = 'solo'").fetchone(),
                   log=lambda *_: None)

    new_price_id = conn.execute(
        "SELECT stripe_price_id FROM plans WHERE id = 'solo'").fetchone()["stripe_price_id"]
    assert new_price_id != old_price_id

    prices = {p.id: p for p in client.v1.prices._store}
    assert prices[old_price_id].active is False   # archivé, jamais supprimé
    assert prices[new_price_id].active is True
    assert prices[new_price_id].unit_amount == 990


# ── Vérification de signature des webhooks (crypto réelle, sans réseau) ───────

WEBHOOK_SECRET = "whsec_test_0123456789abcdef0123456789abcdef"


def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Construit un en-tête `Stripe-Signature` valide (schéma officiel :
    HMAC-SHA256 de « {t}.{payload} »)."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _gateway(secret: str | None = WEBHOOK_SECRET) -> billing_stripe.LiveStripeGateway:
    return billing_stripe.LiveStripeGateway(api_key="sk_test_x",
                                            webhook_secret=secret)


def test_construct_event_accepts_valid_signature():
    # `object: event` : présent sur tout événement Stripe réel (la librairie le
    # lit pour distinguer v1/v2). Le reste du payload est réaliste.
    payload = json.dumps({"id": "evt_1", "object": "event",
                          "type": "checkout.session.completed",
                          "data": {"object": {"customer": "cus_1"}}}).encode()
    header = _sign(payload, WEBHOOK_SECRET)
    event = _gateway().construct_event(payload, header)
    # Renvoie un dict simple exploitable directement par le handler.
    assert event["id"] == "evt_1"
    assert event["data"]["object"]["customer"] == "cus_1"
    assert event.get("type") == "checkout.session.completed"  # bien un dict natif


def test_construct_event_rejects_invalid_signature():
    payload = b'{"id":"evt_2","type":"x"}'
    bad = _sign(payload, "whsec_wrong_secret")
    with pytest.raises(billing_stripe.SignatureError):
        _gateway().construct_event(payload, bad)


def test_construct_event_rejects_tampered_payload():
    payload = b'{"id":"evt_3","type":"x"}'
    header = _sign(payload, WEBHOOK_SECRET)
    with pytest.raises(billing_stripe.SignatureError):
        _gateway().construct_event(b'{"id":"evt_3","type":"TAMPERED"}', header)


def test_construct_event_without_secret_raises():
    payload = b'{"id":"evt_4"}'
    with pytest.raises(ValueError):
        _gateway(secret=None).construct_event(payload, "t=1,v1=deadbeef")


def test_build_stripe_disabled_without_key():
    cfg = SimpleNamespace(stripe_configured=False, stripe_secret_key=None,
                          stripe_webhook_secret=None)
    assert billing_stripe.build_stripe(cfg) is None
    cfg2 = SimpleNamespace(stripe_configured=True, stripe_secret_key="sk_test_x",
                           stripe_webhook_secret=WEBHOOK_SECRET)
    assert isinstance(billing_stripe.build_stripe(cfg2),
                      billing_stripe.LiveStripeGateway)
