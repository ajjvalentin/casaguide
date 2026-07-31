"""Moteur d'interventions d'entretien — **fonction pure** (V2-23b, volet 1).

Le cahier d'équipe répond à trois questions : *depuis quand la maison est libre*,
*pour quand elle doit être prête*, *quoi faire*. Ce module calcule le « quoi » —
et il ne stocke **rien** : les interventions sont **calculées** à partir d'un
séjour (nature, dates, voyageurs) et des `care_rules` du logement, exactement
comme les fenêtres et les chevauchements du calendrier. Aucun accès base, aucun
réseau, aucune horloge implicite (`today` est toujours passé) → entièrement
testable.

Deux principes structurants de la mission :

- **La nature pilote la préparation, jamais le statut** (invariant maison) :
  `reservation` → tout ; `private` → même entretien **sans** welcome pack ni
  prestation commerciale ; `works`/`unavailable` → aucune intervention
  automatique ; `unqualified` → à qualifier (aucune intervention, signalée).

- **Toute sortie est QUANTIFIÉE** à partir du nombre de voyageurs (§1.0). Une
  quantité inconnue s'affiche explicitement (« nombre de voyageurs non
  renseigné ») plutôt que de disparaître en silence : l'équipe doit voir le trou,
  pas le deviner sur place. Dans le doute (`guest_count` NULL), on prend
  l'hypothèse **pessimiste** (pleine occupation) pour l'effort de rotation.

Les valeurs d'effort de rotation en **hommes-heures** vivent dans `care_rules`
(jamais en dur — invariant 8) ; elles sont laissées à `null` par défaut tant
qu'André ne les a pas fournies → le calcul de fenêtre renvoie alors un signal
« non configuré » plutôt qu'une valeur inventée.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

# ── Défauts applicatifs (posés à la CRÉATION du logement, jamais figés en base) ──
#
# Ce sont les valeurs proposées à la création — celles de Villa Ballarin (draps
# inclus dès le 8e jour ; ménage en cours de séjour sur demande). Le propriétaire
# les ajuste ensuite librement ; la vérité vit dans `properties.care_rules`.

# Catalogue de demandes amorcé à la création (§1.2). Les `code` servent de cible
# aux suggestions par tranche d'âge (`age_bands[].suggests`).
DEFAULT_REQUEST_TYPES: list[dict] = [
    {"code": "lit_bebe", "label": "Lit bébé"},
    {"code": "chaise_haute", "label": "Chaise haute"},
    {"code": "parasol", "label": "Parasol"},
    {"code": "lit_appoint", "label": "Lit d'appoint"},
]


def default_care_rules() -> dict:
    """Règles d'entretien par défaut à la création d'un logement (§1.1).

    Les hommes-heures de rotation sont **délibérément à `null`** (à obtenir
    d'André, jamais inventés). `full_occupancy_guests` idem : sans la capacité du
    logement, aucune interpolation n'est possible → le signal de fenêtre reste
    « non configuré » plutôt que faux."""
    return {
        "linen_change_from_day": 8,          # draps changés tous les N jours en cours de séjour
        "midstay_cleaning": "on_request",    # 'included' | 'on_request' | 'none'
        "welcome_pack": "free",              # 'free' | 'paid' | 'none'
        "welcome_pack_note": "",
        "welcome_pack_media_id": None,
        # Tranches d'âge des enfants → équipement SUGGÉRÉ (jamais ajouté d'office).
        "age_bands": [
            {"code": "baby", "max_age": 2, "suggests": ["lit_bebe", "chaise_haute"],
             "counts_as_adult": False},
            {"code": "child", "max_age": 12, "suggests": ["lit_appoint"],
             "counts_as_adult": False},
            {"code": "teen", "max_age": None, "suggests": [],
             "counts_as_adult": True},
        ],
        # Rotation : la CHARGE (hommes-heures) suit l'occupation ; la RESSOURCE
        # (nb de personnes) est une décision d'équipe. Voir turnaround_signal.
        "turnaround": {
            "person_hours_min_occupancy": None,   # ⚠ à obtenir d'André
            "person_hours_full_occupancy": None,  # ⚠ à obtenir d'André
            "min_occupancy_guests": None,         # borne basse de l'interpolation
            "full_occupancy_guests": None,        # capacité du logement (borne haute)
            "max_cleaners": 2,
            "comfort_margin_hours": 1,
        },
    }


def merged_rules(rules: dict | None) -> dict:
    """Fusionne les `care_rules` stockées avec les défauts (tout champ absent
    reprend son défaut). Sûr sur `{}` ou `None` : un logement créé avant la
    migration se comporte comme s'il avait les défauts."""
    base = default_care_rules()
    if not rules:
        return base
    out = {**base, **rules}
    # Fusion peu profonde du sous-objet turnaround (les autres sont remplacés en bloc).
    out["turnaround"] = {**base["turnaround"], **(rules.get("turnaround") or {})}
    if not out.get("age_bands"):
        out["age_bands"] = base["age_bands"]
    return out


# ── Voyageurs & tranches d'âge (§1.0) ────────────────────────────────────────

def children_count(booking: dict) -> int:
    """Nombre d'enfants = longueur de `children_ages` (jamais une colonne
    redondante → pas d'incohérence possible)."""
    return len(booking.get("children_ages") or [])


def age_band(age: int, age_bands: list[dict]) -> dict | None:
    """Tranche d'âge d'un enfant (âge À L'ARRIVÉE). La première bande dont
    `max_age` est `None` (fourre-tout) ou `age <= max_age`, bandes lues dans
    l'ordre croissant."""
    for band in age_bands:
        mx = band.get("max_age")
        if mx is None or age <= mx:
            return band
    return None


def suggest_equipment(children_ages, care_rules: dict) -> list[str]:
    """Codes d'équipement **suggérés** par les âges des enfants (§1.0).

    SUGGÈRE, n'ajoute jamais d'office : la modale propose, le propriétaire
    confirme (un bébé dont les parents ont leur propre lit parapluie ne doit pas
    générer de préparation inutile). Renvoie des `code` du catalogue, sans
    doublon, dans l'ordre de première apparition."""
    rules = merged_rules(care_rules)
    bands = rules["age_bands"]
    out: list[str] = []
    for age in (children_ages or []):
        band = age_band(int(age), bands)
        if not band:
            continue
        for code in band.get("suggests", []):
            if code not in out:
                out.append(code)
    return out


# ── Interventions (§1.3) ─────────────────────────────────────────────────────

# Natures qui déclenchent la préparation par l'équipe (« occupé » = quelqu'un dort
# ici). `private` = même entretien que `reservation`, sans welcome pack.
_PREPARED_NATURES = ("reservation", "private")


@dataclass
class Intervention:
    """Une visite d'entretien datée. `needs_appointment` = la maison est
    **habitée** (intervention en cours de séjour → rendez-vous, donc nom + tél du
    locataire nécessaires : c'est ce qui fonde « le staff voit tout »)."""
    kind: str                       # 'arrival' | 'linen_change' | 'midstay_cleaning'
    on: _dt.date
    label: str                      # titre court FR
    tasks: list[str] = field(default_factory=list)   # sous-tâches quantifiées
    needs_appointment: bool = False

    def as_dict(self) -> dict:
        return {"kind": self.kind, "on": self.on, "label": self.label,
                "tasks": list(self.tasks), "needs_appointment": self.needs_appointment}


def _qty(base: str, guest_count: int | None) -> str:
    """« Draps » → « Draps pour 6 », ou « Draps (nombre de voyageurs non
    renseigné) » — jamais un trou silencieux."""
    if guest_count is None:
        return f"{base} (nombre de voyageurs non renseigné)"
    return f"{base} pour {guest_count}"


def stay_nights(booking: dict) -> int:
    """Nombre de nuits d'un séjour (intervalle semi-ouvert [arrivée, départ))."""
    return (booking["ends_on"] - booking["starts_on"]).days


def _linen_change_dates(booking: dict, from_day: int) -> list[_dt.date]:
    """Dates de changement de draps en cours de séjour : tous les `from_day`
    jours à partir de l'arrivée, tant qu'on reste **avant** le départ. Un séjour
    de 14 nuits (from_day=8) → J+8 seul (J+16 dépasse) ; un séjour de 5 nuits →
    aucun. Un `from_day` nul/négatif désactive la règle."""
    if not from_day or from_day <= 0:
        return []
    out: list[_dt.date] = []
    day = from_day
    nights = stay_nights(booking)
    while day < nights:
        out.append(booking["starts_on"] + _dt.timedelta(days=day))
        day += from_day
    return out


def plan_interventions(booking: dict, care_rules: dict, *,
                       requests: list[dict] | None = None) -> list[Intervention]:
    """Liste des interventions d'un séjour, datées et **quantifiées**.

    Rien n'est stocké. Règles par nature : `reservation` → tout ; `private` →
    même entretien sans welcome pack ni prestation commerciale ; `works` /
    `unavailable` / `unqualified` → aucune intervention automatique.

    `requests` = demandes particulières **acceptées** du séjour (lit bébé, ménage
    supplémentaire…) : elles rejoignent la préparation d'arrivée. L'équipement
    n'apparaît **jamais** d'office par l'âge (§1.0) — seulement via une demande
    confirmée par le propriétaire.
    """
    rules = merged_rules(care_rules)
    nature = booking.get("nature")
    if nature not in _PREPARED_NATURES:
        return []

    gc = booking.get("guest_count")
    is_reservation = nature == "reservation"
    accepted = [r for r in (requests or []) if r.get("status") == "accepted"]

    out: list[Intervention] = []

    # 1. Préparation d'arrivée (le logement est vide : pas de rendez-vous).
    arrival_tasks = ["Ménage complet",
                     _qty("Draps", gc),
                     _qty("Jeux de serviettes", gc)]
    if is_reservation and rules.get("welcome_pack") in ("free", "paid"):
        arrival_tasks.append(_qty("Welcome pack", gc))
    for r in accepted:
        qn = r.get("quantity") or 1
        lbl = r.get("label") or r.get("code") or "Demande"
        arrival_tasks.append(f"{lbl}{f' ×{qn}' if qn > 1 else ''}")
    out.append(Intervention(kind="arrival", on=booking["starts_on"],
                            label="Préparation d'arrivée", tasks=arrival_tasks,
                            needs_appointment=False))

    # 2. Changements de draps en cours de séjour (maison habitée → rendez-vous).
    for d in _linen_change_dates(booking, rules.get("linen_change_from_day", 0)):
        out.append(Intervention(
            kind="linen_change", on=d, label="Changement de draps",
            tasks=[_qty("Changement de draps", gc)], needs_appointment=True))

    # 3. Ménage en cours de séjour s'il est inclus (habitée → rendez-vous). Le
    #    ménage « sur demande » n'apparaît que via une demande acceptée (ci-dessus,
    #    en préparation d'arrivée si daté à l'arrivée ; sinon porté par la demande).
    if rules.get("midstay_cleaning") == "included":
        for d in _linen_change_dates(booking, rules.get("linen_change_from_day", 0)):
            out.append(Intervention(
                kind="midstay_cleaning", on=d, label="Ménage en cours de séjour",
                tasks=["Ménage complet"], needs_appointment=True))

    out.sort(key=lambda i: (i.on, 0 if i.kind == "arrival" else 1))
    return out


# ── Relance active : informations manquantes (§0.6 / §1.0) ───────────────────

def missing_info(booking: dict, care_rules: dict, *,
                 today: _dt.date) -> list[dict]:
    """Signaux de relance pour le calendrier du propriétaire (§0.6).

    Un séjour opérationnellement pertinent (occupé, actif, non rattaché, pas
    terminé) mais incomplet est signalé plutôt que de disparaître : nature à
    qualifier, nombre de voyageurs manquant, et — si sa durée déclenche une
    intervention en cours de séjour — coordonnées manquantes (le flux iCal ne
    transporte jamais le téléphone, or une intervention suppose un rendez-vous).

    Chaque signal : `{code, message}`. Renvoie `[]` s'il n'y a rien à relancer."""
    # Cycle de vie : un séjour annulé ou déjà terminé ne se relance pas.
    if booking.get("status") == "cancelled":
        return []
    if booking.get("linked_booking_id") is not None:
        return []
    if booking["ends_on"] < today:
        return []

    nature = booking.get("nature")
    out: list[dict] = []

    # Nature à qualifier : l'import n'a pas donné de sémantique.
    if nature == "unqualified":
        out.append({"code": "unqualified",
                    "message": "Séjour à qualifier (réservation, privé, travaux…)."})
        return out   # rien d'autre à relancer tant que la nature est inconnue

    # Au-delà, seuls les séjours PRÉPARÉS (occupés) déclenchent des besoins.
    if nature not in _PREPARED_NATURES:
        return out

    rules = merged_rules(care_rules)

    if booking.get("guest_count") is None:
        out.append({"code": "guest_count_missing",
                    "message": "Nombre de voyageurs non renseigné."})

    # Coordonnées manquantes SEULEMENT si une intervention en cours de séjour est
    # prévue (elle suppose un rendez-vous avec le locataire).
    linen_dates = _linen_change_dates(booking, rules.get("linen_change_from_day", 0))
    if linen_dates and not (booking.get("guest_contact") or "").strip():
        first = linen_dates[0]
        day = (first - booking["starts_on"]).days
        out.append({
            "code": "contact_missing",
            "message": (f"Séjour de {stay_nights(booking)} jours · "
                        f"draps à J+{day} · coordonnées manquantes.")})

    return out


# ── Fenêtre de rotation : signal & recommandation d'effectif (§1.1) ──────────

def turnaround_person_hours(care_rules: dict, guest_count: int | None) -> float | None:
    """Charge de travail d'une rotation en **hommes-heures**, interpolée selon le
    nombre de voyageurs entre faible et pleine occupation.

    Renvoie `None` si les valeurs d'effort ne sont pas configurées (laissées à
    `null` tant qu'André ne les a pas données — jamais inventées). Quand
    `guest_count` est inconnu, on prend la charge **pessimiste** (pleine
    occupation) — même principe de prudence que le repli du §0."""
    t = merged_rules(care_rules)["turnaround"]
    ph_min = t.get("person_hours_min_occupancy")
    ph_full = t.get("person_hours_full_occupancy")
    if ph_full is None:
        return None                      # non configuré : aucun signal inventé
    if ph_min is None:
        return float(ph_full)            # une seule valeur → charge constante
    if guest_count is None:
        return float(ph_full)            # pessimiste

    g_min = t.get("min_occupancy_guests")
    g_full = t.get("full_occupancy_guests")
    if not g_min or not g_full or g_full <= g_min:
        return float(ph_full)            # bornes d'occupation absentes → pessimiste
    # Interpolation linéaire, bornée aux extrémités.
    frac = (guest_count - g_min) / (g_full - g_min)
    frac = max(0.0, min(1.0, frac))
    return float(ph_min) + frac * (float(ph_full) - float(ph_min))


def turnaround_signal(care_rules: dict, guest_count: int | None,
                      window_minutes: int) -> dict:
    """Signal de fenêtre de rotation (§1.1) : une fenêtre serrée n'est pas une
    fatalité, c'est une **décision d'équipe** — l'alerte dit quoi faire.

    Renvoie `{level, person_hours, recommended_cleaners, window_hours, message}` :

    | fenêtre disponible | level | message |
    |---|---|---|
    | ≥ charge à 1 pers. + marge | `neutral` | rien à signaler |
    | < charge à 1 pers., tenable à plusieurs | `amber` | « prévoir K personnes » |
    | < charge même avec `max_cleaners` | `red` | « infaisable, même à K » |

    `level='unknown'` quand l'effort n'est pas configuré (person-hours `null`) :
    on ne signale rien plutôt que d'inventer. Le triangle (`red`) reste réservé
    au danger réel (l'autre étant le chevauchement de deux occupations)."""
    t = merged_rules(care_rules)["turnaround"]
    max_cleaners = max(1, int(t.get("max_cleaners") or 1))
    margin_h = float(t.get("comfort_margin_hours") or 0)
    window_h = window_minutes / 60.0
    ph = turnaround_person_hours(care_rules, guest_count)

    if ph is None:
        return {"level": "unknown", "person_hours": None,
                "recommended_cleaners": None, "window_hours": round(window_h, 2),
                "message": "Effort de rotation non configuré."}

    # Effectif minimal tenant dans la fenêtre : durée écoulée ≈ charge / effectif.
    recommended = None
    for k in range(1, max_cleaners + 1):
        if ph / k <= window_h:
            recommended = k
            break

    if window_h >= ph + margin_h:
        level, msg = "neutral", "Fenêtre confortable."
        recommended = recommended or 1
    elif recommended is not None:
        level = "amber"
        msg = (f"Prévoir {recommended} personne"
               f"{'s' if recommended > 1 else ''} sur cette rotation.")
    else:
        level = "red"
        msg = f"Rotation infaisable, même à {max_cleaners}."

    return {"level": level, "person_hours": round(ph, 2),
            "recommended_cleaners": recommended,
            "window_hours": round(window_h, 2), "message": msg}
