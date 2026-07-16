"""Validation des POI suggérés par le pipeline (§5.1 étape 5).

Le propriétaire approuve, édite ou rejette chaque suggestion. Ces statuts sont
ensuite respectés par le pipeline : un POI arbitré n'est jamais réécrit lors
d'un ré-enrichissement (invariant 1, couvert côté enrich/db.py).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import repo
from ..deps import (Conn, DistanceComputer, NominatimSearcher, OwnedProperty,
                    get_distance_computer, get_poi_searcher)
from ..schemas import PoiCandidateOut, PoiCreateIn, PoiEditIn

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


# ── Ajout manuel de lieux (M-22) ─────────────────────────────────────────────

@router.get("/categories")
def poi_categories(conn: Conn, prop: OwnedProperty):
    """Catalogue des catégories POI (pour le sélecteur de l'ajout manuel)."""
    return repo.list_categories(conn)


@router.get("/search", response_model=list[PoiCandidateOut])
def search_pois(conn: Conn, prop: OwnedProperty,
                searcher: Annotated[NominatimSearcher, Depends(get_poi_searcher)],
                q: Annotated[str, Query(min_length=2)]):
    """Recherche Nominatim biaisée autour du logement (M-22). Hors quota
    d'enrichissement (aucun appel IA). Le propriétaire édite puis valide les
    candidats via POST. `OwnedProperty` garantit l'isolation multi-tenant."""
    return searcher(q, prop.get("lat"), prop.get("lon"))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_poi(payload: PoiCreateIn, conn: Conn, prop: OwnedProperty,
               computer: Annotated[DistanceComputer, Depends(get_distance_computer)]):
    """Crée un POI saisi par le propriétaire (source='owner', status='approved').
    Distances calculées à l'insertion (OSRM + repli haversine, comme le pipeline).
    Sans rapport avec le quota d'enrichissement (aucun job, aucun appel IA)."""
    if not repo.poi_category_exists(conn, payload.category_code):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Catégorie inconnue : {payload.category_code}")
    data = payload.model_dump()
    dist = {"dist_walk_m": None, "walk_min": None,
            "dist_drive_m": None, "drive_min": None}
    if prop.get("lat") is not None and prop.get("lon") is not None:
        pts = [{"lat": payload.lat, "lon": payload.lon}]
        computer((prop["lat"], prop["lon"]), pts)
        dist = {k: pts[0].get(k) for k in dist}
    poi = repo.create_manual_poi(conn, str(prop["id"]), {**data, **dist})
    if not poi:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Création du lieu impossible")
    return poi


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
