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
| `db/seed.sql` | 43 sections pré-définies + 28 catégories POI (dont `bus_station`, M-21) + 3 plans — idempotent, testé |
| `db/migrations/001` | Index unique pour l'idempotence des upserts POI — requis |
| `db/migrations/003` | Colonne `cuisine` sur `pois` (type de cuisine, M-16) — idempotent |
| `db/migrations/004` | Colonne `wifi_networks_enc` sur `property_secrets` (multi-wifi, M-15) — idempotent |
| `backend/enrich/` | Pipeline d'enrichissement complet — testé (2 tests d'intégration verts) |
| `backend/api/` | API FastAPI — auth JWT, CRUD logements + secrets chiffrés, sections, déclenchement du pipeline (tâche de fond), validation des POI, **médias par section** (upload/liste/service/ordre, M-12), `/stats`, `/recompute-distances`, **traductions (M-09)** : `POST /{id}/translate` (tâche de fond, trigger='translate', hors quota) + `GET /{id}/translation-status`, **guide voyageur (M-08/M-09)** : `GET /g/{token}[?lang=]` sert une **page HTML** localisée (rendu serveur `api/guide_page.py`), `GET /g/{token}/data[?lang=]` le JSON (`charset=utf-8`), `GET /g/{token}/secrets` (wifi/boîte à clés, mode 'link'), `/g/{token}/media/{id}`, `/g/{token}/manifest.webmanifest`, `/guide/sw.js` — testé (51 tests d'intégration/unitaires verts) |
| `frontend/` | Back-office propriétaire — SPA statique (M-03/M-04/M-05/M-06/M-07/M-09/M-12) : connexion, Mes logements, éditeur de guide (formulaire dynamique + secrets + complétude + **photos & documents par section** + **aperçu/QR wifi + téléchargement PNG, M-06** + **groupe « Équipe d'entretien » (sections staff) avec lien `/s`, M-13** + **bouton « QR à imprimer » PDF, M-07** + **bouton « Traductions » avec état à jour/périmé, M-09**), validation des POI (carte Leaflet), éditeur de position, **choix d'offre à l'inscription + page « Mon abonnement » (#/abonnement, V2-05a)** — servie par FastAPI |
| Multilingue guide (M-09) | **Fait** — traductions FR→EN/ES **générées et stockées** (`enrich/translate.py`, tables `section_translations`/`poi_translations` + `is_stale`), jamais à la volée (invariant 4). Modèle dédié `CASAGUIDE_TRANSLATE_MODEL` (Haiku). Seuls les champs texte + `body_md` + descriptions/coups de cœur POI sont traduits (jamais heures/booléens/URLs/secrets). (Re)traduction ciblée à la (re)publication et via `/translate`. Guide localisé côté serveur (`?lang=`), repli élégant sur le fr, sélecteur de langue. Coûts dans `api_costs` (operation='translate') |
| Cahier équipe d'entretien (M-13) | Schéma : `audience` (guest\|staff) sur `section_templates`, `staff_token` (128 bits) sur `properties` — schema.sql + `db/migrations/002`. Seed : chapitre « S » (5 sections staff). Page publique `GET /s/{staff_token}` (rendu `guide_page.render_staff`, variante sobre check-list, **accessible même en brouillon**, jamais de secrets/POI). Étanchéité guest↔staff dans les deux sens (invariant 7) |
| Affiche QR imprimable (M-07) | `api/poster.py` (reportlab) → `GET /api/properties/{id}/guide-poster.pdf` (A5/A4, propriétaire uniquement) : nom du logement, QR du lien du guide, mot d'accueil FR/EN, identité sable/mer |
| Auth transactionnel (V2-08) | **Fait** — mailer injectable (`deps.get_mailer` : `SmtpMailer` SSL Infomaniak + `ConsoleMailer` dev/tests), gabarits FR `api/emails.py`. **Mot de passe oublié** : `POST /api/auth/forgot` (200 constant anti-énumération, email en tâche de fond, cadence 2 min), `POST /api/auth/reset` (jeton 256 bits **haché SHA-256** en table `password_resets`, expiration 60 min, usage unique). **Vérification d'email** : lien à l'inscription (non bloquant), `POST /api/auth/verify-email` (idempotent), `POST /api/auth/resend-verification`. `owners.email_verified` exposé par `/me`. Migrations 005 (table) + 006 (grand-périsage des comptes existants). Front : routes publiques `#/forgot`, `#/reset/{token}`, `#/verify/{token}` (`js/views/reset.js`) + bandeau « vérifiez votre email » (`app.js`) — testé (22 tests + parcours headless) |
| Plans & abonnements (V2-05a) | **Fait (volets 1-3)** — couche d'accès `api/plans.py` branchée sur le modèle existant `plans`/`subscriptions` (CdC §10) : `get_subscription` / `get_plan` (repli 'free' + warning si abonnement manquant, jamais None) / `check_quota(owner_id, resource)` pour `properties` \| `enrichments` \| `langs` (lit `max_properties`, `enrich_quota` mensuel/logement, `features`) / `cap_target_langs` (plafond langues, source comprise) / `wants_watermark`. Inscription crée toujours une ligne `subscriptions` (`plan_id`='free', **`status='active'`** — pas de logique d'essai). Migration 007 : rattrapage idempotent d'un abo 'free'. Attribution manuelle par email : `ops/set_plan.py`. **Application serveur (volet 2)** : refus **402 `quota_exceeded`** (helper `api/quota.py`, `detail={code,message FR}`) sur `POST /api/properties` (au-delà de `max_properties`) et `POST .../enrich` (au-delà de `enrich_quota`, **remplace l'ancien 429**) ; traductions **plafonnées** par `cap_target_langs` (le runner de traduction reçoit désormais `target_langs`, `deps.TranslationRunner`), plan gratuit → 0 cible → `/translate` renvoie 402, publication ne génère aucune traduction ; **watermark** « Créé avec Holaguia » dans le SSR du guide (`guide_page._watermark_html`, flag via `repo.get_plan_by_guide_token`) si `features.watermark`. **Downgrade non destructif** : aucune donnée/traduction supprimée, seule la création est bloquée. **UI back-office (volet 3)** : endpoints `GET /api/plans` (public, catalogue) + `GET /api/subscription` (auth : plan + jauges d'usage) → `routers/billing.py` ; inscription avec **choix d'offre** (gratuite présélectionnée, payantes « bientôt », prix depuis l'API) `views/login.js` ; page **« Mon abonnement »** `#/abonnement` (`views/subscription.js` : plan courant, jauges logements/enrichissements/langues, boutons de changement inactifs) ; refus quota interceptés côté front par `js/quota.js` (`handleQuotaError` → encart « changez d'offre », jamais d'`alert()`) dans création de logement, enrichissement, traduction. Aucun quota codé en dur (invariant 8) — testé (`tests/test_plans.py` 13 + intégration : quotas, downgrade lecture seule, watermark, `/plans` & `/subscription` ; parcours front vérifié en headless). *Suite : white-label du poster PDF (marque fixe aujourd'hui).* |
| Facturation Stripe (V2-05b) | **Fait (dév/Test ; validation 4242 avec André en attente de ses clés Test)** — passerelle injectable `api/billing_stripe.py` (`StripeGateway`, `deps.get_stripe` → None sans clé → 503 propre, même motif que le mailer) ; `LiveStripeGateway` (StripeClient v1 ; `construct_event` = vérif signature + `json.loads`). **Checkout** `POST /api/billing/checkout` (auth ; plan solo/pro ; Customer rattaché **avant** la session → résolution owner par `customer_id` quel que soit l'ordre des webhooks ; price depuis `plans.stripe_price_id` ; 422 free/inconnu, 503 non synchronisé). **Webhook** `POST /api/stripe/webhook` (public, source de vérité — invariant 9) : `checkout.session.completed` (lien Customer), `customer.subscription.created/updated` (**autorité** : plan via price→plan, statut mappé, `current_period_end`), `.deleted` (retour `free` non destructif), `invoice.payment_failed` (`past_due`) ; dispatch `api/stripe_events.py` ; idempotence `stripe_events` (migration 008). **Portail** `POST /api/billing/portal` (409 sans Customer). **Sync** `ops/stripe_sync_products.py` (plans→Products/Prices, idempotent, archivage non destructif d'un ancien prix). Front : `#/abonnement` boutons réels (Checkout/portail via `js/redirect.js`, bandeau `?checkout=success|cancel`), chooser d'inscription payant → Checkout après création. Runbook `docs/stripe.md`. Testé (`tests/test_stripe.py` 8 + intégration `test_api.py` : checkout, idempotence, mapping statuts, upgrade, deleted→free, signature→400, portail ; parcours front headless 13/13). *Suite : mode Live (échange de clés + re-sync) ; facturation annuelle ; Stripe Tax.* |
| Config (M-02) | Chargement auto de `backend/.env` (`enrich/envfile.py`) ; `backend/.env.example` documenté ; avertissement de démarrage si clés manquantes |
| Stockage médias | `api/storage.py` — interface `Storage` abstraite + `LocalStorage` sous `MEDIA_ROOT` (prêt pour S3) |
| Guide voyageur PWA (M-08) | **Fait** — page HTML mobile-first rendue par `api/guide_page.py`, app shell `frontend/guide/` (modules ES : `app.js` carte/filtres/visionneuse/secrets, `qr.js` QR wifi autonome, `sw.js` hors-ligne, manifest par guide, icônes). Identité `guide_preview.html`. Multilingue (M-09) **fait**. **Hors-ligne complet (M-10) fait** : `sw.js` (v15) pré-charge les tuiles OSM de la zone (zooms 13-16, ~148 tuiles, séquentiel/poli) et les sert cache-first ; message discret hors zone. **Liens de partage (M-25) faits** : Open Graph/Twitter + og:image (photo ou image de marque `api/og_image.py`) + slug `/g/{slug}-{token}`. **Lisibilité (V2-09) faite** : TROIS onglets (Le logement / Urgences / Autour de vous, état dans le hash, `app.js initTabs`) + listes de lieux repliées (4 + « Voir les N autres », `initCategoryLists`) |

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
7. **Étanchéité guest/staff (M-13)** : une section `audience='staff'` (cahier de
   l'équipe d'entretien) ne sort **jamais** sur `/g` ni `/g/{token}/data` (ni son
   média) ; une section `audience='guest'` ne sort **jamais** sur `/s`. Le cahier
   `/s/{staff_token}` n'expose **jamais** de secrets ni de POI/carte. Chaque sens
   est couvert par un test dédié (`test_staff_and_guest_are_watertight_both_ways`).
8. **Plans & quotas (V2-05a)** : la définition des plans vit **en base**
   (`plans` + seed) — aucun quota codé en dur en Python/JS. Les quotas sont
   appliqués **côté serveur uniquement** (refus **HTTP 402**, `detail.code =
   'quota_exceeded'`, message FR) ; le front peut griser, la vérité est dans
   l'API. Un **downgrade ne supprime jamais de données** : logements/langues
   excédentaires deviennent lecture seule, jamais effacés. Tout passe par la
   couche `api/plans.py` (`get_plan` → repli 'free' + warning si abonnement
   manquant, jamais None ; `check_quota` ; `cap_target_langs` ; `wants_watermark`).
9. **Facturation Stripe (V2-05b)** : le **webhook** (`POST /api/stripe/webhook`)
   est la **seule source de vérité** de `subscriptions.status/plan_id/
   current_period_end` — le `success_url` de Checkout ne modifie **jamais**
   l'abonnement (le front n'affiche qu'un bandeau « confirmation en cours »).
   Chaque webhook : **signature vérifiée** (400 sinon), **idempotence** via
   `stripe_events` (un `event.id` déjà reçu est accusé mais non retraité). Les
   **prix** vivent dans `plans.price_month_cts` et sont *synchronisés vers*
   Stripe (`ops/stripe_sync_products.py`), jamais l'inverse ni en dur. Un retour
   à `free` (annulation) reste **non destructif** (invariant 8). Sans clé Stripe,
   `/api/billing/*` et le webhook répondent **503** (mode dégradé propre).

## Commandes

```bash
# Base de données (créer la base 'casaguide' d'abord)
psql -d casaguide -f db/schema.sql
psql -d casaguide -f db/seed.sql
psql -d casaguide -f db/migrations/001_pois_unique_source.sql
psql -d casaguide -f db/migrations/002_staff_cahier.sql   # audience + staff_token (M-13)
psql -d casaguide -f db/migrations/003_pois_cuisine.sql   # colonne cuisine sur pois (M-16)
psql -d casaguide -f db/migrations/004_wifi_networks.sql  # wifi_networks_enc (multi-wifi, M-15)
psql -d casaguide -f db/migrations/007_backfill_free_subscriptions.sql # abo 'free' de rattrapage (V2-05a)
psql -d casaguide -f db/migrations/008_stripe_billing.sql # stripe_events + plans.stripe_price_id (V2-05b)

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
`CASAGUIDE_CORS_ORIGINS`, `CASAGUIDE_MAX_UPLOAD_BYTES` (10 Mo par défaut),
`CASAGUIDE_PUBLIC_BASE_URL` (origine publique des liens du QR imprimable M-07 **et
des emails transactionnels V2-08** — en production `https://holaguia.com` ; à
défaut, `request.base_url`), `CASAGUIDE_ASSET_VERSION` (M-11 : SHA git stampé
par `deploy.sh` → cache-busting des assets ; défaut `dev`). **Emails
transactionnels (V2-08)** : `CASAGUIDE_SMTP_HOST` / `CASAGUIDE_SMTP_PORT` (465) /
`CASAGUIDE_SMTP_USER` / `CASAGUIDE_SMTP_PASSWORD` / `CASAGUIDE_SMTP_FROM`
(`Holaguia <no-reply@holaguia.com>` par défaut) — prestataire **Infomaniak**
(`mail.infomaniak.com:465` SSL). Sans HOST+USER+PASSWORD, repli **ConsoleMailer**
(les emails sont journalisés au lieu d'être envoyés) + avertissement au démarrage.
Le mot de passe SMTP est renseigné à la main dans le `.env` du serveur (jamais
committé). Options : `CASAGUIDE_AUTH_TOKEN_TTL_MIN` (60, validité des jetons
réinit/vérif), `CASAGUIDE_FORGOT_MIN_INTERVAL_S` (120, cadence des demandes de
réinitialisation par email). **Facturation Stripe (V2-05b)** :
`CASAGUIDE_STRIPE_SECRET_KEY` (clé secrète API — `sk_test_…` en Test, `sk_live_…`
en prod ; absente → `/api/billing/*` et le webhook répondent 503, reste de l'app
intact) et `CASAGUIDE_STRIPE_WEBHOOK_SECRET` (`whsec_…` de signature des webhooks
— `stripe listen` en local, endpoint du Dashboard en prod ; absent → webhook 503).
Les deux sont renseignés à la main dans le `.env` du serveur (jamais committés).
Runbook complet : `docs/stripe.md`.

## Production (M-11) — **runbook complet : `docs/deploiement.md`**

**EN LIGNE** sur VPS Infomaniak (Ubuntu 24.04, UE) : **`https://holaguia.com`**
(marque **Holaguia**, adresse canonique — HTTPS de confiance Let's Encrypt, M-28).
`www.holaguia.com`, `holaguia.ch`, l'ancien domaine technique `guide.holaquetalimmo.es`
(M-27) et l'ancienne adresse par IP `http(s)://179.237.85.250` redirigent tous en
**301** vers `holaguia.com` (liens/QR déjà partagés préservés). `holaguia.es` suivra
dès que la délégation .es sera publiée (bloc **prêt-commenté** dans `ops/Caddyfile`).
Architecture volontairement **simple, sans Docker** : Caddy
(frontal :80/:443, cert Let's Encrypt auto-renouvelé, HSTS) → uvicorn
`127.0.0.1:8000` (systemd `casaguide`) → PostgreSQL 16 + PostGIS **local** (jamais
exposé, peer auth).

- **Serveur** : `ssh -i ~/.ssh/casaguide_vps ubuntu@179.237.85.250` (sudo sans mdp).
- **Utilisateur applicatif** `casaguide` (non-root), code dans `/opt/casaguide`
  (clone GitHub via **deploy key** ed25519 lecture seule).
- **Déploiement en UNE commande** : `sudo -u casaguide /opt/casaguide/deploy.sh`
  (pull, pip si `requirements` changé, migrations+seed idempotents, version
  d'assets, restart via sudoers restreint, healthcheck). Idempotent.
- **Config** : `/opt/casaguide/backend/.env` (secrets générés sur place, `600`,
  hors dépôt) + `.env.deploy` (écrit par `deploy.sh`, `CASAGUIDE_ASSET_VERSION`).
  `ANTHROPIC_API_KEY` = **placeholder** tant qu'elle n'est pas fournie à la main
  (seul l'enrichissement IA en dépend).
- **Sécurité** : ufw (22/80/443), fail2ban (sshd), unattended-upgrades ; uvicorn
  local uniquement ; systemd durci ; PostgreSQL en socket local.
- **Cache-busting (dette résolue)** : `api/assets.py` — `?v=<sha>` sur les balises
  JS/CSS (`index.html` + pages guide/staff via `guide_page.py`), statiques servis
  en `Cache-Control: no-cache` (`RevalidatingStaticFiles`), SHA injecté dans le
  nom des caches du service worker (`/guide/sw.js`, placeholder `__ASSET_VERSION__`).
  Chaque déploiement invalide caches navigateur **et** SW sans intervention.
- **Sauvegardes** : timer systemd nocturne (`ops/casaguide-backup.*`, `pg_dump -Fc`
  + médias, rotation 14 j) ; restauration `ops/casaguide-restore.sh` **avec sudo**
  (postgis non « trusted » → extension recréée par `postgres`), testée en base témoin.
- **Bascule domaine + Let's Encrypt** : documentée (un bloc du `Caddyfile` +
  décommenter HSTS + `CASAGUIDE_PUBLIC_BASE_URL`), cf. `ops/Caddyfile` et le runbook.

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
3. ✅ **Guide voyageur PWA** (M-08) : `GET /g/{token}` sert une page HTML mobile-first
   (rendu serveur, `api/guide_page.py`) reprenant `guide_preview.html` ; app shell
   `frontend/guide/` (carte Leaflet, filtres par chapitre, visionneuse, QR wifi,
   service worker hors-ligne, manifest par guide). JSON sur `/g/{token}/data`,
   secrets à la demande sur `/g/{token}/secrets` (mode 'link'). Restent : sélecteur
   de langue actif (M-09), cache des tuiles (M-10).
4. ✅ **Traductions stockées** (M-09 : `section_translations`, `poi_translations`,
   flag `is_stale`, `enrich/translate.py`, guide `?lang=`). Restent : DE/NL et
   relecture propriétaire (V2-06), traduction des `area_facts` (restent en fr).
   Puis Stripe, statistiques, accès par dates de séjour (V2).

## Pièges connus

- Refus de quota (V2-05a) : un dépassement renvoie **402** avec un `detail`
  **objet** `{"code":"quota_exceeded","message":<FR>}` (pas une chaîne). Côté
  front, `api.js handleResponse` extrait `detail.message` pour l'affichage et
  conserve `detail` sur `ApiError.detail` → `js/quota.js handleQuotaError` teste
  `detail.code === 'quota_exceeded'`. Toute nouvelle action soumise à quota doit
  passer son erreur à `handleQuotaError` (encart « changez d'offre ») avant tout
  `toast`/message générique. La vérité des quotas est **serveur** (`api/plans.py`
  `check_quota` / `cap_target_langs`) : le front ne fait que griser/prévenir.
  Traduction : le runner de fond reçoit désormais `target_langs` **déjà plafonné**
  (`deps.TranslationRunner`, signature `(pid, job_id, target_langs)`) — ne jamais
  re-dériver les langues depuis `settings.translate_langs` dans le runner, sinon
  le plafond du plan est court-circuité. Plan gratuit → 0 cible → aucune
  traduction publiée, et **les traductions déjà en base ne sont jamais effacées**
  au downgrade (repli fr, invariant 1).
- Facturation Stripe (V2-05b) : le webhook est la **seule** autorité d'état
  (invariant 9) — ne jamais écrire `subscriptions.status/plan_id/
  current_period_end` depuis le `success_url` ni un endpoint synchrone. Le
  `customer_id` est rattaché à l'abonnement **au moment du Checkout** (avant la
  session), pas au retour du webhook → la résolution owner par `customer_id`
  marche quel que soit l'**ordre d'arrivée** des événements (subscription.updated
  peut précéder checkout.session.completed). Le **plan** est fixé par les
  événements `customer.subscription.created/updated` (via `price→plan`,
  `repo.get_plan_by_stripe_price_id`), **pas** par `checkout.session.completed`
  (le prix n'y figure pas). L'**idempotence** passe par `repo.stripe_event_begin`
  (INSERT … ON CONFLICT DO NOTHING atomique) : si le traitement échoue et que la
  transaction est annulée, la ligne `stripe_events` disparaît aussi → Stripe
  rejouera (comportement voulu). **Signature** : `construct_event` vérifie via
  `stripe.Webhook.construct_event` puis fait `json.loads` du payload brut (pas
  d'accès aux internals de la lib — `StripeObject.get()` **lève** `AttributeError`,
  ne jamais compter dessus). Un événement Stripe réel porte toujours un champ
  top-level `"object": "event"` (la lib le lit) : tout payload de test doit
  l'inclure. **`current_period_end`** a migré au niveau de l'item de facturation
  dans les versions récentes de l'API — `stripe_events._period_end` lit les deux
  emplacements. Les **prix** viennent de `plans` (invariant : jamais en dur) ;
  changer un prix = éditer le seed + relancer `ops/stripe_sync_products.py`
  (nouveau Price, ancien **archivé** non supprimé). L'accès aux quotas ne dépend
  QUE de `plan_id` (jamais du `status`) : `past_due` conserve donc l'accès (grâce
  le temps des relances), seule l'annulation (`subscription.deleted` → `free`) le
  retire. **Redirections front** (`js/redirect.js`) : `window.location.assign` est
  *unforgeable* (non stubable) → les vues passent par `redirect()`, remplaçable en
  test headless via **import map** (le harnais reste hors `frontend/`).
- **Mocks Stripe non représentatifs (OPS-1)** : un `StripeObject` réel **n'est
  pas** un `dict` (MRO `[StripeObject, object]`), n'expose ni `.get`/`.items`/
  `.keys` (interceptés par `__getattr__` → `AttributeError`) et n'implémente pas
  le protocole de mapping → **`dict(stripe_obj)` lève `KeyError: 0`** (il l'itère
  comme une séquence). Pour lire des metadata/`recurring`, passer par
  `.to_dict()` (voir `ops/stripe_sync_products.py` `_as_dict`), **jamais**
  `dict(obj)` ni `obj.metadata.get(...)`. Ce bug était **invisible en test** : les
  fakes originaux stockaient des `SimpleNamespace` dont `metadata` était un `dict`
  simple → `dict()` marchait, alors qu'en prod le sync plantait dès que la liste
  de produits était **non vide** (solo créé, puis crash sur pro). Règle : tout
  fake d'objet Stripe doit se comporter comme la vraie lib — `tests/test_stripe.py`
  construit désormais de **vrais** `stripe.StripeObject` (`construct_from`) et un
  `list()` renvoyant un objet `.data` façon `ListObject`. Un mock plus « simple »
  que le réel masque les bugs de contrat plutôt que de les révéler.
- **Scripts `ops/` et `.env` (OPS-1)** : lancés à la main sur le serveur, ils
  n'héritent **pas** de l'`EnvironmentFile` systemd → ils chargent `backend/.env`
  eux-mêmes via `ops/opsenv.py` (option `--env-file`, `override=False`). **Ne
  jamais** `source backend/.env` en bash : il contient des valeurs non-shell
  (`CASAGUIDE_SMTP_FROM=Holaguia <no-reply@holaguia.com>` → les chevrons sont des
  redirections, `source` explose). `opsenv.parse_env` conserve les valeurs à
  espaces/chevrons littéralement.
- **`deploy.sh` — détection pip (OPS-1)** : l'install des dépendances se décide en
  comparant le hash de `requirements.txt` à un **stamp** écrit dans le venv
  (`$VENV/.requirements.sha1`) **après** chaque pip réussi — **pas** le delta
  avant/après `git pull`. Un pull no-op (code déjà présent) sur un venv périmé
  déclencherait sinon un « pip ignoré » à tort (bug V2-05b : `stripe` ajouté mais
  jamais installé → `ModuleNotFoundError` au restart). Garde-fou : échec du
  healthcheck `/health` → inspection du journal + suggestion de remise à niveau du
  venv. Ne pas revenir à une comparaison basée sur le pull.
- **Service worker du guide (cache-busting)** : les fichiers de `frontend/guide/*`
  sont servis cache-first par `sw.js`. Toute modification d'un de ces fichiers
  DOIT s'accompagner de l'incrément de `VERSION` dans `frontend/guide/sw.js`,
  sinon les visiteurs (et le back-office, qui importe `guide/qr.js`) reçoivent
  l'ancienne version — symptôme vécu le 14/07 : page blanche du back-office
  (import ES cassé sur un module périmé).
- Serveurs OSM publics : 1 req/s max, User-Agent obligatoire → en production,
  prévoir OSRM auto-hébergé et/ou un fournisseur géré.
- Ajout manuel de POI (M-22) : `GET /api/properties/{id}/pois/search` est un
  **proxy Nominatim côté serveur** (`api/poi_search.py`) — le navigateur n'appelle
  jamais Nominatim directement (pas de fuite d'User-Agent, politesse centralisée).
  Il respecte la **politique d'usage Nominatim** : User-Agent obligatoire
  (`settings.user_agent`) **et au plus 1 req/s** (`poi_search._throttle`, borné par
  `settings.politeness_delay_s`, `sleep`/`now` injectables → les tests posent
  `politeness_delay_s = 0`). La recherche est déclenchée par un **debounce 400 ms**
  côté client (`js/views/pois.js`) pour ne pas marteler le service à chaque frappe.
  La catégorie est **devinée** par inversion de `overpass.CATEGORY_TAGS`
  (class/type OSM → `category_code`, repli `sight`) puis corrigeable par le
  propriétaire. `POST /api/properties/{id}/pois` crée un POI `source='owner'`,
  `status='approved'` (jamais écrasé, invariant 1), distances calculées à
  l'insertion via `distance.compute_distances` — **hors quota d'enrichissement**
  (aucun job). Le repli manuel « aéroport en SQL » documenté plus bas (M-18) est
  désormais faisable depuis l'UI (mais bus_station M-21 couvre le hub d'arrivée
  quand il n'y a pas de gare ferroviaire).
- `food_delivery` et `babysitter` : pas de tags OSM fiables → à enrichir via
  Claude + web search (V1.1), voir `CLAUDE_ONLY_CATEGORIES` dans `overpass.py`.
- L'upsert des POI exige la migration 001 (ON CONFLICT sur index partiel :
  la clause `WHERE source_ref IS NOT NULL` doit être répétée dans la requête).
- Guide public : `noindex` + token ≥ 128 bits, ne jamais exposer
  `property_secrets` sur l'endpoint public (déchiffrement à la demande,
  sections sensibles selon `access_mode`).
- Jetons transactionnels (V2-08) : le jeton brut (256 bits, `token_urlsafe(32)`)
  n'est **jamais** stocké ni journalisé — seule son empreinte **SHA-256** est en
  base (`security.hash_reset_token`). SHA-256 nu (sans sel) suffit car le jeton est
  déjà à haute entropie (contrairement à un mot de passe). La table
  `password_resets` sert aux **deux** usages via `purpose` (`reset` | `verify`) ;
  toute requête qui la lit doit filtrer `purpose`. `/forgot` répond **toujours**
  200 au même message et envoie l'email en `BackgroundTask` → délai constant
  (anti-énumération) ; ne jamais renvoyer d'indice sur l'existence du compte. La
  migration 006 (grand-périsage) est sûre au rejeu **parce qu'**elle ne marque que
  les comptes SANS jeton `verify` — ne pas la remplacer par un `UPDATE` global qui
  re-vérifierait les nouveaux comptes en attente à chaque déploiement. Bandeau de
  vérification côté front : n'apparaît que si `email_verified === false` **strict**
  (jamais sur un champ absent → évite les faux positifs sur un profil en cache
  d'avant V2-08). Les emails partent en tâche de fond : une panne SMTP ne casse
  jamais l'inscription ni la demande de réinitialisation (best-effort, comme tout
  `BackgroundTasks` — ne survit pas à un redémarrage d'uvicorn).
- Cohérence catégorie/tags OSM (M-01) : `overpass.category_matches` rejette les
  POI incohérents (agence/minimarket taggés `marketplace`, `office=*`,
  vétérinaire hors catégorie `veterinary`) ; aéroports limités aux aérodromes
  publics/IATA (pas de bases militaires ni d'aéroclubs). Toute nouvelle
  catégorie doit être ajoutée à `CATEGORY_TAGS` (les sélecteurs en dérivent).
- Perf Overpass : `overpass.fetch_grouped` regroupe les catégories par palier de
  rayon (`_RADIUS_BUCKETS`) en ~5 requêtes au lieu de ~25, puis re-filtre chaque
  catégorie à son rayon exact du seed. Un échec de palier marque toutes ses
  catégories `failed` (ré-enrichissables) sans casser le guide.
- Fiabilisation de la moisson (M-18) :
  - **Ré-essai différé** : `pipeline.run_with_retries` (branché dans
    `deps._default_runner`) exécute le pipeline puis, si des catégories ont
    échoué, rejoue **uniquement les manquantes** (`_retry_failed`) après
    `RETRY_DELAY_S` (180 s), jusqu'à `MAX_RETRIES` (3). C'est le **même job**
    (même `job_id`, quota inchangé) ; chaque passage est journalisé dans
    `enrichment_jobs.steps` sous `retry_1`, `retry_2`… Le job reste `done` (les
    retries ne changent jamais son statut) et **aucun POI arbitré n'est touché**
    (l'upsert ne réécrit que `status='suggested'`, invariant 1). `sleep`
    injectable pour les tests. NB : la tâche de fond `run_with_retries` bloque un
    thread du threadpool jusqu'à ~9 min ; elle ne survit pas à un redémarrage
    d'uvicorn (best-effort, comme tout `BackgroundTasks`).
  - **Requête aéroport (100 km)** : le palier ≥ `overpass_far_bucket_m` (50 km)
    est déjà une requête Overpass **séparée** (son propre palier de rayon) et
    reçoit un **timeout dédié plus long** (`overpass_timeout_far_s`, 60 s) —
    `overpass._bucket_timeout`, propagé au `[timeout:]` serveur et au timeout HTTP.
  - **Repli manuel aéroport** (déjà pratiqué en prod) : si l'aéroport reste
    introuvable après les retries (absence de donnée OSM fiable dans le rayon),
    l'insérer à la main en `source='owner'` (jamais écrasé, invariant 1) :
    ```sql
    INSERT INTO pois (property_id, category_code, name, geom, source, status)
    VALUES ('<uuid-logement>', 'airport', 'Aéroport d''Alicante-Elche',
            ST_SetSRID(ST_MakePoint(-0.5582, 38.2822), 4326), 'owner', 'approved');
    ```
    puis recalculer ses distances : `POST /api/properties/{id}/recompute-distances`.
  - **Fournisseur Overpass géré** (à décider plus tard, NON implémenté) — options :
    (1) **auto-héberger Overpass** (Docker `overpass-api`, extrait régional
    Geofabrik) : ~0 €/mois hors VPS, mais RAM/disque et maintenance des mises à
    jour (effort élevé au départ) ; (2) **Overpass géré / mutualisé** type
    kumi.systems ou overpass.private.coffee (déjà en miroir) : gratuit/don, pas
    de SLA (effort nul, fiabilité moyenne — l'actuel) ; (3) **Geoapify / MapTiler
    POI API** (fournisseur commercial) : SLA + quotas, ~50–100 €/mois pour le
    volume MVP, mais mapping catégories à refaire (effort moyen). Recommandation
    provisoire : rester sur (2) + retries M-18, basculer vers (1) quand le volume
    le justifie.
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
- Guide voyageur (M-08) : la page HTML `/g/{token}` est **rendue côté serveur**
  (`api/guide_page.py`, contenu propriétaire échappé via `html.escape` puis Markdown
  minimal) et **enrichie** par `frontend/guide/app.js` (carte, filtres, visionneuse,
  secrets) — sans JS elle reste lisible ; les sections **masquées** ne sont pas dans
  le HTML (test). Le JSON public passe par `_json()` (JSONResponse +
  `jsonable_encoder`, `application/json; charset=utf-8` — ne jamais renvoyer un dict
  brut qui perdrait le charset). Les **secrets** ne sont ni dans le HTML ni dans
  `/data` : seulement sur `/g/{token}/secrets`, et **uniquement** si `access_mode =
  'link'` (`repo.get_published_secrets_by_token`). Le service worker doit être servi
  par la route `/guide/sw.js` (entête `Service-Worker-Allowed: /`) sinon sa portée se
  limite à `/guide/` et n'intercepte pas `/g/…` ; il ne met **pas** en cache les
  tuiles OSM (M-10). Le générateur QR (`qr.js`) est autonome (mode octet, niveau M,
  versions 1-6) — toute modification doit rester scannable. **Vérif QR** :
  privilégier **zbar** (pyzbar), le décodeur des vrais scanners de téléphone —
  le `QRCodeDetector` d'OpenCV est un décodeur **faible** qui échoue sur certains
  masques pourtant valides (constaté : le masque 6, choisi par pénalité minimale
  pour certaines charges wifi, est illisible par OpenCV mais lu par zbar et les
  téléphones). Ne pas « corriger » l'algorithme de qr.js sur la seule foi d'un
  échec OpenCV.
- QR wifi back-office (M-06/M-15) : `frontend/guide/qr.js` est **mutualisé** (exports
  `qrMatrix`/`qrCanvas`/`wifiPayload`) entre le guide voyageur et l'éditeur. Le QR
  est généré **dans le navigateur** à partir des identifiants déjà chargés
  (`GET /secrets`, propriétaire) : le mot de passe ne transite par **aucun** autre
  canal (ni requête, ni serveur). Le PNG à imprimer est produit par
  `canvas.toDataURL`. Depuis M-15, l'éditeur multi-réseaux est
  `js/components/wifinetworks.js` (un QR + un PNG **par réseau**) ; l'ancien
  `wifiqr.js` (réseau unique) a été supprimé.
- Multi-wifi (M-15) : plusieurs réseaux par logement (Maison, Terrasse…). La liste
  `[{label, ssid, pass}]` est **sérialisée en JSON puis chiffrée en un seul bytea**
  (`property_secrets.wifi_networks_enc`) via l'AES applicatif (`api/wifi.py` —
  `encrypt_networks`/`networks_from_row`). Invariant 5 intact : clé hors base,
  jamais de mot de passe en clair côté serveur ni dans `/data`. **Migration lazy**
  (impossible en SQL pur, la clé est hors base) : tant que `wifi_networks_enc` est
  NULL, `networks_from_row` synthétise le **réseau n°1** (label « Wifi ») depuis les
  colonnes legacy `wifi_ssid`/`wifi_pass_enc` → l'ancien wifi n'a rien à re-saisir.
  Le `PUT /secrets` accepte `wifi_networks[]` (et encore les anciens champs simples,
  traités comme réseau unique) ; il écrit `wifi_networks_enc` **et** garde les
  colonnes legacy en miroir du réseau n°1. `GET /secrets` et `/g/{token}/secrets`
  (mode 'link') renvoient `wifi_networks[]` **plus** les anciens champs alimentés
  depuis le réseau n°1 (rétrocompat). Le guide affiche un QR par réseau
  (`app.js fillWifi` → `wifiCard`). La clé JSON du mot de passe est `pass` (aliasée
  `password` côté Pydantic — `pass` est un mot-clé Python).
- Affiche QR imprimable (M-07) : `api/poster.py` (reportlab, QR natif — pas de
  dépendance QR supplémentaire) sert un PDF A5/A4 sur
  `GET /api/properties/{id}/guide-poster.pdf` (réservé au propriétaire via
  `OwnedProperty`). Le QR encode le lien **public** `/g/{guide_token}` (jamais un
  secret). Origine des liens : `CASAGUIDE_PUBLIC_BASE_URL` sinon `request.base_url`.
- Multilingue (M-09) : les traductions sont **stockées**, jamais faites à la volée
  côté voyageur (invariant 4). Langue source = `properties.default_lang` ; cibles
  MVP `en`/`es` (`settings.translate_langs`). On ne traduit **que** le texte libre
  (`text`/`textarea`, `body_md`, descriptions/coups de cœur POI) : jamais un champ
  structuré (heure, booléen, nombre, URL, téléphone, clé de `select`) ni un secret.
  Toute sauvegarde de section (`upsert_section`) / édition de POI (`edit_poi`) pose
  `is_stale=TRUE` ; la (re)traduction (publication ou bouton `/translate`) ne
  retraite **que** le manquant/périmé (ciblage). Le guide ne sert **que** les
  traductions **fraîches** (`is_stale=FALSE`) : une traduction périmée retombe sur
  le français (repli élégant, jamais d'info obsolète — ne pas retirer ce filtre).
  `properties.published_langs` (rempli à la publication) pilote le sélecteur.
  Traducteur **injectable** (`translate.run(..., translator=)`) pour tester sans
  réseau. Le cahier `/s` (M-13) reste **en français** (hors périmètre). NB : les
  `area_facts` sont générés en français ; seuls leurs intitulés sont localisés.
- Cahier équipe d'entretien (M-13) : sections `audience='staff'` (chapitre « S »
  du seed), servies sur `/s/{staff_token}` (`repo.staff_sections` / `staff_media`,
  rendu `guide_page.render_staff`). Ce cahier est **accessible même en brouillon**
  (l'équipe prépare avant publication → `get_property_by_staff_token` ne filtre
  pas `status`), contrairement à `/g` qui exige `status='published'`. La
  complétude du dashboard (`repo.property_stats`) et le compteur de l'éditeur ne
  comptent **que** les sections `guest` (les staff ont leur propre décompte).
  Toute requête publique voyageur (`guide_sections`/`guide_media`/`get_public_media`)
  filtre `audience='guest'` — voir invariant 7.
- Area facts à leur place (M-17) : chaque `area_fact` est rendu **dans la section
  qui le déclare** via `field_schema.area_facts` (`guide_page._FACT_INLINE` :
  `waste_rules`→`C_trash`, `noise_rules`→`B_house_rules`), sous les champs du
  propriétaire, dans un encart sobre (`.sec-facts`). Le bloc de fin de guide
  (`_render_numbers`) ne garde **que** les `emergency_numbers` (liste complète).
  Conséquence à connaître : un fait n'apparaît que si sa **section hôte est
  visible** (les sections invisibles ne sont pas rendues) — `C_trash` /
  `B_house_rules` sont visibles par défaut au seed. Toute nouvelle association
  fait→section passe par l'ajout d'un renderer à `_FACT_INLINE` **et** de la clé
  dans `field_schema.area_facts` du seed.
- Itinéraires en un tap (M-14) : les POI `airport`/`train_station`
  (`_TRANSPORT_CATEGORIES`) sont rendus comme **blocs de trajet** dans la section
  qui les déclare (`field_schema.poi_categories` — `A_arrival`), via
  `guide_page._render_transport` : bouton Google Maps (`/maps/dir/?api=1&origin=
  <lat,lon aéroport>&destination=<lat,lon logement>`) et Waze (`waze.com/ul?ll=
  <lat,lon logement>&navigate=yes`). Le texte libre du propriétaire reste affiché
  **sous** les blocs (en complément). Pour éviter le doublon, ces POI sont
  **retirés** de la liste POI ordinaire du chapitre — **sauf** si aucune section
  hôte n'est visible (repli en cartes POI classiques, jamais de perte). Tout
  dérive de `properties.geom` + POI approuvés/édités : zéro saisie, zéro appel
  externe au rendu (invariant 4).
- Restaurants++ (M-16) : le tag OSM `cuisine` est récolté par `overpass.
  _element_to_poi` et **normalisé** par `_norm_cuisine` (premier terme avant `;`,
  en minuscules → `italian`, `seafood`…), stocké en colonne `pois.cuisine`
  (migration 003). Le champ survit à `_finalize` (il n'est pas dans `_tags`) ; il
  faut le passer explicitement dans `db.upsert_pois` (le COALESCE de l'upsert ne
  l'efface jamais sur ré-enrichissement). Le guide voyageur génère les puces de
  filtre par cuisine **depuis les valeurs présentes** (`guide_page.
  _render_cuisine_chips`, ≥ 2 cuisines distinctes), libellés localisés via
  `_CUISINE_LABELS` (repli sur la valeur brute embellie) ; le filtrage est **côté
  client** (`app.js initCuisineFilter`, attribut `data-cuisine`, aucune requête).
  Les **coups de cœur** (POI avec `owner_comment`) remontent en tête de leur
  catégorie — au tri SQL (`guide_pois`) **et** au rendu (`_render_pois`) : garder
  les deux cohérents. Cuisine saisie au back-office : mise en minuscules (filtre
  cohérent) ; une valeur libre inconnue du dictionnaire s'affiche brute.
- Régénérer des `area_facts` déjà en base (M-17, prompt resserré) : les faits sont
  **mutualisés** par `(country_code, admin_area)` et sautés par le pipeline tant
  qu'ils sont frais (`db.area_facts_fresh`, < 180 j). Pour forcer une régénération
  avec le nouveau prompt : `DELETE FROM area_facts WHERE country_code = 'ES' AND
  admin_area = 'Orihuela Costa';` (ou `admin_area IS NULL` pour le national), puis
  relancer un enrichissement (`POST /api/properties/{id}/enrich` ou
  `python -m enrich.pipeline --property-id <uuid>`) — l'étape 4a les régénère.
  Les faits laissés en base restent tels quels (aucune migration de contenu).
- Fiche du logement éditable (M-24) : modale mutualisée `frontend/js/components/
  propertyinfo.js` (infos + position), ouverte depuis la carte (`properties.js`) ET
  l'éditeur. Le re-géocodage n'est **jamais automatique** : `POST /api/properties/
  {id}/geocode` (`repo.set_geocode`, `deps.get_geocoder` injectable) n'est appelé
  qu'après accord explicite (case décochée par défaut si `geocode_source='manual'`)
  et **uniquement** si l'adresse a changé → une position manuelle n'est jamais
  écrasée en silence. Le placement manuel (`update_property` avec lat/lon) reste
  `geocode_source='manual'` ; le re-géocodage repasse à `'nominatim'`. La mini-carte
  de position est accessible **à tout moment** (plus seulement si accuracy≠rooftop).
- Liens de partage (M-25) : `guide_page._og_tags` ajoute Open Graph/Twitter dans le
  `<head>` (og:title/description **localisées**, og:url en **slug**, og:image en URL
  **absolue**). `og:image` = 1re photo du logement (`guide.py._first_photo_path` :
  niveau logement d'abord, puis 1re photo d'une section visible), sinon image de
  marque générée `api/og_image.py` (Pillow, servie sur `/g/{token}/og-image.png`).
  Slug : `/g/{slug}-{token}` accepté en plus de `/g/{token}` — `guide.py._real_token`
  = `rsplit('-',1)[-1]` (le token est **hex pur**, donc sans tiret : le slug
  décoratif devant est ignoré, seul le token fait foi ; anciens liens nus valides à
  jamais). Le corps porte le **token réel** (`data-token`) → fetches internes
  intacts. `noindex` conservé. Côté back-office, « Copier le lien » copie la forme
  slug (`frontend/js/share.js`, slugify aligné sur le backend).
- Langue du QR PDF (M-26) : `poster.build_guide_poster(..., lang=)` — textes
  localisés FR/EN/ES (`_TEXT`, surtitre via `_spaced`, mot d'accueil avec mention
  wifi) ; le poster ne sort plus qu'en **une** langue. Endpoint `guide-poster.pdf
  ?lang=fr|en|es` (Literal → 422 sinon). Le bouton « QR à imprimer » ouvre un petit
  menu FR/EN/ES (`editor.js openPosterMenu`).
- Hors-ligne des tuiles (M-10) : `sw.js` **cache-first** pour `tile.openstreetmap.
  org` (avant : réseau seul). Pré-chargement de la zone déclenché par l'app une fois
  EN LIGNE (`app.js initPwa` → `postMessage {prefetch-tiles, lat, lon}` après
  `serviceWorker.ready`) : le SW moissonne zooms 13-16 autour du logement (~148
  tuiles) **séquentiellement** avec pause (politesse OSM, pas de rafale), saute les
  tuiles déjà en cache, cache `TILES` plafonné (éviction FIFO). Hors-ligne + tuile
  absente → `Response.error` → `errorTileUrl` transparent + message discret
  `.map-offline`. **Toute modif de `frontend/guide/*` impose de bumper `VERSION`
  dans `sw.js`** (actuellement v15) — voir piège cache-busting SW plus haut. NB : le
  cache `TILES` doit rester dans la liste `keep` de l'`activate` (sinon purgé).
- Guide en TROIS onglets (V2-09) : le guide voyageur n'est plus un rouleau unique
  mais trois espaces — « Le logement » (home), « Urgences » (emergency), « Autour
  de vous » (around). La répartition se fait **chapitre par chapitre** dans
  `guide_page.render_guide` via `_SECTION_TAB` / `_POI_TAB` (les sections d'un
  chapitre et ses POI peuvent aller dans des onglets différents — seul C : sections
  → home, commerces → around). La barre d'urgences **compacte** reste dans l'en-tête
  (persistante sur les 3 onglets) ; la version **`_render_sos(big=True)`** ouvre
  l'onglet Urgences, avec le bloc complet des numéros (`_render_numbers`). La carte
  et les puces de filtre vivent dans le panneau « around » ; `map_data` ne contient
  que les POI de cet espace (`_POI_TAB == "around"`). **Sans JS, tous les panneaux
  restent visibles** (CSS `html.js .tab-panel:not(.tab-active){display:none}` +
  script inline `<head>` qui pose la classe `js` → pas de FOUC, noscript = rouleau
  complet, aucune perte). État d'onglet dans le hash **fixe** `#logement/#urgences/
  #autour` (`_TAB_HASH`, non localisé) → deep-link + retour arrière. `app.js
  initTabs` gère l'activation, résout une ancre de section `#<code>` vers l'onglet
  propriétaire (chaque `.sec-card` porte `id=<code>`), recale la carte
  (`invalidateSize`) à l'activation d'« around », et complète les liens de langue
  du hash courant (onglet conservé au changement de langue). **Une seule page,
  aucune route serveur** → le SW hors-ligne (M-10) et le sélecteur de langue (M-09)
  fonctionnent inchangés. Toute nouvelle catégorie/chapitre doit être ajoutée à
  `_SECTION_TAB`/`_POI_TAB` (défaut `home`).
- Listes de lieux repliées (V2-09) : `_render_pois` enveloppe chaque catégorie en
  `.cat > .poi-group` et rend **4 cartes** puis un bouton « Voir les N autres »
  (`.more-btn`, compte exact) **rendu côté serveur mais masqué par CSS** (sans JS
  toutes les cartes restent visibles). `app.js initCategoryLists` déplie/replie et
  **coopère avec le filtre par cuisine** des restaurants (le compte du bouton suit
  les cartes réellement éligibles ; `data-more-tpl` = gabarit `{n}` réinjecté côté
  client). Les coups de cœur ❤ restent en tête (tri serveur) donc visibles avant la
  troncature. Catégories ≤ 4 : affichées telles quelles (pas de bouton). NB : la
  vérification headless-new clippe les captures à la largeur de fenêtre alors que la
  mise en page se fait à la largeur du `.wrap` (680px) → un « débordement » apparent
  à 390px est un **artefact du screenshot**, pas un bug (vérifier à 900px : colonne
  centrée propre).

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
