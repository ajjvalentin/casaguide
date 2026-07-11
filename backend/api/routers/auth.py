"""Authentification des propriétaires (§3.1) : inscription, connexion, profil."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from psycopg.errors import UniqueViolation

from .. import repo, security
from ..config import settings
from ..deps import Conn, CurrentOwner
from ..schemas import LoginIn, OwnerOut, RegisterIn, TokenOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut,
             status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, conn: Conn):
    """Crée un compte propriétaire + un abonnement d'essai, renvoie un JWT."""
    if repo.get_owner_by_email(conn, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Un compte existe déjà pour cet email")
    try:
        owner = repo.create_owner(
            conn,
            email=payload.email,
            password_hash=security.hash_password(payload.password),
            full_name=payload.full_name,
            company_name=payload.company_name,
            phone=payload.phone,
            locale=payload.locale,
        )
        repo.create_subscription(conn, str(owner["id"]), settings.default_plan)
    except UniqueViolation:
        # Course entre deux inscriptions simultanées sur le même email
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Un compte existe déjà pour cet email")
    return TokenOut(access_token=security.create_access_token(str(owner["id"])))


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, conn: Conn):
    owner = repo.get_owner_by_email(conn, payload.email)
    # Vérifie toujours le hash (temps constant) pour ne pas révéler l'existence
    ok = security.verify_password(
        payload.password, owner["password_hash"] if owner else None)
    if not owner or not ok or not owner["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Email ou mot de passe incorrect")
    return TokenOut(access_token=security.create_access_token(str(owner["id"])))


@router.get("/me", response_model=OwnerOut)
def me(owner: CurrentOwner):
    return OwnerOut(**owner)
