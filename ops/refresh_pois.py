#!/usr/bin/env python3
"""Revérification planifiée des lieux auprès de leur source d'origine (V2-93).

Chaque nuit (timer systemd `casaguide-refresh-pois.timer`), ne traite que les
logements DUS : publiés (hors guide voyageur et démonstration) dont au moins une
fiche retenue OSM/Overture n'a pas été contrôlée depuis
`CASAGUIDE_REFRESH_MAX_AGE_DAYS` (7 j par défaut) — la charge s'étale d'elle-même.

Par logement : un job `enrichment_jobs` (`trigger='refresh'`, hors quota), un lot de
journal (auteur `system`) ; interrogation des sources PAR IDENTIFIANT (Overpass ;
Overture dans le cache de zone local seulement), puis report de `phone` / `website` /
`opening_hours` selon le verrou par champ (`enrich.locks.plan_refresh`). Un lieu
fermé ou disparu à la source est constaté (`pois.source_state`), jamais retiré.
Aucun appel IA. Détail : `docs/ops.md` § 5.

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/refresh_pois.py [--dry-run]
        [--property <uuid>] [--max-properties N]

`--dry-run` : mêmes lectures (réseau compris) et même plan, AUCUNE écriture (ni job,
ni fiche) ; le bilan est celui qu'écrirait l'exécution réelle.
Code de sortie : 0 si tout s'est bien passé, 1 si au moins un logement a échoué
(il sera repris la nuit suivante) ou si le passage a été ARRÊTÉ par un refus du compte
Overpass (quota épuisé, clé refusée — V2-93b : les logements restants ne sont pas
tentés), 2 si la base n'est pas migrée (044).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                  # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # enrich.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.refresh_pois")


def main(argv: list[str] | None = None, *, osm_fetch=None, overture_fetch=None,
         sleep=None) -> int:
    parser = argparse.ArgumentParser(
        description="Revérifie les lieux des guides auprès de leur source (V2-93).")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan sans aucune écriture (lectures réseau réelles).")
    parser.add_argument("--property", dest="property_id",
                        help="un seul logement (quel que soit l'âge de ses contrôles).")
    parser.add_argument("--max-properties", type=int, default=None,
                        help="nombre maximal de logements traités.")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut : backend/.env).")
    parser.add_argument("--verbose", action="store_true",
                        help="détaille chaque différence trouvée.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)

    # Imports APRÈS chargement du .env (enrich.settings lit l'environnement).
    import psycopg
    from enrich import db, overpass, refresh

    log.info("· %s", overpass.describe_mode())   # V2-93b : mode actif, jamais la clé
    sent_before = overpass.request_count

    try:
        conn = db.connect()
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1
    with conn:
        try:
            reports = refresh.run(conn, property_id=args.property_id,
                                  max_properties=args.max_properties,
                                  dry_run=args.dry_run, osm_fetch=osm_fetch,
                                  overture_fetch=overture_fetch, sleep=sleep)
        except psycopg.errors.UndefinedColumn:
            log.error("✗ colonnes de contrôle absentes : appliquer la migration 044 "
                      "(deploy.sh) avant de lancer la revérification.")
            return 2

    tag = "[dry-run] " if args.dry_run else ""
    failed = skipped = 0
    stopped_by = None
    for r in reports:
        if r.skipped:
            skipped += 1
            log.warning("%s– %s — non tenté (passage arrêté)", tag, r.property_id)
            continue
        if not r.ok:
            failed += 1
            stopped_by = r if r.fatal else stopped_by
            log.warning("%s✗ %s — échec : %s (job %s)", tag, r.property_id,
                        overpass.scrub(r.error), r.job_id)
            continue
        log.info("%s✓ %s — %s%s", tag, r.property_id,
                 json.dumps(r.steps(), ensure_ascii=False),
                 f" (job {r.job_id}, lot {r.batch_id})" if r.job_id else "")
        if args.verbose or args.dry_run:
            for c in r.changes:
                log.info("    %s %s : %r → %r", c["source_ref"], c["field"],
                         c["old"], c["new"])
    if stopped_by is not None:
        log.error("%s✗ Passage ARRÊTÉ sur %s (refus du compte Overpass : %s) — %d "
                  "logement(s) restant(s) non tenté(s). Voir docs/deploiement.md "
                  "§ Overpass.", tag, stopped_by.property_id,
                  overpass.scrub(stopped_by.error), skipped)
    log.info("%sTerminé : %d logement(s), %d en échec, %d non tenté(s) ; %d requête(s) "
             "Overpass envoyée(s).", tag, len(reports), failed, skipped,
             overpass.request_count - sent_before)
    return 1 if (failed or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
