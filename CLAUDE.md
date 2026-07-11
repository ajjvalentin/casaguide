# CLAUDE.md — Contexte projet pour Claude Code

## Projet

**CasaGuide** — SaaS multi-propriétaires de guides d'accueil numériques pour
logements de vacances. Un propriétaire saisit l'adresse de son logement et
complète une checklist pré-définie ; un pipeline IA pré-remplit les sections
« environnement » (hôpital, commerces, restaurants, urgences…) qu'il valide ;
le voyageur consulte un guide PWA multilingue avec carte interactive via
lien/QR code.

**Référence fonctionnelle : `docs/cahier_des_charges.md`** (v1.0). Les codes
§4, §5, §8… dans les commentaires du code renvoient à ce document. Le lire
avant toute évolution fonctionnelle.

## État actuel (juillet 2026)

| Composant | État |
|---|---|
| `db/schema.sql` | Schéma PostgreSQL 15+ / PostGIS — testé, validé |
| `db/seed.sql` | 43 sections pré-définies + 27 catégories POI + 3 plans — idempotent, testé |
| `db/migrations/001` | Index unique pour l'idempotence des upserts POI — requis |
| `backend/enrich/` | Pipeline d'enrichissement complet — testé (2 tests d'intégration verts) |
| API FastAPI | **À construire** (prochaine étape) |
| Frontend (back-office + guide PWA) | **À construire** |

## Stack et conventions

- **Python 3.12, psycopg 3, httpx, SDK anthropic** ; FastAPI prévu pour l'API
- **PostgreSQL + PostGIS** : geom en `GEOMETRY(Point, 4326)` ; distances via
  `::geography` ; jamais de calcul de distance côté Python en production
- Commentaires et docstrings **en français** ; identifiants en anglais
- Modèle IA par défaut : `claude-sonnet-4-6` (configurable via `CASAGUIDE_MODEL`)
- Config par variables d'environnement uniquement (`backend/enrich/settings.py`),
  **aucun secret en dur**
- Multi-tenant par `owner_id` : toute requête sur les données d'un logement
  doit filtrer par propriétaire côté API

## Invariants à ne jamais casser (couverts par les tests)

1. Un POI `approved` / `edited` / `rejected` par le propriétaire n'est
   **jamais** écrasé par un ré-enrichissement.
2. Relancer le pipeline est **idempotent** (aucun doublon de POI).
3. Toute réponse IA est du **JSON strict validé** avant insertion ; sinon le
   job passe en `failed` sans rien corrompre.
4. **Aucun appel API externe côté voyageur** : tout est pré-calculé en base.
5. Données sensibles (code boîte à clés, mot de passe wifi) : chiffrement
   applicatif → colonnes `BYTEA` de `property_secrets`, clé hors base.
6. Chaque appel IA est comptabilisé dans `api_costs` (tokens → centimes).

## Commandes

```bash
# Base de données (créer la base 'casaguide' d'abord)
psql -d casaguide -f db/schema.sql
psql -d casaguide -f db/seed.sql
psql -d casaguide -f db/migrations/001_pois_unique_source.sql

# Backend
cd backend
pip install -r requirements.txt
export CASAGUIDE_DB=postgresql://localhost/casaguide
export ANTHROPIC_API_KEY=sk-ant-...
python -m pytest tests/ -v                     # tests (aucun réseau requis)
python -m enrich.pipeline --property-id <uuid> # enrichissement réel
```

## Prochaines étapes (ordre recommandé, cf. §12 du CdC)

1. **API FastAPI** : auth propriétaires (JWT), CRUD logements, endpoints de
   déclenchement du pipeline (tâche de fond), validation/rejet des POI
   suggérés, endpoint public du guide (`/g/{guide_token}`) servant sections +
   POI approuvés + area_facts, avec cache.
2. **Back-office** : formulaire dynamique généré depuis
   `section_templates.field_schema` (types text/textarea/time/bool/number/
   select/url + groupes `repeat`), indicateur de complétude, écran de
   validation des suggestions.
3. **Guide voyageur PWA** : mobile-first, carte Leaflet + tuiles OSM, POI par
   catégorie (icône/couleur du seed), hors-ligne, sélecteur de langue.
4. **Traductions stockées** (`section_translations`, `poi_translations`,
   flag `is_stale`) puis Stripe, statistiques, accès par dates de séjour (V2).

## Pièges connus

- Serveurs OSM publics : 1 req/s max, User-Agent obligatoire → en production,
  prévoir OSRM auto-hébergé et/ou un fournisseur géré.
- `food_delivery` et `babysitter` : pas de tags OSM fiables → à enrichir via
  Claude + web search (V1.1), voir `CLAUDE_ONLY_CATEGORIES` dans `overpass.py`.
- L'upsert des POI exige la migration 001 (ON CONFLICT sur index partiel :
  la clause `WHERE source_ref IS NOT NULL` doit être répétée dans la requête).
- Guide public : `noindex` + token ≥ 128 bits, ne jamais exposer
  `property_secrets` sur l'endpoint public (déchiffrement à la demande,
  sections sensibles selon `access_mode`).
