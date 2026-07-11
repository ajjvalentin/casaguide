"""Dépendances FastAPI : connexion DB, propriétaire courant, exécuteur du pipeline."""
from __future__ import annotations

from typing import Annotated, Callable

import jwt
from fastapi import Depends, Header, HTTPException, Path, status

from enrich import db, pipeline

from . import repo, security


# ── Connexion PostgreSQL (une par requête) ───────────────────────────────────

def get_conn():
    """Ouvre une connexion pour la durée de la requête.

    Le gestionnaire de contexte de psycopg valide (commit) en sortie normale et
    annule (rollback) si l'endpoint lève une exception — y compris HTTPException,
    que FastAPI propage dans le générateur. Aucune écriture partielle ne subsiste.
    """
    with db.connect() as conn:
        yield conn


Conn = Annotated[object, Depends(get_conn)]


# ── Propriétaire authentifié (JWT Bearer) ────────────────────────────────────

def get_current_owner(
    conn: Conn,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton d'authentification manquant",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        owner_id = security.decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton invalide ou expiré",
            headers={"WWW-Authenticate": "Bearer"},
        )
    owner = repo.get_owner(conn, owner_id)
    if not owner or not owner["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Compte inconnu ou désactivé")
    return owner


CurrentOwner = Annotated[dict, Depends(get_current_owner)]


def owned_property(
    conn: Conn,
    owner: CurrentOwner,
    property_id: Annotated[str, Path()],
) -> dict:
    """Charge le logement en garantissant l'appartenance (isolation multi-tenant).
    404 si le logement n'existe pas OU appartient à un autre propriétaire."""
    prop = repo.get_owned_property(conn, str(owner["id"]), property_id)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Logement introuvable")
    return prop


OwnedProperty = Annotated[dict, Depends(owned_property)]


# ── Exécuteur du pipeline d'enrichissement (injectable pour les tests) ───────

EnrichmentRunner = Callable[[str, str, str], None]


def _default_runner(property_id: str, trigger: str, job_id: str) -> None:
    """Lance le vrai pipeline (géo + Overpass + OSRM + Claude) en tâche de fond."""
    pipeline.run(property_id, use_claude=True, trigger=trigger, job_id=job_id)


def get_enrichment_runner() -> EnrichmentRunner:
    """Surchargée dans les tests par un exécuteur sans réseau
    (app.dependency_overrides[get_enrichment_runner])."""
    return _default_runner
