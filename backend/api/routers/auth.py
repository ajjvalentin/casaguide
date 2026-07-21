"""Authentification des propriétaires (§3.1) : inscription, connexion, profil,
mot de passe oublié et vérification d'email (V2-08)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from psycopg.errors import UniqueViolation

from .. import emails, repo, security
from ..config import settings
from ..deps import Conn, CurrentOwner, Mailer
from ..schemas import (ForgotIn, LoginIn, MessageOut, OwnerOut, RegisterIn,
                       ResetIn, TokenOut, VerifyIn)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Réponse neutre commune (anti-énumération : identique que le compte existe ou non).
_NEUTRAL_MSG = ("Si un compte est associé à cette adresse, un email vient d'être "
                "envoyé avec les instructions.")


def _public_base(request: Request) -> str:
    """Origine publique des liens des emails (V2-08). `CASAGUIDE_PUBLIC_BASE_URL`
    en production (https://holaguia.com), sinon l'origine de la requête."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _issue_token(conn, owner_id: str, purpose: str) -> str:
    """Crée un jeton à usage unique : renvoie le jeton BRUT (pour l'email) et
    stocke seulement son empreinte + son expiration."""
    raw = security.generate_reset_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.auth_token_ttl_min)
    repo.create_auth_token(conn, owner_id, security.hash_reset_token(raw),
                           purpose, expires_at)
    return raw


def _send_verification(conn, owner, background: BackgroundTasks, mailer,
                       request: Request) -> None:
    """Émet un jeton de vérification et programme l'envoi de l'email (V2-08)."""
    raw = _issue_token(conn, str(owner["id"]), "verify")
    verify_url = f"{_public_base(request)}/#/verify/{raw}"
    email = emails.verify_email(verify_url, owner.get("full_name"))
    background.add_task(mailer.send, owner["email"], email)


@router.post("/register", response_model=TokenOut,
             status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, conn: Conn, mailer: Mailer,
             background: BackgroundTasks, request: Request):
    """Crée un compte propriétaire + un abonnement d'essai, renvoie un JWT.
    Envoie un email de vérification (non bloquant : la connexion est immédiate)."""
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
    # Vérification d'email (V2-08) : le compte porte full_name dans owner_row.
    owner_row = {"id": owner["id"], "email": payload.email,
                 "full_name": payload.full_name}
    _send_verification(conn, owner_row, background, mailer, request)
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


# ── Mot de passe oublié (V2-08) ──────────────────────────────────────────────

@router.post("/forgot", response_model=MessageOut)
def forgot_password(payload: ForgotIn, conn: Conn, mailer: Mailer,
                    background: BackgroundTasks, request: Request):
    """Demande de réinitialisation. Répond TOUJOURS 200 avec le même message
    (anti-énumération de comptes). Si le compte existe et qu'aucune demande n'a
    été faite dans la dernière fenêtre de cadence, un jeton 256 bits est créé
    (stocké HACHÉ) et un email part avec le lien /#/reset/{token}."""
    owner = repo.get_owner_by_email(conn, payload.email)
    if owner:
        # Cadence : au plus une demande par CASAGUIDE_FORGOT_MIN_INTERVAL_S.
        since = datetime.now(timezone.utc) - timedelta(
            seconds=settings.forgot_min_interval_s)
        if not repo.recent_auth_token(conn, str(owner["id"]), "reset", since):
            raw = _issue_token(conn, str(owner["id"]), "reset")
            reset_url = f"{_public_base(request)}/#/reset/{raw}"
            email = emails.reset_password_email(reset_url, owner.get("full_name"))
            # L'envoi (lent) est différé → temps de réponse constant.
            background.add_task(mailer.send, owner["email"], email)
    return MessageOut(message=_NEUTRAL_MSG)


@router.post("/reset", response_model=MessageOut)
def reset_password(payload: ResetIn, conn: Conn):
    """Consomme un jeton de réinitialisation et remplace le mot de passe.
    Vérifie empreinte + expiration + usage unique ; invalide le jeton (et les
    autres jetons de réinitialisation du compte)."""
    row = repo.get_auth_token(conn, security.hash_reset_token(payload.token), "reset")
    now = datetime.now(timezone.utc)
    if (not row or row["used_at"] is not None or row["expires_at"] <= now):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lien invalide ou expiré. Refaites une demande.")
    repo.set_owner_password(
        conn, str(row["owner_id"]), security.hash_password(payload.password))
    repo.mark_auth_token_used(conn, str(row["id"]))
    repo.invalidate_owner_tokens(conn, str(row["owner_id"]), "reset")
    return MessageOut(message="Votre mot de passe a été réinitialisé. "
                              "Vous pouvez vous connecter.")


# ── Vérification d'email (V2-08) ─────────────────────────────────────────────

@router.post("/verify-email", response_model=MessageOut)
def verify_email_endpoint(payload: VerifyIn, conn: Conn):
    """Consomme un jeton de vérification et pose owners.email_verified = TRUE.
    Idempotent : un second clic (jeton déjà consommé mais non expiré) renvoie
    aussi un succès. Un jeton inconnu ou expiré → 400."""
    row = repo.get_auth_token(conn, security.hash_reset_token(payload.token), "verify")
    now = datetime.now(timezone.utc)
    if not row or row["expires_at"] <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lien de vérification invalide ou expiré.")
    repo.set_owner_email_verified(conn, str(row["owner_id"]))
    repo.mark_auth_token_used(conn, str(row["id"]))  # no-op si déjà consommé
    return MessageOut(message="Votre adresse email est confirmée. Merci !")


@router.post("/resend-verification", response_model=MessageOut)
def resend_verification(owner: CurrentOwner, conn: Conn, mailer: Mailer,
                        background: BackgroundTasks, request: Request):
    """Renvoie un email de vérification au propriétaire connecté. Sans effet si
    l'email est déjà vérifié. Action authentifiée (abus borné à son propre
    compte) → pas de cadence : le bouton « renvoyer » reste toujours réactif."""
    if owner.get("email_verified"):
        return MessageOut(message="Votre adresse email est déjà vérifiée.")
    _send_verification(conn, owner, background, mailer, request)
    return MessageOut(message="Un nouvel email de vérification vient de partir.")
