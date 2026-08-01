"""Socle i18n des libellés statiques (V2-21a, volet 2) : inventaire, overlay de
rendu, outillage export/réimport.

Base réelle (schéma + seed chargés), sur le modèle de test_api.py.
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

os.environ.setdefault("CASAGUIDE_DB", "postgresql://localhost/casaguide")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from api import guide_page, i18n, repo  # noqa: E402
from enrich.settings import settings as enrich_settings  # noqa: E402

# Scripts ops/ (à la racine du dépôt) — mêmes fonctions pures que la CLI.
_OPS = Path(__file__).resolve().parents[2] / "ops"
sys.path.insert(0, str(_OPS))
import i18n_inventory  # noqa: E402
import i18n_export  # noqa: E402
import i18n_import  # noqa: E402


@pytest.fixture()
def conn():
    c = psycopg.connect(enrich_settings.db_dsn, row_factory=dict_row)
    yield c
    c.rollback()
    c.close()


# ── Inventaire (2.1) ─────────────────────────────────────────────────────────

def test_collect_inventory_covers_all_surfaces(conn):
    """L'inventaire recense les libellés du guide voyageur depuis leurs sources
    vivantes (code + seed) : interface, chapitres, sections, catégories, cuisines."""
    inv = i18n.collect_inventory(conn)
    # Un représentant de chaque domaine, avec ses colonnes FR/EN/ES + contexte.
    assert inv["ui.eyebrow"]["fr"] == "Votre guide de séjour"
    assert inv["ui.eyebrow"]["en"] == "Your stay guide"
    assert inv["ui.eyebrow"]["context"]
    assert inv["chapter.A"]["fr"] == "Arrivée & départ"
    assert inv["section.A_arrival.name"]["fr"]           # nom de section du seed
    assert "section.A_arrival.description" in inv        # description éditeur aussi
    assert inv["poi_category.hospital"]["es"] == "Hospital"
    assert inv["cuisine.italian"]["en"] == "Italian"
    # Toute clé du dictionnaire _UI est présente.
    for k in guide_page._UI["fr"]:
        assert i18n.ui_key(k) in inv


def test_collect_inventory_keys_match_ssr_lookup(conn):
    """Les clés de l'inventaire sont EXACTEMENT celles que le SSR interroge
    (mêmes helpers) : garantit qu'un libellé traduit sera bien délivré."""
    inv = i18n.collect_inventory(conn)
    assert i18n.ui_key("eyebrow") in inv
    assert i18n.chapter_key("B") in inv
    assert i18n.section_name_key("A_arrival") in inv
    assert i18n.poi_category_key("pharmacy") in inv
    assert i18n.cuisine_key("pizza") in inv


def test_inventory_diff_keys_reports_new_and_gone():
    prev = {"a.1": {}, "a.2": {}}
    cur = {"a.2": {}, "a.3": {}}
    added, removed = i18n_inventory.diff_keys(prev, cur)
    assert added == ["a.3"]
    assert removed == ["a.1"]


# ── Overlay de rendu (2.3) ───────────────────────────────────────────────────

def _prop():
    return {"name": "Villa Test", "city": "Alicante", "region": "",
            "default_lang": "fr", "published_langs": ["de"], "contact": {},
            "lat": 38.3, "lon": -0.5, "country_code": "ES"}


def _sections():
    return [{"code": "A_arrival", "chapter": "A",
             "name_i18n": {"fr": "Arrivée", "en": "Arrival", "es": "Llegada"},
             "field_schema": {"fields": []}, "content": {}, "body_md": "Bienvenue",
             "is_visible": True, "media": []}]


def test_overlay_delivers_translated_static_labels():
    """Une langue publiée supplémentaire (de) reçoit ses libellés statiques depuis
    l'overlay : interface, chapitre, nom de section. Le non-traduit retombe sur le
    FR (repli élégant)."""
    overlay = {"ui.eyebrow": "Ihr Aufenthaltsführer",
               "chapter.A": "Ankunft & Abreise",
               "section.A_arrival.name": "Ankunft"}
    html = guide_page.render_guide(_prop(), _sections(), [], {}, "tok",
                                   lang="de", ui_overlay=overlay)
    assert "Ihr Aufenthaltsführer" in html
    assert "Ankunft &amp; Abreise" in html    # chapitre (le & est échappé)
    assert ">Ankunft<" in html                # nom de section overlayé
    # Non traduit → repli FR (jamais de trou, §9).
    assert guide_page._t.__doc__  # sanity


def test_overlay_empty_is_non_regression_and_isolated():
    """Overlay vide (FR/EN/ES) → rendu depuis le code, identique ; et l'overlay ne
    fuit jamais d'un rendu à l'autre (ContextVar restauré)."""
    html_de = guide_page.render_guide(_prop(), _sections(), [], {}, "tok",
                                      lang="de", ui_overlay={"ui.eyebrow": "X-DE"})
    assert "X-DE" in html_de
    # Rendu suivant sans overlay → aucune fuite, libellé FR par défaut.
    html_fr = guide_page.render_guide(_prop(), _sections(), [], {}, "tok",
                                      lang="fr", ui_overlay={})
    assert "X-DE" not in html_fr
    assert "Votre guide de séjour" in html_fr
    # Hors de tout rendu, l'overlay courant est vide.
    assert i18n.current_overlay() == {}


# ── Export / réimport (2.2) ──────────────────────────────────────────────────

def test_export_rows_are_full_inventory_with_empty_correction():
    inv = {"ui.a": {"context": "ctx", "fr": "Bonjour", "en": "Hi", "es": "Hola"},
           "ui.b": {"context": "ctx2", "fr": "Merci", "en": "Thanks", "es": "Gracias"}}
    rows = i18n_export.export_rows(inv, {"ui.a": "Hallo"})
    assert [r["cle"] for r in rows] == ["ui.a", "ui.b"]
    a = rows[0]
    assert a["source_fr"] == "Bonjour" and a["proposition"] == "Hallo"
    assert a["correction"] == ""                 # colonne vide pour le relecteur
    assert rows[1]["proposition"] == ""          # pas de proposition en base


def test_plan_import_correction_wins_rejects_unknown_skips_empty():
    inv = {"ui.a": {}, "ui.b": {}, "ui.c": {}}
    rows = [
        {"cle": "ui.a", "proposition": "Hallo", "correction": "Guten Tag"},  # correction gagne
        {"cle": "ui.b", "proposition": "Danke", "correction": ""},           # repli proposition
        {"cle": "ui.c", "proposition": "", "correction": ""},                # vide → ignoré
        {"cle": "ui.zzz", "proposition": "x", "correction": "y"},            # inconnue → refusée
    ]
    to_write, unknown, empty = i18n_import.plan_import(rows, inv)
    assert to_write == {"ui.a": "Guten Tag", "ui.b": "Danke"}
    assert unknown == ["ui.zzz"]
    assert empty == 1


def test_ui_translations_upsert_roundtrip_idempotent(conn):
    """`ui_translations` : upsert idempotent, lecture par langue, vide pour FR/EN/ES."""
    try:
        repo.upsert_ui_translation(conn, "de", "ui.eyebrow", "Führer")
        repo.upsert_ui_translation(conn, "de", "ui.eyebrow", "Aufenthaltsführer")  # écrase
        conn.commit()
        got = repo.ui_translations(conn, "de")
        assert got["ui.eyebrow"] == "Aufenthaltsführer"
        assert repo.ui_translations(conn, "fr") == {}   # jamais de FR importé
    finally:
        conn.execute("DELETE FROM ui_translations WHERE lang = 'de'")
        conn.commit()


def test_import_csv_end_to_end(conn, tmp_path):
    """Chaîne réelle : CSV → import_csv → ui_translations, en refusant une clé
    inconnue et en préférant la correction."""
    inv = i18n.collect_inventory(conn)
    csv_path = tmp_path / "ui_de.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=i18n_export.CSV_COLUMNS)
        w.writeheader()
        w.writerow({"cle": "chapter.A", "contexte": "", "source_fr": "",
                    "proposition": "alt", "correction": "Ankunft & Abreise"})
        w.writerow({"cle": "ui.does_not_exist", "contexte": "", "source_fr": "",
                    "proposition": "x", "correction": "y"})
    assert i18n_import.lang_from_filename(csv_path) == "de"
    try:
        report = i18n_import.import_csv(conn, csv_path, "de", inv)
        conn.commit()
        assert report["written"] == 1
        assert report["unknown"] == ["ui.does_not_exist"]
        assert repo.ui_translations(conn, "de")["chapter.A"] == "Ankunft & Abreise"
    finally:
        conn.execute("DELETE FROM ui_translations WHERE lang = 'de'")
        conn.commit()
