#!/usr/bin/env python3
"""Audit du parc — cohérence commune/CP des géocodages EXISTANTS (V2-46). LECTURE SEULE.

CASA MURCIA (06/09) : « Príncipe de Asturias 38, 30007, MURCIA » a été géocodée sur une
rue HOMONYME de Torre-Pacheco (30700, ~40 km), étiquetée « précis », 132 POI hors sujet
sur une fiche publiée — sans alerte. Le contrôle amont (V2-46) protège les NOUVEAUX
géocodages ; ce script contrôle l'EXISTANT.

Pour chaque logement positionné, on géocode en INVERSE sa position ENREGISTRÉE (Nominatim
reverse) → commune/CP réels du point, qu'on confronte à la SAISIE (ville + code postal)
par le même comparateur PUR que le géocodage (`geocode.check_geocode_consistency`).
Reverse (pas forward) : on veut savoir OÙ le point est réellement, sans reproduire le
défaut d'homonymie du forward. **Aucune écriture** (SELECT + appels réseau read-only) —
André corrige à la main les fiches signalées.

Trivial aujourd'hui (5 fiches) ; le jour où il y a 500 clients, c'est un audit.

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/audit_geocode.py
    …/audit_geocode.py --property-id <uuid>     # une seule fiche

Politesse Nominatim : 1 req/s (pause entre logements). Charge `backend/.env` (OPS-1).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import dict_row

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                       # ops/ (import opsenv)
sys.path.insert(0, str(_HERE.parent / "backend"))    # backend/ (import enrich.*)
import opsenv  # noqa: E402
from enrich import geocode  # noqa: E402

log = logging.getLogger("casaguide.audit_geocode")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def load_properties(conn, property_id: str | None) -> list[dict]:
    """Logements POSITIONNÉS avec leur saisie (ville/CP) et leur position enregistrée."""
    sql = ("""SELECT id::text AS id, name, city, postal_code, geocode_accuracy,
                     geocode_source, ST_Y(geom) AS lat, ST_X(geom) AS lon
              FROM properties WHERE geom IS NOT NULL""")
    params: tuple = ()
    if property_id:
        sql += " AND id = %s"
        params = (property_id,)
    return conn.execute(sql + " ORDER BY name", params).fetchall()


def audit(properties: list[dict],
          reverse: Callable[[float, float], dict | None]) -> list[dict]:
    """Confronte chaque position à sa saisie. Renvoie la liste des ÉCARTS (dicts prêts
    pour le rapport). `reverse(lat, lon) -> address|None` est injecté (réel : Nominatim ;
    test : bouchon). PUR hors l'appel `reverse`."""
    findings: list[dict] = []
    for p in properties:
        addr = reverse(p["lat"], p["lon"])
        mm = geocode.check_geocode_consistency(p["city"], p["postal_code"], addr)
        if mm is not None:
            findings.append({
                "id": p["id"], "name": p["name"],
                "input_city": mm.input_city, "input_postcode": mm.input_postcode,
                "result_locality": mm.result_locality,
                "result_postcode": mm.result_postcode,
                "accuracy": p["geocode_accuracy"], "source": p["geocode_source"],
                "message": mm.message_fr()})
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit lecture seule de la cohérence commune/CP des géocodages "
                    "existants (V2-46).")
    parser.add_argument("--property-id", default=None)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="pause (s) entre logements — politesse Nominatim.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        props = load_properties(conn, args.property_id)
    log.info("· %d logement(s) positionné(s) à auditer.", len(props))

    # Reverse réel Nominatim, avec pause de politesse ENTRE les appels.
    first = {"done": False}

    def reverse(lat: float, lon: float) -> dict | None:
        if first["done"]:
            time.sleep(args.delay)
        first["done"] = True
        try:
            return geocode.reverse(lat, lon)
        except Exception as exc:  # noqa: BLE001 — un logement illisible ne casse pas l'audit
            log.warning("  ⚠ reverse échoué (%.5f,%.5f) : %s", lat, lon, exc)
            return None

    findings = audit(props, reverse)
    if not findings:
        log.info("✔ aucun écart commune/CP détecté.")
        return 0
    log.warning("⚠ %d logement(s) avec écart commune/CP :", len(findings))
    for f in findings:
        log.warning("  · %s [%s] — %s (accuracy=%s, source=%s)",
                    f["name"], f["id"], f["message"], f["accuracy"], f["source"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
