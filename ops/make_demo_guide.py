#!/usr/bin/env python3
"""Crée / régénère le guide de DÉMONSTRATION de la vitrine (V2-58) — idempotent.

La page d'accueil holaguia.com montre un VRAI guide cliquable (le différenciateur du
créneau). Ce script crée UNE fiche guest dédiée « Démo — La Zenia » marquée `demo`
(exclue du cache anti-abus, jamais un guide client), l'enrichit par la chaîne STANDARD
(le pipeline juge et publie), puis la traduit. Rejouable : sans `--force`, si la démo
existe déjà, on ne la recrée pas (le token reste STABLE pour la vitrine).

Usage (dans le venv de l'app, `backend/.env` chargé — OPS-1) :
    python ops/make_demo_guide.py                 # crée si absente, sinon ne touche à rien
    python ops/make_demo_guide.py --force         # re-enrichit la démo existante
    python ops/make_demo_guide.py --no-claude     # étapes géo seules (test)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "backend"))
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.make_demo_guide")

DEMO_NAME = "Démo — La Zenia"
DEMO_CITY = "La Zenia"
DEMO_COUNTRY = "ES"
DEMO_LAT, DEMO_LON = 37.926, -0.749


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guide de démonstration de la vitrine (V2-58).")
    parser.add_argument("--force", action="store_true",
                        help="re-enrichir la démo existante (sinon on n'y touche pas).")
    parser.add_argument("--no-claude", action="store_true")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    if args.dsn:
        os.environ["CASAGUIDE_DB"] = args.dsn

    from enrich import db, pipeline, translate  # noqa: PLC0415
    from api import repo  # noqa: PLC0415

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id::text AS id, guide_token, status FROM properties "
            "WHERE demo AND guest_guide ORDER BY created_at LIMIT 1").fetchone()
        if row and not args.force:
            log.info("✔ démo déjà présente (token stable) : /g/%s [%s]",
                     row["guide_token"], row["status"])
            return 0
        if row:
            pid, token = row["id"], row["guide_token"]
            log.info("· re-enrichissement de la démo existante : %s", pid)
        else:
            prop = repo.create_guest_property(
                conn, name=DEMO_NAME, city=DEMO_CITY, country_code=DEMO_COUNTRY,
                lat=DEMO_LAT, lon=DEMO_LON, demo=True)
            conn.commit()
            pid, token = str(prop["id"]), prop["guide_token"]
            log.info("· démo créée : %s", pid)

    pipeline.run_with_retries(pid, use_claude=not args.no_claude, trigger="demo")
    if not args.no_translate:
        try:
            translate.run(pid)
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("Traduction de la démo non résolue : %s", exc)

    log.info("✔ démo prête : /g/%s", token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
