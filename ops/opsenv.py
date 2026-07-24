"""Chargement autonome de `backend/.env` pour les scripts ops/ (OPS-1, volet 2).

Les scripts ops/ (`stripe_sync_products.py`, `set_plan.py`) sont lancés **à la
main** sur le serveur (`/opt/casaguide/ops/…`) : contrairement au service uvicorn,
ils n'héritent **pas** de l'`EnvironmentFile` systemd. Il faut donc qu'ils
chargent le `.env` eux-mêmes.

Pourquoi pas un `source backend/.env` en bash : le fichier contient des valeurs
**non-shell**, en particulier
`CASAGUIDE_SMTP_FROM=Holaguia <no-reply@holaguia.com>` — en bash les chevrons
`<`/`>` sont des redirections, donc `source` explose (constaté en prod, « ligne
51 »). On parse donc le fichier avec un petit lecteur dotecv robuste, sans
dépendance (les scripts ops/ ne présument pas `python-dotenv`).

Politique : `override=False` par défaut → une variable déjà présente dans
l'environnement **prime** ; le `.env` ne fait que **compléter** ce qui manque
(repli sur l'environnement, comme `enrich/envfile.py`). Fichier absent → no-op.
"""
from __future__ import annotations

import os
from pathlib import Path

# ops/opsenv.py → parents[1] = racine du dépôt ; backend/.env est à côté.
# Sur le serveur : /opt/casaguide/ops/opsenv.py → /opt/casaguide/backend/.env.
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[1] / "backend" / ".env"


def parse_env(text: str) -> dict[str, str]:
    """Parse un contenu façon dotenv en `dict`.

    Robuste aux réalités du `.env` de prod :
      - valeurs contenant **espaces et chevrons** (`Holaguia <no-reply@…>`) —
        tout ce qui suit le premier `=` est conservé tel quel ;
      - **guillemets** optionnels autour de la valeur (retirés) ;
      - lignes de **commentaire** (`#…`) et lignes vides ignorées ;
      - préfixe `export ` toléré ;
      - valeur **vide** (`CLE=`) → chaîne vide.

    Les commentaires *en fin de valeur* ne sont PAS retirés d'une valeur nue (le
    `.env` n'en contient pas sur les lignes actives, et un `#` peut être littéral
    dans une adresse/clé) — ne jamais mutiler une valeur.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        # Guillemets équilibrés → contenu littéral (espaces internes préservés).
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        out[key] = val
    return out


def load_env(env_file: str | os.PathLike | None = None, *,
             override: bool = False) -> Path | None:
    """Charge le `.env` dans `os.environ`. Retourne le chemin chargé, ou None.

    `env_file` : chemin explicite (option `--env-file`), sinon `backend/.env`.
    `override=False` (défaut) : ne remplace jamais une variable déjà exportée.
    Fichier absent → None (repli silencieux sur l'environnement courant).
    """
    path = Path(env_file) if env_file else DEFAULT_ENV_PATH
    if not path.is_file():
        return None
    for key, val in parse_env(path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = val
    return path
