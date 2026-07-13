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
| `backend/api/` | API FastAPI — auth JWT, CRUD logements + secrets chiffrés, sections, déclenchement du pipeline (tâche de fond), validation des POI, **médias par section** (upload/liste/service/ordre, M-12), `/stats`, `/recompute-distances`, guide public `GET /g/{token}` (+ `/g/{token}/media/{id}`) — testé (30 tests d'intégration verts) |
| `frontend/` | Back-office propriétaire — SPA statique (M-03/M-04/M-05/M-12) : connexion, Mes logements, éditeur de guide (formulaire dynamique + secrets + complétude + **photos & documents par section**), validation des POI (carte Leaflet), éditeur de position — servie par FastAPI |
| Config (M-02) | Chargement auto de `backend/.env` (`enrich/envfile.py`) ; `backend/.env.example` documenté ; avertissement de démarrage si clés manquantes |
| Stockage médias | `api/storage.py` — interface `Storage` abstraite + `LocalStorage` sous `MEDIA_ROOT` (prêt pour S3) |
| Guide voyageur PWA | **À construire** (M-08, base visuelle `guide_preview.html`) |

## Architecture frontend (`frontend/`, M-03/M-04/M-05)

- **SPA sans build** : HTML + modules ES natifs, servie en statique par FastAPI
  (`api/main.py` monte `frontend/` sur `/` **en dernier** — les routes `/api`,
  `/g`, `/health`, `/docs` sont déclarées avant et priment ; routage par ancre,
  le serveur ne sert jamais que `index.html` + assets). Aucun framework lourd.
- **Leaflet** (tuiles OSM) et **Lucide** (icônes) chargés par CDN ; l'app reste
  fonctionnelle si le CDN d'icônes tombe (libellés textuels toujours présents).
- **Identité** : tokens visuels de `guide_preview.html` (sable `#FAF7F2`, encre
  `#1E2A32`, mer `#0E5A73`, Fraunces titres / Instrument Sans texte, couleurs de
  chapitre du seed) — centralisés dans `frontend/css/app.css`.
- **Organisation** : `js/api.js` (client + 401→déconnexion), `js/store.js`
  (jeton en sessionStorage), `js/ui.js` (DOM/toasts/modales), `js/nav.js`
  (routage par ancre), `js/app.js` (ossature), `js/views/*` (login, properties,
  editor, pois), `js/components/dynform.js` (formulaire généré depuis
  `field_schema`). **Tout passe par l'API existante** (même origine, pas de CORS).
- **Secrets** (§8) : les champs chiffrés (wifi_pass, keybox_code) sont saisis dans
  l'éditeur mais envoyés à `PUT /secrets` (jamais dans le contenu de section). Le
  `PUT /secrets` **remplace** l'objet complet → l'éditeur conserve l'état des
  autres secrets et renvoie l'objet entier à chaque sauvegarde.
- **Médias** (M-12) : `js/components/media.js` — zone « Photos & documents » par
  section, montée par l'éditeur. Upload multipart via `api.uploadMedia` (le client
  ne fixe pas Content-Type) ; les vignettes protégées sont chargées avec le jeton
  (`api.mediaBlobUrl` → `URL.createObjectURL`, révoquées au re-rendu). Gros JPEG/WebP
  réduits côté client (canvas) avant envoi ; PNG laissés tels quels (le serveur
  ré-encode et retire l'EXIF de toute façon).
- **Test navigateur** : Chrome headless (`--dump-dom`, `--screenshot`) contre un
  harnais à `fetch` simulé (créé puis supprimé — ne jamais laisser de fichier de
  test dans `frontend/`, qui est servi publiquement en statique).

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
cp .env.example .env   # M-02 : chargé automatiquement au démarrage (aucun export requis)
uvicorn api.main:app --reload                  # docs interactives sur /docs
```

Configuration (M-02) : `backend/.env` est chargé automatiquement au démarrage
(`enrich/envfile.py`, appelé par `api/__init__.py` et `enrich/__init__.py`,
`override=False` → n'écrase jamais l'environnement exporté ni les valeurs des
tests). Modèle documenté dans `backend/.env.example` ; `.env` est dans
`.gitignore`. Si `CASAGUIDE_JWT_SECRET` ou `CASAGUIDE_SECRET_KEY` manquent, le
`lifespan` journalise un avertissement listant exactement quoi mettre dans `.env`.

Variables d'environnement de l'API (aucun secret en dur) : `CASAGUIDE_JWT_SECRET`
(clé HS256 ; éphémère par processus si absente), `CASAGUIDE_SECRET_KEY` (clé
AES-256 hex/base64 des colonnes `property_secrets` ; les endpoints de secrets
répondent 503 si absente), `MEDIA_ROOT` (répertoire de stockage des médias,
défaut `var/media`, relatif à `backend/`, exclu de git), `CASAGUIDE_JWT_EXPIRE_MIN`,
`CASAGUIDE_CORS_ORIGINS`, `CASAGUIDE_MAX_UPLOAD_BYTES` (10 Mo par défaut).

## Prochaines étapes (ordre recommandé, cf. §12 du CdC)

1. ✅ **API FastAPI** (`backend/api/`) : auth propriétaires (JWT), CRUD logements,
   déclenchement du pipeline (tâche de fond via `BackgroundTasks`, suivi par
   `/jobs`), validation/rejet/édition des POI suggérés, endpoint public
   `GET /g/{guide_token}` servant sections visibles + POI approuvés/édités +
   area_facts (jamais les secrets), avec entêtes `noindex` et cache. Restent à
   ajouter selon les besoins : upload media (S3), OAuth Google, rate-limiting,
   pool de connexions (`psycopg_pool`), traductions.
2. ✅ **Back-office** (`frontend/`, M-03/M-04/M-05) : formulaire dynamique généré
   depuis `section_templates.field_schema` (text/textarea/time/bool/number/
   select/url/phone + groupes `repeat` + secrets chiffrés), complétude par
   chapitre, validation des POI (carte Leaflet synchronisée, actions groupées),
   éditeur de position sur carte. Restent : upload media (S3), traductions UI.
3. **Guide voyageur PWA** (M-08) : mobile-first, carte Leaflet + tuiles OSM, POI par
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
- Médias (M-12) : le type est validé par les **magic bytes** (`media_files.sniff`),
  jamais par le nom ni le Content-Type déclaré ; un média n'apparaît dans le guide
  public que si le logement est **publié** et sa section **visible** (`repo.guide_media`
  / `get_public_media`) — ne jamais servir un média de section masquée. Les clés de
  stockage sont non devinables et confinées sous `MEDIA_ROOT` (`storage.LocalStorage`
  rejette tout path traversal). Rattacher un média à une section la crée si besoin
  (`repo.ensure_section`) → une section « photo seule » peut exister sans contenu.

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
