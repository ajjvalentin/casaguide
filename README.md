# CasaGuide

SaaS de guides d'accueil numériques pour logements de vacances : checklist
guidée pour le propriétaire, enrichissement automatique par IA à partir de
l'adresse (commerces, santé, restaurants, activités…), guide voyageur PWA
multilingue avec carte interactive.

📄 Spécifications complètes : [`docs/cahier_des_charges.md`](docs/cahier_des_charges.md)
🤖 Contexte pour Claude Code : [`CLAUDE.md`](CLAUDE.md)

## Structure du dépôt

```
docs/       Cahier des charges (référence fonctionnelle, §-références du code)
db/         schema.sql (PostgreSQL 15+/PostGIS), seed.sql (checklist §4), migrations/
backend/    Pipeline d'enrichissement Python (enrich/) + tests d'intégration
```

## Démarrage rapide

```bash
# 1. Base de données
createdb casaguide
psql -d casaguide -f db/schema.sql
psql -d casaguide -f db/seed.sql
psql -d casaguide -f db/migrations/001_pois_unique_source.sql

# 2. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Tests (aucun accès réseau requis — APIs simulées)
export CASAGUIDE_DB=postgresql://localhost/casaguide
python -m pytest tests/ -v

# 4. Enrichissement réel d'un logement
export ANTHROPIC_API_KEY=sk-ant-...
python -m enrich.pipeline --property-id <uuid> --trigger initial
```

## Pipeline d'enrichissement

`adresse → Nominatim (géocodage) → Overpass/OSM (POI par catégorie, rayons du
seed) → OSRM (distances à pied/en voiture) → Claude (numéros d'urgence, règles
de tri et de bruit locales, descriptions) → PostgreSQL (statut "suggested",
validation par le propriétaire)`

Garanties : idempotent, ne touche jamais aux choix du propriétaire, JSON strict
validé, coûts IA comptabilisés par logement dans `api_costs`.
