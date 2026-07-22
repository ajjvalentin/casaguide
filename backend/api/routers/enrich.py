"""Déclenchement du pipeline d'enrichissement en tâche de fond (§5, §12).

L'endpoint crée un job 'pending' (validé/commité aussitôt pour être visible de
la tâche de fond, qui ouvre sa propre connexion), puis programme l'exécution du
pipeline via BackgroundTasks et renvoie l'identifiant du job. Le suivi se fait
via GET .../jobs et .../jobs/{id} (statut, étapes, erreurs).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, status)

from enrich.settings import settings as enrich_settings

from .. import plans, repo
from ..deps import (
    Conn, CurrentOwner, EnrichmentRunner, OwnedProperty, TranslationRunner,
    get_enrichment_runner, get_translation_runner,
)
from ..quota import quota_exceeded
from ..schemas import EnrichIn, JobOut

router = APIRouter(prefix="/api/properties/{property_id}", tags=["enrichment"])


def _target_langs(prop: dict) -> list[str]:
    """Langues cibles de traduction : les langues MVP hors langue source (M-09)."""
    source = prop.get("default_lang") or "fr"
    return [l for l in enrich_settings.translate_langs if l and l != source]


def schedule_translation(background, conn, prop: dict, runner: TranslationRunner,
                         plan: dict) -> str | None:
    """Programme une (re)traduction en tâche de fond (M-09), bornée aux langues
    autorisées par le plan (V2-05a, invariant 8). Crée un job 'pending'
    (trigger='translate'), le commit pour qu'il soit visible de la tâche de fond,
    puis renvoie son identifiant. None si aucune langue cible **autorisée** — ce
    qui inclut le plan gratuit (`langs=1` → FR seul) : on ne publie alors aucune
    traduction, et les traductions déjà en base ne sont **jamais** touchées
    (invariant 1)."""
    langs = plans.cap_target_langs(plan, _target_langs(prop))
    if not langs:
        return None
    job_id = repo.create_pending_job(conn, str(prop["id"]), "translate")
    conn.commit()
    background.add_task(runner, str(prop["id"]), job_id, langs)
    return job_id


@router.post("/enrich", status_code=status.HTTP_202_ACCEPTED)
def trigger_enrich(
    payload: EnrichIn,
    background: BackgroundTasks,
    conn: Conn,
    owner: CurrentOwner,
    prop: OwnedProperty,
    runner: Annotated[EnrichmentRunner, Depends(get_enrichment_runner)],
):
    """Programme un enrichissement. Respecte le quota mensuel du plan (§5.2,
    V2-05a) : refus propre 402 `quota_exceeded` au-delà de `enrich_quota` jobs
    par mois calendaire et par logement (les jobs `failed` ne comptent pas)."""
    q = plans.check_quota(conn, str(owner["id"]), "enrichments",
                          property_id=str(prop["id"]))
    if not q.ok:
        raise quota_exceeded(
            f"Quota d'enrichissement mensuel atteint ({q.limit}) pour l'offre "
            f"« {q.plan['name']} ». Il se réinitialise le 1er du mois prochain ; "
            f"passez à une offre supérieure pour un quota plus élevé.")

    job_id = repo.create_pending_job(conn, str(prop["id"]), payload.trigger)
    # Rendre le job visible de la tâche de fond (connexion distincte) avant de la lancer.
    conn.commit()
    background.add_task(runner, str(prop["id"]), payload.trigger, job_id)
    return {"job_id": job_id, "status": "accepted"}


@router.post("/translate", status_code=status.HTTP_202_ACCEPTED)
def trigger_translation(
    background: BackgroundTasks,
    conn: Conn,
    owner: CurrentOwner,
    prop: OwnedProperty,
    runner: Annotated[TranslationRunner, Depends(get_translation_runner)],
):
    """Programme une mise à jour des traductions du guide (M-09). Ne retraite que
    le manquant ou le périmé (ciblage, §9). Hors quota d'enrichissement, mais
    borné au plafond de langues du plan (V2-05a)."""
    plan = plans.get_plan(conn, str(owner["id"]))
    job_id = schedule_translation(background, conn, prop, runner, plan)
    if job_id is None:
        # Distinguer « pas de langue cible du tout » (guide monolingue) de « le
        # plan interdit toute traduction » (plafond `features.langs`).
        if _target_langs(prop):
            raise quota_exceeded(
                f"L'offre « {plan['name']} » ne permet pas de publier de "
                f"traduction (guide en une seule langue). Passez à une offre "
                f"supérieure pour proposer votre guide en plusieurs langues.")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucune langue cible pour ce logement")
    return {"job_id": job_id, "status": "accepted"}


@router.get("/translation-status")
def translation_status(conn: Conn, prop: OwnedProperty):
    """État des traductions par langue (à jour / périmé) pour l'éditeur (M-09)."""
    return repo.translation_status(conn, str(prop["id"]), _target_langs(prop))


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
