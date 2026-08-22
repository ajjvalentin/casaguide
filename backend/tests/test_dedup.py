"""Dédoublonnage à la suggestion (V2-40) — passe PURE, sans base ni réseau.

Reproduit les trois doublons réels du 18/08 (Murcie, Alicante, Guardamar) + les
sentinelles : Aguamarina (parenthèses de zone) ne fusionne pas deux agences, un
candidat qui double une fiche retenue est retiré, XiaoWu à l'adresse du Bon Bon
rejeté VIT (nom différent), une variante de nom d'une fiche rejetée est retirée,
et deux catégories à 10 m ne fusionnent jamais.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from enrich import dedup  # noqa: E402


def _c(name, lat=38.0, lon=-0.7, category="airport", **fields):
    return {"name": name, "lat": lat, "lon": lon, "category": category, **fields}


# ── name_similar : le trigramme généralisé (paren-strip) ─────────────────────

def test_name_similar_real_variants_and_distinct_places():
    # Variantes du MÊME lieu → similaires (paren-strip attrape « (ALC) »).
    assert dedup.name_similar("Aeropuerto de Alicante-Elche (ALC)",
                              "Aeropuerto de Alicante-Elche Miguel Hernández")
    assert dedup.name_similar("Estación de Guardamar", "Estació de Guardamar")
    # Lieux DISTINCTS → jamais fusionnés.
    assert not dedup.name_similar("Aeropuerto de Murcia (RMU)",
                                  "Aeropuerto de Alicante-Elche (ALC)")
    assert not dedup.name_similar("Farmacia Lo Pou", "Farmacia San Miguel")


def test_aguamarina_zone_suffix_does_not_merge_distinct_agencies():
    """Le suffixe de zone « (Aguamarina) » retiré, deux agences distinctes gardent
    des bases différentes → jamais fusionnées (précédent Aguamarina)."""
    a = _c("Inmobiliaria Costa Blanca (Aguamarina)", lon=-0.70, category="rental")
    b = _c("Fincas del Sol (Aguamarina)", lon=-0.90, category="rental")   # loin
    survivors, merged = dedup.deduplicate([a, b])
    assert merged == 0 and len(survivors) == 2


# ── Les trois cas réels du 18/08 ─────────────────────────────────────────────

def test_murcia_two_elements_merge_by_distance_best_scored_survives():
    """Murcie : deux éléments OSM du même aéroport au même emplacement (< 150 m),
    noms peu semblables → fusionnés par la DISTANCE ; le mieux renseigné survit."""
    rmu = _c("Aeropuerto de Murcia (RMU)", lat=38.0, lon=-1.000,
             phone="+34 968 000 000")                      # score 1
    official = _c("Aeropuerto Internacional Región de Murcia", lat=38.0, lon=-1.001)
    assert not dedup.name_similar(rmu["name"], official["name"])   # pas par le nom
    survivors, merged = dedup.deduplicate([rmu, official])
    assert merged == 1 and len(survivors) == 1
    assert survivors[0]["name"] == "Aeropuerto de Murcia (RMU)"    # mieux renseigné


def test_alicante_two_geometry_points_merge_by_name_shortest_travel_survives():
    """Alicante-Elche : deux POINTS de géométrie (52 min vs 72 min), noms « (ALC) »
    et « Miguel Hernández » → fusionnés par le NOM (paren-strip) ; à score égal, le
    plus proche (52 min) survit — le point de géométrie le plus juste."""
    alc = _c("Aeropuerto de Alicante-Elche (ALC)", lat=38.3, lon=-0.55, drive_min=72)
    miguel = _c("Aeropuerto de Alicante-Elche Miguel Hernández",
                lat=38.28, lon=-0.56, drive_min=52)
    survivors, merged = dedup.deduplicate([alc, miguel])
    assert merged == 1 and len(survivors) == 1
    assert survivors[0]["drive_min"] == 52                          # le 52 min gagne
    assert "Miguel" in survivors[0]["name"]


def test_guardamar_bilingual_merge_by_name_even_when_far_apart():
    """Guardamar : castillan + valencien de la même gare, éloignés (> 150 m) → seul
    le NOM les rapproche."""
    cast = _c("Estación de Guardamar", lat=38.09, lon=-0.65, category="train_station")
    vale = _c("Estació de Guardamar", lat=38.20, lon=-0.65, category="train_station")
    assert dedup._distance_m(cast, vale) > dedup.DUP_DIST_M      # pas par la distance
    survivors, merged = dedup.deduplicate([cast, vale])
    assert merged == 1 and len(survivors) == 1


def test_different_categories_never_merge_even_at_10m():
    """Pharmacie dans le centre commercial : même point (10 m), catégories
    différentes → JAMAIS fusionnées (même à nom identique)."""
    pharm = _c("La Zenia", lat=37.93, lon=-0.735, category="pharmacy")
    mall = _c("La Zenia", lat=37.9301, lon=-0.735, category="mall")   # ~11 m
    assert dedup._distance_m(pharm, mall) < dedup.DUP_DIST_M
    survivors, merged = dedup.deduplicate([pharm, mall])
    assert merged == 0 and len(survivors) == 2


# ── Contre l'existant : retenue vs rejetée (la nuance Bon Bon → XiaoWu) ───────

def test_candidate_doubling_a_retained_fiche_under_another_ref_is_removed():
    """Le propriétaire a déjà approuvé son aéroport → ne pas le lui reproposer sous
    un AUTRE source_ref (nom OU distance)."""
    existing = [{"name": "Aeropuerto de Alicante-Elche Miguel Hernández",
                 "lat": 38.28, "lon": -0.56, "status": "approved",
                 "source_ref": "node/500"}]
    by_name = _c("Aeropuerto de Alicante-Elche (ALC)", lat=38.3, lon=-0.55,
                 source_ref="way/600")
    by_dist = _c("Terminal T1", lat=38.2801, lon=-0.5601, source_ref="node/700")
    kept, removed = dedup.filter_against_existing([by_name, by_dist], existing)
    assert removed == 2 and kept == []


def test_same_source_ref_is_never_removed_flows_to_idempotent_upsert():
    """La MÊME fiche re-moissonnée (source_ref identique à une fiche retenue) n'est
    JAMAIS retirée : elle passe par l'upsert idempotent, respectueux du statut (c'est
    ainsi qu'une fiche approuvée gagne une localité NULL — V2-38bis)."""
    existing = [{"name": "Aeropuerto de Alicante-Elche Miguel Hernández",
                 "lat": 38.28, "lon": -0.56, "status": "approved",
                 "source_ref": "node/333"}]
    same = _c("Aeropuerto de Alicante-Elche Miguel Hernández", lat=38.28, lon=-0.56,
              source_ref="node/333")
    kept, removed = dedup.filter_against_existing([same], existing)
    assert removed == 0 and kept == [same]


def test_xiaowu_survives_bon_bon_rejected_same_address():
    """SENTINELLE (leçon Bon Bon → XiaoWu) : un successeur légitime s'installe où le
    mort a fermé — nom DIFFÉRENT + fiche REJETÉE → la proximité seule ne condamne
    pas. XiaoWu à l'adresse exacte du Bon Bon rejeté VIT."""
    rejected = [{"name": "Restaurante Bon Bon", "lat": 37.93, "lon": -0.735,
                 "status": "rejected"}]
    xiaowu = _c("XiaoWu", lat=37.93, lon=-0.735, category="restaurant")  # même point
    assert dedup._distance_m(xiaowu, {"lat": 37.93, "lon": -0.735}) == 0
    kept, removed = dedup.filter_against_existing([xiaowu], rejected)
    assert removed == 0 and kept == [xiaowu]


def test_name_variant_of_a_rejected_fiche_is_removed():
    """La variante valencienne d'une gare REJETÉE disparaît — nom similaire ✓, la
    distance n'entre jamais en jeu face à une fiche rejetée."""
    rejected = [{"name": "Estación de Guardamar", "lat": 38.09, "lon": -0.65,
                 "status": "rejected"}]
    variant = _c("Estació de Guardamar", lat=38.30, lon=-0.90,   # LOIN (distance nulle)
                 category="train_station")
    assert dedup._distance_m(variant, rejected[0]) > dedup.DUP_DIST_M
    kept, removed = dedup.filter_against_existing([variant], rejected)
    assert removed == 1 and kept == []


def test_rejected_fiche_distance_only_never_removes_a_different_name():
    """Face à une fiche rejetée, la DISTANCE seule (nom différent) ne retire jamais —
    corollaire du test-sentinelle, mais à distance < 150 m explicite."""
    rejected = [{"name": "Bon Bon", "lat": 38.0, "lon": -0.7, "status": "rejected"}]
    near = _c("Peluquería Marta", lat=38.0, lon=-0.7005, category="restaurant")  # ~44 m
    assert 0 < dedup._distance_m(near, rejected[0]) < dedup.DUP_DIST_M
    kept, removed = dedup.filter_against_existing([near], rejected)
    assert removed == 0 and kept == [near]


# ── Survivant = le mieux renseigné (score de champs) ─────────────────────────

def test_survivor_carries_the_best_field_score():
    """À doublon, le survivant est celui qui porte le plus de champs (tél, site,
    horaires, cuisine, locality)."""
    rich = _c("Farmacia Centro", lon=-0.7000, category="pharmacy",
              phone="+34 900", website="https://x", opening_hours="Mo-Fr 09:00-20:00")
    poor = _c("Farmacia Centro", lon=-0.70005, category="pharmacy")   # même lieu
    survivors, merged = dedup.deduplicate([poor, rich])   # le pauvre d'abord
    assert merged == 1 and len(survivors) == 1
    assert survivors[0] is rich                            # le mieux renseigné survit
    assert dedup._score(rich) == 3 and dedup._score(poor) == 0
