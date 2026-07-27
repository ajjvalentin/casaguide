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


# Extensions immuables de fait (images, polices) : cache long, aucun busting
# nécessaire (icônes PWA versionnées par ailleurs via le service worker, jamais
# critiques). Tout le reste — code (JS/MJS/CSS), HTML, manifeste — revalide.
_LONG_CACHE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
                   ".woff", ".woff2", ".ttf", ".otf")


class RevalidatingStaticFiles(StaticFiles):
    """`StaticFiles` forçant la revalidation navigateur du CODE (`Cache-Control:
    no-cache, must-revalidate`).

    Sans cet entête, Starlette laisse le navigateur appliquer un cache heuristique
    → risque d'assets JS/CSS périmés servis après un déploiement (symptôme du
    14/07 : back-office en page blanche sur un module ES obsolète ; généralisé le
    27/07 — OPS-2 : le `?v=<sha>` ne couvre que le point d'entrée, les **imports ES
    relatifs** (`views/*.js`, `components/*.js`…) restaient en cache). Avec
    `no-cache, must-revalidate`, chaque requête revalide (ETag fourni par
    Starlette) : 304 quand rien n'a bougé (coût négligeable, fichiers petits),
    200 avec le nouveau contenu sinon → un module modifié est TOUJOURS re-servi.

    Les images et polices (`_LONG_CACHE_EXT`) sont au contraire en cache long
    (30 j) : immuables ou peu critiques, inutile de les revalider à chaque vue."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        ext = os.path.splitext(path)[1].lower()
        if ext in _LONG_CACHE_EXT:
            resp.headers.setdefault("Cache-Control", "public, max-age=2592000")
        else:
            resp.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return resp
