"""Tests du fil des 7 étapes (V2-31, volet 2) — `api.journey`.

Module **pur** (aucun réseau, aucune base) → testé directement. Le cœur de
l'audit : les états se calculent sur la **SUBSTANCE**, jamais sur la bascule
déclarative « Section complétée ». Le cas Villa Ballarin (publiée + servie) est
le test ANTI-MENSONGE : le fil doit l'annoncer au-delà de l'étape 6.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from api import journey  # noqa: E402


def _prop(**over):
    p = {"id": "p1", "lat": None, "lon": None, "status": "draft",
         "contact_phone": None, "contact_whatsapp": None, "contact_email": None,
         "cover_media_id": None, "default_checkin_time": None,
         "default_checkout_time": None}
    p.update(over)
    return p


def _run(prop, *, sections=(), keybox=False, wifi=False, pois=None, sends=0):
    return journey.compute(prop, sections=list(sections), keybox_present=keybox,
                           wifi_present=wifi, poi_counts=pois or {}, sends=sends)


def _state(j, n):
    return next(s for s in j["steps"] if s["n"] == n)["state"]


# ── Le fil avance étape par étape (les scénarios de la mission) ───────────────

def test_vierge_etape1_courante():
    """Compte vierge : rien n'est fait → étape 1 courante, aucune action au-delà."""
    j = _run(_prop())
    assert j["current_step"] == 1
    assert j["next_action"]["route"] == "#/properties/p1/editor"
    assert _state(j, 1) == "current"
    assert j["done_count"] == 0
    assert j["sent"] is False


def test_adresse_et_cover_font_etape2():
    """Adresse géocodée + photo de couverture → étape 1 faite, étape 2 courante."""
    j = _run(_prop(lat=38.2, cover_media_id="m1"))
    assert _state(j, 1) == "done"
    assert j["current_step"] == 2


def test_contact_seul_suffit_a_etape1():
    """Le contact voyageur SUFFIT (photo de couverture OU contact), pas les deux."""
    j = _run(_prop(lat=38.2, contact_email="h@ex.com"))
    assert _state(j, 1) == "done"


def test_adresse_sans_contact_ni_cover_bloque_etape1():
    j = _run(_prop(lat=38.2))
    assert _state(j, 1) == "current"
    assert any("contact" in m.lower() for m in
               next(s for s in j["steps"] if s["n"] == 1)["missing"])


def test_secrets_poses_font_etape3():
    """Check-in + accès/boîte à clés + wifi renseignés → étape 2 faite, étape 3
    courante. Les secrets sont jugés par présence (jamais déchiffrés)."""
    p = _prop(lat=38.2, contact_email="h@ex.com", default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=True)
    assert _state(j, 2) == "done"
    assert j["current_step"] == 3


def test_etape2_manque_wifi_message_humain():
    p = _prop(lat=38.2, contact_email="h@ex.com", default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=False)
    assert _state(j, 2) == "current"
    msgs = next(s for s in j["steps"] if s["n"] == 2)["missing"]
    assert any("wifi" in m.lower() for m in msgs)


def test_checkin_par_contenu_de_section():
    """L'arrivée peut être attestée par le CONTENU de la section (pas seulement
    l'heure standard) — c'est la substance qui compte."""
    p = _prop(lat=38.2, contact_email="h@ex.com")
    secs = [{"code": "A_checkin", "content": {"instructions": "Sonnez au 2e."},
             "body_md": None}]
    j = _run(p, sections=secs, keybox=True, wifi=True)
    assert _state(j, 2) == "done"


def test_pois_produits_font_etape3_puis_validation():
    """Des lieux existent (tout statut) → étape 3 faite ; tant qu'il reste des
    suggestions, l'étape 4 est « en cours (N à examiner) »."""
    p = _prop(lat=38.2, contact_email="h@ex.com", default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=True, pois={"suggested": 12, "approved": 3})
    assert _state(j, 3) == "done"
    assert j["current_step"] == 4
    step4 = next(s for s in j["steps"] if s["n"] == 4)
    assert step4["detail"] == "12 à examiner"


def test_validation_terminee_quand_zero_en_attente():
    p = _prop(lat=38.2, contact_email="h@ex.com", default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=True, pois={"approved": 30, "rejected": 5})
    assert _state(j, 4) == "done"
    assert j["current_step"] == 6   # É5 sautée → publier


def test_publie_mais_pas_envoye_etape7_courante():
    p = _prop(lat=38.2, contact_email="h@ex.com", status="published",
              default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=True, pois={"approved": 10})
    assert _state(j, 6) == "done"
    assert j["current_step"] == 7


def test_tout_fait_et_envoye_guide_envoye():
    """Tout validé + publié + envoyé → plus d'étape courante, `sent` vrai
    (« Guide envoyé ✓ »), progression 7/7."""
    p = _prop(lat=38.2, contact_email="h@ex.com", status="published",
              default_checkin_time=dt.time(15, 0))
    j = _run(p, keybox=True, wifi=True, pois={"approved": 10}, sends=1)
    assert j["current_step"] is None
    assert j["next_action"] is None
    assert j["sent"] is True
    assert j["done_count"] == 7
    assert _state(j, 7) == "done"


# ── L'étape 5 n'est JAMAIS un état binaire ───────────────────────────────────

def test_etape5_toujours_optionnelle():
    """Quel que soit l'avancement, l'étape 5 reste 'optional' (jamais done/todo)
    et n'entre pas dans le choix de l'étape courante."""
    for j in (_run(_prop()),
              _run(_prop(lat=38.2, contact_email="h@ex.com", status="published"),
                   keybox=True, wifi=True, pois={"approved": 3}, sends=1)):
        assert _state(j, 5) == "optional"
        assert j["current_step"] != 5


def test_etape5_compte_les_rubriques_facultatives_garnies():
    p = _prop(lat=38.2, contact_email="h@ex.com")
    secs = [
        {"code": "A_checkin", "content": {"x": "arrivée"}, "body_md": None},  # essentielle → hors compte
        {"code": "G_beaches", "content": {}, "body_md": "La plage à 200 m."},  # facultative garnie
        {"code": "F_restaurants", "content": {"tip": "Chez Paco"}, "body_md": None},  # facultative garnie
        {"code": "E_taxi", "content": {}, "body_md": None},  # vide → non comptée
    ]
    j = _run(p, sections=secs, keybox=True, wifi=True)
    step5 = next(s for s in j["steps"] if s["n"] == 5)
    assert step5["detail"] == "2 rubrique(s) complétée(s)"


# ── Substance vs déclaration : le mensonge que l'audit dénonce ────────────────

def test_section_vide_ne_compte_pas_meme_marquee_completee():
    """Une section SANS contenu ne fait jamais avancer le fil, quoi qu'en dise la
    bascule déclarative (dont `journey` ne reçoit même pas la valeur)."""
    p = _prop(lat=38.2, contact_email="h@ex.com", default_checkin_time=dt.time(15, 0))
    secs = [{"code": "A_access", "content": {}, "body_md": ""}]   # « complétée » mais vide
    j = _run(p, sections=secs, keybox=False, wifi=True)   # ni code ni contenu d'accès
    assert _state(j, 2) == "current"   # accès non renseigné → étape 2 pas faite


def test_ballarin_publie_et_servi_est_au_dela_de_letape6():
    """CAS ANTI-MENSONGE (Villa Ballarin réelle : publiée, validée, servie —
    l'ancien indicateur affichait pourtant 4 %). Avec ses données, le fil DOIT
    l'annoncer au-delà de l'étape 6 (jamais « étape 1 »)."""
    p = _prop(lat=38.29, lon=-0.55, status="published", contact_phone="+34 600 11 22 33",
              contact_email="ballarin@ex.com", cover_media_id="cover1",
              default_checkin_time=dt.time(16, 0), default_checkout_time=dt.time(10, 0))
    secs = [
        {"code": "A_checkin", "content": {"i": "Digicode 1234"}, "body_md": None},
        {"code": "A_access", "content": {"i": "3e étage"}, "body_md": None},
        {"code": "G_beaches", "content": {}, "body_md": "Playa Flamenca à 5 min."},
    ]
    j = _run(p, sections=secs, keybox=True, wifi=True,
             pois={"approved": 80, "edited": 12, "rejected": 20}, sends=1)
    assert j["current_step"] is None and j["sent"] is True
    assert j["done_count"] == 7
    assert _state(j, 1) == "done" and _state(j, 6) == "done"
