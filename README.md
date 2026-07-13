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
backend/    Pipeline d'enrichissement (enrich/) + API FastAPI (api/) + tests
frontend/   Back-office propriétaire — SPA statique (HTML + modules ES, sans build)
```

## Installation (une fois)

```bash
# Base de données
createdb casaguide
psql -d casaguide -f db/schema.sql
psql -d casaguide -f db/seed.sql
psql -d casaguide -f db/migrations/001_pois_unique_source.sql

# Dépendances backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Démarrage

```bash
cd backend
cp .env.example .env      # puis renseigner les clés (openssl rand -hex 32)
uvicorn api.main:app --reload --host 0.0.0.0
```

`--host 0.0.0.0` rend le serveur joignable depuis les autres appareils du
réseau local (test iPhone : `http://<ip-du-mac>:8000/`, IP via
`ipconfig getifaddr en0`). Sans lui, uvicorn n'écoute que sur le Mac
(127.0.0.1) — piège classique après un redémarrage. Pour que les QR PDF
encodent une adresse scannable, définir `CASAGUIDE_PUBLIC_BASE_URL` dans
`.env` (ex. `http://192.168.18.231:8000`, puis le domaine en production).

`backend/.env` est chargé automatiquement au démarrage (voir `.env.example`
pour chaque variable). Back-office : http://localhost:8000/ · API docs : `/docs`.

```bash
# Tests d'intégration (aucun accès réseau requis — APIs simulées ; PostgreSQL requis)
cd backend && python -m pytest tests/ -v

# (alternative) Enrichissement réel en ligne de commande
python -m enrich.pipeline --property-id <uuid> --trigger initial
```

## Pipeline d'enrichissement

`adresse → Nominatim (géocodage) → Overpass/OSM (POI par catégorie, rayons du
seed) → OSRM (distances à pied/en voiture) → Claude (numéros d'urgence, règles
de tri et de bruit locales, descriptions) → PostgreSQL (statut "suggested",
validation par le propriétaire)`

Garanties : idempotent, ne touche jamais aux choix du propriétaire, JSON strict
validé, coûts IA comptabilisés par logement dans `api_costs`.

## Back-office propriétaire (frontend/)

SPA légère servie en statique par FastAPI (aucune étape de build) : HTML +
modules ES natifs, Leaflet pour les cartes, identité visuelle de
`guide_preview.html`. Écrans : connexion/inscription, « Mes logements »,
éditeur de guide (formulaire dynamique des sections généré depuis
`section_templates.field_schema`, secrets chiffrés, complétude, **photos &
documents par section** : ajout glisser-déposer, légendes, réordonnancement),
validation des POI suggérés (carte synchronisée, approuver/rejeter/éditer),
éditeur de position du logement sur carte.

### Scénario de bout en bout (démo)

Depuis `http://localhost:8000/`, l'accueil du back-office :

1. **Créer un compte** — onglet *Inscription* : nom, email, mot de passe (≥ 8
   caractères). Un abonnement d'essai *free* est attribué automatiquement.
2. **Créer le logement** — bouton *Nouveau logement* : nom, adresse, ville, pays
   (ex. *Villa Mar Azul*, *Calle Ejemplo 1*, *Orihuela Costa*, *ES*).
3. **Enrichir** — accepter la proposition *Lancer l'enrichissement* : le suivi en
   direct affiche géocodage → recherche des lieux → distances → IA. (Nécessite
   `ANTHROPIC_API_KEY` et un accès réseau OSM.)
4. **Valider 3 POI** — écran *Suggestions* : survoler la liste surligne la carte ;
   *Approuver* un hôpital, *Modifier* un restaurant (ajouter un coup de cœur),
   *Rejeter* un doublon. « Tout approuver » traite une catégorie d'un coup.
5. **Remplir 2 sections** — éditeur : *Check-in* (heure + déroulé) et *Wifi*
   (emplacement box + mot de passe chiffré). Marquer *Section complétée*
   (Cmd/Ctrl+S), la complétude globale progresse.
6. **Ajuster la position** (si le bandeau l'indique) — glisser le marqueur sur la
   carte, enregistrer, accepter le recalcul des distances.
7. **Publier** — bouton *Publier le guide* : le lien public `/g/{token}` est
   affiché (copiable) et le guide voyageur devient consultable.
