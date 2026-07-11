"""Application FastAPI de CasaGuide.

Lancement : uvicorn api.main:app --reload  (depuis backend/, CASAGUIDE_DB défini).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
