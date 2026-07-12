"""CRUD des logements, données sensibles chiffrées et sections du guide (§3.1)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from .. import crypto, repo
from ..deps import (
    Conn, CurrentOwner, DistanceComputer, OwnedProperty, get_distance_computer,
)
from ..schemas import (
    PropertyIn, PropertyOut, PropertyStatsOut, PropertyUpdate, RecomputeOut,
    SecretsIn, SecretsOut, SectionUpsertIn,
)

router = APIRouter(prefix="/api/properties", tags=["properties"])


# ── CRUD logements ───────────────────────────────────────────────────────────

@router.get("", response_model=list[PropertyOut])
def list_properties(conn: Conn, owner: CurrentOwner):
    return repo.list_properties(conn, str(owner["id"]))


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
def create_property(payload: PropertyIn, conn: Conn, owner: CurrentOwner):
    # Limite de logements selon le plan (§10)
    plan = repo.get_owner_plan(conn, str(owner["id"]))
    if plan and plan["max_properties"] is not None:
        if repo.count_properties(conn, str(owner["id"])) >= plan["max_properties"]:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Limite de logements atteinte pour le plan « {plan['name']} »")
    data = payload.model_dump()
    data["country_code"] = data["country_code"].upper()
    return repo.create_property(conn, str(owner["id"]), data)


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(prop: OwnedProperty):
    return prop


@router.patch("/{property_id}", response_model=PropertyOut)
def update_property(payload: PropertyUpdate, conn: Conn, owner: CurrentOwner,
                    prop: OwnedProperty):
    fields = payload.model_dump(exclude_unset=True)
    if "country_code" in fields and fields["country_code"]:
        fields["country_code"] = fields["country_code"].upper()
    updated = repo.update_property(conn, str(owner["id"]), str(prop["id"]), fields)
    return updated


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_property(conn: Conn, owner: CurrentOwner, prop: OwnedProperty):
    repo.delete_property(conn, str(owner["id"]), str(prop["id"]))
    return None


@router.get("/{property_id}/stats", response_model=PropertyStatsOut)
def property_stats(conn: Conn, prop: OwnedProperty):
    """Complétude des sections + décompte des POI par statut (tableau de bord)."""
    return repo.property_stats(conn, str(prop["id"]))


# ── Repositionnement du logement : recalcul des distances (§5.1, M-05) ───────

@router.post("/{property_id}/recompute-distances", response_model=RecomputeOut)
def recompute_distances(
    conn: Conn, prop: OwnedProperty,
    computer: Annotated[DistanceComputer, Depends(get_distance_computer)],
):
    """Recalcule les distances/temps de tous les POI depuis la position courante
    du logement. Appelé après un placement manuel du point sur la carte : les
    distances pré-calculées deviennent cohérentes avec la nouvelle position.

    N'altère que les colonnes de distance (jamais le statut ni le contenu
    arbitré par le propriétaire — invariant 1)."""
    if prop["lat"] is None or prop["lon"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le logement n'a pas encore de position géographique")
    pois = repo.list_poi_positions(conn, str(prop["id"]))
    if not pois:
        return RecomputeOut(updated=0)
    computer((prop["lat"], prop["lon"]), pois)
    for p in pois:
        repo.update_poi_distances(
            conn, str(p["id"]),
            dist_walk_m=p.get("dist_walk_m"), walk_min=p.get("walk_min"),
            dist_drive_m=p.get("dist_drive_m"), drive_min=p.get("drive_min"))
    return RecomputeOut(updated=len(pois))


# ── Données sensibles (chiffrées AES-GCM, invariant 5) ───────────────────────

@router.put("/{property_id}/secrets", response_model=SecretsOut)
def set_secrets(payload: SecretsIn, conn: Conn, prop: OwnedProperty):
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement non configuré (CASAGUIDE_SECRET_KEY absente)")
    repo.upsert_secrets(
        conn, str(prop["id"]),
        wifi_ssid=payload.wifi_ssid,
        wifi_pass_enc=crypto.encrypt(payload.wifi_pass) if payload.wifi_pass else None,
        keybox_code_enc=crypto.encrypt(payload.keybox_code) if payload.keybox_code else None,
        keybox_notes=payload.keybox_notes,
    )
    return SecretsOut(
        wifi_ssid=payload.wifi_ssid, wifi_pass=payload.wifi_pass,
        keybox_code=payload.keybox_code, keybox_notes=payload.keybox_notes)


@router.get("/{property_id}/secrets", response_model=SecretsOut)
def get_secrets(conn: Conn, prop: OwnedProperty):
    """Déchiffrement réservé au propriétaire authentifié (jamais côté voyageur)."""
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement non configuré (CASAGUIDE_SECRET_KEY absente)")
    row = repo.get_secrets(conn, str(prop["id"]))
    if not row:
        return SecretsOut()
    return SecretsOut(
        wifi_ssid=row["wifi_ssid"],
        wifi_pass=crypto.decrypt(row["wifi_pass_enc"]),
        keybox_code=crypto.decrypt(row["keybox_code_enc"]),
        keybox_notes=row["keybox_notes"],
    )


# ── Sections du guide (contenu saisi par le propriétaire) ────────────────────

@router.get("/{property_id}/sections")
def list_sections(conn: Conn, prop: OwnedProperty):
    """Catalogue complet des sections pré-définies + contenu déjà saisi.
    Sert de base au formulaire guidé du back-office (§4)."""
    rows = repo.list_sections_with_templates(conn, str(prop["id"]))
    total = len(rows)
    done = sum(1 for r in rows if r["completed"])
    return {
        "completion_pct": round(done / total * 100) if total else 0,
        "sections": rows,
    }


@router.put("/{property_id}/sections/{template_code}")
def upsert_section(template_code: str, payload: SectionUpsertIn, conn: Conn,
                   prop: OwnedProperty):
    if not repo.section_template_exists(conn, template_code):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Section inconnue : {template_code}")
    return repo.upsert_section(
        conn, str(prop["id"]), template_code,
        content=payload.content, body_md=payload.body_md,
        is_visible=payload.is_visible, completed=payload.completed)
