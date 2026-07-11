"""Déclenchement du pipeline d'enrichissement en tâche de fond (§5, §12).

L'endpoint crée un job 'pending' (validé/commité aussitôt pour être visible de
la tâche de fond, qui ouvre sa propre connexion), puis programme l'exécution du
pipeline via BackgroundTasks et renvoie l'identifiant du job. Le suivi se fait
via GET .../jobs et .../jobs/{id} (statut, étapes, erreurs).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, status)

from .. import repo
from ..deps import Conn, CurrentOwner, EnrichmentRunner, OwnedProperty, get_enrichment_runner
from ..schemas import EnrichIn, JobOut

router = APIRouter(prefix="/api/properties/{property_id}", tags=["enrichment"])


@router.post("/enrich", status_code=status.HTTP_202_ACCEPTED)
def trigger_enrich(
    payload: EnrichIn,
    background: BackgroundTasks,
    conn: Conn,
    owner: CurrentOwner,
    prop: OwnedProperty,
    runner: Annotated[EnrichmentRunner, Depends(get_enrichment_runner)],
):
    """Programme un enrichissement. Respecte le quota mensuel du plan (§5.2)."""
    plan = repo.get_owner_plan(conn, str(owner["id"]))
    if plan:
        used = repo.count_jobs_current_month(conn, str(prop["id"]))
        if used >= plan["enrich_quota"]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(f"Quota d'enrichissement mensuel atteint "
                        f"({plan['enrich_quota']}) pour le plan « {plan['name']} »"))

    job_id = repo.create_pending_job(conn, str(prop["id"]), payload.trigger)
    # Rendre le job visible de la tâche de fond (connexion distincte) avant de la lancer.
    conn.commit()
    background.add_task(runner, str(prop["id"]), payload.trigger, job_id)
    return {"job_id": job_id, "status": "accepted"}


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(conn: Conn, prop: OwnedProperty):
    return repo.list_jobs(conn, str(prop["id"]))


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, conn: Conn, prop: OwnedProperty):
    job = repo.get_job(conn, str(prop["id"]), job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Job introuvable")
    return job
