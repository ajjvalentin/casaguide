"""Tests du moteur d'interventions d'entretien (V2-23b, volet 1) — `api.care`.

Module **pur** (aucun réseau, aucune base, `today` toujours passé) → testé
directement. Couvre les règles de la mission :
  §1.3  la NATURE pilote la préparation (reservation=tout, private=sans pack,
        works/unavailable/unqualified=rien) ; interventions QUANTIFIÉES ;
  §1.0  suggestions d'équipement d'après les âges (SUGGÈRE, jamais d'office) ;
  §0.6  relance active (voyageurs/coordonnées manquants, nature à qualifier) ;
  §1.1  signal de fenêtre de rotation (neutral/amber/red, null-safe).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import care  # noqa: E402

TODAY = dt.date(2026, 8, 1)


def _bk(**over):
    b = {"starts_on": dt.date(2026, 8, 10), "ends_on": dt.date(2026, 8, 24),
         "nature": "reservation", "status": "active", "linked_booking_id": None,
         "guest_count": 6, "children_ages": [], "guest_contact": "+34 600"}
    b.update(over)
    return b


# ── Défauts & fusion ─────────────────────────────────────────────────────────

def test_default_care_rules_person_hours_left_null():
    """Les hommes-heures sont délibérément à null (à obtenir d'André)."""
    t = care.default_care_rules()["turnaround"]
    assert t["person_hours_min_occupancy"] is None
    assert t["person_hours_full_occupancy"] is None
    assert care.default_care_rules()["linen_change_from_day"] == 8


def test_merged_rules_backfills_missing_from_defaults():
    m = care.merged_rules({"linen_change_from_day": 5})
    assert m["linen_change_from_day"] == 5
    assert m["welcome_pack"] == "free"                    # repli défaut
    assert m["turnaround"]["max_cleaners"] == 2           # sous-objet fusionné
    assert care.merged_rules({})["age_bands"]             # {} → défauts complets


# ── Suggestions d'équipement (§1.0) ──────────────────────────────────────────

def test_suggest_equipment_by_age():
    cr = care.default_care_rules()
    assert care.suggest_equipment([1], cr) == ["lit_bebe", "chaise_haute"]
    assert care.suggest_equipment([8], cr) == ["lit_appoint"]
    assert care.suggest_equipment([15], cr) == []                 # ado = adulte
    # dédoublonnage + ordre de première apparition
    assert care.suggest_equipment([1, 2, 8], cr) == \
        ["lit_bebe", "chaise_haute", "lit_appoint"]


def test_children_count_derived_from_ages():
    assert care.children_count(_bk(children_ages=[1, 3, 14])) == 3
    assert care.children_count(_bk(children_ages=None)) == 0


# ── Interventions : la nature pilote (§1.3) ──────────────────────────────────

def test_reservation_14_nights_has_linen_change_at_day_8():
    """Un séjour de 14 nuits produit un changement de draps à J+8."""
    b = _bk(starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 24))  # 14 nuits
    kinds = [(i.kind, i.on) for i in care.plan_interventions(b, {})]
    assert ("arrival", dt.date(2026, 8, 10)) in kinds
    assert ("linen_change", dt.date(2026, 8, 18)) in kinds  # J+8
    # une seule fois (J+16 dépasserait le départ)
    assert sum(1 for k, _ in kinds if k == "linen_change") == 1


def test_short_stay_has_no_linen_change():
    """Un séjour de 5 nuits ne produit aucun changement de draps."""
    b = _bk(starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 15))
    kinds = [i.kind for i in care.plan_interventions(b, {})]
    assert "linen_change" not in kinds
    assert "arrival" in kinds


def test_private_stay_has_linen_but_no_welcome_pack():
    """Occupation privée de 14 jours : les draps oui, le welcome pack non."""
    b = _bk(nature="private")
    inter = care.plan_interventions(b, {})
    kinds = [i.kind for i in inter]
    assert "linen_change" in kinds
    arrival = next(i for i in inter if i.kind == "arrival")
    assert not any("Welcome pack" in t for t in arrival.tasks)


def test_reservation_arrival_has_welcome_pack():
    arrival = next(i for i in care.plan_interventions(_bk(), {})
                   if i.kind == "arrival")
    assert any("Welcome pack pour 6" in t for t in arrival.tasks)


def test_works_and_unavailable_have_no_interventions():
    assert care.plan_interventions(_bk(nature="works"), {}) == []
    assert care.plan_interventions(_bk(nature="unavailable"), {}) == []
    assert care.plan_interventions(_bk(nature="unqualified"), {}) == []


def test_quantities_show_missing_explicitly():
    """Une quantité inconnue s'affiche, ne disparaît jamais en silence."""
    arrival = next(i for i in care.plan_interventions(_bk(guest_count=None), {})
                   if i.kind == "arrival")
    assert any("nombre de voyageurs non renseigné" in t for t in arrival.tasks)


def test_accepted_requests_join_arrival_prep():
    reqs = [{"label": "Lit bébé", "quantity": 1, "status": "accepted"},
            {"label": "Parasol", "quantity": 2, "status": "pending"}]
    arrival = next(i for i in care.plan_interventions(_bk(), {}, requests=reqs)
                   if i.kind == "arrival")
    assert any("Lit bébé" in t for t in arrival.tasks)      # accepté → présent
    assert not any("Parasol" in t for t in arrival.tasks)   # pending → absent


def test_linen_change_needs_appointment():
    linen = next(i for i in care.plan_interventions(_bk(), {})
                 if i.kind == "linen_change")
    assert linen.needs_appointment is True


# ── Relance active (§0.6) ────────────────────────────────────────────────────

def test_missing_info_flags_unqualified():
    codes = [m["code"] for m in
             care.missing_info(_bk(nature="unqualified"), {}, today=TODAY)]
    assert codes == ["unqualified"]


def test_missing_info_flags_guest_count_and_contact():
    b = _bk(guest_count=None, guest_contact="")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY)}
    assert "guest_count_missing" in codes
    assert "contact_missing" in codes       # 14 nuits → draps à J+8 → rdv


def test_missing_info_contact_only_when_midstay_intervention():
    """Un séjour court sans intervention en cours n'exige pas les coordonnées."""
    b = _bk(starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 13),
            guest_contact="")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY)}
    assert "contact_missing" not in codes


def test_missing_info_ignores_past_cancelled_and_linked():
    past = _bk(starts_on=dt.date(2025, 1, 1), ends_on=dt.date(2025, 1, 10))
    assert care.missing_info(past, {}, today=TODAY) == []
    assert care.missing_info(_bk(status="cancelled"), {}, today=TODAY) == []
    assert care.missing_info(_bk(linked_booking_id="x"), {}, today=TODAY) == []


def test_missing_info_none_for_complete_booking():
    assert care.missing_info(_bk(), {}, today=TODAY) == []


# ── Fenêtre de rotation (§1.1) ───────────────────────────────────────────────

def test_turnaround_signal_unknown_when_not_configured():
    """Sans hommes-heures configurés, on ne signale rien (jamais inventé)."""
    s = care.turnaround_signal(care.default_care_rules(), 6, window_minutes=300)
    assert s["level"] == "unknown"
    assert s["person_hours"] is None


def _rules_with_hours(**over):
    cr = care.default_care_rules()
    cr["turnaround"].update({"person_hours_full_occupancy": 6.0,
                             "person_hours_min_occupancy": 4.0,
                             "min_occupancy_guests": 2, "full_occupancy_guests": 8,
                             "max_cleaners": 2, "comfort_margin_hours": 1})
    cr["turnaround"].update(over)
    return cr


def test_turnaround_signal_neutral_when_window_comfortable():
    cr = _rules_with_hours()
    # pleine occupation → 6 h de charge ; fenêtre de 8 h ≥ 6 + 1 → neutre.
    s = care.turnaround_signal(cr, 8, window_minutes=8 * 60)
    assert s["level"] == "neutral"
    assert s["recommended_cleaners"] == 1


def test_turnaround_signal_amber_recommends_two():
    cr = _rules_with_hours()
    # pleine occupation → 6 h ; fenêtre 4 h < 6 mais 6/2 = 3 ≤ 4 → ambre, 2 pers.
    s = care.turnaround_signal(cr, 8, window_minutes=4 * 60)
    assert s["level"] == "amber"
    assert s["recommended_cleaners"] == 2


def test_turnaround_signal_red_when_infeasible():
    cr = _rules_with_hours()
    # fenêtre 2 h ; même à 2 personnes il faut 3 h → rouge.
    s = care.turnaround_signal(cr, 8, window_minutes=2 * 60)
    assert s["level"] == "red"
    assert s["recommended_cleaners"] is None


def test_turnaround_person_hours_pessimistic_when_guest_count_unknown():
    cr = _rules_with_hours()
    assert care.turnaround_person_hours(cr, None) == 6.0     # pleine occupation


def test_turnaround_person_hours_interpolates():
    cr = _rules_with_hours()
    # milieu de la plage (5 voyageurs sur 2..8) → entre 4 et 6.
    ph = care.turnaround_person_hours(cr, 5)
    assert 4.0 < ph < 6.0
