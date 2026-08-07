"""Le fil des 7 étapes — **fonction pure** (V2-31, volet 2).

Chaque logement sait où il en est dans la CHAÎNE cible en 7 étapes
(`docs/audit_ux.md` §2) et le dit : un état par étape + l'action suivante. Ce
module calcule cet état — et il ne stocke **rien**, comme `api.care` : tout se
déduit de l'existant (adresse, sections, secrets, POI, envois). Aucun accès base,
aucun réseau → entièrement testable.

**Règle cardinale (le cœur de l'audit).** Les états se calculent sur la
**SUBSTANCE**, jamais sur la bascule déclarative « Section complétée » — c'est
elle qui produisait le pourcentage MENSONGER de l'étape 2 (Villa Ballarin,
publiée et servie, affichait 4 %). Une rubrique « complétée » sans contenu ne
compte pas ; une rubrique remplie compte, cochée ou non. Le fil mesure ce qui
est là, pas ce qui est déclaré.

Le parcours a 7 étapes mais **6 jalons de progression** : l'étape 5 (« le
reste, à votre rythme ») n'est jamais un état binaire — on ne culpabilise pas
sur le facultatif. Elle affiche un simple compte informatif de rubriques garnies
et n'entre pas dans le calcul de l'« étape courante » ; par choix assumé elle est
réputée « faite » dès que l'étape 6 (publication) l'est, pour rester dans le
vocabulaire « k/7 » du parcours.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# ── Sections essentielles (impératif du parcours, audit §2) ───────────────────
#
# Codes des sections VITALES pour le voyageur. Elles portent le badge « Essentiel »
# dans l'éditeur (barre latérale) ; l'absence de badge dit « à votre rythme ».
# Dupliqué côté front (`editor.js ESSENTIAL_CODES`) — même règle des deux côtés,
# à garder alignée (comme la règle d'intervalle du calendrier).
ESSENTIAL_CODES: tuple[str, ...] = (
    "A_checkin", "A_checkout", "A_keybox", "A_access", "B_wifi")

# Sous-ensemble qui atteste l'étape 2. Le check-out (`A_checkout`) est essentiel
# mais n'est pas EXIGÉ pour « prêt à publier » : utile, jamais bloquant.
_CHECKIN_CODE = "A_checkin"
_ACCESS_CODES = ("A_access", "A_keybox")

# La séquence des jalons de progression (l'étape 5 en est absente — elle ne se
# « fait » pas). Chaque entrée : (n, clé, titre, route de l'action, libellé de
# l'action). Les libellés reprennent le vocabulaire des « Premiers pas » (volet
# 3b) — même vocabulaire partout. Les routes sont des ancres du back-office
# (`:pid` = ce logement) ; celles qui vivent sur la carte (recherche, envoi)
# pointent vers « Mes logements ».
_SEQUENCE = (
    (1, "logement", "Votre logement", "{base}/editor", "Renseigner votre logement"),
    (2, "indispensables", "Les indispensables", "{base}/editor", "Ajouter les indispensables"),
    (3, "recherche", "Holaguia cherche les lieux", "#/properties", "Rechercher les lieux"),
    (4, "validation", "Vous validez", "{base}/pois", "Valider vos lieux suggérés"),
    (6, "publier", "Publier et tester", "{base}/editor", "Publier votre guide"),
    (7, "envoyer", "Envoyer", "#/properties", "Envoyer le guide"),
)
TOTAL = 7


def _nonempty(v: Any) -> bool:
    """Une valeur scalaire « renseignée » : non nulle, et non vide si texte.
    Tolère les objets `time`/`date`/nombres (non nuls → renseignés)."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def _value_filled(v: Any) -> bool:
    """Un champ de `content` porte-t-il une vraie saisie ? (jamais un booléen
    décoché ni une chaîne blanche)."""
    if v is None or v is False:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return True


def section_has_substance(content: Any, body_md: Any) -> bool:
    """Une section est « garnie » si elle porte un contenu RÉEL — c'est la mesure
    de substance qui remplace la bascule déclarative partout (éditeur compris)."""
    if _nonempty(body_md):
        return True
    if isinstance(content, Mapping):
        return any(_value_filled(v) for v in content.values())
    return False


def compute(prop: Mapping[str, Any], *, sections: Sequence[Mapping[str, Any]],
            keybox_present: bool, wifi_present: bool,
            poi_counts: Mapping[str, int], sends: int) -> dict:
    """Calcule le fil des étapes pour un logement.

    Paramètres (tous des FAITS déjà rassemblés — aucune requête ici) :
      · `prop` : la ligne du logement (lat, contacts, cover_media_id, statut,
        heures d'arrivée) ;
      · `sections` : les sections voyageur existantes `{code, content, body_md}` ;
      · `keybox_present`/`wifi_present` : présence des secrets (IS NOT NULL —
        jamais déchiffrés, invariant 5) ;
      · `poi_counts` : décompte des POI par statut ;
      · `sends` : nombre d'envois du guide déjà tracés (tout kind/origin).
    """
    substance = {s["code"]: section_has_substance(s.get("content"), s.get("body_md"))
                 for s in sections}
    base = f"#/properties/{prop.get('id', '')}"

    # ── Critères, calculés sur la SUBSTANCE (définition produit de « où j'en suis ») ──

    # É1 — Votre logement : adresse géocodée ET (un contact voyageur OU une
    # photo de couverture). C'est l'identité + l'image de vente du logement.
    geocoded = prop.get("lat") is not None
    has_contact = any(_nonempty(prop.get(k)) for k in
                      ("contact_phone", "contact_whatsapp", "contact_email"))
    has_cover = prop.get("cover_media_id") is not None
    s1_done = geocoded and (has_contact or has_cover)
    s1_missing: list[str] = []
    if not geocoded:
        s1_missing.append("L'adresse n'est pas encore localisée sur la carte.")
    if not (has_contact or has_cover):
        s1_missing.append("Aucun contact voyageur ni photo de couverture.")

    # É2 — Les indispensables : arrivée renseignée ET accès/boîte à clés ET wifi.
    # « Renseigné » = contenu non vide (sections) ou secret posé (IS NOT NULL).
    checkin_ok = substance.get(_CHECKIN_CODE, False) or _nonempty(prop.get("default_checkin_time"))
    access_ok = keybox_present or any(substance.get(c, False) for c in _ACCESS_CODES)
    wifi_ok = bool(wifi_present)
    s2_done = checkin_ok and access_ok and wifi_ok
    s2_missing = []
    if not checkin_ok:
        s2_missing.append("L'heure ou les consignes d'arrivée ne sont pas renseignées.")
    if not access_ok:
        s2_missing.append("L'accès au logement (emplacement ou code de la boîte à clés) manque.")
    if not wifi_ok:
        s2_missing.append("Le wifi n'est pas renseigné — votre voyageur le cherchera.")

    # É3 — Holaguia cherche : un enrichissement a produit des lieux (tout statut).
    pois_total = sum(poi_counts.values())
    s3_done = pois_total > 0

    # É4 — Vous validez : au moins un lieu retenu (approved/edited/owner) ET zéro
    # en attente ; « en cours » tant que des suggestions attendent l'arbitrage.
    pending = poi_counts.get("suggested", 0)
    retained = poi_counts.get("approved", 0) + poi_counts.get("edited", 0)
    s4_done = retained >= 1 and pending == 0

    # É6 — Publier : le guide est publié.
    s6_done = prop.get("status") == "published"

    # É7 — Envoyer : au moins un envoi tracé (fenêtre, J-7, email…).
    s7_done = sends >= 1

    # É5 — Le reste, à votre rythme : compte informatif des rubriques FACULTATIVES
    # garnies (jamais un état binaire, jamais dans le « k/7 »).
    optional_filled = sum(1 for s in sections
                          if s["code"] not in ESSENTIAL_CODES and substance.get(s["code"]))

    done = {1: s1_done, 2: s2_done, 3: s3_done, 4: s4_done, 6: s6_done, 7: s7_done}
    missing = {1: s1_missing, 2: s2_missing}
    details = {}
    if pending:
        details[4] = f"{pending} à examiner"

    # L'« étape courante » = la première non faite de la séquence (É5 exclue).
    current = next((n for n, *_ in _SEQUENCE if not done[n]), None)

    steps: list[dict] = []
    for n, key, title, route_tpl, label in _SEQUENCE:
        route = route_tpl.format(base=base)
        state = "done" if done[n] else ("current" if n == current else "todo")
        steps.append({
            "n": n, "key": key, "title": title, "state": state,
            "detail": details.get(n), "route": route, "label": label,
            "missing": missing.get(n, []),
        })
        if n == 4:
            # É5 s'insère APRÈS la validation, avant la publication (ordre du parcours).
            steps.append({
                "n": 5, "key": "reste", "title": "Le reste, à votre rythme",
                "state": "optional", "route": f"{base}/editor",
                "label": "Compléter le reste du guide",
                "detail": (f"{optional_filled} rubrique(s) complétée(s)"
                           if optional_filled else "quand vous voulez"),
                "missing": [],
            })

    # Progression affichée en « k/7 » (audit) mais calculée sur 6 jalons : l'É5
    # compte comme faite dès que l'É6 l'est (choix assumé — on ne jauge pas le
    # facultatif).
    done_count = sum(1 for v in done.values() if v) + (1 if s6_done else 0)

    next_action = None
    if current is not None:
        cur = next(s for s in steps if s["n"] == current)
        next_action = {"route": cur["route"], "label": cur["label"]}

    return {
        "steps": steps,
        "current_step": current,
        "next_action": next_action,
        "done_count": done_count,
        "total": TOTAL,
        "sent": s7_done,
    }
