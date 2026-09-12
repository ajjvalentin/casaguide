#!/usr/bin/env python3
"""Génère un GUIDE VOYAGEUR (V2-54, offre one-shot) — recette & socle backend.

Crée une fiche guest à une adresse (ou un point lat/lon déjà ajusté), lance
l'enrichissement complet (le pipeline juge et publie automatiquement), puis traduit
le guide en 7 langues. C'est l'entrée de génération de l'offre « Guide Voyageur » —
la MÊME fonction (`api.guest_guides.generate_guest_guide`) sera appelée par le webhook
Stripe en Mission B. Ce script est le chemin de recette manuelle (Mission A, item 5).

Usage (dans le venv de l'app, `backend/.env` chargé automatiquement — OPS-1) :

    python ops/make_guest_guide.py --city "Orihuela Costa" --country ES \\
        --address "Calle Ejemplo 1"
    python ops/make_guest_guide.py --city Ardon --country CH --lat 46.21 --lon 7.26
    python ops/make_guest_guide.py … --no-claude   # étapes géo seules (test rapide)

Connexion : DSN dans `CASAGUIDE_DB` (ou --dsn). L'enrichissement/traduction réels
exigent `ANTHROPIC_API_KEY` (sauf --no-claude / --no-translate).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                     # ops/ (opsenv)
sys.path.insert(0, str(_HERE.parent / "backend"))  # backend/ (api.*, enrich.*)
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.make_guest_guide")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Génère un guide voyageur (V2-54).")
    parser.add_argument("--city", required=True)
    parser.add_argument("--country", required=True, help="code ISO 3166-1 (ES, FR, CH…)")
    parser.add_argument("--address", default=None, help="rue (géocodée si --lat/--lon absents)")
    parser.add_argument("--postal", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--name", default=None, help="défaut : « Guide — <commune> »")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--email", default=None, help="anti-abus (facultatif)")
    parser.add_argument("--no-claude", action="store_true", help="sauter l'IA (test géo)")
    parser.add_argument("--no-translate", action="store_true", help="pas de traduction")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    if args.dsn:
        os.environ["CASAGUIDE_DB"] = args.dsn  # avant l'import des modules enrich/api

    # Import TARDIF (après chargement .env et override du DSN).
    from api import guest_guides  # noqa: PLC0415

    try:
        res = guest_guides.generate_guest_guide(
            city=args.city, country_code=args.country, address=args.address,
            postal_code=args.postal, region=args.region, name=args.name,
            lat=args.lat, lon=args.lon, email=args.email,
            use_claude=not args.no_claude, do_translate=not args.no_translate)
    except guest_guides.GuestGuideMismatch as exc:
        log.error("✖ position incohérente : %s", exc.message)
        log.error("  → ajustez le point (le tunnel le fera ; ici passez --lat/--lon).")
        return 3
    except guest_guides.GuestGuideError as exc:
        log.error("✖ génération refusée (%s) : %s", exc.code, exc.message)
        return 4

    prop = res["property"] or {}
    token = prop.get("guide_token")
    if res["cached"]:
        log.info("✔ CACHE : guide voisin récent resservi — token %s", token)
        return 0

    summary = res["summary"] or {}
    print("\n=== Guide voyageur généré ===", flush=True)
    print(f"  Logement        : {prop.get('id')}  ({prop.get('city')}, {prop.get('country_code')})")
    print(f"  Lien du guide   : /g/{token}")
    print(f"  Statut          : {prop.get('status')}")
    print(f"  Langues publiées : {', '.join(prop.get('published_langs') or []) or '(fr seul)'}")
    print(f"  POI moissonnés  : {summary.get('pois', 0)}")
    print(f"  Juge → approuvés : {summary.get('judge_approved', 0)}  "
          f"rejetés : {summary.get('judge_rejected', 0)}")
    print(f"  Coût IA         : {summary.get('cost_cts', 0.0):.2f} ct")
    failed = summary.get("failed_categories") or {}
    if failed:
        print(f"  Catégories en échec : {', '.join(sorted(failed))}")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
