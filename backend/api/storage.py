"""Stockage des médias — abstraction prête pour S3 (M-12).

Interface minimale `Storage` (write / read / delete) : le back-office écrit et
lit les fichiers via cette interface sans jamais connaître le backend concret.
Le backend par défaut `LocalStorage` écrit sous `MEDIA_ROOT` (hors dépôt) ; un
backend S3 ultérieur n'a qu'à réimplémenter la même interface.

Les clés de stockage sont **non devinables** (composant aléatoire de 128 bits)
et confinées sous la racine : `_full()` rejette toute clé qui tenterait de
remonter l'arborescence (path traversal).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import settings


class Storage:
    """Contrat de stockage d'un média (octets bruts, clé opaque)."""

    def write(self, key: str, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, key: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class LocalStorage(Storage):
    """Stockage sur disque local sous une racine configurable (MEDIA_ROOT)."""

    def __init__(self, root: str) -> None:
        p = Path(root).expanduser()
        if not p.is_absolute():
            # Relatif à la racine backend/ (ce fichier est backend/api/storage.py)
            p = Path(__file__).resolve().parents[1] / p
        self.root = p

    def _full(self, key: str) -> Path:
        base = self.root.resolve()
        full = (base / key).resolve()
        # Confinement strict : la clé ne doit jamais sortir de la racine.
        if base != full and base not in full.parents:
            raise ValueError("clé de stockage invalide")
        return full

    def write(self, key: str, data: bytes) -> None:
        full = self._full(key)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._full(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._full(key).unlink()
        except FileNotFoundError:
            pass


_storage: Storage | None = None


def get_storage() -> Storage:
    """Backend de stockage courant (singleton). Local par défaut ; le choix du
    backend se fera ici (env `CASAGUIDE_STORAGE`) lors de l'ajout de S3."""
    global _storage
    if _storage is None:
        _storage = LocalStorage(settings.media_root)
    return _storage


def new_key(property_id: str, ext: str) -> str:
    """Clé de stockage non devinable, rangée par logement : `<pid>/<aléa>.<ext>`."""
    return f"{property_id}/{secrets.token_urlsafe(16)}.{ext}"
