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
            # Le travail à plusieurs n'est PAS parfaitement divisible (§2.3) : on se
            # coordonne, certaines tâches ne se partagent pas. La 2e personne (et les
            # suivantes) ne rend qu'une fraction d'un plein temps → `parallel_efficiency`.
            # 0.75 : mesure André pour Villa Ballarin (~6 h à 1, ~4 h à 2). Diviser
            # naïvement par le nombre de personnes promettrait des rotations
            # intenables — la pire erreur pour un outil censé rassurer.
            "parallel_efficiency": 0.75,
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


def _looks_like_phone(value: str | None) -> bool:
    """Heuristique : au moins six chiffres, pas d'arobase (miroir du backfill de
    la migration 018). Sert à repérer un téléphone dans le legacy `guest_contact`."""
    if not value or "@" in value:
        return False
    return sum(c.isdigit() for c in value) >= 6


def effective_phone(b: dict) -> str | None:
    """Téléphone effectif du locataire (§3.0) : `guest_phone` si renseigné, sinon
    le legacy `guest_contact` **s'il ressemble à un téléphone** (repli tant que la
    migration/saisie n'a pas séparé les champs). None si aucun téléphone."""
    phone = (b.get("guest_phone") or "").strip()
    if phone:
        return phone
    legacy = (b.get("guest_contact") or "").strip()
    return legacy if _looks_like_phone(legacy) else None


def effective_email(b: dict) -> str | None:
    """Email effectif du locataire (§3.0) : `guest_email` si renseigné, sinon le
    legacy `guest_contact` s'il contient un '@'. None sinon."""
    email = (b.get("guest_email") or "").strip()
    if email:
        return email
    legacy = (b.get("guest_contact") or "").strip()
    return legacy if "@" in legacy else None


def _is_occupied(b: dict) -> bool:
    """Un séjour occupe le logement (quelqu'un y dort → préparation) : nature
    préparée (réservation/privé), cycle de vie actif, non rattaché à un autre
    séjour. Miroir de `calendars._is_occupied` (même règle, gardée alignée) — défini
    ici pour que le moteur reste pur et sans dépendance au module de synchro."""
    return (b.get("status") == "active"
            and b.get("nature") in _PREPARED_NATURES
            and b.get("linked_booking_id") is None)


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
                 today: _dt.date, auto_send_guide: bool = False) -> list[dict]:
    """Signaux de relance pour le calendrier du propriétaire (§0.6).

    Un séjour opérationnellement pertinent (occupé, actif, non rattaché, pas
    terminé) mais incomplet est signalé plutôt que de disparaître : nature à
    qualifier, nombre de voyageurs manquant, et — si sa durée déclenche une
    intervention en cours de séjour — coordonnées manquantes (le flux iCal ne
    transporte jamais le téléphone, or une intervention suppose un rendez-vous).

    `auto_send_guide` : quand le logement a l'envoi automatique du guide activé
    (V2-23d volet 2), un séjour éligible qui entre dans la fenêtre J-7 **sans
    email** ne peut pas être servi automatiquement → on le signale ici (pastille
    calendrier), plutôt qu'un email de relance séparé.

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

    # Téléphone manquant SEULEMENT si une intervention en cours de séjour est
    # prévue (elle suppose un rendez-vous, calé par appel/WhatsApp — un email ne
    # permet pas de convenir d'une heure le jour même, §3.0). C'est donc le
    # TÉLÉPHONE qui manque, pas « les coordonnées ».
    linen_dates = _linen_change_dates(booking, rules.get("linen_change_from_day", 0))
    if linen_dates and effective_phone(booking) is None:
        first = linen_dates[0]
        day = (first - booking["starts_on"]).days
        out.append({
            "code": "phone_missing",
            "message": (f"Séjour de {stay_nights(booking)} jours · "
                        f"draps à J+{day} · téléphone manquant.")})

    # Envoi automatique du guide à J-7 (V2-23d volet 2) : le logement a l'option
    # activée, le séjour entre dans la fenêtre et n'a pas d'email → il ne partira
    # pas tout seul. On invite le propriétaire à compléter (jamais un email dédié).
    # Le libellé calcule l'échéance RÉELLE (jamais le figé « dans 7 jours » : à J-1
    # c'était faux — constat André 06/08).
    if (auto_send_guide
            and today <= booking["starts_on"] <= today + _dt.timedelta(days=AUTO_SEND_HORIZON_DAYS)
            and effective_email(booking) is None):
        days = (booking["starts_on"] - today).days
        out.append({
            "code": "auto_send_email_missing",
            "message": (f"Séjour {_horizon_phrase(days)} — email manquant pour "
                        "l'envoi automatique du guide.")})

    return out


def _horizon_phrase(days: int) -> str:
    """Échéance humaine d'une arrivée : « aujourd'hui » (J), « demain » (J-1),
    « dans N jours » au-delà. Jamais un « dans 7 jours » figé (faux dès J-1)."""
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return "demain"
    return f"dans {days} jours"


# ── Succession d'identifiants : hériter la fiche d'un prédécesseur annulé ─────
# (V2-23h)
#
# Une modification de réservation côté plateforme (Vrbo/Abritel constaté, 06/08)
# réémet l'événement iCal sous un NOUVEL uid. Pour notre import, cela donne DEUX
# séjours : l'ancien uid passe 'cancelled' (invariant maison — jamais supprimé) en
# gardant TOUTE sa fiche (nom, email, langue, code de boîte à clés, demandes) ; un
# séjour VIERGE naît du nouvel uid, aux mêmes dates. Le propriétaire re-saisit tout
# sans comprendre, et le lien /b/ déjà envoyé au locataire est mort (la résolution
# refuse les annulés). Preuve en base : Ballarin porte f221c1cf (Tracy Russel,
# cancelled, fiche complète) et 571cf102 (créée 06/08, vierge, mêmes dates).
#
# On DÉTECTE la situation AU RENDU (aucun état de plus, aucun rapprochement dans la
# synchro) et on PROPOSE la reprise — jamais l'automatisme (le propriétaire décide,
# doctrine 0.4). Cette fonction ne fait que repérer le meilleur prédécesseur ; la
# reprise elle-même est un acte explicite (endpoint dédié `.../inherit`).

# Champs de fiche qui font la « substance » d'un séjour : au moins l'un renseigné
# → le prédécesseur porte quelque chose qui vaut d'être repris.
_SUBSTANCE_TEXT_FIELDS = ("guest_name", "guest_email", "guest_phone",
                          "guest_contact", "guest_lang", "notes")


def _has_substance(b: dict, has_requests: bool) -> bool:
    """Le séjour porte-t-il quelque chose à hériter ? Au moins un champ de fiche
    non vide, un nombre de voyageurs, des âges d'enfants, ou des demandes."""
    if has_requests:
        return True
    if any((b.get(f) or "").strip() for f in _SUBSTANCE_TEXT_FIELDS):
        return True
    return bool(b.get("guest_count")) or bool(b.get("children_ages"))


def _fiche_is_poor(b: dict) -> bool:
    """Fiche pauvre = le propriétaire n'a PAS repris la main : ni nom, ni email, ni
    téléphone. Au-delà, on ne propose aucune reprise (il sait ce qu'il fait)."""
    return (not (b.get("guest_name") or "").strip()
            and effective_email(b) is None
            and effective_phone(b) is None)


def _overlap_days(a_start, a_end, b_start, b_end) -> int:
    """Recouvrement en jours de deux intervalles semi-ouverts [start, end) — 0 si
    disjoints ou seulement adjacents (départ = arrivée : rotation, pas succession)."""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0, (hi - lo).days)


def find_succession_candidate(booking: dict, others: list[dict], *,
                              requested_ids: set) -> dict | None:
    """Le prédécesseur ANNULÉ dont `booking` (nouvel import vierge) devrait hériter
    la fiche, ou None. PURE (aucune base) — `others` est la liste des séjours du
    même logement, `requested_ids` l'ensemble des id de séjours porteurs d'au moins
    une demande (la substance peut ne tenir qu'aux demandes).

    `booking` éligible : importé (external_uid), actif, à fiche pauvre — sinon le
    propriétaire a déjà repris la main. Candidat : importé d'un AUTRE uid,
    'cancelled', dates identiques ou chevauchantes, porteur de substance. Plusieurs
    candidats → le meilleur : recouvrement maximal, puis le plus récemment créé."""
    if booking.get("external_uid") is None or booking.get("status") != "active":
        return None
    if not _fiche_is_poor(booking):
        return None
    uid = booking.get("external_uid")
    tid = str(booking.get("id"))
    matches: list[tuple[int, dict]] = []
    for c in others:
        if str(c.get("id")) == tid or c.get("status") != "cancelled":
            continue
        if c.get("external_uid") is None or c.get("external_uid") == uid:
            continue
        ov = _overlap_days(booking["starts_on"], booking["ends_on"],
                           c["starts_on"], c["ends_on"])
        if ov <= 0 or not _has_substance(c, str(c.get("id")) in requested_ids):
            continue
        matches.append((ov, c))
    if not matches:
        return None
    best_ov = max(ov for ov, _ in matches)
    top = [c for ov, c in matches if ov == best_ov]
    # Départage sûr (tuple court-circuité) : présence de created_at d'abord, puis
    # sa valeur — jamais de comparaison entre None et une date.
    top.sort(key=lambda c: (c.get("created_at") is not None,
                            c.get("created_at") or _dt.datetime.min))
    return top[-1]


# ── Envoi automatique du guide à J-7 : sélection PURE (V2-23d volet 2) ────────

AUTO_SEND_HORIZON_DAYS = 7   # fenêtre d'anticipation de l'envoi (J-7)


@dataclass
class AutoSendPlan:
    """Résultat de la sélection : les séjours à SERVIR (email prêt) et ceux à
    RELANCER (éligibles mais sans email). Deux sorties séparées — l'envoi et la
    relance sont deux gestes distincts (§2/§4)."""
    to_send: list[dict] = field(default_factory=list)
    to_remind: list[dict] = field(default_factory=list)


def select_auto_sends(candidates: list[dict], *, today: _dt.date,
                      horizon_days: int = AUTO_SEND_HORIZON_DAYS) -> AutoSendPlan:
    """Sélectionne les séjours à qui envoyer automatiquement le guide (fonction
    **pure**, testable sans base). Chaque `candidate` est une ligne de séjour
    enrichie du contexte logement (cf. `repo.list_auto_send_candidates`) :
    `auto_send_guide`, `published`, `already_sent`, plus les champs de séjour.

    Critères (décisions André, 05/08) :
      · logement `auto_send_guide` vrai **et** publié ;
      · nature `reservation`/`private`, actif, non rattaché (`_is_occupied`) ;
      · **fenêtre** `today <= starts_on <= today + horizon_days` — le `>= today`
        borne l'arrivée (on n'envoie pas pour un séjour déjà commencé), et le
        `<=` couvre le **rattrapage** : un timer en panne deux jours ne saute
        personne (tout séjour encore dans la fenêtre, non servi, reste candidat) ;
      · **pas déjà envoyé** : une ligne `guide_sends` kind='stay' (manuelle OU
        auto) fait office de verrou — un manuel à J-60 supprime l'automatique
        (assumé : le guide est déjà chez le locataire).

    Un candidat retenu **sans email effectif** (`effective_email`) n'est pas
    servi mais rejoint `to_remind` (le calendrier relance, §4)."""
    horizon_end = today + _dt.timedelta(days=horizon_days)
    plan = AutoSendPlan()
    for b in candidates:
        if not b.get("auto_send_guide") or not b.get("published"):
            continue
        if not _is_occupied(b):          # nature préparée + actif + non rattaché
            continue
        # V2-23g — `starts_on` est la date qui FAIT FOI : si le propriétaire a ajusté
        # l'arrivée à la main (`dates_overridden`), starts_on porte la date ajustée
        # (la synchro ne l'écrase plus), et c'est bien ELLE qui doit décider de la
        # fenêtre J-7. Comportement voulu : le guide part sur les vraies dates du
        # séjour, pas sur celles, périmées, du flux.
        if not (today <= b["starts_on"] <= horizon_end):
            continue
        if b.get("already_sent"):        # le registre est le verrou d'idempotence
            continue
        if effective_email(b) is None:
            plan.to_remind.append(b)
        else:
            plan.to_send.append(b)
    return plan


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


def _clamp_efficiency(value) -> float:
    """Rendement de la Nᵉ personne (§2.3), borné à ]0, 1]. Défaut 0.75. On ne
    descend jamais à 0 (une personne qui n'apporte rien n'a pas de sens) ni au-delà
    de 1 (elle ne peut pas rendre plus qu'un plein temps)."""
    try:
        eff = float(value)
    except (TypeError, ValueError):
        eff = 0.75
    return max(0.05, min(1.0, eff))


def _effective_throughput(cleaners: int, efficiency: float) -> float:
    """Débit (en équivalents plein temps) de `cleaners` personnes travaillant en
    parallèle. La 1re rend 1 ; chacune des suivantes rend `efficiency`. À 2
    personnes et efficiency=0.75 : débit 1.75 → une charge de 6 h-homme se fait en
    6 / 1.75 ≈ 3 h 26 (jamais 3 h : le naïf ÷2 mentirait, §2.3)."""
    return 1.0 + (max(1, cleaners) - 1) * efficiency


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
    efficiency = _clamp_efficiency(t.get("parallel_efficiency"))
    window_h = window_minutes / 60.0
    ph = turnaround_person_hours(care_rules, guest_count)

    if ph is None:
        return {"level": "unknown", "person_hours": None,
                "recommended_cleaners": None, "window_hours": round(window_h, 2),
                "message": "Effort de rotation non configuré."}

    # Effectif minimal tenant dans la fenêtre : la durée écoulée n'est PAS
    # charge / effectif (le travail à plusieurs n'est pas parfaitement divisible,
    # §2.3) mais charge / débit_effectif, où la 2e personne (et les suivantes) ne
    # rend qu'une fraction (`parallel_efficiency`) d'un plein temps.
    recommended = None
    for k in range(1, max_cleaners + 1):
        if ph / _effective_throughput(k, efficiency) <= window_h:
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


# ── Planning du cahier d'équipe : la FENÊTRE, pas le séjour (§2) ──────────────
#
# Le planning ne répond qu'à trois questions : *depuis quand la maison est libre*,
# *pour quand doit-elle être prête*, *quoi faire*. L'entité affichée est donc la
# FENÊTRE de préparation (entre deux occupations), pas le séjour. À quoi s'ajoutent
# les interventions **en cours de séjour** (maison habitée → rendez-vous, donc
# coordonnées du locataire), et les séjours non occupés, **grisés** (« rien à
# préparer ») : un trou inexpliqué fait téléphoner, une ligne grise ferme la
# question. Fonction **pure** (aucune base, `today` passé) → entièrement testable.


def _effective_times(booking: dict, default_in: _dt.time,
                     default_out: _dt.time) -> tuple[_dt.time, _dt.time]:
    """Heures effectives (saisies sinon standard). Miroir de
    `calendars.effective_times` — dupliqué ici pour garder le moteur pur."""
    return (booking.get("checkin_time") or default_in,
            booking.get("checkout_time") or default_out)


def _prep_deadline(booking: dict, default_in: _dt.time) -> _dt.time:
    """Échéance de préparation la plus proche (§2.2) : l'heure de **dépôt de
    bagages** si elle est annoncée (la maison doit être accessible et présentable
    pour cette heure-là), sinon l'heure d'arrivée effective. Un dépôt de bagages
    avance l'échéance et peut resserrer une rotation confortable."""
    checkin = booking.get("checkin_time") or default_in
    return booking.get("luggage_drop_time") or checkin


def _previous_occupation(booking: dict, occupied: list[dict]) -> dict | None:
    """Occupation qui **précède** immédiatement `booking` : celle dont le départ
    est le plus tardif tout en restant ≤ à l'arrivée de `booking` (intervalle
    semi-ouvert → un départ le jour même est une rotation, pas un chevauchement)."""
    prev = None
    for p in occupied:
        if p["id"] == booking["id"]:
            continue
        if p["ends_on"] <= booking["starts_on"]:
            if prev is None or p["ends_on"] > prev["ends_on"]:
                prev = p
    return prev


def _show_contact(booking: dict, today: _dt.date) -> bool:
    """RGPD (§2) : les coordonnées ne s'affichent que pour les séjours **en cours
    ou à venir** (départ ≥ aujourd'hui) — jamais sur l'historique. Un lien `/s/`
    partagé ne doit pas donner accès au répertoire des locataires passés."""
    return booking.get("ends_on") is not None and booking["ends_on"] >= today


def build_planning(bookings: list[dict], care_rules: dict,
                   default_in: _dt.time, default_out: _dt.time, *,
                   today: _dt.date,
                   requests_by_booking: dict | None = None) -> list[dict]:
    """Frise chronologique du cahier d'équipe (§2). Trois types d'entrées, toutes
    à venir (l'historique n'a rien à faire sur un planning) :

    - `window`  : préparation avant une arrivée occupée — libre depuis / à préparer
      pour / fenêtre + signal de rotation (recommandation d'effectif) ;
    - `midstay` : intervention en cours de séjour (draps à J+8, ménage demandé) —
      maison habitée → rendez-vous, donc nom + contact du locataire ;
    - `idle`    : séjour non occupé (travaux, indisponible, à qualifier), **grisé**,
      « rien à préparer » — pour fermer la question plutôt que laisser un trou.

    Toutes les sorties héritent des quantités du moteur d'interventions (§1.3).
    Purement calculée : rien n'est stocké."""
    reqs = requests_by_booking or {}
    active = [b for b in bookings
              if b.get("status") == "active" and b.get("linked_booking_id") is None]
    occupied = sorted([b for b in active if _is_occupied(b)],
                      key=lambda b: (b["starts_on"], b["ends_on"]))

    entries: list[dict] = []

    # (i) Fenêtres de préparation : une par arrivée occupée encore à venir.
    for b in occupied:
        if b["starts_on"] < today:
            continue                          # arrivée déjà passée (déjà préparée)
        entries.append(_window_entry(b, occupied, care_rules,
                                     default_in, default_out, today,
                                     reqs.get(str(b["id"]))))

    # (ii) Interventions en cours de séjour, pour les séjours occupés non terminés.
    for b in occupied:
        if b["ends_on"] < today:
            continue
        for iv in plan_interventions(b, care_rules, requests=reqs.get(str(b["id"]))):
            if not iv.needs_appointment or iv.on < today:
                continue
            # RGPD (§2/§3.0) : coordonnées visibles seulement pour un séjour en
            # cours/à venir. Le téléphone est l'ACTION (rendez-vous par appel/
            # WhatsApp) ; l'email et la langue complètent l'abord du locataire.
            show = _show_contact(b, today)
            entries.append({
                "kind": "midstay", "on": iv.on, "booking_id": b["id"],
                "label": iv.label, "tasks": list(iv.tasks),
                "guest_name": b.get("guest_name"),
                "guest_phone": effective_phone(b) if show else None,
                "guest_email": effective_email(b) if show else None,
                "guest_lang": b.get("guest_lang") if show else None,
                "guest_count": b.get("guest_count"),
                "nature": b.get("nature")})

    # (iii) Séjours non occupés à venir : grisés, « rien à préparer ».
    for b in active:
        if _is_occupied(b) or b["ends_on"] < today:
            continue
        entries.append({
            "kind": "idle", "on": b["starts_on"], "ends_on": b["ends_on"],
            "booking_id": b["id"], "nature": b.get("nature"),
            "guest_name": b.get("guest_name")})

    # Tri chronologique ; à date égale, la fenêtre (préparation d'arrivée) d'abord.
    _order = {"window": 0, "midstay": 1, "idle": 2}
    entries.sort(key=lambda e: (e["on"], _order.get(e["kind"], 9)))
    return entries


def _window_entry(booking: dict, occupied: list[dict], care_rules: dict,
                  default_in: _dt.time, default_out: _dt.time,
                  today: _dt.date, requests: list[dict] | None) -> dict:
    """Une fenêtre de préparation (§2, entrée (i))."""
    checkin = booking.get("checkin_time") or default_in
    luggage = booking.get("luggage_drop_time")
    deadline = _prep_deadline(booking, default_in)

    prev = _previous_occupation(booking, occupied)
    free_since_date = free_since_time = None
    free_days: int | None = None
    same_day = False
    window_minutes: int | None = None
    if prev is not None:
        _, prev_out = _effective_times(prev, default_in, default_out)
        free_since_date = prev["ends_on"]
        free_since_time = prev_out
        free_days = (booking["starts_on"] - prev["ends_on"]).days
        same_day = free_days == 0
        start_dt = _dt.datetime.combine(free_since_date, prev_out)
        end_dt = _dt.datetime.combine(booking["starts_on"], deadline)
        window_minutes = int((end_dt - start_dt).total_seconds() // 60)

    # Tâches = préparation d'arrivée du moteur d'interventions (quantifiées).
    arrival = next((iv for iv in plan_interventions(booking, care_rules,
                                                    requests=requests)
                    if iv.kind == "arrival"), None)
    tasks = list(arrival.tasks) if arrival else []

    # Signal de rotation seulement quand une occupation précède (une fenêtre
    # après une longue vacance n'a pas d'échéance serrée). Le calcul part de
    # l'échéance la plus proche (le dépôt de bagages, déjà intégré dans deadline).
    signal = (turnaround_signal(care_rules, booking.get("guest_count"),
                                window_minutes)
              if window_minutes is not None else None)

    return {
        "kind": "window", "on": booking["starts_on"], "booking_id": booking["id"],
        "guest_name": booking.get("guest_name"),
        "nature": booking.get("nature"),
        "arrival_date": booking["starts_on"], "checkin_time": checkin,
        "luggage_drop_time": luggage,
        "free_since_date": free_since_date, "free_since_time": free_since_time,
        "free_days": free_days, "same_day": same_day,
        "window_minutes": window_minutes, "signal": signal,
        "tasks": tasks, "guest_count": booking.get("guest_count"),
        "children_count": children_count(booking)}
