"""Tests unitaires du rendu HTML du guide voyageur (`api/guide_page.py`).

Fonctions pures, sans base ni réseau : on éprouve directement le rendu à partir
de dictionnaires de test. Couvre M-17 (chaque area_fact à sa place) et M-14
(blocs d'itinéraire dans la section d'arrivée).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import guide_page  # noqa: E402
from enrich import claude_enrich  # noqa: E402


# ── Données de test partagées ────────────────────────────────────────────────

def _prop(**over):
    base = {
        "name": "Villa Test", "city": "Orihuela Costa", "region": "Alicante",
        "country_code": "ES", "lat": 37.928, "lon": -0.748,
        "default_lang": "fr", "published_langs": [], "tourism_license": None,
        "contact": {},
    }
    base.update(over)
    return base


def _section(code, chapter, schema, content=None, body_md=None, name=None):
    return {"code": code, "chapter": chapter, "name_i18n": name or {"fr": code},
            "field_schema": schema, "content": content or {}, "body_md": body_md,
            "media": []}


AREA_FACTS = {
    "waste_rules": {"summary": "Sortie des ordures le soir.",
                    "containers": [{"color_or_type": "jaune", "accepts": "emballages"},
                                   {"color_or_type": "vert", "accepts": "verre"}]},
    "noise_rules": {"summary": "Silence la nuit.", "quiet_hours": "23h00-08h00"},
    "emergency_numbers": {"items": [{"label": "Urgences (UE)", "number": "112"},
                                    {"label": "Guardia Civil", "number": "062"}],
                          "notes": "Le 112 fonctionne partout."},
}


# ── M-17 : chaque area_fact rendu DANS sa section ────────────────────────────

def test_area_facts_render_inside_declaring_section_not_in_final_block():
    sections = [
        _section("B_house_rules", "B",
                 {"fields": [{"key": "smoking", "type": "bool",
                              "label": {"fr": "Fumeurs"}}],
                  "area_facts": ["noise_rules"]},
                 content={"smoking": False}),
        _section("C_trash", "C",
                 {"fields": [{"key": "container_location", "type": "textarea",
                              "label": {"fr": "Emplacement"}}],
                  "area_facts": ["waste_rules"]},
                 content={"container_location": "Au bout de la rue"}),
    ]
    html = guide_page.render_guide(_prop(), sections, [], AREA_FACTS, "tok")

    # Le bloc de fin de guide « Bon à savoir sur place » existe et contient les
    # numéros utiles.
    marker = f'<h2>{guide_page._t("fr", "good_to_know")}</h2>'
    assert marker in html
    final = html[html.index(marker):]
    assert "112" in final and "Guardia Civil" in final

    # waste_rules et noise_rules sont rendus AVANT le bloc final (donc dans leur
    # section) et ABSENTS du bloc final (M-17).
    assert "Sortie des ordures le soir." in html
    assert html.index("Sortie des ordures le soir.") < html.index(marker)
    assert "Sortie des ordures le soir." not in final
    assert "Silence la nuit." in html
    assert html.index("Silence la nuit.") < html.index(marker)
    assert "Silence la nuit." not in final

    # L'encart sobre est bien présent, et les couleurs de conteneurs aussi.
    assert '<div class="sec-facts">' in html
    assert "jaune" in html and "emballages" in html

    # Le bloc final ne contient QUE les numéros (aucun encart tri/bruit).
    assert guide_page._t("fr", "waste") not in final
    assert guide_page._t("fr", "noise") not in final


def test_waste_fact_lands_in_trash_section_and_noise_in_rules_section():
    """Chaque fait tombe précisément dans la section qui le déclare."""
    sections = [
        _section("B_house_rules", "B", {"area_facts": ["noise_rules"]}),
        _section("C_trash", "C", {"area_facts": ["waste_rules"]}),
    ]
    html = guide_page.render_guide(_prop(), sections, [], AREA_FACTS, "tok")

    def chapter_slice(ch):
        start = html.index(f'<section class="chapter" data-chapter="{ch}"')
        rest = html[start + len('<section class="chapter"'):]
        nxt = rest.find('<section class="chapter"')
        return rest[:nxt] if nxt != -1 else rest

    b_slice = chapter_slice("B")
    c_slice = chapter_slice("C")
    assert "Silence la nuit." in b_slice and "Sortie des ordures le soir." not in b_slice
    assert "Sortie des ordures le soir." in c_slice and "Silence la nuit." not in c_slice


def test_final_block_absent_when_no_emergency_numbers():
    """Sans numéros, plus aucun bloc de fin de guide (les autres faits sont dans
    leur section)."""
    sections = [_section("C_trash", "C", {"area_facts": ["waste_rules"]})]
    facts = {"waste_rules": AREA_FACTS["waste_rules"]}
    html = guide_page.render_guide(_prop(), sections, [], facts, "tok")
    assert f'<h2>{guide_page._t("fr", "good_to_know")}</h2>' not in html
    assert "Sortie des ordures le soir." in html  # toujours rendu dans C_trash


def test_section_without_area_facts_declares_nothing():
    """Une section qui ne déclare pas d'area_fact n'affiche aucun encart."""
    sections = [_section("C_trash", "C", {"fields": []})]  # pas de clé area_facts
    html = guide_page.render_guide(_prop(), sections, [], AREA_FACTS, "tok")
    assert '<div class="sec-facts">' not in html


# ── M-16 : filtre par cuisine + coups de cœur en tête ────────────────────────

def _resto(name, cuisine=None, walk=None, comment=None):
    return {"id": name, "category_code": "restaurant", "chapter": "F",
            "category_name": {"fr": "Restaurants"}, "map_color": "#EF6C00",
            "name": name, "lat": 37.9, "lon": -0.74, "cuisine": cuisine,
            "walk_min": walk, "dist_walk_m": (walk or 0) * 70,
            "drive_min": None, "owner_comment": comment,
            "description_md": None, "opening_hours": None,
            "phone": None, "website": None}


def test_cuisine_filter_chips_and_tags_localised():
    pois = [_resto("Trattoria", "italian", 5),
            _resto("El Puerto", "seafood", 8),
            _resto("Pizzeria Napoli", "pizza", 10)]
    html = guide_page._render_pois(pois, "fr")
    # Barre de filtre par cuisine présente, avec la puce « Tout ».
    assert '<div class="cuisines" data-cat="restaurant"' in html
    assert 'data-cuisine=""' in html  # puce Tout
    # Une puce par cuisine présente, libellés localisés (FR).
    assert 'data-cuisine="italian"' in html and "Italien" in html
    assert 'data-cuisine="seafood"' in html and "Fruits de mer" in html
    assert 'data-cuisine="pizza"' in html and "Pizza" in html
    # Chaque carte porte son attribut de filtrage + son étiquette.
    assert '<div class="poi-group" data-cat="restaurant">' in html
    assert '<div class="poi-card" data-cuisine="italian"' in html
    assert '<span class="cuisine-tag">' in html

    # Localisation ES : les libellés changent, les clés de filtrage non.
    html_es = guide_page._render_pois(pois, "es")
    assert "Marisco" in html_es and 'data-cuisine="seafood"' in html_es


def test_cuisine_chips_absent_when_less_than_two_cuisines():
    pois = [_resto("Trattoria", "italian", 5), _resto("Da Vinci", "italian", 8),
            _resto("Sin Datos", None, 3)]
    html = guide_page._render_pois(pois, "fr")
    assert '<div class="cuisines"' not in html   # une seule cuisine distincte


def test_unknown_cuisine_falls_back_to_raw_value():
    pois = [_resto("Fusion Bar", "peruvian", 5), _resto("Tapas", "spanish", 6)]
    html = guide_page._render_pois(pois, "fr")
    assert 'data-cuisine="peruvian"' in html
    assert "Peruvian" in html            # repli embelli (capitalisé)


def test_owner_favourites_lead_their_category():
    # Le coup de cœur est plus loin (walk=20) qu'un POI sans commentaire (walk=3),
    # mais doit tout de même remonter en tête de sa catégorie (M-16).
    pois = [_resto("Proche sans avis", None, 3),
            _resto("Coup de cœur", "italian", 20, comment="Notre préféré !"),
            _resto("Autre", "pizza", 8)]
    html = guide_page._render_pois(pois, "fr")
    assert html.index("Coup de cœur") < html.index("Proche sans avis")
    assert html.index("Coup de cœur") < html.index("Autre")
    assert "❤ Notre préféré !" in html


# ── M-14 : itinéraires « en un tap » dans A_arrival ──────────────────────────

_ARRIVAL_SCHEMA = {
    "fields": [{"key": "from_airport", "type": "textarea",
                "label": {"fr": "Depuis l'aéroport"}}],
    "poi_categories": ["airport", "train_station"],
}


def _airport(name="Aéroport d'Alicante", lat=38.2822, lon=-0.5581, drive=35):
    return {"id": name, "category_code": "airport", "chapter": "A",
            "category_name": {"fr": "Aéroports"}, "map_color": "#546E7A",
            "name": name, "lat": lat, "lon": lon, "cuisine": None,
            "walk_min": None, "dist_walk_m": None, "drive_min": drive,
            "owner_comment": None, "description_md": None, "opening_hours": None,
            "phone": None, "website": None}


def test_arrival_renders_itinerary_blocks_with_correct_urls():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA,
                         content={"from_airport": "Prenez la N-332 vers le sud."},
                         name={"fr": "Venir depuis l'aéroport"})]
    html = guide_page.render_guide(_prop(lat=37.928, lon=-0.748),
                                   sections, [_airport()], {}, "tok")

    # Bloc de trajet présent, avec durée en voiture.
    assert '<div class="transport"' in html and '<div class="trip">' in html
    assert "Aéroport d'Alicante" in html
    assert "35 min en voiture" in html

    # Google Maps : origine = aéroport, destination = logement (pré-rempli).
    assert ("https://www.google.com/maps/dir/?api=1&origin=38.2822,-0.5581"
            "&destination=37.928,-0.748") in html
    # Waze : navigation vers le logement.
    assert "https://waze.com/ul?ll=37.928,-0.748&navigate=yes" in html

    # Le texte libre du propriétaire reste affiché, EN COMPLÉMENT (après le bloc).
    assert "Prenez la N-332 vers le sud." in html
    assert html.index('<div class="transport"') < html.index("Prenez la N-332")

    # L'aéroport n'est PAS aussi rendu en carte POI ordinaire (pas de doublon).
    assert 'class="poi-card"' not in html
    assert "Aéroports ·" not in html   # pas de titre de catégorie POI


def test_itinerary_labels_localised_es():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [_airport()], {}, "tok",
                                   lang="es")
    assert "35 min en coche" in html          # durée localisée
    assert 'aria-label="Cómo llegar al alojamiento"' in html


def test_train_station_also_gets_itinerary_block():
    station = _airport("Gare de Torrevieja")
    station["category_code"] = "train_station"
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [station], {}, "tok")
    assert "Gare de Torrevieja" in html and '<div class="trip">' in html


def test_transport_falls_back_to_poi_card_when_arrival_section_hidden():
    """Sans section hôte visible, les aéroports restent des cartes POI (repli :
    jamais de perte d'information)."""
    # Aucune section A_arrival dans les sections visibles.
    sections = [_section("A_checkin", "A", {"fields": []})]
    html = guide_page.render_guide(_prop(), sections, [_airport()], {}, "tok")
    assert '<div class="trip">' not in html
    assert 'class="poi-card"' in html          # rendu en carte POI ordinaire
    assert "Aéroports ·" in html               # avec son titre de catégorie


# ── M-17 : le prompt de génération est resserré (on vérifie les CONSIGNES) ───

def test_area_prompt_forbids_administrative_context_and_generalities():
    prompt = claude_enrich._AREA_PROMPT
    low = prompt.lower()
    # L'exemple de généralité administrative interdit est explicitement cité.
    assert "la commune applique" in low
    # Interdictions clés présentes.
    assert "contexte administratif" in low
    assert "généralité" in low
    assert "essentiel actionnable" in low
    # On demande couleurs de conteneurs + ce qu'on y met, heures de silence.
    assert "couleur" in low and "on y met" in low
    assert "silence" in low
