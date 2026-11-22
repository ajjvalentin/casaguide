"""Verrou PAR CHAMP d'une fiche POI (V2-91, fondation des mises à jour automatisées).

Aujourd'hui l'invariant 1 gèle TOUTE la fiche dès qu'elle est arbitrée. La cible
(V2-93) est plus fine : un champ saisi ou modifié par un humain n'est jamais touché,
les autres suivent la source. Ce module porte la règle, PURE (aucune base, aucun
réseau), pour qu'elle soit testable seule et partagée par tous les écrivains.

`pois.human_fields` (migration 042) liste les colonnes dont la valeur COURANTE vient
d'un humain. Les noms admis sont `LOCKABLE_FIELDS` — source unique, reprise par
`api.repo._POI_EDITABLE` et par la contrainte CHECK de la migration 042.

Aucun appelant en production dans V2-91 : V2-93 branchera `system_may_write`.
"""
from __future__ import annotations

from collections.abc import Collection
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
