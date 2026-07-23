"""Application FastAPI de CasaGuide.

Lancement : uvicorn api.main:app --reload  (depuis backend/, CASAGUIDE_DB défini).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from enrich import db as enrich_db

from . import repo
from .assets import RevalidatingStaticFiles, versioned
from .config import missing_production_config, settings
from .routers import auth, billing, enrich, guide, media, pois, properties

log = logging.getLogger("casaguide.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Au démarrage : requalifier les jobs d'enrichissement orphelins.

    Les BackgroundTasks ne survivent pas à un redémarrage d'uvicorn : tout job
    resté 'running' est en réalité interrompu et doit passer 'failed' (M-01)."""
    # Avertissement de configuration production (M-02) : lister exactement ce
    # qu'il manque dans backend/.env plutôt qu'échouer silencieusement.
    missing = missing_production_config()
    if missing:
        log.warning(
            "Configuration incomplète : %s absente(s). Créez backend/.env "
            "(cf. backend/.env.example) avec, au minimum :\n%s",
            ", ".join(missing),
            "\n".join(f"  {name}=$(openssl rand -hex 32)" for name in missing),
        )

    # Emails transactionnels (V2-08) : sans SMTP, repli ConsoleMailer (les emails
    # de réinitialisation / vérification sont journalisés, jamais envoyés).
    if not settings.smtp_configured:
        log.warning(
            "SMTP non configuré (CASAGUIDE_SMTP_HOST/USER/PASSWORD) : les emails "
            "transactionnels seront journalisés au lieu d'être envoyés "
            "(ConsoleMailer). Définissez ces variables dans backend/.env pour "
            "activer l'envoi réel (Infomaniak : mail.infomaniak.com:465)."
        )

    # Facturation Stripe (V2-05b) : sans clé API, les endpoints de paiement
    # répondent 503 (le reste de l'app est intact — même motif que le mailer).
    if not settings.stripe_configured:
        log.warning(
            "Stripe non configuré (CASAGUIDE_STRIPE_SECRET_KEY) : les endpoints "
            "de facturation (checkout, portail, webhook) répondront 503. "
            "Renseignez la clé (sk_test_… en mode Test) dans backend/.env."
        )
    elif not settings.stripe_webhook_secret:
        log.warning(
            "CASAGUIDE_STRIPE_WEBHOOK_SECRET absent : les webhooks Stripe seront "
            "refusés (400). Renseignez le whsec_… (stripe listen en local, ou "
            "l'endpoint du Dashboard en production)."
        )

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
app.include_router(billing.router)
app.include_router(properties.router)
app.include_router(pois.router)
app.include_router(media.router)
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


@app.get("/", include_in_schema=False)
def index():
    """Sert `index.html` avec `?v=<sha>` injecté sur les assets locaux (M-11).

    Déclaré avant le montage statique pour capter « / » : c'est le seul point
    d'entrée du back-office (SPA à routage par ancre). Chaque déploiement change
    le SHA → les balises `css/app.css?v=…` / `js/app.js?v=…` invalident les caches
    navigateur sans intervention manuelle."""
    path = _FRONTEND_DIR / "index.html"
    if not path.is_file():  # pragma: no cover - dépend du déploiement
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    html = path.read_text(encoding="utf-8")
    html = html.replace('href="css/app.css"', f'href="{versioned("css/app.css")}"')
    html = html.replace('src="js/app.js"', f'src="{versioned("js/app.js")}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


if _FRONTEND_DIR.is_dir():
    # `RevalidatingStaticFiles` : Cache-Control no-cache → revalidation ETag de
    # chaque asset (busting des imports ES relatifs même sans ?v).
    app.mount("/", RevalidatingStaticFiles(directory=_FRONTEND_DIR, html=True),
              name="frontend")
else:  # pragma: no cover - dépend du déploiement
    log.warning("Dossier frontend introuvable (%s) : back-office non servi.",
                _FRONTEND_DIR)
