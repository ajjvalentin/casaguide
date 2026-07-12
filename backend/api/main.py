"""Application FastAPI de CasaGuide.

Lancement : uvicorn api.main:app --reload  (depuis backend/, CASAGUIDE_DB défini).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from enrich import db as enrich_db

from . import repo
from .config import settings
from .routers import auth, enrich, guide, pois, properties

log = logging.getLogger("casaguide.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Au démarrage : requalifier les jobs d'enrichissement orphelins.

    Les BackgroundTasks ne survivent pas à un redémarrage d'uvicorn : tout job
    resté 'running' est en réalité interrompu et doit passer 'failed' (M-01)."""
    with enrich_db.connect() as conn:
        n = repo.fail_orphan_running_jobs(conn)
        conn.commit()
    if n:
        log.warning("%d job(s) d'enrichissement orphelin(s) requalifié(s) en failed.", n)
    yield


app = FastAPI(
    title="CasaGuide API",
    version="0.1.0",
    summary="Guides d'accueil numériques pour logements de vacances",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(properties.router)
app.include_router(pois.router)
app.include_router(enrich.router)
app.include_router(guide.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ── Back-office propriétaire (SPA statique, M-03/M-04/M-05) ───────────────────
# Servi en dernier : les routes API (/api, /g, /health, /docs) sont déclarées
# avant et ont donc priorité ; le montage racine ne capte que le reste (SPA à
# routage par ancre, le serveur ne sert jamais que index.html + les assets).
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
else:  # pragma: no cover - dépend du déploiement
    log.warning("Dossier frontend introuvable (%s) : back-office non servi.",
                _FRONTEND_DIR)
