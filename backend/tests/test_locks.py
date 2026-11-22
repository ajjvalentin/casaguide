"""Règle PURE du verrou par champ (V2-91, `enrich/locks.py`) — aucune base."""
from __future__ import annotations

import pytest

from api import repo
from enrich.locks import EDITABLE_FIELDS, LOCKABLE_FIELDS, system_may_write


def test_locked_field_is_never_written():
    """Branche 1 : un champ saisi par un humain n'est jamais réécrit, même si la
    source apporte une valeur neuve et non vide."""
    assert system_may_write("phone", {"phone", "name"}, "+41 27 000 00 00",
                            "+41 27 111 11 11") is False


@pytest.mark.parametrize("incoming", [None, "", "   ", [], {}])
def test_silent_source_never_erases(incoming):
    """Branche 2 : une source muette (None / vide) n'efface jamais une valeur."""
    assert system_may_write("website", set(), "https://ex.example", incoming) is False


def test_zero_is_a_value_not_silence():
    """0 n'est pas « vide » : seul None/chaîne blanche/collection vide l'est."""
    assert system_may_write("weekday", (), None, 0) is True


def test_identical_value_is_not_rewritten():
    """Branche 3 : rien à écrire si la source redit la valeur courante."""
    assert system_may_write("opening_hours", [], "Mo-Fr 08:00-18:00",
                            "Mo-Fr 08:00-18:00") is False


def test_unlocked_changed_value_may_be_written():
    """Branche 4 : champ non verrouillé + valeur neuve non vide → écriture permise,
    y compris pour remplir un champ vide et pour `geom`."""
    assert system_may_write("opening_hours", {"name"}, "Mo-Fr 08:00-18:00",
                            "Mo-Sa 08:00-19:00") is True
    assert system_may_write("phone", [], None, "+34 965 00 00 00") is True
    assert system_may_write("geom", ["phone"], (7.1, 46.2), (7.2, 46.2)) is True


@pytest.mark.parametrize("field", ["opening_hour", "status", "dist_walk_m", ""])
def test_unknown_field_fails_loudly(field):
    """Champ inconnu : une faute de frappe ne serait jamais dans `human_fields` et
    ouvrirait l'écrasement d'une saisie humaine → ValueError, jamais True."""
    with pytest.raises(ValueError):
        system_may_write(field, set(), "a", "b")


def test_lockable_fields_are_the_editable_ones_plus_geom():
    """Source unique : `repo._POI_EDITABLE` EST `locks.EDITABLE_FIELDS`."""
    assert repo._POI_EDITABLE == EDITABLE_FIELDS
    assert LOCKABLE_FIELDS == EDITABLE_FIELDS + ("geom",)


# ── V2-93 : plan_refresh (règle de report des champs simples) ────────────────

from enrich.locks import REFRESH_FIELDS, FieldChange, plan_refresh, source_values_of  # noqa: E402

_BASE = {"phone": "+34 1", "website": "https://a.example", "opening_hours": "Mo-Fr 09:00-18:00"}
_NEW = {"phone": "+34 2", "website": "https://b.example", "opening_hours": "Mo-Sa 09:00-20:00"}


@pytest.mark.parametrize("f", REFRESH_FIELDS)
def test_plan_refresh_locked_field_untouched(f):
    poi = {**_BASE, "human_fields": [f]}
    assert all(c.field != f for c in plan_refresh(poi, {f: _NEW[f]}))


@pytest.mark.parametrize("f", REFRESH_FIELDS)
@pytest.mark.parametrize("silent", [None, "", "   "])
def test_plan_refresh_silent_source_never_erases(f, silent):
    assert plan_refresh({**_BASE, "human_fields": []}, {f: silent}) == []


@pytest.mark.parametrize("f", REFRESH_FIELDS)
def test_plan_refresh_identical_value_nothing(f):
    assert plan_refresh({**_BASE, "human_fields": []}, {f: _BASE[f]}) == []


@pytest.mark.parametrize("f", REFRESH_FIELDS)
def test_plan_refresh_changed_value_written(f):
    assert plan_refresh({**_BASE, "human_fields": []}, {f: _NEW[f]}) == [
        FieldChange(f, _BASE[f], _NEW[f])]


def test_plan_refresh_fills_null_and_ignores_other_fields():
    """Un champ NULL est rempli ; un nom/une adresse fournis par la source sont ignorés."""
    poi = {"phone": None, "website": None, "opening_hours": None, "human_fields": []}
    plan = plan_refresh(poi, {"phone": "+34 3", "name": "Autre", "address": "Calle 9"})
    assert plan == [FieldChange("phone", None, "+34 3")]


def test_secondary_phone_on_osm_fiche_origin_wins_or_stays():
    """Téléphone venu d'Overture ou de la complétion web sur une fiche OSM : si OSM
    donne AUJOURD'HUI un téléphone, il l'emporte ; si OSM reste muet, on ne touche à
    rien (la source secondaire n'est jamais la référence)."""
    poi = {"phone": "+34 999 (overture)", "website": None, "opening_hours": None,
           "human_fields": [],
           "completion_meta": {"_overture": {"fields": ["phone"]},
                               "phone": {"source_url": "https://web.example"}}}
    assert plan_refresh(poi, {"phone": "+34 111"}) == [
        FieldChange("phone", "+34 999 (overture)", "+34 111")]
    assert plan_refresh(poi, {"phone": None}) == []


def test_source_values_of_prefers_harvest_origin_values():
    """À l'upsert : `source_values` (posé par la moisson OSM, avant tout comblement)
    fait foi ; à défaut, les champs comblés par Overture sont retirés."""
    harvested = {"source": "osm", "phone": "+34 ovt", "website": "https://osm",
                 "opening_hours": None,
                 "source_values": {"phone": None, "website": "https://osm",
                                   "opening_hours": None}}
    assert source_values_of(harvested) == {"phone": None, "website": "https://osm",
                                           "opening_hours": None}
    filled = {"source": "osm", "phone": "+34 ovt", "website": "https://w",
              "completion_meta": {"_overture": {"fields": ["phone"]}}}
    assert source_values_of(filled) == {"phone": None, "website": "https://w",
                                        "opening_hours": None}
    ovt = {"source": "overture", "phone": "+34 5", "website": None}
    assert source_values_of(ovt)["phone"] == "+34 5"
