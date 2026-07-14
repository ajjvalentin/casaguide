"""Configuration du pipeline d'enrichissement CasaGuide.

Tout est surchargeable par variable d'environnement — aucun secret en dur.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Base de données — sans utilisateur explicite : psycopg prend l'utilisateur
    # système (comportement standard sur macOS/Homebrew où le rôle 'postgres'
    # n'existe pas par défaut).
    db_dsn: str = os.getenv("CASAGUIDE_DB", "postgresql://localhost/casaguide")

    # Géocodage — Nominatim (OSM). Politique d'usage : 1 req/s max, User-Agent requis.
    nominatim_url: str = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
    user_agent: str = os.getenv("CASAGUIDE_UA", "CasaGuide/0.1 (contact@casaguide.example)")

    # POI — Overpass API (OSM). Le serveur principal overpass-api.de renvoie des
    # 406 aux clients automatisés depuis avril 2026 : on privilégie les miroirs,
    # avec bascule automatique (voir overpass.py).
    overpass_url: str = os.getenv("OVERPASS_URL", "https://overpass.kumi.systems/api/interpreter")
    overpass_mirrors: tuple = (
        "https://overpass.private.coffee/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    )
    overpass_timeout_s: int = 15
    politeness_delay_s: float = float(os.getenv("CASAGUIDE_DELAY", "1.0"))
    max_pois_per_category: int = int(os.getenv("CASAGUIDE_MAX_POIS", "8"))

    # Distances — OSRM (profils séparés voiture / piéton)
    osrm_drive_url: str = os.getenv("OSRM_DRIVE_URL", "https://router.project-osrm.org")
    osrm_walk_url: str = os.getenv("OSRM_WALK_URL", "https://routing.openstreetmap.de/routed-foot")

    # IA — API Claude
    anthropic_model: str = os.getenv("CASAGUIDE_MODEL", "claude-sonnet-4-6")
    anthropic_max_tokens: int = 2000
    # Traduction du guide (M-09, §9) : modèle dédié, moins cher que Sonnet —
    # la qualité de Haiku est largement suffisante en traduction FR→EN/ES.
    translate_model: str = os.getenv("CASAGUIDE_TRANSLATE_MODEL",
                                     "claude-haiku-4-5-20251001")
    translate_max_tokens: int = 4000
    # Langues cibles MVP du guide voyageur (la langue source vient de
    # properties.default_lang et n'est jamais dans cette liste). DE/NL en V2.
    translate_langs: tuple = tuple(
        l.strip() for l in os.getenv("CASAGUIDE_TRANSLATE_LANGS", "en,es").split(",")
        if l.strip())
    # Tarifs $/MTok (input, output) pour la comptabilité api_costs — à tenir à jour
    model_prices_usd: dict = field(default_factory=lambda: {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
    })
    usd_to_eur: float = float(os.getenv("CASAGUIDE_USD_EUR", "0.86"))

    # Catégories décrites par l'IA (coût maîtrisé : uniquement l'éditorial)
    describe_categories: tuple = ("restaurant", "beach", "sight", "family_activity", "market")


settings = Settings()
