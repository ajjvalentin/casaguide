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
