"""Tests unitaires des icônes de catégorie (`api/poi_icons.py`, V2-12).

Le guide voyageur n'embarque pas Lucide (offline-first) : chaque icône du seed
est rendue en SVG inline côté serveur. On vérifie ici le contrat (icône trouvée
par nom Lucide ou par code, repli neutre, ordre du seed).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import poi_icons  # noqa: E402


def test_icon_svg_found_by_lucide_name():
    svg = poi_icons.category_icon_svg("restaurant", "utensils")
    assert svg.startswith('<svg class="svc-svg"') and svg.endswith("</svg>")
    assert 'aria-hidden="true"' in svg           # décorative (le libellé porte le sens)
    assert "<path" in svg                          # tracé réel embarqué


def test_icon_svg_found_by_category_code_without_name():
    # Sans nom d'icône, on retrouve l'icône via le miroir du seed (_CAT_ICON).
    svg = poi_icons.category_icon_svg("fuel")
    assert "<path" in svg and svg != poi_icons._FALLBACK


def test_icon_svg_falls_back_for_unknown_category():
    svg = poi_icons.category_icon_svg("totally_unknown_cat")
    assert poi_icons._FALLBACK in svg              # point neutre, jamais d'erreur


def test_category_rank_follows_seed_order():
    # Ordre du seed : supermarket avant restaurant avant fuel.
    assert (poi_icons.category_rank("supermarket")
            < poi_icons.category_rank("restaurant")
            < poi_icons.category_rank("fuel"))
    # Catégorie inconnue → rejetée en fin d'ordre.
    assert poi_icons.category_rank("zzz") == len(poi_icons._CAT_ORDER)


def test_all_around_categories_have_an_embedded_icon():
    """Toutes les catégories susceptibles d'apparaître dans « Autour de vous »
    (chapitres C/E/F/G/H) ont un tracé embarqué (jamais de repli en prod)."""
    around = ["supermarket", "market", "bakery", "atm", "post_office", "mall",
              "laundry", "taxi", "babysitter", "food_delivery", "rental",
              "restaurant", "bar", "cafe", "beach", "sight", "family_activity",
              "sport", "bus_stop", "bus_station", "train_station", "airport",
              "fuel", "charging_station"]
    for code in around:
        assert poi_icons._FALLBACK not in poi_icons.category_icon_svg(code), code
