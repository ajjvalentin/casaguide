#!/usr/bin/env python3
"""Synchronise les plans payants (table `plans`) vers Stripe (V2-05b).

C'est LE pont plans↔Stripe, dans un seul sens : la table `plans` est la source de
vérité des prix (`price_month_cts`) ; ce script crée/retrouve les Products et
Prices Stripe correspondants et écrit le `stripe_price_id` en base. Jamais
l'inverse — Stripe n'est jamais la source d'un prix.

Idempotent et relançable sans doublon :
  - Product retrouvé par metadata `plan_id` (créé s'il manque) ;
  - Price retrouvé par (montant, devise, mensuel) sur ce Product (créé s'il
    manque ; un ancien Price au montant différent est **archivé**, jamais
    supprimé — les abonnements en cours dessus restent valides côté Stripe).

Un changement de prix = mettre à jour le seed + relancer ce script : un nouveau
Price est créé, l'ancien archivé, `plans.stripe_price_id` pointe sur le nouveau.

Usage (mode Test en développement, live en production — au choix de la clé) :

    export CASAGUIDE_STRIPE_SECRET_KEY=sk_test_...        # ou sk_live_...
    export CASAGUIDE_DB=postgresql:///casaguide           # défaut ci-dessous
    /opt/casaguide/.venv/bin/python ops/stripe_sync_products.py

Le plan 'free' (prix 0) est ignoré : aucun Price Stripe (rien à facturer).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))  # ops/ (import opsenv)
import opsenv  # noqa: E402

CURRENCY = "eur"


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


# ── Accès normalisés (fonctionne avec les objets Stripe ET les fakes de test) ─

def _as_dict(value) -> dict:
    """Normalise une valeur Stripe imbriquée (metadata, recurring…) en dict Python.

    ATTENTION : un `StripeObject` **n'est pas** un `dict` (son MRO est
    `[StripeObject, object]`), n'expose ni `.get`/`.items`/`.keys` (interceptés par
    `__getattr__` → `AttributeError`) et n'implémente pas le protocole de mapping :
    `dict(stripe_obj)` l'itère comme une séquence et lève **`KeyError: 0`**. On
    passe donc par `.to_dict()` (fourni par tout StripeObject) ; repli `dict(...)`
    pour un dict/namespace simple, `{}` pour None ou l'imprévu."""
    if value is None:
        return {}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    try:
        return dict(value)
    except (TypeError, ValueError, KeyError):
        return {}


def _metadata(obj) -> dict:
    """Metadata d'un objet Stripe en dict simple (jamais None)."""
    return _as_dict(getattr(obj, "metadata", None))


def _interval(price) -> str | None:
    """Intervalle de récurrence ('month', 'year'…) d'un Price, ou None."""
    return _as_dict(getattr(price, "recurring", None)).get("interval")


# ── Cœur testable (client Stripe injecté, aucune I/O directe hormis via lui) ──

def find_product(client, plan_id: str):
    """Product Stripe portant `metadata.plan_id == plan_id`, ou None."""
    for p in client.v1.products.list({"active": True, "limit": 100}).data:
        if _metadata(p).get("plan_id") == plan_id:
            return p
    return None


def find_price(client, product_id: str, amount_cts: int):
    """Price actif mensuel de ce Product au bon montant/devise, ou None."""
    for pr in client.v1.prices.list(
            {"product": product_id, "active": True, "limit": 100}).data:
        if (pr.unit_amount == amount_cts and pr.currency == CURRENCY
                and _interval(pr) == "month"):
            return pr
    return None


def _archive_stale_prices(client, product_id: str, keep_price_id: str) -> int:
    """Archive (active=False) les autres Prices actifs de ce Product : ils
    correspondent à d'anciens montants. Jamais de suppression. Renvoie le nombre
    archivé."""
    n = 0
    for pr in client.v1.prices.list(
            {"product": product_id, "active": True, "limit": 100}).data:
        if pr.id != keep_price_id:
            # Surface StripeClient : `client.v1.<svc>.update`, PAS `.modify`
            # (modify = ancienne API par ressources `stripe.Price.modify`).
            client.v1.prices.update(pr.id, {"active": False})
            n += 1
    return n


def sync_plan(client, conn, plan: dict, *, log=print) -> str:
    """Synchronise un plan payant. Renvoie l'id du Price Stripe actif."""
    plan_id = plan["id"]
    amount = plan["price_month_cts"]
    name = f"Holaguia {plan['name']}"

    product = find_product(client, plan_id)
    if product is None:
        product = client.v1.products.create({
            "name": name,
            "metadata": {"plan_id": plan_id},
        })
        log(f"  ✓ Product créé : {product.id} ({plan_id})")
    else:
        # Garde le nom à jour (le prix, lui, vit dans un Price immuable).
        # Surface StripeClient : `.update`, pas `.modify` (cf. _archive_stale_prices).
        client.v1.products.update(product.id, {"name": name})
        log(f"  = Product existant : {product.id} ({plan_id})")

    price = find_price(client, product.id, amount)
    if price is None:
        price = client.v1.prices.create({
            "product": product.id,
            "unit_amount": amount,
            "currency": CURRENCY,
            "recurring": {"interval": "month"},
            "metadata": {"plan_id": plan_id},
        })
        log(f"  ✓ Price créé : {price.id} ({amount/100:.2f} {CURRENCY.upper()}/mois)")
    else:
        log(f"  = Price existant : {price.id} ({amount/100:.2f} {CURRENCY.upper()}/mois)")

    archived = _archive_stale_prices(client, product.id, price.id)
    if archived:
        log(f"  ⤷ {archived} ancien(s) Price(s) archivé(s) (montant obsolète)")

    conn.execute("UPDATE plans SET stripe_price_id = %s WHERE id = %s",
                 (price.id, plan_id))
    return price.id


def sync_plans(client, conn, *, log=print) -> dict[str, str]:
    """Synchronise tous les plans payants (prix > 0). Renvoie {plan_id: price_id}.
    Testable : `client` est injecté (aucun appel réseau dans la suite de tests)."""
    plans = conn.execute(
        "SELECT id, name, price_month_cts FROM plans "
        "WHERE price_month_cts > 0 ORDER BY price_month_cts"
    ).fetchall()
    result: dict[str, str] = {}
    for plan in plans:
        log(f"Plan « {plan['id']} » :")
        result[plan["id"]] = sync_plan(client, conn, plan, log=log)
    conn.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronise les plans payants (table plans) vers Stripe.")
    parser.add_argument(
        "--env-file",
        help="chemin d'un .env à charger (défaut : backend/.env ; les variables "
             "déjà exportées priment).")
    parser.add_argument("--dsn", help="DSN PostgreSQL (défaut : CASAGUIDE_DB ou "
                        "postgresql:///casaguide).")
    args = parser.parse_args(argv)

    # Volet 2 : les scripts ops/ chargent le .env eux-mêmes (pas d'EnvironmentFile
    # systemd en exécution manuelle ; pas de `source` bash — chevrons du SMTP_FROM).
    loaded = opsenv.load_env(args.env_file)
    if loaded:
        print(f"· configuration chargée depuis {loaded}")
    elif args.env_file:
        print(f"⚠ --env-file introuvable : {args.env_file} (repli sur "
              "l'environnement).", file=sys.stderr)

    api_key = os.getenv("CASAGUIDE_STRIPE_SECRET_KEY")
    if not api_key:
        print("✗ CASAGUIDE_STRIPE_SECRET_KEY absente : impossible de synchroniser "
              "(renseignez la clé sk_test_… ou sk_live_… dans le .env ou "
              "l'environnement).", file=sys.stderr)
        return 2

    import stripe  # import tardif : le script n'est utile que si stripe est là
    client = stripe.StripeClient(api_key)

    mode = "LIVE" if api_key.startswith("sk_live_") else "TEST"
    print(f"Synchronisation des plans vers Stripe (mode {mode})…")

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        print(f"✗ connexion à la base impossible : {exc}", file=sys.stderr)
        return 1

    with conn:
        result = sync_plans(client, conn)
    print(f"\n✓ Terminé : {len(result)} plan(s) synchronisé(s) → "
          + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
