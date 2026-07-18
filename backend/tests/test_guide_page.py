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
        "name": "Villa Test", "address_line1": "Calle Ejemplo 1",
        "address_line2": None, "postal_code": "03189",
        "city": "Orihuela Costa", "region": "Alicante",
        "country_code": "ES", "lat": 37.9261992, "lon": -0.7233174,
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
    # Chapitre H (Transports) comme au seed : les POI transport relèvent de
    # l'espace « Autour de vous » (V2-09), même s'ils sont rendus en blocs de
    # trajet dans A_arrival.
    return {"id": name, "category_code": "airport", "chapter": "H",
            "category_name": {"fr": "Aéroports"}, "map_color": "#546E7A",
            "name": name, "lat": lat, "lon": lon, "cuisine": None,
            "walk_min": None, "dist_walk_m": None, "drive_min": drive,
            "owner_comment": None, "description_md": None, "opening_hours": None,
            "phone": None, "website": None}


def test_arrival_renders_nav_banner_and_planning_blocks_with_correct_urls():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA,
                         content={"from_airport": "Prenez la N-332 vers le sud."},
                         name={"fr": "Venir depuis l'aéroport"})]
    html = guide_page.render_guide(_prop(lat=37.928, lon=-0.748),
                                   sections, [_airport()], {}, "tok")

    # M-20 : bandeau de navigation universelle en tête, destination seule.
    assert '<div class="nav-banner"' in html
    assert "Me guider vers le logement" in html
    assert ("https://www.google.com/maps/dir/?api=1&destination=37.928,-0.748"
            in html)                                     # Google Maps destination seule
    assert "https://waze.com/ul?ll=37.928,-0.748&navigate=yes" in html   # Waze du bandeau

    # Waze n'apparaît QU'UNE seule fois dans toute la page (le bandeau).
    assert html.count("waze.com/ul") == 1

    # Bloc de PLANIFICATION présent, avec durée en voiture et un bouton « Voir l'itinéraire ».
    assert '<div class="transport"' in html and '<div class="trip">' in html
    assert "Aéroport d'Alicante" in html
    assert "35 min en voiture" in html
    assert ">Voir l&#x27;itinéraire<" in html   # apostrophe échappée par html.escape
    # Google Maps du bloc : origine = aéroport, destination = logement (planification).
    assert ("https://www.google.com/maps/dir/?api=1&origin=38.2822,-0.5581"
            "&destination=37.928,-0.748") in html
    # Le bloc ne contient PLUS de bouton Waze (retiré, redondant avec le bandeau).
    assert 'class="trip-btn waze"' not in html

    # Ordre : bandeau → adresse/GPS → blocs → texte libre du propriétaire.
    assert (html.index('<div class="nav-banner"')
            < html.index('<div class="transport"')
            < html.index("Prenez la N-332"))

    # Le texte libre du propriétaire reste affiché, EN COMPLÉMENT (après le bloc).
    assert "Prenez la N-332 vers le sud." in html

    # L'aéroport n'est PAS aussi rendu en carte POI ordinaire (pas de doublon).
    assert 'class="poi-card"' not in html
    assert "Aéroports ·" not in html   # pas de titre de catégorie POI


def test_nav_banner_and_route_labels_localised_es():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [_airport()], {}, "tok",
                                   lang="es")
    assert "35 min en coche" in html                    # durée localisée
    assert "Llévame al alojamiento" in html             # bandeau localisé
    assert ">Ver ruta<" in html                         # bouton de bloc localisé
    assert 'aria-label="Cómo llegar al alojamiento"' in html


def test_train_station_also_gets_planning_block():
    station = _airport("Gare de Torrevieja")
    station["category_code"] = "train_station"
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [station], {}, "tok")
    assert "Gare de Torrevieja" in html and '<div class="trip">' in html


def test_bus_station_gets_planning_block_like_airport():
    """M-21 : la gare routière (bus_station) rejoint aéroport/gare dans les blocs
    de planification M-14/M-20 (durée voiture + « Voir l'itinéraire »)."""
    station = _airport("Gare routière de Torrevieja", lat=37.978, lon=-0.682, drive=22)
    station["category_code"] = "bus_station"
    station["category_name"] = {"fr": "Gares routières"}
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(lat=37.928, lon=-0.748),
                                   sections, [station], {}, "tok")
    assert '<div class="trip">' in html and "Gare routière de Torrevieja" in html
    assert "22 min en voiture" in html
    assert ">Voir l&#x27;itinéraire<" in html
    assert ("https://www.google.com/maps/dir/?api=1&origin=37.978,-0.682"
            "&destination=37.928,-0.748") in html
    # Rendu en bloc de planification, PAS en carte POI ordinaire (pas de doublon).
    assert 'class="poi-card"' not in html


def test_bus_stop_stays_ordinary_poi_card_in_transit_chapter():
    """M-21 : les arrêts bus_stop NE sont PAS des trajets de planification —
    ils remontent en cartes POI ordinaires dans le chapitre Transports (H)."""
    stop = _airport("Arrêt Avenida", lat=37.930, lon=-0.750, drive=None)
    stop["category_code"] = "bus_stop"
    stop["chapter"] = "H"
    stop["category_name"] = {"fr": "Arrêts de bus"}
    stop["walk_min"] = 4
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [stop], {}, "tok")
    assert 'class="poi-card"' in html and "Arrêt Avenida" in html
    # Jamais rendu comme un bloc de trajet.
    assert '<div class="trip">' not in html


def test_transport_falls_back_to_poi_card_when_arrival_section_hidden():
    """Sans section hôte visible, les aéroports restent des cartes POI (repli :
    jamais de perte d'information)."""
    # Aucune section A_arrival dans les sections visibles.
    sections = [_section("A_checkin", "A", {"fields": []})]
    html = guide_page.render_guide(_prop(), sections, [_airport()], {}, "tok")
    assert '<div class="trip">' not in html
    assert '<div class="nav-banner"' not in html   # pas de section hôte → pas de bandeau
    assert 'class="poi-card"' in html          # rendu en carte POI ordinaire
    assert "Aéroports ·" in html               # avec son titre de catégorie


# ── M-19 : adresse & GPS copiables dans A_arrival ────────────────────────────

def test_arrival_shows_copyable_address_and_gps():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA,
                         content={"from_airport": "Prenez la N-332."})]
    prop = _prop(lat=37.9261992, lon=-0.7233174, address_line1="Calle Ejemplo 1",
                 postal_code="03189", city="Orihuela Costa")
    html = guide_page.render_guide(prop, sections, [_airport()], AREA_FACTS, "tok")

    # Bloc adresse/GPS présent, avec libellés localisés FR.
    assert '<div class="arrival-meta">' in html
    assert "Adresse" in html and "Coordonnées GPS" in html
    # Adresse complète (voie + code postal + ville) copiable.
    assert 'data-copy="Calle Ejemplo 1, 03189 Orihuela Costa"' in html
    # GPS à 6 décimales, format « lat, lon ».
    assert 'data-copy="37.926199, -0.723317"' in html
    assert "37.926199, -0.723317" in html
    # Boutons Copier avec libellé de confirmation localisé.
    assert 'class="copy-btn"' in html and 'data-copied="Copié ✓"' in html
    assert ">Copier<" in html

    # Ordre M-20 : bandeau de navigation → adresse/GPS → blocs de planification.
    assert (html.index('<div class="nav-banner"')
            < html.index('<div class="arrival-meta">')
            < html.index('<div class="transport"'))


def test_arrival_copy_labels_localised_es():
    sections = [_section("A_arrival", "A", _ARRIVAL_SCHEMA)]
    html = guide_page.render_guide(_prop(), sections, [], AREA_FACTS, "tok", lang="es")
    assert "Dirección" in html and "Coordenadas GPS" in html
    assert ">Copiar<" in html and 'data-copied="Copiado ✓"' in html


def test_gps_uses_adjusted_position_six_decimals():
    """Le GPS reflète la position (lat/lon du logement) à 6 décimales exactement."""
    assert guide_page._gps_string(37.9261992, -0.7233174) == "37.926199, -0.723317"
    assert guide_page._gps_string(38, -0.5) == "38.000000, -0.500000"


def test_arrival_meta_absent_without_arrival_section():
    """Pas de section d'arrivée visible → pas de bloc adresse/GPS."""
    sections = [_section("C_trash", "C", {"area_facts": ["waste_rules"]})]
    html = guide_page.render_guide(_prop(), sections, [], AREA_FACTS, "tok")
    assert '<div class="arrival-meta">' not in html


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


# ── V2-09 : trois espaces à onglets ──────────────────────────────────────────

def _panel(html, key):
    """Extrait le HTML du panneau d'onglet `key` (home|emergency|around)."""
    start = html.index(f'id="tab-{key}"')
    rest = html[start:]
    nxt = rest.find('<section class="tab-panel', 1)
    return rest if nxt == -1 else rest[:nxt]


def _poi(name, cat, ch, walk=5, comment=None):
    return {"id": name, "name": name, "category_code": cat, "chapter": ch,
            "category_name": {"fr": cat}, "map_color": "#0E5A73",
            "lat": 37.9, "lon": -0.74, "walk_min": walk, "dist_walk_m": walk * 70,
            "drive_min": None, "owner_comment": comment, "description_md": None,
            "opening_hours": None, "phone": None, "website": None, "cuisine": None}


def test_three_tabs_present_and_labelled():
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok")
    assert '<nav class="guide-tabs"' in html
    for key in ("home", "emergency", "around"):
        assert f'data-tab="{key}"' in html and f'id="tab-{key}"' in html
    assert "Le logement" in html and "Urgences" in html and "Autour de vous" in html
    # L'onglet « Le logement » est actif par défaut (SSR).
    assert 'class="tab-panel tab-active" data-tab="home"' in html


def test_tab_labels_localised_es():
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok", lang="es")
    assert "El alojamiento" in html and "Emergencias" in html and "A tu alrededor" in html


def test_chapters_and_pois_distributed_across_the_three_tabs():
    sections = [
        _section("A_checkin", "A", {"fields": []}, body_md="Arrivée 16h"),
        _section("B_house_rules", "B", {"fields": []}, body_md="Règles"),
        _section("C_trash", "C", {"fields": []}, body_md="Tri des déchets"),
        _section("D_safety", "D", {"fields": []}, body_md="Consignes de sécurité"),
        _section("I_license", "I", {"fields": []}, body_md="Licence"),
        _section("F_restaurants", "F", {"fields": []}, body_md="Nos restos"),
    ]
    pois = [_poi("Mercadona", "supermarket", "C"),     # commerce C → around
            _poi("Farmacia Sol", "pharmacy", "D"),     # santé D → urgences
            _poi("La Marejada", "restaurant", "F")]    # resto F → around
    html = guide_page.render_guide(_prop(lat=37.9, lon=-0.74), sections, pois, {}, "tok")

    home = _panel(html, "home")
    emergency = _panel(html, "emergency")
    around = _panel(html, "around")

    # « Le logement » : sections A, B, C, I (mais PAS les commerces de C).
    assert "Arrivée 16h" in home and "Règles" in home
    assert "Tri des déchets" in home and "Licence" in home
    assert "Mercadona" not in home
    # « Urgences » : sections + santé du chapitre D.
    assert "Consignes de sécurité" in emergency and "Farmacia Sol" in emergency
    assert "Farmacia Sol" not in around and "Farmacia Sol" not in home
    # « Autour de vous » : sections E/F/G/H + commerces de C + carte.
    assert "Nos restos" in around and "La Marejada" in around
    assert "Mercadona" in around
    assert '<div id="map"></div>' in around
    # Les commerces de C ne fuient pas dans « Le logement ».
    assert "La Marejada" not in home


def test_emergency_tab_has_big_sos_and_numbers_block():
    facts = {"emergency_numbers": {"items": [
        {"number": "112", "label": "Urgences"}, {"number": "062", "label": "Guardia Civil"}]}}
    html = guide_page.render_guide(_prop(), [_section("D_safety", "D", {"fields": []})],
                                   [], facts, "tok")
    emergency = _panel(html, "emergency")
    assert 'class="sos sos-lg"' in emergency          # barre d'urgences EN GRAND
    assert "Tous les numéros utiles" in emergency      # bloc complet des numéros
    # La barre compacte reste dans l'en-tête, en tête des trois onglets.
    header = html[:html.index('<nav class="guide-tabs"')]
    assert '<div class="sos">' in header


def test_pois_chapter_order_respected_within_around():
    """L'ordre du seed (E→F→G→H) est conservé dans l'espace « Autour »."""
    pois = [_poi("Playa", "beach", "G"), _poi("Taxi Sur", "taxi", "E"),
            _poi("Bar Pepe", "bar", "F")]
    html = guide_page.render_guide(_prop(), [], pois, {}, "tok")
    around = _panel(html, "around")
    assert around.index("Taxi Sur") < around.index("Bar Pepe") < around.index("Playa")


# ── V2-09 : listes de lieux repliées (4 + « Voir les N autres ») ─────────────

def test_collapse_more_button_exact_count_and_all_cards_ssr():
    pois = [_poi(f"Super {i}", "supermarket", "C", walk=i + 1) for i in range(6)]
    html = guide_page._render_pois(pois, "fr")
    # Le HTML contient TOUTES les cartes (SSR : repli purement client).
    for i in range(6):
        assert f"Super {i}" in html
    # Bouton « Voir les N autres » avec le compte exact (6 - 4 = 2) + gabarit/less
    # pour le calcul dynamique côté client.
    assert 'class="more-btn"' in html
    assert 'data-more-tpl="Voir les {n} autres"' in html
    assert 'data-less="Réduire"' in html
    assert "Voir les 2 autres" in html


def test_collapse_absent_when_four_or_fewer():
    pois = [_poi(f"Super {i}", "supermarket", "C", walk=i + 1) for i in range(4)]
    html = guide_page._render_pois(pois, "fr")
    assert 'class="more-btn"' not in html      # ≤ 4 : affichée telle quelle


def test_collapse_button_localised_es():
    pois = [_poi(f"Bar {i}", "bar", "F", walk=i + 1) for i in range(7)]
    html = guide_page._render_pois(pois, "es")
    assert "Ver 3 más" in html                 # 7 - 4
    assert 'data-less="Reducir"' in html


def test_collapse_favourites_still_lead_before_truncation():
    """Le coup de cœur (loin) reste en tête → visible parmi les 4 premières."""
    pois = ([_poi(f"Proche {i}", "supermarket", "C", walk=i + 1) for i in range(5)]
            + [_poi("Coup de cœur", "supermarket", "C", walk=99, comment="Le meilleur !")])
    html = guide_page._render_pois(pois, "fr")
    # ❤ d'abord dans l'ordre SSR, donc avant les 4 premières cartes de distance.
    assert html.index("Coup de cœur") < html.index("Proche 0")
    assert "❤ Le meilleur !" in html


# ── V2-09 (cohérences) : ancres de section → bon onglet, sélecteur de langue ──

def test_section_anchors_live_in_their_owning_tab_panel():
    """Chaque section porte un id=<code> logé DANS le panneau de son onglet :
    une ancre profonde #<code> mène donc au bon onglet (résolu côté client)."""
    sections = [_section("B_house_rules", "B", {"fields": []}, body_md="Règles"),
                _section("C_trash", "C", {"fields": []}, body_md="Tri"),
                _section("D_safety", "D", {"fields": []}, body_md="Sécurité")]
    html = guide_page.render_guide(_prop(), sections, [], {}, "tok")
    assert 'id="B_house_rules"' in _panel(html, "home")
    assert 'id="C_trash"' in _panel(html, "home")
    assert 'id="D_safety"' in _panel(html, "emergency")


def test_language_selector_rendered_for_hash_preserving_switch():
    """Le sélecteur de langue est rendu (liens ?lang=xx que le client complète du
    hash courant) → l'onglet actif survit au changement de langue (une seule page)."""
    prop = _prop(default_lang="fr", published_langs=["es"])
    html = guide_page.render_guide(prop, [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok")
    assert 'class="langs"' in html and 'data-lang="es"' in html


def test_single_page_no_new_routes_hashes_are_fixed():
    """Les onglets sont de simples ancres FIXES (#logement/#urgences/#autour) sur
    la même page : aucune route serveur nouvelle."""
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok")
    # Les panneaux portent les data-tab attendus ; le mapping hash est stable.
    assert guide_page._TAB_HASH == {"home": "logement", "emergency": "urgences",
                                    "around": "autour"}
    for key in ("home", "emergency", "around"):
        assert f'id="tab-{key}"' in html
