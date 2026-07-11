"""Configuration du pipeline d'enrichissement CasaGuide.

Tout est surchargeable par variable d'environnement — aucun secret en dur.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Base de données
    db_dsn: str = os.getenv("CASAGUIDE_DB", "postgresql://postgres@localhost/casaguide")

    # Géocodage — Nominatim (OSM). Politique d'usage : 1 req/s max, User-Agent requis.
    nominatim_url: str = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
    user_agent: str = os.getenv("CASAGUIDE_UA", "CasaGuide/0.1 (contact@casaguide.example)")

    # POI — Overpass API (OSM)
    overpass_url: str = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    overpass_timeout_s: int = 30
    politeness_delay_s: float = float(os.getenv("CASAGUIDE_DELAY", "1.0"))
    max_pois_per_category: int = int(os.getenv("CASAGUIDE_MAX_POIS", "8"))

    # Distances — OSRM (profils séparés voiture / piéton)
    osrm_drive_url: str = os.getenv("OSRM_DRIVE_URL", "https://router.project-osrm.org")
    osrm_walk_url: str = os.getenv("OSRM_WALK_URL", "https://routing.openstreetmap.de/routed-foot")

    # IA — API Claude
    anthropic_model: str = os.getenv("CASAGUIDE_MODEL", "claude-sonnet-4-6")
    anthropic_max_tokens: int = 2000
    # Tarifs $/MTok (input, output) pour la comptabilité api_costs — à tenir à jour
    model_prices_usd: dict = field(default_factory=lambda: {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
    })
    usd_to_eur: float = float(os.getenv("CASAGUIDE_USD_EUR", "0.86"))

    # Catégories décrites par l'IA (coût maîtrisé : uniquement l'éditorial)
    describe_categories: tuple = ("restaurant", "beach", "sight", "family_activity", "market")


settings = Settings()
