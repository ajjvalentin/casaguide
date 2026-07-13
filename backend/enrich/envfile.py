"""Chargement du fichier `backend/.env` au démarrage (M-02).

Charge les variables d'environnement depuis `backend/.env` **avant** que les
modules de configuration (`api/config.py`, `api/crypto.py`, `enrich/settings.py`)
ne lisent `os.environ` à l'import. Les variables déjà présentes dans
l'environnement (shell exporté, tests) ne sont **jamais** écrasées
(`override=False`) : le `.env` ne fait que compléter ce qui manque.

Aucun secret n'est stocké ici — seul le chemin du fichier est connu ; son
contenu reste hors dépôt (`.env` est dans `.gitignore`).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("casaguide")

# backend/.env (ce fichier est backend/enrich/envfile.py → parents[1] = backend/)
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

_loaded = False


def load_env() -> None:
    """Charge `backend/.env` une seule fois (idempotent). No-op s'il est absent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not ENV_PATH.is_file():
        return
    try:
        from dotenv import load_dotenv  # python-dotenv, léger
        load_dotenv(ENV_PATH, override=False)
    except ModuleNotFoundError:  # repli sans dépendance
        _load_minimal(ENV_PATH)
    log.info("Configuration chargée depuis %s", ENV_PATH)


def _load_minimal(path: Path) -> None:
    """Analyseur minimal `KEY=VALUE` (repli si python-dotenv n'est pas installé)."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)
