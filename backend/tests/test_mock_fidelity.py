"""V2-76b — les faux des services externes reflètent leurs réponses ACTUELLES.

Le cas jsonv2 (V2-79) : depuis le 01/09 le code interroge Nominatim en `format=jsonv2`,
qui range la classe OSM sous `category` et porte `place_rank` ; les faux renvoyaient
encore `class: building` (forme `format=json`) → tout pick était « placé » en test, aucun
en production, trois semaines durant. Ce garde rend la dérive MÉCANIQUE à voir : un faux
Nominatim qui réintroduit `"class":` fait rougir la suite (sauf ligne marquée
`format=json`, réservée aux tests de rétro-compatibilité explicites).

Audit du 05/10 (consigné dans CLAUDE.md, « Leçons de méthode », mock ≠ réel) :
  · Nominatim — 5 faux en `class` (forme json) → alignés sur jsonv2 (category+place_rank).
  · Overture  — faux en `bank_credit_union` (feuille disparue) et sans hiérarchie →
    alignés sur la release 2026-09-23.1 (`_OVT_HIERARCHY`, helper strict) ; fixture
    parquet du schéma `taxonomy` recopiée du DESCRIBE réel.
  · Overpass  — `{"elements":[{type,id,lat/lon|center,tags}]}` : fidèle.
  · OSRM      — `{"code":"Ok","durations","distances"}` : fidèle.
  · Anthropic — `content[].type/text`, `stop_reason`, `usage.input/output_tokens`,
    `usage.server_tool_use.web_search_requests` : fidèle aux champs lus (SDK 0.116).
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_no_nominatim_fake_in_legacy_json_form():
    offenders = []
    for f in sorted(TESTS.glob("*.py")):
        if f.name == Path(__file__).name:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"class"\s*:', line) and "format=json" not in line:
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, (
        "Faux Nominatim en forme `format=json` (`class`) — le code interroge en "
        "`format=jsonv2` (`category` + `place_rank`). Recopiez une réponse réelle :\n"
        + "\n".join(offenders))


def test_geocode_really_asks_jsonv2():
    """Le garde ci-dessus n'a de sens que si le code demande bien jsonv2 — sinon c'est
    le garde qui deviendrait le faux. Vérifié sur la source, pas sur une supposition."""
    src = (TESTS.parent / "enrich" / "geocode.py").read_text(encoding="utf-8")
    assert '"format": "jsonv2"' in src
