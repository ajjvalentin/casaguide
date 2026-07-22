#!/usr/bin/env python3
"""Fixe le plan d'abonnement d'un ou plusieurs comptes par email (comptes internes).

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/set_plan.py \
        --plan pro andre@exemple.com florian.schefer@mac.com

Idempotent : chaque exécution fait converger le plan *courant* du compte vers la
valeur demandée, sans jamais créer de doublon de ligne `subscriptions`.
Relançable sans risque.

Modèle de données réel (cf. db/schema.sql) : le « plan courant » d'un compte est
la ligne `subscriptions` la plus récente (`ORDER BY created_at DESC`), via la
colonne **`plan_id`** (clé étrangère vers `plans(id)` — 'free' | 'solo' | 'pro').
Ce script écrit donc `plan_id`, jamais une colonne `plan` (qui n'existe pas).

Compte introuvable : signalé proprement, **sans erreur bloquante** (le script
continue avec les autres emails et sort en code 0) → relançable après inscription.

Connexion : DSN dans `CASAGUIDE_DB` (défaut `postgresql:///casaguide`, comme
`deploy.sh` — socket local / peer auth côté serveur).
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def _plan_exists(conn, plan_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM plans WHERE id = %s", (plan_id,)
    ).fetchone() is not None


def set_plan_for_email(conn, email: str, plan_id: str, status: str) -> str:
    """Fixe le plan courant du compte `email`. Retourne un code de résultat :
    'set' | 'unchanged' | 'created' | 'missing'."""
    owner = conn.execute(
        "SELECT id FROM owners WHERE lower(email) = lower(%s)", (email,)
    ).fetchone()
    if owner is None:
        return "missing"

    owner_id = owner["id"]
    current = conn.execute(
        """SELECT id, plan_id FROM subscriptions
           WHERE owner_id = %s ORDER BY created_at DESC LIMIT 1""",
        (owner_id,),
    ).fetchone()

    if current is None:
        # Aucun abonnement (compte OAuth n'ayant jamais reçu de ligne, ou base
        # partielle) → on en crée un, actif, sur le plan demandé.
        conn.execute(
            """INSERT INTO subscriptions (owner_id, plan_id, status)
               VALUES (%s, %s, %s)""",
            (owner_id, plan_id, status),
        )
        return "created"

    if current["plan_id"] == plan_id:
        return "unchanged"

    # Met à jour la ligne d'abonnement courante (la plus récente) — c'est elle
    # que lit l'API (repo.get_owner). Pas de nouvelle ligne → pas de doublon.
    conn.execute(
        """UPDATE subscriptions
           SET plan_id = %s, status = %s, updated_at = now()
           WHERE id = %s""",
        (plan_id, status, current["id"]),
    )
    return "set"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fixe le plan d'abonnement de comptes internes par email.",
    )
    parser.add_argument("emails", nargs="+", help="un ou plusieurs emails de comptes")
    parser.add_argument("--plan", default="pro",
                        help="identifiant de plan (défaut : pro ; cf. table plans)")
    parser.add_argument("--status", default="active",
                        help="statut de l'abonnement écrit (défaut : active)")
    parser.add_argument("--dsn", default=DEFAULT_DSN,
                        help=f"DSN PostgreSQL (défaut : {DEFAULT_DSN})")
    args = parser.parse_args(argv)

    try:
        conn = psycopg.connect(args.dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        print(f"✗ connexion à la base impossible : {exc}", file=sys.stderr)
        return 1

    missing: list[str] = []
    with conn:
        # `subscriptions` absente → erreur de configuration réelle (bloquante).
        try:
            plan_ok = _plan_exists(conn, args.plan)
        except psycopg.errors.UndefinedTable:
            print("✗ table 'subscriptions'/'plans' absente : appliquer le schéma "
                  "+ migrations (deploy.sh) avant de fixer les plans.",
                  file=sys.stderr)
            return 2

        if not plan_ok:
            print(f"✗ plan inconnu : '{args.plan}' (valeurs attendues : "
                  "free | solo | pro).", file=sys.stderr)
            return 2

        for email in args.emails:
            result = set_plan_for_email(conn, email, args.plan, args.status)
            if result == "missing":
                missing.append(email)
                print(f"⚠ {email} : compte introuvable — ignoré "
                      "(relancer après son inscription).")
            elif result == "unchanged":
                print(f"= {email} : déjà '{args.plan}', aucune modification.")
            elif result == "created":
                print(f"✓ {email} : abonnement '{args.plan}' créé (status={args.status}).")
            else:  # set
                print(f"✓ {email} : plan porté à '{args.plan}' (status={args.status}).")

    if missing:
        print(f"\n{len(missing)} compte(s) introuvable(s) : "
              f"{', '.join(missing)} — non bloquant, relançable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
