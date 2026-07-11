"""Chiffrement applicatif des données sensibles (§8, invariant 5 du CLAUDE.md).

Le mot de passe wifi et le code de la boîte à clés sont chiffrés en AES-256-GCM
avant insertion dans les colonnes BYTEA de `property_secrets`. La clé vit hors
base, dans la variable d'environnement CASAGUIDE_SECRET_KEY (32 octets encodés
en hex 64 caractères ou en base64). Format stocké : nonce (12 o) || ciphertext.

Si la clé n'est pas configurée, `is_configured()` renvoie False et les endpoints
touchant aux secrets répondent 503 plutôt que de manipuler des données en clair.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


def _load_key() -> bytes | None:
    raw = os.getenv("CASAGUIDE_SECRET_KEY")
    if not raw:
        return None
    raw = raw.strip()
    # Essai hex (64 caractères) puis base64
    try:
        if len(raw) == 64:
            key = bytes.fromhex(raw)
        else:
            key = base64.b64decode(raw)
    except (ValueError, TypeError):
        raise RuntimeError("CASAGUIDE_SECRET_KEY : format hex ou base64 attendu")
    if len(key) != 32:
        raise RuntimeError("CASAGUIDE_SECRET_KEY doit faire 32 octets (AES-256)")
    return key


_KEY = _load_key()


def is_configured() -> bool:
    return _KEY is not None


def encrypt(plaintext: str) -> bytes:
    if _KEY is None:
        raise RuntimeError("Clé de chiffrement non configurée")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_KEY).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct


def decrypt(blob: bytes | memoryview | None) -> str | None:
    if blob is None:
        return None
    if _KEY is None:
        raise RuntimeError("Clé de chiffrement non configurée")
    blob = bytes(blob)
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(_KEY).decrypt(nonce, ct, None).decode("utf-8")
