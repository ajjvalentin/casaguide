#!/usr/bin/env python3
"""(Re)construit le CACHE OVERTURE par zone (V2-84) — un parquet local par zone active.

Le pipeline lit ce cache au lieu de S3 quand la zone couvre le logement (quelques
millisecondes au lieu de 15 à 92 s, étude V2-83). Les zones vivent dans la configuration
(`ops/overture_zones.json`, ou `CASAGUIDE_OVERTURE_ZONES`) — jamais dans le code.

    …/python ops/overture_cache.py --list                 # état : release, âge, taille
    …/python ops/overture_cache.py --zone costa_blanca    # (re)construit une zone
    …/python ops/overture_cache.py --all                  # toutes les zones déclarées
    …/python ops/overture_cache.py --stale                # seulement les périmées/absentes

Mémoire : DuckDB plafonné (`CASAGUIDE_DUCKDB_MEMORY_LIMIT`, 1 Go par défaut), débordement
en flux sur disque — compatible avec un VPS de 3,8 Go. Écriture ATOMIQUE (fichier
temporaire puis renommage) : une génération en cours n'est jamais perturbée. Le pic de
mémoire du processus est affiché en fin de construction.

Reconstruction : à la main, ou par le timer OPTIONNEL `ops/optionnel/casaguide-overture-
cache.timer` (non installé par deploy.sh — à activer explicitement, cf. son en-tête).
"""
from __future__ import annotations

import argparse
import resource
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                       # ops/ (import opsenv)
sys.path.insert(0, str(_HERE.parent / "backend"))    # backend/ (import enrich.*)
import opsenv  # noqa: E402


def peak_rss_mb() -> float:
    """Pic de mémoire résidente du processus (Mo). ru_maxrss : Ko sous Linux, octets
    sous macOS."""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024 * 1024) if sys.platform == "darwin" else r / 1024, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="état des caches")
    g.add_argument("--zone", help="identifiant de zone à (re)construire")
    g.add_argument("--all", action="store_true", help="toutes les zones déclarées")
    g.add_argument("--stale", action="store_true",
                   help="seulement les zones absentes ou plus vieilles que le seuil")
    ap.add_argument("--release", default=None, help="release AAAA-MM-JJ.N (défaut : dernière)")
    ap.add_argument("--env-file", default=None, help="backend/.env à charger")
    args = ap.parse_args()
    opsenv.load_env(args.env_file)
    from enrich import overture                       # noqa: PLC0415 (après .env)
    from enrich.settings import settings              # noqa: PLC0415

    zones = overture.load_zones()
    if not zones:
        print(f"Aucune zone déclarée ({settings.overture_zones_file}).")
        return 1
    if args.list:
        print(f"{'zone':14} {'release':14} {'âge':>5} {'lieux':>8} {'Mo':>6}  état")
        for z in zones:
            m = overture.cache_meta(z["id"])
            if m is None:
                print(f"{z['id']:14} {'—':14} {'—':>5} {'—':>8} {'—':>6}  ABSENT")
                continue
            age = overture.cache_age_days(m)
            state = ("PÉRIMÉ" if age is None or age > settings.overture_cache_max_age_days
                     else "ok")
            print(f"{z['id']:14} {m.get('release') or '?':14} {age if age is not None else '?':>5} "
                  f"{m.get('places', 0):>8} {m.get('size_bytes', 0) / 1e6:>6.1f}  {state}")
        return 0

    if args.zone:
        targets = [z for z in zones if z["id"] == args.zone]
        if not targets:
            print(f"Zone inconnue : {args.zone} (déclarées : "
                  f"{', '.join(z['id'] for z in zones)})")
            return 2
    elif args.stale:
        targets = []
        for z in zones:
            m = overture.cache_meta(z["id"])
            age = overture.cache_age_days(m) if m else None
            if m is None or age is None or age > settings.overture_cache_max_age_days:
                targets.append(z)
    else:
        targets = zones

    release = overture.resolve_release(args.release)
    failures = 0
    for z in targets:
        try:
            m = overture.build_zone_cache(z, release=release)
            print(f"✓ {z['id']} : {m['places']} lieux, {m['size_bytes'] / 1e6:.1f} Mo, "
                  f"release {m['release']}, {m['build_seconds']} s — pic mémoire "
                  f"{peak_rss_mb()} Mo")
        except Exception as exc:  # noqa: BLE001 — une zone en échec n'arrête pas les autres
            failures += 1
            print(f"✗ {z['id']} : {type(exc).__name__}: {exc}")
    if not targets:
        print("Rien à reconstruire (tous les caches sont à jour).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
