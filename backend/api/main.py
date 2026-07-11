"""Application FastAPI de CasaGuide.

Lancement : uvicorn api.main:app --reload  (depuis backend/, CASAGUIDE_DB défini).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import auth, enrich, guide, pois, properties

app = FastAPI(
    title="CasaGuide API",
    version="0.1.0",
    summary="Guides d'accueil numériques pour logements de vacances",
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
