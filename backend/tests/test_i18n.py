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
from enrich import translate_ui  # noqa: E402
from enrich.settings import settings as enrich_settings  # noqa: E402

# Scripts ops/ (à la racine du dépôt) — mêmes fonctions pures que la CLI.
_OPS = Path(__file__).resolve().parents[2] / "ops"
sys.path.insert(0, str(_OPS))
import i18n_inventory  # noqa: E402
import i18n_export  # noqa: E402
import i18n_import  # noqa: E402
import i18n_generate  # noqa: E402


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


# ── Libellés du field_schema : inventaire + overlay SSR (extension avant V3) ──

def test_inventory_covers_field_schema_labels(conn):
    """Les libellés portés par field_schema (labels de champs, options de select,
    labels de groupes répétables) sont recensés, avec les clés du SSR."""
    inv = i18n.collect_inventory(conn)
    # Champ simple, clé scopée par section.
    assert i18n.field_label_key("A_parking", "parking_type") in inv
    assert inv[i18n.field_label_key("A_parking", "parking_type")]["en"] == "Parking type"
    # Champ d'un groupe répétable (segment repeat).
    rk = i18n.repeat_field_label_key("B_appliances", "appliances", "name")
    assert inv[rk]["fr"] == "Équipement"
    # Valeur de select : couverte par _OPTION_LABELS (source préservée, pas écrasée).
    assert inv[i18n.option_key("private")]["fr"] == "Place privée"
    # Un même `key` dans deux sections ne collisionne pas (libellés distincts).
    assert (i18n.field_label_key("B_wifi", "notes") in inv
            and i18n.field_label_key("D_contact", "notes") in inv)
    assert (inv[i18n.field_label_key("B_wifi", "notes")]["fr"]
            != inv[i18n.field_label_key("D_contact", "notes")]["fr"])


def _prop_de():
    return {"name": "Villa", "city": "Alicante", "region": "", "default_lang": "fr",
            "published_langs": ["de"], "contact": {}, "lat": 38.3, "lon": -0.5,
            "country_code": "ES"}


def _parking_and_appliances_sections():
    return [
        {"code": "A_parking", "chapter": "A",
         "name_i18n": {"fr": "Parking", "en": "Parking", "es": "Aparcamiento"},
         "field_schema": {"fields": [
             {"key": "parking_type", "type": "select",
              "options": ["private", "street", "public"],
              "label": {"fr": "Type de stationnement", "en": "Parking type", "es": "Tipo"}}]},
         "content": {"parking_type": "private"}, "body_md": "", "is_visible": True,
         "media": []},
        {"code": "B_appliances", "chapter": "B",
         "name_i18n": {"fr": "Équipements", "en": "Appliances", "es": "Equipos"},
         "field_schema": {"repeat": {"key": "appliances", "fields": [
             {"key": "name", "type": "text",
              "label": {"fr": "Équipement", "en": "Appliance", "es": "Equipo"}}]}},
         "content": {"appliances": [{"name": "Machine à laver"}]},
         "body_md": "", "is_visible": True, "media": []},
    ]


def test_overlay_delivers_field_schema_labels():
    """Une langue supplémentaire reçoit ses libellés de champ, options de select et
    champs de groupe répétable depuis l'overlay (mêmes clés que l'inventaire)."""
    overlay = {
        i18n.field_label_key("A_parking", "parking_type"): "Parkplatztyp",
        i18n.option_key("private"): "Privater Stellplatz",
        i18n.repeat_field_label_key("B_appliances", "appliances", "name"): "Gerät",
    }
    html = guide_page.render_guide(_prop_de(), _parking_and_appliances_sections(),
                                   [], {}, "tok", lang="de", ui_overlay=overlay)
    assert "Parkplatztyp" in html           # libellé de champ
    assert "Privater Stellplatz" in html    # valeur de select
    assert "Gerät" in html                  # champ de groupe répétable


def test_field_labels_non_regression_without_overlay():
    """Overlay vide (FR/EN/ES) → libellés de champ rendus depuis le seed, inchangés."""
    html = guide_page.render_guide(_prop_de(), _parking_and_appliances_sections(),
                                   [], {}, "tok", lang="fr", ui_overlay={})
    assert "Type de stationnement" in html and "Place privée" in html
    assert "Équipement" in html
    assert "Parkplatztyp" not in html


# ── Volet 3 : génération des propositions ────────────────────────────────────

class _FakeAnthropic:
    """Client Claude simulé : renvoie une réponse JSON déterministe + usage."""

    def __init__(self, mapper):
        self._mapper = mapper
        self.messages = self
        self.prompts: list[str] = []

    def create(self, *, model, max_tokens, messages):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        # Rejoue le payload : traduit chaque valeur via `mapper`.
        import json as _json
        payload = _json.loads(prompt.split("libellé français) :\n", 1)[1])
        data = {k: self._mapper(v) for k, v in payload.items()}
        block = type("B", (), {"type": "text", "text": _json.dumps(data, ensure_ascii=False)})
        usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})
        return type("M", (), {"content": [block()], "usage": usage()})()


def test_select_labels_skips_existing_and_empty():
    inv = {"a.1": {"fr": "Bonjour"}, "a.2": {"fr": ""}, "a.3": {"fr": "Merci"}}
    # Par défaut : ignore FR vide (a.2) et la clé déjà présente (a.3).
    got = translate_ui.select_labels(inv, {"a.3": "existant"})
    assert got == {"a.1": "Bonjour"}
    # overwrite=True : régénère la clé présente, jamais la FR vide.
    got2 = translate_ui.select_labels(inv, {"a.3": "existant"}, overwrite=True)
    assert got2 == {"a.1": "Bonjour", "a.3": "Merci"}


def test_iter_batches_stable_and_bounded():
    labels = {f"k{i}": str(i) for i in range(5)}
    batches = list(translate_ui.iter_batches(labels, 2))
    assert [list(b) for b in batches] == [["k0", "k1"], ["k2", "k3"], ["k4"]]


def test_claude_translator_parses_and_costs_and_uses_register():
    """Le traducteur Claude passe le REGISTRE dans l'invite, ne garde que les clés
    demandées, et comptabilise un coût > 0."""
    client = _FakeAnthropic(lambda v: f"DE::{v}")
    tr = translate_ui.ClaudeUILabelTranslator(client)
    out, meta = tr.translate({"chapter.A": "Arrivée", "ui.all": "Tout"},
                             target_lang="de",
                             register_note="Vouvoiement (Sie).")
    assert out == {"chapter.A": "DE::Arrivée", "ui.all": "DE::Tout"}
    assert meta["units"] == 150 and meta["cost_cts"] > 0
    assert "Vouvoiement (Sie)." in client.prompts[0]
    assert "allemand" in client.prompts[0]        # nom de langue cible


def test_generate_lang_writes_proposals_and_accounts_cost(conn):
    """Chaîne réelle : generate_lang → ui_translations peuplée + coût comptabilisé
    (property_id/job_id NULL). Rejouable : re-run ne reclobbe pas (skip-existing)."""
    inv = i18n.collect_inventory(conn)
    client = _FakeAnthropic(lambda v: f"[de] {v}")
    tr = translate_ui.ClaudeUILabelTranslator(client)
    try:
        note = i18n_generate.register_note_for(conn, "de")
        rep = i18n_generate.generate_lang(conn, "de", inv, tr, register_note=note,
                                          batch_size=10, limit=15)
        assert rep["candidates"] == 15 and rep["written"] == 15
        assert rep["batches"] == 2                       # 15 clés / lot de 10
        got = repo.ui_translations(conn, "de")
        assert len(got) == 15
        assert got["chapter.A"] == "[de] Arrivée & départ"
        # Coût comptabilisé côté produit (pas de logement).
        n = conn.execute("SELECT count(*) c FROM api_costs WHERE operation="
                         "'ui_translate' AND property_id IS NULL").fetchone()["c"]
        assert n == rep["batches"]
        # Re-run : les 15 déjà présentes sont sautées → 15 AUTRES clés générées.
        rep2 = i18n_generate.generate_lang(conn, "de", inv, tr, register_note=note,
                                           batch_size=10, limit=15)
        assert rep2["written"] == 15
        new_keys = set(repo.ui_translations(conn, "de")) - set(got)
        assert len(new_keys) == 15 and new_keys.isdisjoint(set(got))
    finally:
        conn.execute("DELETE FROM ui_translations WHERE lang = 'de'")
        conn.execute("DELETE FROM api_costs WHERE operation = 'ui_translate'")
        conn.commit()


def test_target_languages_excludes_source_langs(conn):
    """Les cibles de génération sont les langues du registre NON portées par le
    code/seed (jamais fr/en/es)."""
    codes = [t["code"] for t in i18n_generate.target_languages(conn)]
    assert set(codes).isdisjoint(set(i18n.SOURCE_LANGS))
    assert "de" in codes and "nl" in codes           # langues supplémentaires du seed
