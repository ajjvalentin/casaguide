#!/usr/bin/env python3
"""Génère les PROPOSITIONS de traduction des libellés statiques (V2-21a, volet 3).

Remplit `ui_translations` (source unique, invariant 15) avec des traductions
Claude des libellés de l'inventaire (`i18n/inventory.json`), langue par langue,
en imposant au modèle le REGISTRE de chaque langue (`languages.register_note`).
Ces propositions apparaissent ensuite dans l'export relecteur (colonne
« proposition ») — le relecteur corrige, le réimport écrase.

Cibles = les langues du registre qui NE sont PAS portées par le code/seed
(nl/de/it/sq…) — jamais fr/en/es (déjà en dur dans le code). Par défaut, toutes ;
`--lang xx` en cible une seule.

SÛR PAR DÉFAUT :
- saute les clés **déjà présentes** en base pour la langue → ne clobbe jamais une
  correction relue déjà importée (`--overwrite` pour tout régénérer) ;
- `--dry-run` n'appelle PAS l'API : il montre seulement combien de libellés
  seraient traduits (utile avant de dépenser) ;
- `--limit N` borne le nombre de libellés traduits (fumée bon marché).

Chaque appel Claude est comptabilisé dans `api_costs` (provider='anthropic',
operation='ui_translate', property_id/job_id NULL — coût produit, pas par
logement). Charge `backend/.env` lui-même (OPS-1) ; DSN dans `CASAGUIDE_DB`,
clé dans `ANTHROPIC_API_KEY`.

Lancement :

    python ops/i18n_generate.py --dry-run                 # plan (aucun appel API)
    python ops/i18n_generate.py --lang de                 # génère l'allemand
    python ops/i18n_generate.py                            # toutes les langues cibles
    python ops/i18n_generate.py --lang de --overwrite      # régénère tout
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))                    # opsenv
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))    # api.*/enrich.*
import opsenv  # noqa: E402
from i18n_inventory import INVENTORY_PATH, load_inventory  # noqa: E402

log = logging.getLogger("casaguide.i18n_generate")


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


def target_languages(conn) -> list[dict]:
    """Langues du registre candidates à la génération : celles NON portées par le
    code/seed (`api.i18n.SOURCE_LANGS`). Renvoie `[{code, register_note}, …]`
    triées par `sort_order` (ordre d'affichage du produit)."""
    from api import i18n
    rows = conn.execute(
        "SELECT code, register_note FROM languages ORDER BY sort_order, code"
    ).fetchall()
    return [dict(r) for r in rows if r["code"] not in i18n.SOURCE_LANGS]


def register_note_for(conn, lang: str) -> str | None:
    row = conn.execute(
        "SELECT register_note FROM languages WHERE code = %s", (lang,)).fetchone()
    return row["register_note"] if row else None


def generate_lang(conn, lang: str, inventory: dict, translator, *,
                  register_note: str | None = None, overwrite: bool = False,
                  batch_size: int = 40, limit: int | None = None) -> dict:
    """(Re)génère les propositions de `lang` et les écrit dans `ui_translations`.

    Écrit + comptabilise **par lot** (chaque lot committé) : une interruption
    laisse un état cohérent (les lots déjà traités sont conservés). Renvoie un
    rapport `{candidates, written, cost_cts, units, batches}`. Le `translator`
    (objet exposant `.translate(labels, target_lang, register_note)`) est
    injectable pour les tests."""
    from api import repo
    from enrich import db, translate_ui

    existing = repo.ui_translations(conn, lang)
    labels = translate_ui.select_labels(inventory, existing, overwrite=overwrite)
    if limit is not None:
        labels = dict(list(labels.items())[:limit])

    report = {"candidates": len(labels), "written": 0, "cost_cts": 0.0,
              "units": 0, "batches": 0}
    for batch in translate_ui.iter_batches(labels, batch_size):
        translations, meta = translator.translate(
            batch, target_lang=lang, register_note=register_note)
        for key, text in translations.items():
            repo.upsert_ui_translation(conn, lang, key, text)
        db.record_cost(conn, None, None, "anthropic", "ui_translate",
                       meta["units"], meta["cost_cts"])
        conn.commit()
        report["written"] += len(translations)
        report["cost_cts"] += meta["cost_cts"]
        report["units"] += meta["units"]
        report["batches"] += 1
    report["cost_cts"] = round(report["cost_cts"], 4)
    return report


def _build_translator():
    """Traducteur Claude réel (clé dans `ANTHROPIC_API_KEY`)."""
    import anthropic
    from enrich import translate_ui
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return translate_ui.ClaudeUILabelTranslator(anthropic.Anthropic(api_key=key))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Génère les propositions de traduction des libellés statiques "
                    "(V2-21a, volet 3).")
    parser.add_argument("--lang", default=None,
                        help="code langue cible (défaut : toutes les cibles).")
    parser.add_argument("--overwrite", action="store_true",
                        help="régénère aussi les clés déjà en base (par défaut "
                             "seules les manquantes sont générées).")
    parser.add_argument("--batch-size", type=int, default=40,
                        help="nombre de libellés par appel Claude (défaut 40).")
    parser.add_argument("--limit", type=int, default=None,
                        help="borne le nombre de libellés traduits (fumée).")
    parser.add_argument("--dry-run", action="store_true",
                        help="n'appelle pas l'API : montre seulement le plan.")
    parser.add_argument("--dsn", default=None, help="DSN (défaut CASAGUIDE_DB).")
    parser.add_argument("--env-file", help="chemin d'un .env (défaut backend/.env).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    loaded = opsenv.load_env(args.env_file)
    if loaded:
        log.info("· configuration chargée depuis %s", loaded)

    inventory = load_inventory()
    if not inventory:
        log.error("✗ inventaire absent (%s) : lancer d'abord "
                  "ops/i18n_inventory.py.", INVENTORY_PATH)
        return 2

    dsn = args.dsn or _default_dsn()
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    from api import i18n
    from enrich import translate_ui

    with conn:
        if args.lang:
            if args.lang in i18n.SOURCE_LANGS:
                log.error("✗ « %s » est portée par le code/seed (fr/en/es) : rien "
                          "à générer.", args.lang)
                return 2
            note = register_note_for(conn, args.lang)
            if note is None and conn.execute(
                    "SELECT 1 FROM languages WHERE code = %s", (args.lang,)
                    ).fetchone() is None:
                log.error("✗ langue « %s » absente du registre (table languages).",
                          args.lang)
                return 2
            targets = [{"code": args.lang, "register_note": note}]
        else:
            targets = target_languages(conn)

        if not targets:
            log.info("Aucune langue cible (le registre ne contient que fr/en/es).")
            return 0

        translator = None
        if not args.dry_run:
            translator = _build_translator()
            if translator is None:
                log.error("✗ ANTHROPIC_API_KEY absente : impossible de générer "
                          "(utiliser --dry-run pour le plan sans appel API).")
                return 1

        total_written = total_cost = 0.0
        for t in targets:
            lang = t["code"]
            existing = None
            if args.dry_run:
                from api import repo
                existing = repo.ui_translations(conn, lang)
                labels = translate_ui.select_labels(
                    inventory, existing, overwrite=args.overwrite)
                if args.limit is not None:
                    labels = dict(list(labels.items())[:args.limit])
                log.info("[dry-run] %s : %d libellé(s) à générer "
                         "(%d déjà en base%s).", lang, len(labels), len(existing),
                         ", --overwrite ignoré ici" if args.overwrite else "")
                continue
            log.info("→ %s : génération en cours…", lang)
            report = generate_lang(
                conn, lang, inventory, translator,
                register_note=t["register_note"], overwrite=args.overwrite,
                batch_size=args.batch_size, limit=args.limit)
            log.info("  ✓ %s : %d/%d libellé(s) écrit(s) en %d lot(s) — %.4f ct.",
                     lang, report["written"], report["candidates"],
                     report["batches"], report["cost_cts"])
            total_written += report["written"]
            total_cost += report["cost_cts"]

        if not args.dry_run:
            log.info("Terminé : %d libellé(s) généré(s), %.4f ct au total.",
                     int(total_written), round(total_cost, 4))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
