"""Sécurité : hachage des mots de passe et jetons JWT.

Mots de passe : PBKDF2-HMAC-SHA256 (stdlib) — évite toute dépendance native,
comparaison en temps constant. Format stocké dans owners.password_hash :
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    it = settings.pbkdf2_iterations
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it)
    return f"{_ALGO}${it}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, it_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(it_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def create_access_token(owner_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(owner_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str:
    """Retourne l'owner_id (sub) ou lève jwt.PyJWTError si invalide/expiré."""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("sub manquant")
    return sub
