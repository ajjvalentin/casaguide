"""Configuration de l'API — tout par variables d'environnement, aucun secret en dur.

Cohérent avec `enrich/settings.py` : la DSN PostgreSQL est partagée avec le
pipeline (`CASAGUIDE_DB`).
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

log = logging.getLogger("casaguide.api")


def _cors_origins() -> list[str]:
    raw = os.getenv("CASAGUIDE_CORS_ORIGINS", "*").strip()
    if raw == "*" or not raw:
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _jwt_secret() -> str:
    """Clé de signature JWT. À défaut d'env, une clé aléatoire par processus est
    générée : sûre par défaut (aucun secret en dur), mais les jetons ne survivent
    pas à un redémarrage — en production, définir CASAGUIDE_JWT_SECRET."""
    env = os.getenv("CASAGUIDE_JWT_SECRET")
    if env:
        return env
    log.warning(
        "CASAGUIDE_JWT_SECRET non défini : génération d'une clé éphémère. "
        "Les jetons émis seront invalidés au redémarrage."
    )
    return secrets.token_urlsafe(48)


@dataclass
class ApiSettings:
    jwt_secret: str = field(default_factory=_jwt_secret)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = int(os.getenv("CASAGUIDE_JWT_EXPIRE_MIN", str(60 * 24 * 7)))
    # Hachage de mot de passe (pbkdf2-hmac-sha256, stdlib — aucune dépendance native)
    pbkdf2_iterations: int = int(os.getenv("CASAGUIDE_PBKDF2_ITER", "480000"))
    cors_origins: list[str] = field(default_factory=_cors_origins)
    # Cache de l'endpoint public du guide (secondes)
    guide_cache_seconds: int = int(os.getenv("CASAGUIDE_GUIDE_CACHE_S", "300"))
    # Plan attribué à l'inscription
    default_plan: str = os.getenv("CASAGUIDE_DEFAULT_PLAN", "free")
    # Stockage des médias (photos/PDF des sections, M-12). Chemin local par défaut,
    # relatif à backend/ ; exclu de git. Architecture prête pour un backend S3.
    media_root: str = os.getenv("MEDIA_ROOT", "var/media")
    max_upload_bytes: int = int(os.getenv("CASAGUIDE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    # Origine publique servant à construire les liens absolus (QR imprimable M-07).
    # À défaut, on retombe sur l'origine de la requête (request.base_url).
    public_base_url: str | None = os.getenv("CASAGUIDE_PUBLIC_BASE_URL") or None


settings = ApiSettings()


def missing_production_config() -> list[str]:
    """Variables de sécurité indispensables en production, absentes de l'env.

    Utilisé au démarrage pour avertir clairement le déployeur (M-02) : sans
    CASAGUIDE_JWT_SECRET les jetons sont invalidés à chaque redémarrage ; sans
    CASAGUIDE_SECRET_KEY les endpoints de secrets répondent 503."""
    missing = []
    if not os.getenv("CASAGUIDE_JWT_SECRET"):
        missing.append("CASAGUIDE_JWT_SECRET")
    if not os.getenv("CASAGUIDE_SECRET_KEY"):
        missing.append("CASAGUIDE_SECRET_KEY")
    return missing
