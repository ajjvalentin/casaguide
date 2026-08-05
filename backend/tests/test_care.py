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
         "guest_count": 6, "children_ages": [], "guest_contact": None,
         "guest_phone": "+34 600 11 22 33", "guest_email": None, "guest_lang": None}
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


def test_missing_info_flags_guest_count_and_phone():
    b = _bk(guest_count=None, guest_phone="", guest_contact="")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY)}
    assert "guest_count_missing" in codes
    assert "phone_missing" in codes         # 14 nuits → draps à J+8 → rdv → tél


def test_missing_info_phone_satisfied_by_legacy_contact():
    """Un legacy `guest_contact` qui ressemble à un téléphone tient lieu de tél
    (repli tant que la migration/saisie n'a pas séparé les champs)."""
    b = _bk(guest_phone="", guest_contact="+34 600 11 22 33")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY)}
    assert "phone_missing" not in codes
    # Un email seul ne cale pas un rendez-vous → le téléphone manque toujours.
    b2 = _bk(guest_phone="", guest_contact="jean@example.com")
    codes2 = {m["code"] for m in care.missing_info(b2, {}, today=TODAY)}
    assert "phone_missing" in codes2


def test_missing_info_phone_only_when_midstay_intervention():
    """Un séjour court sans intervention en cours n'exige pas le téléphone."""
    b = _bk(starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 13),
            guest_phone="", guest_contact="")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY)}
    assert "phone_missing" not in codes


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


# ── Rendement du travail à plusieurs (§2.3) ──────────────────────────────────

def test_default_rules_carry_parallel_efficiency():
    assert care.default_care_rules()["turnaround"]["parallel_efficiency"] == 0.75


def test_parallel_efficiency_is_not_naive_division():
    """À 2 personnes, 0.75 → débit 1.75 (pas 2) : une charge de 6 h-homme se fait
    en ~3 h 26, pas 3 h. Une fenêtre de 3 h reste donc ROUGE (infaisable) alors
    qu'une division naïve la dirait tenable — la pire erreur possible (§2.3)."""
    cr = _rules_with_hours(parallel_efficiency=0.75)
    s = care.turnaround_signal(cr, 8, window_minutes=3 * 60)   # 3 h, charge 6
    assert s["level"] == "red"                                 # 6/1.75 = 3.43 > 3
    # Avec un rendement parfait (1.0 → débit 2), la même fenêtre passerait ambre.
    s2 = care.turnaround_signal(_rules_with_hours(parallel_efficiency=1.0), 8,
                                window_minutes=3 * 60)
    assert s2["level"] == "amber"
    assert s2["recommended_cleaners"] == 2


# ── Planning du cahier d'équipe (§2) ─────────────────────────────────────────

TODAY_P = dt.date(2026, 8, 1)


def _pb(**over):
    """Séjour pour le planning : porte un id, des heures et des bornes."""
    b = {"id": "b1", "starts_on": dt.date(2026, 8, 10),
         "ends_on": dt.date(2026, 8, 24), "nature": "reservation",
         "status": "active", "linked_booking_id": None, "guest_count": 6,
         "children_ages": [], "guest_name": "Dupont", "guest_contact": None,
         "guest_phone": "+34 600 11 22 33", "guest_email": None, "guest_lang": None,
         "checkin_time": None, "checkout_time": None, "luggage_drop_time": None}
    b.update(over)
    return b


DEF_IN, DEF_OUT = dt.time(15, 0), dt.time(10, 0)


def _planning(bookings, care_rules=None, **kw):
    return care.build_planning(bookings, care_rules or {}, DEF_IN, DEF_OUT,
                               today=TODAY_P, **kw)


def test_planning_window_between_two_occupations_is_a_rotation():
    prev = _pb(id="a", starts_on=dt.date(2026, 8, 5), ends_on=dt.date(2026, 8, 10))
    nxt = _pb(id="b", starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 17))
    plan = _planning([prev, nxt])
    win = next(e for e in plan if e["kind"] == "window" and e["booking_id"] == "b")
    assert win["same_day"] is True
    assert win["free_days"] == 0
    assert win["free_since_time"] == DEF_OUT
    assert win["window_minutes"] == 300              # 10:00 → 15:00 = 5 h


def test_planning_luggage_drop_shrinks_window_and_worsens_signal():
    """Le dépôt de bagages avance l'échéance : la fenêtre part de l'échéance la
    plus proche (§2.2) → une rotation confortable peut basculer en rouge."""
    cr = _rules_with_hours()                          # charge 6 h à pleine occup.
    prev = _pb(id="a", starts_on=dt.date(2026, 8, 5), ends_on=dt.date(2026, 8, 10))
    nxt = _pb(id="b", starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 17),
              guest_count=8, luggage_drop_time=dt.time(13, 0))
    win = next(e for e in _planning([prev, nxt], cr)
               if e["kind"] == "window" and e["booking_id"] == "b")
    assert win["window_minutes"] == 180              # 10:00 → 13:00 (bagages)
    assert win["signal"]["level"] == "red"           # 6/1.75 = 3.43 > 3


def test_planning_long_vacancy_anchors_on_arrival():
    prev = _pb(id="a", starts_on=dt.date(2025, 7, 20), ends_on=dt.date(2025, 7, 28))
    nxt = _pb(id="b", starts_on=dt.date(2026, 8, 9), ends_on=dt.date(2026, 8, 16))
    win = next(e for e in _planning([prev, nxt])
               if e["kind"] == "window" and e["booking_id"] == "b")
    assert win["same_day"] is False
    assert win["free_days"] == (dt.date(2026, 8, 9) - dt.date(2025, 7, 28)).days


def test_planning_midstay_carries_contact_and_greys_non_occupied():
    stay = _pb(id="b", starts_on=dt.date(2026, 8, 10), ends_on=dt.date(2026, 8, 24),
               guest_phone="+34 600 11 22 33", guest_email="a@b.com",
               guest_lang="en")
    works = _pb(id="w", starts_on=dt.date(2026, 8, 26), ends_on=dt.date(2026, 8, 28),
                nature="works")
    plan = _planning([stay, works])
    mid = next(e for e in plan if e["kind"] == "midstay")
    assert mid["on"] == dt.date(2026, 8, 18)          # draps J+8
    # À venir → coordonnées visibles (téléphone = action, email + langue en appui).
    assert mid["guest_phone"] == "+34 600 11 22 33"
    assert mid["guest_email"] == "a@b.com"
    assert mid["guest_lang"] == "en"
    idle = next(e for e in plan if e["kind"] == "idle")
    assert idle["nature"] == "works"                  # grisé, « rien à préparer »


def test_planning_midstay_hides_contact_for_past_stay():
    """RGPD : un séjour terminé ne divulgue plus tél/email/langue (§3.0)."""
    # `today` du planning = TODAY_P ; un séjour dont le départ est passé mais dont
    # une intervention tombe encore à l'avenir ne doit pas fuiter les coordonnées.
    show = care._show_contact(_pb(ends_on=dt.date(2025, 1, 1)), today=TODAY_P)
    assert show is False


def test_planning_excludes_past_and_cancelled_and_linked():
    past = _pb(id="p", starts_on=dt.date(2025, 1, 1), ends_on=dt.date(2025, 1, 8))
    cancelled = _pb(id="c", status="cancelled")
    linked = _pb(id="l", linked_booking_id="b")
    plan = _planning([past, cancelled, linked])
    assert all(e["booking_id"] not in ("p", "c", "l") for e in plan)


def test_planning_show_contact_gate_hides_history():
    past = _pb(ends_on=dt.date(2025, 12, 31))
    assert care._show_contact(past, today=TODAY_P) is False
    assert care._show_contact(_pb(), today=TODAY_P) is True


# ── Envoi automatique du guide à J-7 : sélection PURE (V2-23d volet 2) ────────

def _cand(**over):
    """Candidat à l'envoi auto : un séjour enrichi du contexte logement, tel que le
    produit `repo.list_auto_send_candidates`. Défauts favorables (à envoyer),
    surchargeables. `starts_on` par défaut = J-5 (dans la fenêtre)."""
    c = {"id": "b1", "property_id": "p1", "property_name": "Villa Ballarin",
         "default_lang": "fr", "published_langs": [], "cover_media_id": None,
         "auto_send_guide": True, "published": True, "already_sent": False,
         "starts_on": TODAY + dt.timedelta(days=5),
         "ends_on": TODAY + dt.timedelta(days=12),
         "nature": "reservation", "status": "active", "linked_booking_id": None,
         "guest_count": 4, "children_ages": [], "guest_name": "Tracy",
         "guest_contact": None, "guest_phone": None,
         "guest_email": "tracy@ex.com", "guest_lang": None}
    c.update(over)
    return c


def test_auto_send_selects_stay_in_window_with_email():
    plan = care.select_auto_sends([_cand()], today=TODAY)
    assert [b["id"] for b in plan.to_send] == ["b1"]
    assert plan.to_remind == []


def test_auto_send_window_boundaries_and_catchup():
    """J-0 (arrivée aujourd'hui) et J-7 (bord) sont dans la fenêtre ; J-8 (trop
    tôt) et une arrivée passée (hier) sont hors fenêtre. Le rattrapage vient du
    `<=` : tout séjour ENTRANT dans la fenêtre non servi reste candidat."""
    today0 = _cand(id="j0", starts_on=TODAY)
    j7 = _cand(id="j7", starts_on=TODAY + dt.timedelta(days=7))
    j8 = _cand(id="j8", starts_on=TODAY + dt.timedelta(days=8))
    past = _cand(id="past", starts_on=TODAY - dt.timedelta(days=1))
    plan = care.select_auto_sends([today0, j7, j8, past], today=TODAY)
    assert {b["id"] for b in plan.to_send} == {"j0", "j7"}


def test_auto_send_natures_reservation_and_private_only():
    keep = [_cand(id="res", nature="reservation"),
            _cand(id="priv", nature="private")]
    drop = [_cand(id="unq", nature="unqualified"),
            _cand(id="wrk", nature="works"),
            _cand(id="una", nature="unavailable"),
            _cand(id="canc", status="cancelled"),
            _cand(id="link", linked_booking_id="x")]
    plan = care.select_auto_sends(keep + drop, today=TODAY)
    assert {b["id"] for b in plan.to_send} == {"res", "priv"}


def test_auto_send_respects_opt_out_and_unpublished():
    plan = care.select_auto_sends(
        [_cand(id="off", auto_send_guide=False),
         _cand(id="draft", published=False)], today=TODAY)
    assert plan.to_send == [] and plan.to_remind == []


def test_auto_send_ledger_is_idempotency_lock():
    """Déjà envoyé (manuel OU auto) → jamais renvoyé : re-run = zéro double."""
    plan = care.select_auto_sends([_cand(already_sent=True)], today=TODAY)
    assert plan.to_send == [] and plan.to_remind == []


def test_auto_send_missing_email_goes_to_reminders_not_sends():
    """Éligible mais sans email → relance (sortie séparée), jamais un envoi."""
    plan = care.select_auto_sends([_cand(guest_email=None, guest_contact=None)],
                                  today=TODAY)
    assert plan.to_send == []
    assert [b["id"] for b in plan.to_remind] == ["b1"]


def test_auto_send_legacy_contact_email_counts():
    """L'email effectif accepte le legacy `guest_contact` s'il contient un '@'."""
    plan = care.select_auto_sends(
        [_cand(guest_email=None, guest_contact="legacy@ex.com")], today=TODAY)
    assert [b["id"] for b in plan.to_send] == ["b1"]


# ── Relance §0.6 : email manquant pour l'envoi automatique (V2-23d volet 2) ───

def _win(**over):
    """Séjour éligible dans la fenêtre J-7 (arrivée à J-3), sans email par défaut."""
    base = dict(starts_on=TODAY + dt.timedelta(days=3),
                ends_on=TODAY + dt.timedelta(days=7),
                guest_email=None, guest_contact=None, guest_phone="+34 600")
    base.update(over)
    return _bk(**base)


def test_missing_info_auto_send_email_only_when_enabled():
    b = _win()
    codes_off = {m["code"] for m in care.missing_info(b, {}, today=TODAY,
                                                      auto_send_guide=False)}
    codes_on = {m["code"] for m in care.missing_info(b, {}, today=TODAY,
                                                     auto_send_guide=True)}
    assert "auto_send_email_missing" not in codes_off
    assert "auto_send_email_missing" in codes_on


def test_missing_info_auto_send_absent_when_email_present():
    b = _win(guest_email="tracy@ex.com")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY,
                                                  auto_send_guide=True)}
    assert "auto_send_email_missing" not in codes


def test_missing_info_auto_send_absent_outside_window():
    b = _win(starts_on=TODAY + dt.timedelta(days=20),
             ends_on=TODAY + dt.timedelta(days=27))
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY,
                                                  auto_send_guide=True)}
    assert "auto_send_email_missing" not in codes


def test_missing_info_auto_send_absent_for_unqualified_nature():
    b = _win(nature="unqualified")
    codes = {m["code"] for m in care.missing_info(b, {}, today=TODAY,
                                                  auto_send_guide=True)}
    assert "auto_send_email_missing" not in codes
