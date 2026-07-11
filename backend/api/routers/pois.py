"""Validation des POI suggérés par le pipeline (§5.1 étape 5).

Le propriétaire approuve, édite ou rejette chaque suggestion. Ces statuts sont
ensuite respectés par le pipeline : un POI arbitré n'est jamais réécrit lors
d'un ré-enrichissement (invariant 1, couvert côté enrich/db.py).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from .. import repo
from ..deps import Conn, OwnedProperty
from ..schemas import PoiEditIn

router = APIRouter(prefix="/api/properties/{property_id}/pois", tags=["pois"])

_STATUSES = {"suggested", "approved", "edited", "rejected"}


@router.get("")
def list_pois(conn: Conn, prop: OwnedProperty,
              status_filter: str | None = Query(default=None, alias="status")):
    """Liste les POI du logement, éventuellement filtrés par statut (écran de
    validation : `?status=suggested`)."""
    if status_filter and status_filter not in _STATUSES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Statut invalide : {status_filter}")
    return repo.list_pois(conn, str(prop["id"]), status_filter)


def _require_poi(conn, property_id: str, poi_id: str) -> dict:
    poi = repo.get_poi(conn, property_id, poi_id)
    if not poi:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="POI introuvable pour ce logement")
    return poi


@router.post("/{poi_id}/approve")
def approve_poi(poi_id: str, conn: Conn, prop: OwnedProperty):
    _require_poi(conn, str(prop["id"]), poi_id)
    return repo.set_poi_status(conn, str(prop["id"]), poi_id, "approved")


@router.post("/{poi_id}/reject")
def reject_poi(poi_id: str, conn: Conn, prop: OwnedProperty):
    _require_poi(conn, str(prop["id"]), poi_id)
    return repo.set_poi_status(conn, str(prop["id"]), poi_id, "rejected")


@router.patch("/{poi_id}")
def edit_poi(poi_id: str, payload: PoiEditIn, conn: Conn, prop: OwnedProperty):
    """Édite un POI (nom, description, commentaire…) et le passe en 'edited'."""
    _require_poi(conn, str(prop["id"]), poi_id)
    return repo.edit_poi(conn, str(prop["id"]), poi_id,
                         payload.model_dump(exclude_unset=True))
