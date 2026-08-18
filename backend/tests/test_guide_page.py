"""Tests unitaires du rendu HTML du guide voyageur (`api/guide_page.py`).

Fonctions pures, sans base ni réseau : on éprouve directement le rendu à partir
de dictionnaires de test. Couvre M-17 (chaque area_fact à sa place) et M-14
(blocs d'itinéraire dans la section d'arrivée).
"""
from __future__ import annotations

import json
import re
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


# ── V2-23b §3.1 : bouton « Demander ce service » sur les sections requestable ─

def test_requestable_section_shows_request_button():
    """Une section « sur demande » (field_schema.request) porte le bouton +
    ses libellés localisés en data-* ; une section ordinaire n'en a pas."""
    reqable = _section("B_cleaning", "B",
                       {"request": {"label": "Ménage / draps supplémentaires"},
                        "fields": []}, content={})
    plain = _section("A_checkin", "A", {"fields": []})
    html = guide_page.render_guide(_prop(), [reqable, plain], [], {}, "tok")
    assert 'class="svc-request-btn"' in html
    assert "Demander ce service" in html
    assert 'data-section="B_cleaning"' in html
    # La section ordinaire ne propose aucun service.
    assert html.count("svc-request-btn") == 1


def test_request_button_localised_es():
    reqable = _section("E_services", "E",
                       {"request": {"label": "Service supplémentaire"}}, content={})
    html = guide_page.render_guide(_prop(), [reqable], [], {}, "tok", lang="es")
    assert "Solicitar este servicio" in html


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


# ── V2-27 : chemins de retour vers la grille de services ─────────────────────

def test_back_to_services_link_in_each_around_category_and_floating_button():
    pois = [_resto("Trattoria", "italian", 5), _resto("El Puerto", "seafood", 8)]
    html = guide_page.render_guide(_prop(), [], pois, {}, "tok")
    # La grille porte l'ancre de retour (id = « autour ») : cible du retour, natif
    # sans JS (défilement vers la grille), hash propre (#autour).
    assert 'class="svc-grid" id="autour"' in html
    # Chaque catégorie « Autour de vous » se termine par un retour aux services.
    assert 'class="back-services" href="#autour"' in html
    assert "Retour aux services" in html
    # Bouton flottant rendu (masqué par défaut, révélé par JS pendant le défilement).
    assert 'class="back-services back-float" href="#autour"' in html


def test_back_to_services_localised_es():
    pois = [_resto("Trattoria", "italian", 5), _resto("El Puerto", "seafood", 8)]
    html = guide_page.render_guide(_prop(), [], pois, {}, "tok", lang="es")
    assert "Volver a los servicios" in html
    assert "Retour aux services" not in html


def test_no_back_float_without_around_pois():
    # Sans POI en cartes autour, pas de grille → pas de bouton flottant.
    html = guide_page.render_guide(_prop(), [], [], {}, "tok")
    assert "back-float" not in html


def test_staff_page_has_back_to_top_when_sections_present():
    staff = [_section("S_checklist", "S",
                      {"fields": [{"key": "steps", "type": "textarea",
                                   "label": {"fr": "Étapes"}}]},
                      content={"steps": "Aérer, nettoyer, vérifier."})]
    html = guide_page.render_staff(_prop(), staff, "stok")
    assert 'class="back-services" href="#content"' in html
    assert "Haut de page" in html


# ── V2-23b §2 : planning du cahier d'équipe (la FENÊTRE, pas le séjour) ───────

import datetime as _dt  # noqa: E402

from api import care  # noqa: E402

_DEF_IN, _DEF_OUT = _dt.time(15, 0), _dt.time(10, 0)
_TODAY = _dt.date(2026, 8, 1)


def _pbk(**over):
    b = {"id": "b1", "starts_on": _dt.date(2026, 8, 10),
         "ends_on": _dt.date(2026, 8, 24), "nature": "reservation",
         "status": "active", "linked_booking_id": None, "guest_count": 6,
         "children_ages": [], "guest_name": "Famille Martin",
         "guest_contact": "+34 600 12 34 56", "checkin_time": None,
         "checkout_time": None, "luggage_drop_time": None}
    b.update(over)
    return b


def _staff_html(bookings, care_rules=None):
    planning = care.build_planning(bookings, care_rules or {}, _DEF_IN, _DEF_OUT,
                                   today=_TODAY)
    return guide_page.render_staff(_prop(), [], "stok", planning=planning)


def test_staff_planning_absent_when_planning_none():
    """Rétrocompat : sans planning, aucune frise n'est rendue (ancien appel)."""
    html = guide_page.render_staff(_prop(), [], "stok")
    assert "prep-block" not in html


def test_staff_planning_renders_window_with_tasks():
    prev = _pbk(id="a", starts_on=_dt.date(2026, 8, 5), ends_on=_dt.date(2026, 8, 10))
    nxt = _pbk(id="b", starts_on=_dt.date(2026, 8, 10), ends_on=_dt.date(2026, 8, 17))
    html = _staff_html([prev, nxt])
    assert "Préparations à venir" in html
    assert "Libre depuis" in html
    assert "fenêtre 5 h" in html
    assert "À préparer" in html
    assert "Welcome pack pour 6" in html


def test_staff_planning_luggage_shows_two_deadlines():
    prev = _pbk(id="a", starts_on=_dt.date(2026, 8, 5), ends_on=_dt.date(2026, 8, 10))
    nxt = _pbk(id="b", starts_on=_dt.date(2026, 8, 10), ends_on=_dt.date(2026, 8, 17),
               luggage_drop_time=_dt.time(11, 30))
    html = _staff_html([prev, nxt])
    assert "Dépôt de bagages annoncé à" in html
    assert "11:30" in html


def test_staff_planning_midstay_shows_contact():
    html = _staff_html([_pbk()])                       # 14 nuits → draps J+8
    assert "Changement de draps" in html
    assert "maison habitée" in html
    assert "+34 600 12 34 56" in html                  # coordonnées (séjour à venir)


def test_staff_planning_midstay_renders_call_whatsapp_mailto_and_lang():
    """§3.0 — le téléphone est une ACTION (tel:/WhatsApp), l'email en est une
    autre (mailto:) ; la langue aide l'équipe à aborder le locataire."""
    html = _staff_html([_pbk(guest_contact=None, guest_phone="+34 600 12 34 56",
                             guest_email="famille@example.com", guest_lang="en")])
    assert 'href="tel:+34600123456"' in html
    assert "wa.me/34600123456" in html
    assert 'href="mailto:famille@example.com"' in html
    assert "parle anglais" in html


def test_staff_planning_midstay_hides_contact_for_past_stay():
    """RGPD : un séjour terminé ne divulgue plus les coordonnées, même si une
    intervention calculée tomberait encore dans le futur (§3.0)."""
    past = _pbk(starts_on=_dt.date(2025, 7, 1), ends_on=_dt.date(2025, 7, 15),
                guest_phone="+34 600 99 88 77")
    # Un séjour passé est exclu du planning : ses coordonnées ne sortent jamais.
    planning = care.build_planning([past], {}, _DEF_IN, _DEF_OUT, today=_TODAY)
    html = guide_page.render_staff(_prop(), [], "stok", planning=planning)
    assert "+34 600 99 88 77" not in html


def test_staff_planning_greys_non_occupied():
    works = _pbk(id="w", nature="works", starts_on=_dt.date(2026, 8, 20),
                 ends_on=_dt.date(2026, 8, 22))
    html = _staff_html([works])
    assert "prep-idle" in html
    assert "rien à préparer" in html


def test_staff_planning_empty_state():
    html = guide_page.render_staff(_prop(), [], "stok", planning=[])
    assert "Aucune préparation à venir" in html


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


# ── V2-24 : mode de trajet par catégorie + boutons d'itinéraire par POI ──────

def _catpoi(code, *, walk=None, drive=None, mode=None, lat=37.9, lon=-0.74):
    return {"id": code, "category_code": code, "chapter": "C",
            "category_name": {"fr": code}, "map_color": "#2E7D32",
            "name": code.title(), "lat": lat, "lon": lon, "cuisine": None,
            "walk_min": walk, "dist_walk_m": (walk or 0) * 70,
            "drive_min": drive, "travel_mode": mode, "owner_comment": None,
            "description_md": None, "opening_hours": None,
            "phone": None, "website": None}


def test_fmt_dist_driving_mode_always_shows_car_even_when_close():
    # Station-service à 3 min à pied : on y va quand même EN VOITURE (V2-24).
    n, u = guide_page._fmt_dist(_catpoi("fuel", walk=3, drive=2, mode="driving"), "fr")
    assert n == "2" and u == "min en voiture"


def test_fmt_dist_driving_mode_falls_back_to_walk_when_no_car_time():
    # POI ancien sans temps voiture : repli propre sur le temps à pied.
    n, u = guide_page._fmt_dist(_catpoi("fuel", walk=5, drive=None, mode="driving"), "fr")
    assert n == "5" and u == "min à pied"


def test_fmt_dist_walking_mode_keeps_walk_in_the_30_45_band():
    # Plage à 38 min : l'auto basculerait en voiture (>30), 'walking' garde à pied.
    n, u = guide_page._fmt_dist(_catpoi("beach", walk=38, drive=9, mode="walking"), "fr")
    assert n == "38" and u == "min à pied"


def test_fmt_dist_walking_mode_switches_to_car_when_absurdly_far():
    # Plage à 90 min à pied : rester cohérent → voiture.
    n, u = guide_page._fmt_dist(_catpoi("beach", walk=90, drive=14, mode="walking"), "fr")
    assert n == "14" and u == "min en voiture"


def test_fmt_dist_null_mode_keeps_historical_auto():
    # Sans mode : à pied si ≤ 30, sinon voiture (comportement M-01 inchangé).
    assert guide_page._fmt_dist(_catpoi("bakery", walk=10, drive=3), "fr") == ("10", "min à pied")
    assert guide_page._fmt_dist(_catpoi("mall", walk=42, drive=8), "fr") == ("8", "min en voiture")


def test_itinerary_buttons_on_every_poi_with_localised_apple_label():
    html = guide_page._render_pois([_catpoi("bakery", walk=6, lat=38.28, lon=-0.55)], "fr")
    assert '<div class="poi-nav">' in html
    assert "https://www.google.com/maps/dir/?api=1&destination=38.28,-0.55" in html
    assert "https://waze.com/ul?ll=38.28,-0.55&navigate=yes" in html
    assert "https://maps.apple.com/?daddr=38.28,-0.55" in html
    assert ">Google Maps<" in html and ">Waze<" in html
    assert ">Plans<" in html                    # libellé Apple localisé FR
    html_en = guide_page._render_pois([_catpoi("bakery", walk=6)], "en")
    assert ">Apple Maps<" in html_en            # libellé Apple localisé EN
    html_es = guide_page._render_pois([_catpoi("bakery", walk=6)], "es")
    assert ">Mapas<" in html_es                 # libellé Apple localisé ES


def test_no_itinerary_block_without_coordinates():
    html = guide_page._render_pois([_catpoi("bakery", walk=6, lat=None, lon=None)], "fr")
    assert '<div class="poi-nav">' not in html


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


def _poi(name, cat, ch, walk=5, comment=None, weekday=None, weekday_note=None,
         locality=None):
    return {"id": name, "name": name, "category_code": cat, "chapter": ch,
            "category_name": {"fr": cat}, "map_color": "#0E5A73",
            "lat": 37.9, "lon": -0.74, "walk_min": walk, "dist_walk_m": walk * 70,
            "drive_min": None, "owner_comment": comment, "description_md": None,
            "opening_hours": None, "phone": None, "website": None, "cuisine": None,
            "weekday": weekday, "weekday_note": weekday_note, "locality": locality}


def test_poi_locality_shown_only_when_different_from_home_commune():
    """V2-38 pièce 2 — la commune d'un lieu s'affiche discrètement (« · Vétroz »)
    UNIQUEMENT quand elle diffère de la commune du logement (comparaison normalisée
    casse/accents). Identique → masquée (bruit) ; absente → rien."""
    pois = [
        _poi("Caveau Régence", "restaurant", "F", locality="Vétroz"),      # ≠ → affichée
        _poi("Café du Village", "restaurant", "F", locality="ardon"),      # = (casse) → masquée
        _poi("Chez Sans-Ville", "restaurant", "F", locality=None),         # absente → rien
    ]
    html = guide_page.render_guide(_prop(city="Ardon"), [], pois, {}, "tok")
    # Différente → rendue à côté du nom, avec le séparateur « · » et la classe dédiée.
    assert 'class="poi-loc"' in html and "· Vétroz" in html
    # Identique (à la casse près) → jamais affichée.
    assert "· ardon" not in html and "· Ardon" not in html
    # Un seul badge de localité au total (seule « Vétroz » diffère).
    assert html.count('class="poi-loc"') == 1


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


# ── i18n du bloc secrets rendu côté client (V2-39) ───────────────────────────
# Le bloc Wifi/boîte à clés est rendu par app.js (secrets chiffrés). Le SSR sert
# ses libellés dans la langue du guide via `data-secret-labels` (blob JSON) — même
# régime que ui.search_* (V2-33). On vérifie le CONTENU servi (le rendu client est
# couvert par le harnais headless `guide-secrets`).

def _secret_labels(html: str) -> dict:
    """Extrait et parse le blob `data-secret-labels` du body (HTML-déséchappé)."""
    import html as _html
    m = re.search(r'data-secret-labels="([^"]*)"', html)
    assert m, "attribut data-secret-labels absent du body"
    return json.loads(_html.unescape(m.group(1)))


def test_secret_labels_served_localised_en():
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok", lang="en")
    lbl = _secret_labels(html)
    assert lbl["wifiTitle"] == "Wifi connection"
    assert lbl["wifiNetwork"] == "Network"
    assert lbl["wifiPassword"] == "Password"
    assert lbl["wifiScan"] == "Scan to connect to the Wifi automatically."
    assert lbl["keyboxTitle"] == "Key box" and lbl["keyboxCode"] == "Code"
    assert lbl["copy"] == "Copy" and lbl["copied"] == "Copied ✓"


def test_secret_labels_served_french_by_default():
    lbl = _secret_labels(guide_page.render_guide(
        _prop(), [_section("B_wifi", "B", {"fields": []})], [], {}, "tok"))
    assert lbl["wifiTitle"] == "Connexion Wifi"
    assert lbl["wifiNetwork"] == "Réseau" and lbl["wifiPassword"] == "Mot de passe"
    assert lbl["wifiScan"] == "Scannez pour vous connecter automatiquement au Wifi."
    assert lbl["keyboxTitle"] == "Boîte à clés" and lbl["keyboxCode"] == "Code"
    assert lbl["copy"] == "Copier" and lbl["copied"] == "Copié ✓"


def test_secret_labels_use_overlay_for_extra_language_with_fr_fallback():
    """Langue publiée supplémentaire (de) : l'overlay sert ce qu'il connaît, le reste
    retombe sur le FR (repli élégant, jamais de trou — comme tout `_t`)."""
    overlay = {"ui.wifi_scan": "Scannen, um dich automatisch mit dem WLAN zu verbinden.",
               "ui.wifi_network": "Netzwerk"}
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok", lang="de", ui_overlay=overlay)
    lbl = _secret_labels(html)
    assert lbl["wifiScan"] == "Scannen, um dich automatisch mit dem WLAN zu verbinden."
    assert lbl["wifiNetwork"] == "Netzwerk"
    # Non traduit dans l'overlay → repli FR (jamais un allemand inventé).
    assert lbl["wifiPassword"] == "Mot de passe" and lbl["copy"] == "Copier"


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
    # Le sélecteur est piloté par le REGISTRE (V2-21a) : render_guide reçoit la
    # carte des langues publiées (code→nom natif). Sans elle, seule la langue
    # source est offerte.
    html = guide_page.render_guide(prop, [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok",
                                   lang_names={"fr": "Français", "es": "Español"})
    assert 'class="langs"' in html and 'data-lang="es"' in html


def test_language_selector_hidden_when_lang_not_in_registry():
    """Registre = source unique (V2-21a) : une langue publiée pour le logement mais
    ABSENTE du registre (ex. dépubliée globalement) ne sort PAS du sélecteur, même
    si elle reste dans `published_langs`."""
    prop = _prop(default_lang="fr", published_langs=["es"])
    # `es` retirée du registre → le sélecteur ne doit proposer que la source, donc
    # pas de sélecteur du tout (une seule langue).
    html = guide_page.render_guide(prop, [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok", lang_names={"fr": "Français"})
    assert 'data-lang="es"' not in html
    assert 'class="langs"' not in html


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


# ── V2-12 : grille de pictogrammes (navigation principale d'« Autour de vous ») ─

def _grid(html):
    """Extrait le HTML de la grille de services, ou '' si absente."""
    import re
    m = re.search(r'<nav class="svc-grid".*?</nav>', html, re.S)
    return m.group(0) if m else ""


def test_service_grid_only_populated_categories_with_exact_counts():
    """Une tuile par catégorie ayant ≥1 POI retenu, avec le compte exact ; aucune
    tuile pour une catégorie « autour » sans POI."""
    pois = [_poi("Mercadona", "supermarket", "C"),
            _poi("Aldi", "supermarket", "C", walk=9),
            _poi("La Marejada", "restaurant", "F"),
            _poi("Playa Flamenca", "beach", "G")]
    html = guide_page.render_guide(_prop(), [], pois, {}, "tok")
    grid = _grid(html)
    assert grid, "la grille doit être présente dès qu'il y a des POI « autour »"
    # Tuiles présentes (une par catégorie peuplée), compte exact.
    assert 'href="#autour/supermarket"' in grid
    assert 'href="#autour/restaurant"' in grid
    assert 'href="#autour/beach"' in grid
    # supermarket a 2 POI, restaurant/beach 1 chacun (aria-label = « nom : N » ;
    # le nom vient du category_name — ici {"fr": <code>} via l'aide de test).
    assert 'aria-label="supermarket : 2"' in grid
    assert 'aria-label="restaurant : 1"' in grid
    assert '<span class="svc-count">2</span>' in grid
    # Catégorie « autour » non peuplée → pas de tuile.
    assert 'href="#autour/pharmacy"' not in grid   # pharmacy = onglet urgences de toute façon
    assert 'href="#autour/bar"' not in grid


def test_service_grid_order_follows_seed():
    """L'ordre des tuiles suit celui du seed (poi_icons.category_rank), pas l'ordre
    d'entrée ni l'alphabet."""
    # Entrée volontairement désordonnée / à cheval sur plusieurs chapitres.
    pois = [_poi("Playa", "beach", "G"), _poi("Bar Pepe", "bar", "F"),
            _poi("La Marejada", "restaurant", "F"), _poi("Mercadona", "supermarket", "C")]
    grid = _grid(guide_page.render_guide(_prop(), [], pois, {}, "tok"))
    order = [m for m in ("supermarket", "restaurant", "bar", "beach")
             if f'href="#autour/{m}"' in grid]
    idx = [grid.index(f'href="#autour/{m}"') for m in order]
    assert idx == sorted(idx)
    # Concrètement : supermarket(1) < restaurant(12) < bar(13) < beach(15).
    assert order == ["supermarket", "restaurant", "bar", "beach"]


def test_service_grid_names_localised_and_inline_svg_icon():
    pois = [_poi("La Marejada", "restaurant", "F")]
    # Nom localisé : on remplace le category_name par un dict multilingue.
    pois[0]["category_name"] = {"fr": "Restaurant", "en": "Restaurant", "es": "Restaurante"}
    pois[0]["category_icon"] = "utensils"
    grid_es = _grid(guide_page.render_guide(_prop(), [], pois, {}, "tok", lang="es"))
    assert "Restaurante" in grid_es
    assert 'aria-label="Servicios a tu alrededor"' in grid_es   # aria-label de la grille localisé
    # Icône : SVG inline (pas de dépendance Lucide côté client, offline-first).
    assert '<svg class="svc-svg"' in grid_es and "</svg>" in grid_es


def test_service_grid_tiles_anchor_to_category_blocks_in_around_panel():
    """Chaque tuile pointe vers l'id du bloc de sa catégorie, présent dans le
    panneau « around » (navigation par ancre, fonctionne sans JS)."""
    pois = [_poi("Mercadona", "supermarket", "C"), _poi("La Marejada", "restaurant", "F")]
    html = guide_page.render_guide(_prop(), [], pois, {}, "tok")
    around = _panel(html, "around")
    for code in ("supermarket", "restaurant"):
        assert f'href="#autour/{code}"' in _grid(html)          # cible de la tuile
        assert f'id="autour/{code}"' in around                  # bloc ancré, dans le bon onglet


def test_service_grid_absent_when_no_around_pois():
    """Pas de POI « autour » → pas de grille (repli : carte/listes suffisent)."""
    html = guide_page.render_guide(_prop(), [_section("B_wifi", "B", {"fields": []})],
                                   [], {}, "tok")
    assert '<nav class="svc-grid"' not in html


def test_service_grid_is_head_of_around_before_map():
    """La grille est en TÊTE de l'onglet, avant la carte (navigation principale)."""
    pois = [_poi("Mercadona", "supermarket", "C")]
    around = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [], pois, {}, "tok"),
                    "around")
    assert '<nav class="svc-grid"' in around and '<div id="map"></div>' in around
    assert around.index('<nav class="svc-grid"') < around.index('<div id="map"></div>')


# ── Jour du marché (V2-33 volet 1) ───────────────────────────────────────────

def _markets():
    """Trois marchés dans le chapitre C : samedi (loin), mercredi (près, avec
    note), et un sans jour (le plus près) — pour éprouver tri + badge."""
    return [_poi("Mercadillo Zoco", "market", "C", walk=20, weekday=6),
            _poi("Mercadillo Puerto", "market", "C", walk=8, weekday=3,
                 weekday_note="soir (été)"),
            _poi("Mercadillo sans jour", "market", "C", walk=2)]


def test_market_day_badge_translated_fr_and_en():
    """Le badge du jour est TRADUIT dans la langue du guide (Babel/CLDR), sans clé
    i18n : « samedi »/« mercredi » en FR, « Saturday »/« Wednesday » en EN. La note
    s'affiche à côté quand elle est présente."""
    pois = _markets()
    fr = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [], pois, {}, "tok"),
                "around")
    assert "market-badge" in fr
    assert "samedi" in fr and "mercredi" in fr
    assert "soir (été)" in fr                       # la note à côté du badge
    en = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [], pois, {},
                                        "tok", lang="en"), "around")
    assert "Saturday" in en and "Wednesday" in en
    # Casing NATUREL de la langue : pas de capitalisation forcée dans le texte.
    assert "Samedi" not in en and "samedi" not in en   # EN, pas de mot FR


def test_market_sorted_by_weekday_then_distance():
    """Les marchés sont triés lundi→dimanche (jour absent EN DERNIER), puis par
    distance — « un voyageur se repère par jour », pas par distance."""
    around = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [],
                                            _markets(), {}, "tok"), "around")
    order = re.findall(r"<h4>(Mercadillo [^<]+)", around)
    assert order == ["Mercadillo Puerto",       # mercredi (3)
                     "Mercadillo Zoco",          # samedi (6)
                     "Mercadillo sans jour"]     # sans jour → dernier


def test_market_without_weekday_has_no_badge():
    """Un marché sans jour ne porte aucun badge (et finit la liste)."""
    around = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [],
                                            _markets(), {}, "tok"), "around")
    # Deux badges pour trois marchés (celui sans jour n'en a pas).
    assert around.count("market-badge") == 2
    # La carte « sans jour » n'a pas de bloc .market-day.
    tail = around[around.index("Mercadillo sans jour"):]
    assert "market-day" not in tail


def test_non_market_category_still_sorted_by_distance():
    """Les AUTRES catégories restent triées par distance (le repère naturel d'un
    lieu ordinaire) — le tri par jour ne concerne que les marchés."""
    pois = [_poi("Lidl", "supermarket", "C", walk=15),
            _poi("Mercadona", "supermarket", "C", walk=4),
            _poi("Consum", "supermarket", "C", walk=9)]
    around = _panel(guide_page.render_guide(_prop(lat=37.9, lon=-0.74), [],
                                            pois, {}, "tok"), "around")
    order = re.findall(r"<h4>(Lidl|Mercadona|Consum)", around)
    assert order == ["Mercadona", "Consum", "Lidl"]   # 4 < 9 < 15 min
    assert "market-badge" not in around


# ── V2-07 volet 1 : livraison de repas rendue dans E_food_delivery ────────────

def test_food_delivery_fact_renders_inside_its_section():
    """Restitution selon le motif area_facts : la section E_food_delivery déclare
    `area_facts:["food_delivery"]` et le fait (plateformes résolues par zone)
    s'affiche DANS cette section, noms de marques NEUTRES + lien de preuve."""
    sections = [_section("E_food_delivery", "E",
                         {"fields": [], "area_facts": ["food_delivery"]})]
    facts = {"food_delivery": {"platforms": [
        {"name": "Glovo", "url": "https://glovoapp.com/es", "verified_on": "2026-08-11"},
        {"name": "Just Eat", "url": "https://just-eat.es"},
    ], "note": ""}}
    html = guide_page.render_guide(_prop(), sections, [], facts, "tok")
    assert 'href="https://glovoapp.com/es"' in html and ">Glovo<" in html
    assert ">Just Eat<" in html
    # Le rendu se fait dans la carte de la section (encart .facts food-delivery).
    assert 'class="facts food-delivery"' in html


def test_food_delivery_empty_list_renders_no_encart():
    """Liste vide (résultat valide) → aucun encart superflu dans le guide."""
    sections = [_section("E_food_delivery", "E",
                         {"fields": [], "area_facts": ["food_delivery"]})]
    facts = {"food_delivery": {"platforms": [], "note": ""}}
    html = guide_page.render_guide(_prop(), sections, [], facts, "tok")
    assert "food-delivery" not in html


# ── V2-07 volet 1bis : sections vierges + tuile de service ────────────────────

def _virtual(code, chapter, schema, name=None):
    """Section VIRTUELLE : déclarée par un template mais jamais enregistrée par le
    propriétaire (aucune ligne `property_sections`) → `virtual=True`, sans contenu
    ni média. Son seul contenu possible est l'encart d'un fait de zone déclaré."""
    return {"code": code, "chapter": chapter, "name_i18n": name or {"fr": code},
            "field_schema": schema, "content": None, "body_md": None,
            "media": [], "virtual": True}


_FD_SCHEMA = {"fields": [], "area_facts": ["food_delivery"]}
_FD_NAME = {"fr": "Livraison de repas", "en": "Food delivery",
            "es": "Comida a domicilio"}
_FD_FACT = {"food_delivery": {"platforms": [
    {"name": "Glovo", "url": "https://glovoapp.com/es"},
    {"name": "Just Eat", "url": "https://just-eat.es"}], "note": "Bonne couverture."}}


def test_virtual_section_renders_its_zone_fact_even_if_never_saved():
    """Pièce 1 — le fait de zone d'une section JAMAIS enregistrée s'affiche quand
    même (bug prod 11/08 : sans ligne, l'encart restait invisible). La carte de
    section apparaît avec son encart, ancrable (`id`)."""
    html = guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                                   _FD_SCHEMA, _FD_NAME)], [], _FD_FACT, "tok")
    assert 'id="E_food_delivery"' in html          # la carte de section existe
    assert 'class="facts food-delivery"' in html   # son encart est rendu
    assert ">Glovo<" in html and ">Just Eat<" in html


def test_virtual_section_without_resolved_fact_is_not_a_shell():
    """Pièce 1 — une section virtuelle dont le fait est absent/vide ne produit
    AUCUNE coquille (pas de titre nu) : ni carte de section, ni encart."""
    for facts in ({}, {"food_delivery": {"platforms": [], "note": ""}}):
        html = guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                                       _FD_SCHEMA, _FD_NAME)], [], facts, "tok")
        assert 'id="E_food_delivery"' not in html
        assert "food-delivery" not in html


def test_real_section_is_never_pruned_even_with_empty_fact():
    """Pièce 1 — une section RÉELLE (enregistrée, `virtual` absent) n'est jamais
    élaguée : sa visibilité est le choix du propriétaire, même sans contenu."""
    real = _section("E_food_delivery", "E", _FD_SCHEMA, name=_FD_NAME)  # pas virtual
    kept = guide_page._prune_virtual_sections([real], {}, "fr")
    assert kept == [real]
    virtual_empty = _virtual("E_food_delivery", "E", _FD_SCHEMA, _FD_NAME)
    assert guide_page._prune_virtual_sections([virtual_empty], {}, "fr") == []


def test_food_delivery_service_tile_present_and_anchors_to_section():
    """Pièce 2 — une tuile « Livraison de repas » entre dans la grille de services
    (même grammaire que les catégories) quand le fait a ≥1 plateforme ; elle mène
    (ancre `#{code}`) à la section et son encart, pas à un mode filtré catégorie."""
    html = guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                                   _FD_SCHEMA, _FD_NAME)], [], _FD_FACT, "tok")
    grid = _grid(html)
    assert grid, "la grille de services doit exister (au moins la tuile livraison)"
    # href = ancre native (repli sans JS, 1quater) ; data-fact = mode filtré (1quinquies).
    assert 'class="svc-tile" href="#E_food_delivery" data-fact="E_food_delivery"' in grid
    assert ">Livraison de repas<" in grid
    assert ">2<" in grid                    # compte = nombre de plateformes
    assert 'data-cat="food_delivery"' not in grid   # pas une tuile de catégorie POI


def test_food_delivery_service_tile_absent_without_platforms():
    """Pièce 2 — pas de plateforme → pas de tuile (miroir exact de « pas d'encart »)."""
    facts = {"food_delivery": {"platforms": [], "note": "x"}}
    html = guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                                   _FD_SCHEMA, _FD_NAME)], [], facts, "tok")
    assert "svc-tile" not in html and "svc-grid" not in html


def test_food_delivery_platforms_render_as_link_pills_with_note():
    """V2-07 volet 1ter — chaque plateforme avec URL est un bouton-pilule
    (`.route-link`, grammaire des cartes de lieux) `target="_blank"` qui ouvre son
    site ; la note de couverture reste rendue sous les pilules ; plus de `<ul>`."""
    facts = {"food_delivery": {"platforms": [
        {"name": "Glovo", "url": "https://glovoapp.com/es"},
        {"name": "Just Eat", "url": "https://just-eat.es"}],
        "note": "Bonne couverture le soir."}}
    html = guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                                   _FD_SCHEMA, _FD_NAME)], [], facts, "tok")
    for brand, url in (("Glovo", "https://glovoapp.com/es"),
                       ("Just Eat", "https://just-eat.es")):
        assert (f'<a class="route-link" href="{url}" target="_blank" '
                f'rel="noopener nofollow">{brand}') in html
    assert "fd-list" not in html                       # l'ancienne liste a disparu
    assert "Bonne couverture le soir." in html         # la note reste rendue


def test_food_delivery_tile_localized_label_without_new_i18n_key():
    """Pièce 2 — le libellé de la tuile suit `name_i18n` du template (les langues
    existent déjà) : rendu en espagnol → « Comida a domicilio »."""
    grid = _grid(guide_page.render_guide(_prop(), [_virtual("E_food_delivery", "E",
                 _FD_SCHEMA, _FD_NAME)], [], _FD_FACT, "tok", lang="es"))
    assert ">Comida a domicilio<" in grid


# ── V2-34 : horaires normalisés + mention systématique (au rendu) ────────────

def test_hours_osm_range_localized_fr_en_de():
    assert guide_page._normalize_hours("Mo-Sa 09:00-21:30", "fr") == "lundi–samedi 09:00–21:30"
    en = guide_page._normalize_hours("Mo-Sa 09:00-21:30", "en")
    assert en.startswith("Monday–Saturday") and "AM" in en and "PM" in en   # 12 h en anglais
    de = guide_page._normalize_hours("Mo-Sa 09:00-21:30", "de")
    assert de.startswith("Montag–Samstag") and "09:00" in de


def test_hours_247_multiranges_and_closed():
    assert guide_page._normalize_hours("24/7", "fr") == "24/7"
    assert (guide_page._normalize_hours("Mo-Fr 09:00-13:00,16:00-20:00", "fr")
            == "lundi–vendredi 09:00–13:00, 16:00–20:00")
    assert guide_page._normalize_hours("Su off", "en") == "Sunday closed"
    assert guide_page._normalize_hours("Su off", "fr").endswith("fermé")
    # Plusieurs règles → jointes par « · ».
    rules = guide_page._normalize_hours("Mo-Fr 09:00-18:00; Sa 09:00-13:00; Su off", "fr")
    assert rules == "lundi–vendredi 09:00–18:00 · samedi 09:00–13:00 · dimanche fermé"


def test_hours_complex_rule_falls_back_to_raw_never_deformed():
    """Règle d'or : toute valeur non parsée (PH, prose, saisie libre) reste BRUTE."""
    for raw in ("Mo-Fr 08:00-18:00; PH off", "Mo-Su sunrise-sunset",
                "Lun–Sam 9h–21h30, Dim fermé", "sur rendez-vous"):
        assert guide_page._normalize_hours(raw, "en") == raw


def test_hours_render_adds_localized_mention_and_dedupes():
    fr = guide_page._render_opening_hours("Mo-Sa 09:00-21:30", "fr")
    assert 'class="hours"' in fr and "lundi–samedi 09:00–21:30" in fr
    assert "· Horaires indicatifs</div>" in fr
    en = guide_page._render_opening_hours("Mo-Sa 09:00-21:30", "en")
    assert "· Indicative hours</div>" in en
    # Prose héritée du volet 2 : la mention STOCKÉE est dédupliquée (jamais deux).
    dedup = guide_page._render_opening_hours("Lun–Sam 8h–22h · Horaires indicatifs", "fr")
    assert dedup.count("Horaires indicatifs") == 1 and "Lun–Sam 8h–22h" in dedup
    # En anglais : mention localisée, prose héritée restée FR (dette V2-29).
    dedup_en = guide_page._render_opening_hours("Lun–Sam 8h–22h · Horaires indicatifs", "en")
    assert "Indicative hours" in dedup_en and "Horaires indicatifs" not in dedup_en


def test_hours_empty_renders_nothing():
    assert guide_page._render_opening_hours(None, "fr") == ""
    assert guide_page._render_opening_hours("", "fr") == ""
    # Une mention héritée SEULE (aucun horaire) → ni horaire ni mention.
    assert guide_page._render_opening_hours(" · Horaires indicatifs", "fr") == ""


def _poi_with_hours(name, hours):
    p = _poi(name, "supermarket", "C")
    p["opening_hours"] = hours
    return p


def test_hours_rendered_in_poi_card_fr_and_en():
    """DOM rendu : la carte Consum (OSM brut) devient lisible + mention, FR et EN."""
    consum = _poi_with_hours("Consum", "Mo-Sa 09:00-21:30")
    fr = guide_page.render_guide(_prop(), [], [consum], {}, "tok")
    assert "lundi–samedi 09:00–21:30 · Horaires indicatifs" in fr
    en = guide_page.render_guide(_prop(), [], [consum], {}, "tok", lang="en")
    assert "Monday–Saturday" in en and "· Indicative hours</div>" in en
    assert "Mo-Sa" not in fr and "Mo-Sa" not in en   # plus jamais la syntaxe OSM brute
