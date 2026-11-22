"""Verrou PAR CHAMP d'une fiche POI (V2-91, fondation des mises à jour automatisées).

Aujourd'hui l'invariant 1 gèle TOUTE la fiche dès qu'elle est arbitrée. La cible
(V2-93) est plus fine : un champ saisi ou modifié par un humain n'est jamais touché,
les autres suivent la source. Ce module porte la règle, PURE (aucune base, aucun
réseau), pour qu'elle soit testable seule et partagée par tous les écrivains.

`pois.human_fields` (migration 042) liste les colonnes dont la valeur COURANTE vient
d'un humain. Les noms admis sont `LOCKABLE_FIELDS` — source unique, reprise par
`api.repo._POI_EDITABLE` et par la contrainte CHECK de la migration 042.

V2-93 : `plan_refresh` applique cette règle aux trois champs simples d'une fiche
retenue (`REFRESH_FIELDS`). Deux appelants, UNE règle : la revérification planifiée
(`enrich/refresh.py`) et le ré-enrichissement (`db.upsert_pois`, dont le SQL est
prouvé équivalent par test). `source_values_of` isole, dans un POI moissonné, ce que
la SOURCE D'ORIGINE dit vraiment (jamais un remplissage secondaire).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

# Colonnes qu'un propriétaire peut éditer (PATCH /pois/{id}) — l'ordre est celui de
# l'historique `repo._POI_EDITABLE`.
EDITABLE_FIELDS: tuple[str, ...] = (
    "category_code", "name", "address", "locality", "phone", "website",
    "opening_hours", "cuisine", "weekday", "weekday_note",
    "description_md", "owner_comment",
)

# Colonnes verrouillables : les éditables + la position (posée à la création manuelle).
LOCKABLE_FIELDS: tuple[str, ...] = EDITABLE_FIELDS + ("geom",)


def _is_empty(value: Any) -> bool:
    """Une source « muette » : None, chaîne vide ou blanche, collection vide.
    0 n'est PAS vide (une valeur numérique nulle reste une valeur)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def system_may_write(field: str, human_fields: Collection[str],
                     current: Any, incoming: Any) -> bool:
    """Vrai si un écrivain AUTOMATIQUE (revérification, ré-enrichissement) peut poser
    `incoming` dans `field` d'une fiche dont la valeur actuelle est `current`.

    - faux si le champ est verrouillé par un humain (`field in human_fields`) ;
    - faux si la source est muette (une source muette n'efface jamais une valeur) ;
    - faux si rien ne change (`incoming == current`) ;
    - vrai sinon.

    Un nom de champ hors `LOCKABLE_FIELDS` lève `ValueError` : une faute de frappe
    (« opening_hour ») ne figurerait jamais dans `human_fields` et ouvrirait
    silencieusement l'écrasement d'une saisie humaine — on échoue bruyamment."""
    if field not in LOCKABLE_FIELDS:
        raise ValueError(f"Champ non verrouillable : {field!r}")
    if field in human_fields:
        return False
    if _is_empty(incoming):
        return False
    if incoming == current:
        return False
    return True


# ── V2-93 : report des champs simples d'une fiche retenue ───────────────────

# Les SEULS champs qu'un écrivain automatique reporte sur une fiche retenue. Nom,
# adresse, position, description, coup de cœur… ne suivent jamais la source.
REFRESH_FIELDS: tuple[str, ...] = ("phone", "website", "opening_hours")


@dataclass(frozen=True)
class FieldChange:
    """Un champ à réécrire : valeur actuelle → valeur de la source."""
    field: str
    old: Any
    new: Any


def plan_refresh(poi: Mapping[str, Any], source_values: Mapping[str, Any]
                 ) -> list[FieldChange]:
    """Changements à appliquer à une fiche RETENUE d'après sa source d'origine. PURE.

    Pour chacun de `REFRESH_FIELDS`, `system_may_write` décide : champ dans
    `human_fields` ⇒ rien ; source muette (absente, None, vide) ⇒ rien ; valeur
    identique ⇒ rien ; sinon ⇒ changement. `source_values` ne doit porter QUE ce que
    la source d'origine dit aujourd'hui (cf. `source_values_of`) : un téléphone venu
    d'Overture ou de la complétion web sur une fiche OSM est donc remplacé si OSM en
    donne un, et conservé si OSM reste muet.

    Le statut n'est pas vérifié ici (l'appelant ne passe que des fiches retenues)."""
    human = poi.get("human_fields") or ()
    out: list[FieldChange] = []
    for f in REFRESH_FIELDS:
        cur, new = poi.get(f), source_values.get(f)
        if system_may_write(f, human, cur, new):
            out.append(FieldChange(f, cur, new))
    return out


def source_values_of(poi: Mapping[str, Any]) -> dict[str, Any]:
    """Ce que la SOURCE D'ORIGINE d'un POI moissonné dit des `REFRESH_FIELDS`.

    La moisson OSM pose `source_values` (valeurs brutes de l'élément, AVANT tout
    comblement par Overture, la mémoire éditoriale ou une fusion de doublons) ; on la
    prend telle quelle. À défaut (POI construit ailleurs), on part des champs du POI
    et on retire ceux qu'un comblement Overture déclare avoir posés
    (`completion_meta._overture.fields`)."""
    sv = poi.get("source_values")
    if isinstance(sv, Mapping):
        return {f: sv.get(f) for f in REFRESH_FIELDS}
    filled = set(((poi.get("completion_meta") or {}).get("_overture") or {})
                 .get("fields") or ())
    if (poi.get("source") or "osm") == "overture":
        filled = set()
    return {f: (None if f in filled else poi.get(f)) for f in REFRESH_FIELDS}
