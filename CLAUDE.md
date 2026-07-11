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

**Source de vérité de l'avancement : `project_tracker.html`** (double-cliquer pour
l'ouvrir). À la fin de chaque session de travail, mettre à jour le bloc
`const PROJECT` de ce fichier : statuts TODO / IN_PROGRESS / BLOCKED / DONE /
LOCKED — une tâche n'est DONE qu'avec **date + preuve concrète** (fichier,
commit, résultat de test). Mettre aussi à jour le champ `updated`.

| Composant | État |
|---|---|
| `db/schema.sql` | Schéma PostgreSQL 15+ / PostGIS — testé, validé |
| `db/seed.sql` | 43 sections pré-définies + 27 catégories POI + 3 plans — idempotent, testé |
| `db/migrations/001` | Index unique pour l'idempotence des upserts POI — requis |
| `backend/enrich/` | Pipeline d'enrichissement complet — testé (2 tests d'intégration verts) |
| `backend/api/` | API FastAPI — auth JWT, CRUD logements + secrets chiffrés, sections, déclenchement du pipeline (tâche de fond), validation des POI, guide public `GET /g/{token}` — testé (9 tests d'intégration verts) |
| Frontend (back-office + guide PWA) | **À construire** (prochaine étape) |

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

# API FastAPI (back-office + guide public)
export CASAGUIDE_JWT_SECRET=$(openssl rand -hex 32)   # signature des jetons
export CASAGUIDE_SECRET_KEY=$(openssl rand -hex 32)   # AES-256 des secrets (§8)
uvicorn api.main:app --reload                  # docs interactives sur /docs
```

Variables d'environnement de l'API (aucun secret en dur) : `CASAGUIDE_JWT_SECRET`
(clé HS256 ; éphémère par processus si absente), `CASAGUIDE_SECRET_KEY` (clé
AES-256 hex/base64 des colonnes `property_secrets` ; les endpoints de secrets
répondent 503 si absente), `CASAGUIDE_JWT_EXPIRE_MIN`, `CASAGUIDE_CORS_ORIGINS`.

## Prochaines étapes (ordre recommandé, cf. §12 du CdC)

1. ✅ **API FastAPI** (`backend/api/`) : auth propriétaires (JWT), CRUD logements,
   déclenchement du pipeline (tâche de fond via `BackgroundTasks`, suivi par
   `/jobs`), validation/rejet/édition des POI suggérés, endpoint public
   `GET /g/{guide_token}` servant sections visibles + POI approuvés/édités +
   area_facts (jamais les secrets), avec entêtes `noindex` et cache. Restent à
   ajouter selon les besoins : upload media (S3), OAuth Google, rate-limiting,
   pool de connexions (`psycopg_pool`), traductions.
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
- Cohérence catégorie/tags OSM (M-01) : `overpass.category_matches` rejette les
  POI incohérents (agence/minimarket taggés `marketplace`, `office=*`,
  vétérinaire hors catégorie `veterinary`) ; aéroports limités aux aérodromes
  publics/IATA (pas de bases militaires ni d'aéroclubs). Toute nouvelle
  catégorie doit être ajoutée à `CATEGORY_TAGS` (les sélecteurs en dérivent).
- Perf Overpass : `overpass.fetch_grouped` regroupe les catégories par palier de
  rayon (`_RADIUS_BUCKETS`) en ~5 requêtes au lieu de ~25, puis re-filtre chaque
  catégorie à son rayon exact du seed. Un échec de palier marque toutes ses
  catégories `failed` (ré-enrichissables) sans casser le guide.
- Jobs orphelins : les `BackgroundTasks` ne survivent pas à un redémarrage
  d'uvicorn → le `lifespan` de l'API requalifie au démarrage les jobs `running`
  en `failed` (`repo.fail_orphan_running_jobs`). À terme : file persistante.
- Encodage : le stockage est en UTF-8 correct (psycopg) ; tout mojibake `U+FFFD`
  provient d'un **export/affichage** mal encodé, pas de la base — déclarer
  `charset=utf-8` et écrire les fichiers avec `encoding="utf-8"`.

## Enseignements du premier test réel (11/07/2026, Orihuela Costa — 125 POI, 3,45 ct d'IA)

Correctifs déjà appliqués pendant le test : échelle de repli du géocodage
(rooftop→street→city), miroirs Overpass avec bascule automatique (le serveur
principal renvoie 406 aux clients automatisés depuis 2026), disjoncteur OSRM,
tolérance aux échecs par catégorie, commits de progression en temps réel.

Traité par **M-01** (12/07/2026, commit dédié — voir `project_tracker.html`) :
1. ✅ Filtres qualité POI (aéroports publics/IATA, cohérence catégorie/tags,
   dédoublonnage santé) — `overpass.category_matches` + `_dedup_health_categories`.
2. ✅ Prompt descriptions anti-hallucination — `claude_enrich._POI_PROMPT`.
3. ✅ Jobs `failed` hors quota — `repo.count_jobs_current_month`.
4. ✅ Requalification des jobs orphelins au démarrage — `api/main.py` (lifespan).
6. ✅ Perf Overpass : regroupement par palier de rayon (~5 requêtes) —
   `overpass.fetch_grouped`. Diagnostic du guide de test : `supermarket`/`taxi`
   manquaient à cause d'**échecs Overpass 406 transitoires** (trop de requêtes
   séquentielles → corrigé par le regroupement + repli miroirs) ; `train_station`
   manquait car **aucune gare dans le rayon** (absence de donnée réelle).
7. ✅ Encodage : **aucun** `U+FFFD` en base (stockage UTF-8 correct) ; le mojibake
   observé était un artefact d'export/affichage (voir Pièges connus).

Restant :
5. UI (back-office/PWA, cf. M-03/M-05) : masquer `walk_min` au-delà de ~30 min
   (n'afficher que la voiture) ; éditeur de position du logement sur carte quand
   `geocode_accuracy != rooftop`.
8. Ops — Console Anthropic : activer le rechargement automatique du crédit pour
   que l'API accepte les requêtes (constaté empiriquement ; cf. M-02).
