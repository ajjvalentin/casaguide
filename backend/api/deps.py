"""Dépendances FastAPI : connexion DB, propriétaire courant, exécuteur du pipeline."""
from __future__ import annotations

from typing import Annotated, Callable

import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from enrich import db, distance, pipeline

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

# Schéma de sécurité déclaré à OpenAPI -> Swagger affiche le bouton « Authorize »
# et transmet automatiquement l'entête Authorization. auto_error=False pour
# conserver nos réponses 401 (HTTPBearer renverrait 403 sur jeton absent).
_bearer = HTTPBearer(
    auto_error=False,
    description="Jeton JWT obtenu via /api/auth/login ou /api/auth/register.",
)


def get_current_owner(
    conn: Conn,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton d'authentification manquant",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        owner_id = security.decode_token(credentials.credentials)
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


# ── Calcul de distances (recalcul après repositionnement manuel, M-05) ───────

# (origin lat/lon, liste de POI mutés en place avec dist_*_m / *_min)
DistanceComputer = Callable[[tuple[float, float], list[dict]], None]


def _default_distance_computer(origin: tuple[float, float],
                               pois: list[dict]) -> None:
    """Recalcul réel via OSRM (repli haversine intégré à compute_distances)."""
    distance.compute_distances(origin, pois)


def get_distance_computer() -> DistanceComputer:
    """Surchargée dans les tests par un calcul sans réseau
    (app.dependency_overrides[get_distance_computer])."""
    return _default_distance_computer
