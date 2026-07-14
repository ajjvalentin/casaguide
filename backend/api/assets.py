"""Versionnage des assets front — cache-busting automatique (M-11).

Dette connue traitée en production : chaque déploiement stampe la variable
`CASAGUIDE_ASSET_VERSION` avec le SHA git court (voir `deploy.sh`). Trois leviers
combinés suppriment le besoin de Cmd+Option+R et de bump manuel du service
worker :

  1. les URL d'assets locales portent `?v=<sha>` (`versioned`) — busting positif
     injecté dans les balises de `index.html` et des pages guide/staff ;
  2. les fichiers statiques du back-office sont servis en `Cache-Control:
     no-cache` (`RevalidatingStaticFiles`) : le navigateur revalide via ETag à
     chaque requête (304 si inchangé), donc un module modifié est toujours
     re-téléchargé même sans `?v` sur les imports ES relatifs ;
  3. le service worker intègre `<sha>` dans le nom de ses caches (placeholder
     `__ASSET_VERSION__` remplacé à la volée, cf. route `/guide/sw.js`) : à chaque
     déploiement les octets du SW changent → le navigateur réactive le SW → les
     anciens caches (autre nom) sont purgés.

En dev/local (variable absente) la version vaut `"dev"` : comportement stable,
aucun impact sur les tests.
"""
from __future__ import annotations

import os

from starlette.staticfiles import StaticFiles

# Placeholder remplacé à la volée dans le service worker servi (frontend/guide/sw.js).
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"


def asset_version() -> str:
    """SHA git court du déploiement courant (`deploy.sh`), sinon 'dev'."""
    return os.getenv("CASAGUIDE_ASSET_VERSION", "dev") or "dev"


def versioned(path: str) -> str:
    """Ajoute `?v=<sha>` à une URL d'asset locale (busting des caches navigateur)."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}v={asset_version()}"


class RevalidatingStaticFiles(StaticFiles):
    """`StaticFiles` forçant la revalidation navigateur (`Cache-Control: no-cache`).

    Sans cet entête, Starlette laisse le navigateur appliquer un cache heuristique
    → risque d'assets JS/CSS périmés servis après un déploiement (symptôme du
    14/07 : back-office en page blanche sur un module ES obsolète). Avec
    `no-cache`, chaque requête revalide (ETag) : 304 quand rien n'a bougé (léger),
    200 avec le nouveau contenu sinon."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp
