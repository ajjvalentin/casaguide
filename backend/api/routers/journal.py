"""Lecture du journal des modifications d'un logement (V2-92).

Réservée au propriétaire du logement (`OwnedProperty` : isolation multi-tenant).
Aucune écriture ici : le journal est écrit par le déclencheur `trg_pois_journal`, et
l'annulation passe par `ops/revert_changes.py` (interface propriétaire : V2-94).
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from .. import repo
from ..deps import Conn, OwnedProperty

router = APIRouter(prefix="/api/properties/{property_id}/changes", tags=["journal"])


@router.get("")
def list_changes(conn: Conn, prop: OwnedProperty,
                 poi_id: UUID | None = None,
                 limit: Annotated[int, Query(ge=1, le=200)] = 50,
                 before_id: Annotated[int | None, Query(ge=1)] = None):
    """Dernières modifications du logement (plus récentes d'abord), paginées par
    curseur : repasser `before_id` = l'`id` le plus petit de la page reçue."""
    return repo.list_content_changes(conn, str(prop["id"]),
                                     poi_id=str(poi_id) if poi_id else None,
                                     limit=limit, before_id=before_id)
