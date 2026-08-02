# CLAUDE.md — Contexte projet pour Claude Code

## Projet

**Holaguia** (marque commerciale, `holaguia.com`, société HOLAQUETALIMMO SL) —
SaaS multi-propriétaires de guides d'accueil numériques pour
logements de vacances. Un propriétaire saisit l'adresse de son logement et
complète une checklist pré-définie ; un pipeline IA pré-remplit les sections
« environnement » (hôpital, commerces, restaurants, urgences…) qu'il valide ;
le voyageur consulte un guide PWA multilingue avec carte interactive via
lien/QR code.

**Référence fonctionnelle : `docs/cahier_des_charges.md`** (v1.0). Les codes
§4, §5, §8… dans les commentaires du code renvoient à ce document. Le lire
avant toute évolution fonctionnelle.

**Marque vs. nom technique (M-29).** Le produit s'appelle **Holaguia** (H
majuscule, sans accent — jamais « HolaGuia » ni « Holaguía »). C'est le seul
nom qu'un humain doit voir : back-office, guides `/g` et `/s`, PWA, emails,
PDF, cartes de partage, docs. En revanche `casaguide` est le **nom technique
historique** et **reste inchangé partout où il est invisible** — préfixe
`CASAGUIDE_` des variables d'env, `casaguide_token` du sessionStorage,
`casaguide:lang`, nom du cache SW `casaguide-guide-v*`, chemins `/opt/casaguide`,
utilisateur/base/service systemd, nom du dépôt. **Ne pas « corriger » ces
occurrences techniques** : les renommer n'apporte rien et crée un risque de
migration pour zéro valeur. Le watermark « Créé avec Holaguia » et le code de
marque `emails._BRAND = "Holaguia"` sont les références d'orthographe.

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
| Cahier équipe d'entretien (M-13) | Schéma : `audience` (guest\|staff) sur `section_templates`, `staff_token` (128 bits) sur `properties` — schema.sql + `db/migrations/002`. Seed : chapitre « S » (5 sections staff). Page publique `GET /s/{staff_token}` (rendu `guide_page.render_staff`, variante sobre check-list, **accessible même en brouillon**, jamais de secrets/POI). Étanchéité guest↔staff dans les deux sens (invariant 7). **Exclusif Pro depuis V2-18b** (aperçu essai, grand-père comptes existants ; page d'upsell `render_staff_locked` + édition 402 `staff_locked` sinon — invariant 11) |
| Affiche QR imprimable (M-07) | `api/poster.py` (reportlab) → `GET /api/properties/{id}/guide-poster.pdf` (A5/A4, propriétaire uniquement) : nom du logement, QR du lien du guide, mot d'accueil FR/EN, identité sable/mer |
| Auth transactionnel (V2-08) | **Fait** — mailer injectable (`deps.get_mailer` : `SmtpMailer` SSL Infomaniak + `ConsoleMailer` dev/tests), gabarits FR `api/emails.py`. **Mot de passe oublié** : `POST /api/auth/forgot` (200 constant anti-énumération, email en tâche de fond, cadence 2 min), `POST /api/auth/reset` (jeton 256 bits **haché SHA-256** en table `password_resets`, expiration 60 min, usage unique). **Vérification d'email** : lien à l'inscription (non bloquant), `POST /api/auth/verify-email` (idempotent), `POST /api/auth/resend-verification`. `owners.email_verified` exposé par `/me`. Migrations 005 (table) + 006 (grand-périsage des comptes existants). Front : routes publiques `#/forgot`, `#/reset/{token}`, `#/verify/{token}` (`js/views/reset.js`) + bandeau « vérifiez votre email » (`app.js`) — testé (22 tests + parcours headless) |
| Plans & abonnements (V2-05a) | **Fait (volets 1-3)** — couche d'accès `api/plans.py` branchée sur le modèle existant `plans`/`subscriptions` (CdC §10) : `get_subscription` / `get_plan` (repli 'free' + warning si abonnement manquant, jamais None) / `check_quota(owner_id, resource)` pour `properties` \| `enrichments` \| `langs` (lit `max_properties`, `enrich_quota` mensuel/logement, `features`) / `cap_target_langs` (plafond langues, source comprise) / `wants_watermark`. Inscription crée toujours une ligne `subscriptions` (`plan_id`='free', **`status='active'`** — pas de logique d'essai). Migration 007 : rattrapage idempotent d'un abo 'free'. Attribution manuelle par email : `ops/set_plan.py`. **Application serveur (volet 2)** : refus **402 `quota_exceeded`** (helper `api/quota.py`, `detail={code,message FR}`) sur `POST /api/properties` (au-delà de `max_properties`) et `POST .../enrich` (au-delà de `enrich_quota`, **remplace l'ancien 429**) ; traductions **plafonnées** par `cap_target_langs` (le runner de traduction reçoit désormais `target_langs`, `deps.TranslationRunner`), plan gratuit → 0 cible → `/translate` renvoie 402, publication ne génère aucune traduction ; **watermark** « Créé avec Holaguia » dans le SSR du guide (`guide_page._watermark_html`, flag via `repo.get_plan_by_guide_token`) si `features.watermark`. **Downgrade non destructif** : aucune donnée/traduction supprimée, seule la création est bloquée. **UI back-office (volet 3)** : endpoints `GET /api/plans` (public, catalogue) + `GET /api/subscription` (auth : plan + jauges d'usage) → `routers/billing.py` ; inscription avec **choix d'offre** (gratuite présélectionnée, payantes « bientôt », prix depuis l'API) `views/login.js` ; page **« Mon abonnement »** `#/abonnement` (`views/subscription.js` : plan courant, jauges logements/enrichissements/langues, boutons de changement inactifs) ; refus quota interceptés côté front par `js/quota.js` (`handleQuotaError` → encart « changez d'offre », jamais d'`alert()`) dans création de logement, enrichissement, traduction. Aucun quota codé en dur (invariant 8) — testé (`tests/test_plans.py` 13 + intégration : quotas, downgrade lecture seule, watermark, `/plans` & `/subscription` ; parcours front vérifié en headless). *Suite : white-label du poster PDF (marque fixe aujourd'hui).* |
| Facturation Stripe (V2-05b) | **Fait (dév/Test ; validation 4242 avec André en attente de ses clés Test)** — passerelle injectable `api/billing_stripe.py` (`StripeGateway`, `deps.get_stripe` → None sans clé → 503 propre, même motif que le mailer) ; `LiveStripeGateway` (StripeClient v1 ; `construct_event` = vérif signature + `json.loads`). **Checkout** `POST /api/billing/checkout` (auth ; plan solo/pro ; Customer rattaché **avant** la session → résolution owner par `customer_id` quel que soit l'ordre des webhooks ; price depuis `plans.stripe_price_id` ; 422 free/inconnu, 503 non synchronisé). **Webhook** `POST /api/stripe/webhook` (public, source de vérité — invariant 9) : `checkout.session.completed` (lien Customer), `customer.subscription.created/updated` (**autorité** : plan via price→plan, statut mappé, `current_period_end`), `.deleted` (retour `free` non destructif), `invoice.payment_failed` (`past_due`) ; dispatch `api/stripe_events.py` ; idempotence `stripe_events` (migration 008). **Portail** `POST /api/billing/portal` (409 sans Customer). **Sync** `ops/stripe_sync_products.py` (plans→Products/Prices, idempotent, archivage non destructif d'un ancien prix). Front : `#/abonnement` boutons réels (Checkout/portail via `js/redirect.js`, bandeau `?checkout=success|cancel`), chooser d'inscription payant → Checkout après création. Runbook `docs/stripe.md`. Testé (`tests/test_stripe.py` 8 + intégration `test_api.py` : checkout, idempotence, mapping statuts, upgrade, deleted→free, signature→400, portail ; parcours front headless 13/13). *Suite : mode Live (échange de clés + re-sync) ; facturation annuelle ; Stripe Tax.* |
| Config (M-02) | Chargement auto de `backend/.env` (`enrich/envfile.py`) ; `backend/.env.example` documenté ; avertissement de démarrage si clés manquantes |
| Stockage médias | `api/storage.py` — interface `Storage` abstraite + `LocalStorage` sous `MEDIA_ROOT` (prêt pour S3) |
| Guide voyageur PWA (M-08) | **Fait** — page HTML mobile-first rendue par `api/guide_page.py`, app shell `frontend/guide/` (modules ES : `app.js` carte/filtres/visionneuse/secrets, `qr.js` QR wifi autonome, `sw.js` hors-ligne, manifest par guide, icônes). Identité `guide_preview.html`. Multilingue (M-09) **fait**. **Hors-ligne complet (M-10) fait** : `sw.js` (v15) pré-charge les tuiles OSM de la zone (zooms 13-16, ~148 tuiles, séquentiel/poli) et les sert cache-first ; message discret hors zone. **Liens de partage (M-25) faits** : Open Graph/Twitter + og:image (photo ou image de marque `api/og_image.py`) + slug `/g/{slug}-{token}`. **Lisibilité (V2-09) faite** : TROIS onglets (Le logement / Urgences / Autour de vous, état dans le hash, `app.js initTabs`) + listes de lieux repliées (4 + « Voir les N autres », `initCategoryLists`) |
| Calendrier des séjours (V2-23a) | **Volet 1+2 faits** — migration 014 (`bookings`, `property_calendars` avec `ical_url_enc` chiffrée, heures standard `properties.default_checkin/checkout_time`). Parser iCal pur `api/ical.py` (DTSTART/DTEND dates & datetimes, DTEND exclusif, heuristique blocked). Moteur `api/calendars.py` (fetch httpx timeout/UA/redirections/non-bloquant ; upsert idempotent par UID ; disparu→cancelled ; champs manuels & promotion préservés ; overlaps/rotations purs ; `mask_url`). Fetch injectable (`deps.get_calendar_fetcher`). API `routers/calendars.py` (`GET /calendar` vue complète, CRUD `/bookings`, `/calendars` avec validation au collage, `DELETE` flux→cancel, `POST /calendar/sync` rate-limité). Front `js/views/calendar.js` (liste chronologique, badges, alerte chevauchement, rotations, blocs à compléter, annulés repliés, flux masqués + ajout/synchro/suppression). Bouton « Calendrier » (carte + porte staff), route `#/properties/:id/calendrier`. Ops `sync_calendars.py` + timer systemd 4 h. Testé (parser + moteur + API intégration + headless `calendar.test.mjs`). **V2-23b volet 0 fait** (migration 015) : séparation `nature` (sémantique, pilote la préparation — invariant 14) / `status` (cycle de vie `active`/`cancelled`) ; chevauchement = occupé↔occupé ; avertissement **à la saisie** (`js/lib/overlaps.js` pur + `overlaps.test.mjs`) ; auto-promotion nature à la saisie d'un nom ; rattachement d'un bloc miroir (`linked_booking_id`, masqué jamais supprimé) ; bagages (`luggage_drop_time`) ; vue ancrée sur aujourd'hui (à venir / passés repliés). **V2-23b volet 1 fait** (migrations 016 nb voyageurs/âges enfants + 017 `care_rules` JSONB/`property_request_types`/`booking_requests`) : moteur d'interventions **pur** `api/care.py` (la nature pilote — reservation=tout, private=sans pack, works/unavailable/unqualified=rien ; interventions datées & **quantifiées** par `guest_count` ; draps à J+N ; demandes acceptées ; signal de fenêtre de rotation en **hommes-heures** neutre/ambre/rouge, null-safe tant qu'André n'a pas mesuré) ; `care_rules` amorcées + catalogue amorcé à la création (`create_property`) ; endpoints `GET/POST/PATCH /request-types`, `GET/POST /bookings/{id}/requests`, `PATCH/DELETE /requests/{id}`, `GET /bookings/{id}/interventions` ; care_rules éditables via `PATCH /properties/{id}` ; **relance active §0.6** (`missing_info` par séjour dans `GET /calendar`) ; front : modale séjour (voyageurs + âges en chips + suggestions + demandes), « Réglages d'entretien » (règles + catalogue), pastilles de relance ; module pur `js/lib/care.js` (duplication volontaire du back). Testé (`test_care.py` 23 unitaires + intégration `test_calendar_api.py` + `care.test.mjs` + headless calendar). **V2-23b volet 2 fait** : **planning dans le cahier `/s/`** — frise chronologique **pure** `care.build_planning` (fenêtres de préparation entre occupations + interventions en cours de séjour + séjours non occupés grisés), rendue en tête par `guide_page._render_staff`/`_render_planning` ; **la fenêtre, pas le séjour** (« libre depuis / prochaine arrivée → fenêtre X h », deux échéances si dépôt de bagages, longue vacance ancrée sur l'arrivée) ; **RGPD** : coordonnées visibles seulement pour les séjours en cours/à venir (`care._show_contact`, `ends_on ≥ today`) ; gating Pro assuré par `staff_access` en amont. **Signal de rotation gradué** (`turnaround_signal`) affiché **des deux côtés** : `RotationOut.signal` sur le calendrier propriétaire + planning `/s/` ; part de l'**échéance la plus proche** (dépôt de bagages). **Rendement du travail à plusieurs** (`care_rules.turnaround.parallel_efficiency`, défaut 0,75 — jamais la division naïve). **Anti-saturation du signal** (front) : l'incomplétude est **sobre** (pastille « Incomplet », jamais le triangle), **agrégée en tête** (« N séjours incomplets » dépliable) avec **saisie rapide en ligne** du nb de voyageurs ; champs de personnes **entiers ≥ 1**. Testé (`test_care.py` planning+efficiency, `test_guide_page.py` frise/RGPD, `test_calendar_api.py` signal+`/s/`, `calendar.test.mjs` étendu). **V2-23b volet 3 fait** (migration 018 `guest_phone`/`guest_email`/`guest_lang` + backfill heuristique) : **coordonnées séparées** — le téléphone est une ACTION (liens `tel:`/WhatsApp du planning `/s/`), l'email une autre (`mailto:`), la langue proposée depuis les **langues publiées** (jamais en dur) ; relance §0.6 → « **téléphone manquant** » (`care.effective_phone`, repli legacy `guest_contact`) ; modale calendrier à trois champs. **Boucle demande du voyageur** : sections « sur demande » (`field_schema.request` du seed sur `B_cleaning`/`E_services`) → bouton **« Demander ce service »** SSR (`guide_page._render_section`) enrichi par `frontend/guide/app.js` `initRequestService` ; `POST /g/{token}/requests` (public) crée une `booking_requests` `origin='guest'`/`status='pending'` rattachée au séjour en cours (à défaut le suivant, `repo.current_or_next_booking_by_guide_token`), **libellé du template jamais du voyageur**, **rate-limité par guide** (`CASAGUIDE_GUEST_REQUEST_MIN_INTERVAL_S`, 60 s → 429), **notifie le propriétaire** (email best-effort `emails.guest_service_request_email`, commit AVANT la tâche de fond) + **badge** calendrier (`BookingOut.pending_guest_requests`) ; le propriétaire **accepte/refuse** (`reqRow`, `PATCH /requests/{id}`) → une demande acceptée devient une intervention. Invariant 4 intact (action du voyageur). Testé (`test_calendar_api.py` : champs séparés, endpoint, rate-limit, notif, accept ; `test_guide_page.py` : bouton requestable + `tel:`/WhatsApp/`mailto`/langue du planning ; `test_care.py` : `phone_missing` + planning ; `calendar.test.mjs` étendu + `guide-request.test.mjs`). **V2-23b terminé.** |

## Préfixes d'URL publics réservés (V2-23c)

Quatre préfixes publics, **captés par l'API avant l'attrape-tout SPA**, jamais
confondables (ni en code, ni dans les journaux) :

| Préfixe | Rôle | Token | Résolution |
|---|---|---|---|
| `/g/` | **Maison** (QR imprimé, à vie) | `guide_token` (hex, slug décoratif possible via `_real_token()`) | par token |
| `/s/` | **Équipe** d'entretien (cahier) | `staff_token` | par token |
| `/v/` | **Vitrine** (prospect/annonce, secrets d'exemple) | `showcase_token` | par token |
| `/b/` | **Séjour** (locataire réservé, J+7) | `stay_token` | logement résolu **côté serveur** depuis le séjour — le `guide_token` ne voyage **jamais** dans l'URL du locataire |

`_real_token()` (slug décoratif) ne s'applique **qu'à `/g/`** : un lien de séjour
est *envoyé, jamais retapé* → `/b/{slug}-{token}` n'est pas rattrapé. Les cas
morts des **nouveaux** préfixes (`/b/` inconnu/annulé/expiré, `/v/` inconnu)
servent **la même page neutre** (`guide_page.render_stay_expired`, aucune donnée
du logement, libellé neutre FR/EN/ES) ; `/g/` et `/s/` gardent `render_not_found`.

**Le `guide_token` ne quitte JAMAIS la maison — ni par l'URL, ni par le DOM
(V2-23c volet 1bis).** `/b/` et `/v/` ont chacun leurs **sous-routes** dédiées, si
bien que le rendu d'un lien de séjour ne contient le `guide_token` éternel **nulle
part** (ni `data-token`, ni URL de média, ni og, ni manifeste) : `app.js` déduit
son préfixe d'API d'**une seule source de vérité**, `data-api-base` (`/g/{guide_
token}` maison · `/b/{stay_token}` séjour · `/v/{showcase_token}` vitrine). Les
sous-routes séjour `GET /b/{t}/data|secrets|media/{id}|og-image.png` et `POST
/b/{t}/requests` sont **toutes gardées par `_resolve_stay`** (token mort → 404) →
« la page ET les endpoints meurent ensemble », **secrets compris** (les vrais codes
meurent à J+8 avec la page — c'est LE progrès de sécurité du lien de séjour). Le
`stay_token` se rattache de façon **certaine** par la **route** `POST /b/{t}/
requests` (plus de `stay_token` dans le corps ni le schema). **Pas de manifeste PWA
sur `/b/` ni `/v/`** (`manifest=False`) : installer une PWA depuis un lien qui meurt
à J+7 la casserait — l'installation reste le métier du QR maison `/g/`.

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
10. **Modèle d'essai (V2-18a)** : toute inscription démarre un **essai de 21 jours**
    (`plan_id='trial'`, `status='trialing'`, `subscriptions.trial_ends_at`), aux
    capacités Pro **sauf** l'export PDF/hors-ligne (`features.pdf_export=false`,
    verrou prêt pour V2-14). **Rien n'est jamais supprimé** à l'expiration :
    lecture seule = refus des **écritures** (403 `detail.code='trial_expired'`,
    garde `deps.require_write_access` sur les seules routes d'écriture), jamais
    des lectures ; guides `/g` et `/s` restent servis (avec watermark). **L'expiration
    est calculée à la LECTURE** (`plans.is_trial_expired` : `status='trialing'` +
    `trial_ends_at < now`), **jamais** par un job qui basculerait des lignes (pas
    d'état à maintenir, pas de course). **Point d'entrée unique** des droits :
    `plans.effective_entitlements(conn, owner_id)` (→ `plan`, `on_trial`,
    `trial_expired`, `read_only`) ; `check_quota`, le garde de lecture seule et
    `/me` s'y branchent. Le **watermark** « Créé avec Holaguia » est présent sur
    **essai + solo**, absent **pro** seulement (échelle de marque, décision 25/07).
    Souscrire (webhook plan payant → `status='active'`) sort du régime d'essai et
    réactive les écritures. La définition du plan `trial` vit **en base** (seed),
    aucun droit codé en dur (invariant 8).
11. **Grille, add-on logements & gating staff (V2-18b)** : la grille vit **en base**
    (invariant 8) — Pro 24 €/**6 logements inclus** + **add-on 3 €/mois par
    logement** (`plans.addon_property_price_cts`), fin de l'illimité. Le quota
    logements effectif = `max_properties + subscriptions.addon_qty`, exposé par
    `plans.effective_entitlements` (`max_properties` effectif). **`addon_qty`
    n'est écrit QUE par le webhook** `customer.subscription.updated` (lecture de
    **tous** les items — plan via le price principal, add-on via
    `plans.addon_stripe_price_id`) : l'endpoint `POST /api/billing/addons` demande
    à Stripe (proration) mais **n'écrit rien** (l'UI demande, Stripe dispose, le
    webhook écrit — corollaire de l'invariant 9). Une **baisse/suppression**
    d'add-on ne supprime **jamais** de logement (excédent en lecture seule, comme
    invariant 8). Le **plafond de langues est supprimé** (`features.langs='all'` →
    illimité, `plans.max_langs` renvoie `None`) sur **tous** les plans. Le **guide
    équipe `/s/`** est **exclusif Pro** (`features.staff_guide` : `true` pro,
    `'preview'` essai, `false` solo/free), avec **clause de grand-père**
    (`subscriptions.staff_grandfathered=true` pour tous les comptes existant à la
    migration 011) — décidé par `plans.staff_access(plan, grandfathered)` via
    `effective_entitlements['staff_access']`. Hors droit : `/s/` sert une page
    sobre d'upsell (jamais d'erreur brute, jamais de secret) et l'édition d'une
    section `audience='staff'` répond **402 `staff_locked`** (`quota.staff_locked`,
    même forme objet, intercepté par `handlePlanLimit`).
12. **Changement d'offre : sens = politique (V2-18e)**. Le SENS du changement
    tranche, comparé sur le **prix de base** de l'offre (jamais le total effectif
    avec add-ons) : **UPGRADE** (cible plus chère) = effet **IMMÉDIAT** avec
    prorata (`gateway.change_plan`, comportement V2-18d) ; **DOWNGRADE** (cible
    moins chère) = effet **À L'ÉCHÉANCE** (`gateway.schedule_downgrade` via un
    **Subscription Schedule** Stripe : phase 1 = offre courante + add-ons jusqu'à
    `current_period_end`, phase 2 = nouvelle offre **sans add-on**). Un downgrade
    programmé est **annulable** tant qu'il n'a pas pris effet
    (`/billing/cancel-scheduled-change` → `subscription_schedules.release`). Comme
    l'add-on (inv. 11), ces endpoints **demandent** à Stripe et **n'écrivent RIEN**
    en base : `subscriptions.scheduled_plan_id` / `scheduled_change_at` (bandeau
    back-office) sont posés **EXCLUSIVEMENT par le webhook** — événements
    `subscription_schedule.created/updated` (programmation), `.released/.canceled/
    .completed/.aborted` (effacement), et la **transition de phase** qui arrive par
    `customer.subscription.updated` à l'échéance (le plan appliqué == le programmé
    → effacement, seule autorité, invariants 9/12). Ces deux colonnes sont
    **purement informatives** : l'accès aux quotas ne dépend QUE de `plan_id`
    (un downgrade programmé garde l'accès Pro jusqu'à la bascule ; l'excédent
    devient alors lecture seule, jamais supprimé — inv. 8).
13. **Calendrier des séjours (V2-23a)** : une **URL iCal est un secret** (elle
    donne accès au calendrier complet du bien) → chiffrée AES en base
    (`property_calendars.ical_url_enc`, même régime que le wifi), jamais en clair
    dans les réponses (affichage `mask_url`) ni les logs. **Idempotence des
    imports par (calendar_id, external_uid)** : re-synchroniser N fois = zéro
    doublon ; un événement disparu du flux passe `cancelled` (conservé, **jamais
    supprimé**) et **réapparaît** réactivé. Une sync ne rafraîchit que les
    **dates** : les champs saisis à la main (nom, contact, heures, notes) et une
    **promotion manuelle** `blocked`→`confirmed` ne sont **jamais** écrasés. Le
    fetch iCal est le **premier flux réseau sortant régulier** hors
    OSM/Claude/Stripe/SMTP (`api/calendars.fetch_ical`) : timeout court (10 s),
    User-Agent propre, suivi des redirections, et **jamais bloquant** — l'échec
    d'un flux est enregistré sur le flux (`last_sync_status='error'`, `sync_error`)
    sans empêcher les autres. L'anti-chevauchement **alerte, ne bloque jamais**
    (le propriétaire arbitre) ; les séjours sont modélisés en intervalle
    **semi-ouvert** `[arrivée, départ)` → deux séjours qui se touchent (départ =
    arrivée le même jour) ne se chevauchent pas : c'est une **rotation** (fenêtre
    horaire calculée avec les heures effectives). Le fetch est **injectable**
    (`deps.get_calendar_fetcher`) → tests sans réseau.
14. **La nature pilote la préparation, jamais le statut (V2-23b, migration 015)** :
    un séjour porte DEUX axes indépendants. **`nature`** = la SÉMANTIQUE
    (`reservation` | `private` | `works` | `unavailable` | `unqualified`) — c'est
    elle, et elle seule, qui décide de la préparation par l'équipe. **`status`** =
    le CYCLE DE VIE (`active` | `cancelled`). La bonne question n'est pas « est-ce
    loué » mais « est-ce **occupé** » : `_is_occupied` = `nature IN
    ('reservation','private')` + `status='active'` + non rattaché. Le chevauchement
    (`compute_overlaps`) et les rotations raisonnent sur **l'occupation**, pas sur
    le statut (une occupation privée qui recouvre une réservation est une double
    réservation). La synchro iCal ne touche **jamais** la nature saisie à la main
    (prolongement de l'invariant 13) : elle ne sert qu'à la **création** (`reservation`
    si le flux donne un nom, sinon `unqualified`) et ne rafraîchit que les dates
    (+ réactive un `cancelled` réapparu → `active`). Un **bloc miroir** rattaché
    (`linked_booking_id`) est le double d'un séjour déjà présent : ignoré des
    chevauchements et **masqué** de la vue, mais **jamais supprimé** (la synchro le
    recréerait). Aucune valeur codée en dur : les natures vivent dans la contrainte
    CHECK de la base (invariant 8).
15. **Aucune liste de langues en dur — le registre fait foi (V2-21a, migration 019)** :
    la table `languages` (`code`, `name_native`, `status` ∈ draft/in_review/
    published, `sort_order`, `register_note`) est la **source UNIQUE** des langues
    offertes par le produit (invariant 8 étendu aux langues). Le produit n'offre
    **JAMAIS** que les langues `status='published'` : une langue en brouillon ou en
    relecture est **invisible** partout (sélecteur du guide SSR, détection
    `navigator.language`, menu de partage `?lang=`, modale « langue du locataire »,
    cibles de traduction, `/g/{token}/data`). Passer une langue de `published` à
    `draft` **en base** la fait disparaître partout **sans redéploiement**. Tout
    passe par `repo.published_languages`/`published_language_codes` (backend),
    `db.published_language_codes` (enrich/CLI), l'endpoint public `GET /languages`
    et `frontend/js/languages.js` (front) ; le SSR lit la base directement. Le
    `published_langs` d'un logement est **croisé** avec le registre à la lecture (une
    langue dépubliée globalement disparaît même si elle reste dans `published_langs`).
    Le **seed ne touche jamais `status`** (état d'exploitation, pas donnée de seed).
    `register_note` porte la consigne de registre (vouvoiement…) imposée au modèle,
    ajustable en base. Vérification : `grep -rnE "'(en|es|de|nl|it|sq)'" backend/api
    frontend/js` ne doit révéler que des **libellés** d'affichage (repli), jamais une
    liste qui décide de ce qui est *offert*. (Poster PDF FR/EN/ES M-26 : hors
    périmètre tant que son inventaire de libellés n'est pas traduit — V2-21b…n.)
    **Libellés statiques (volet 2, migration 020)** : les libellés d'interface du
    guide voyageur ont une **clé stable** (inventaire `i18n/inventory.json`, généré
    par `ops/i18n_inventory.py` depuis le code+seed, **jamais à la main** ;
    `--check` = gate de non-régression). Les traductions des langues
    **supplémentaires** (nl/de/it/sq) vivent dans **`ui_translations(lang, key,
    text)`** — **UNE seule source de vérité** ; FR/EN/ES restent portés par le code
    (`guide_page._UI`) et le seed (`name_i18n`). Le SSR **superpose** l'overlay de
    la langue effective (`api/i18n.py` ContextVar ; `render_guide(ui_overlay=…)` ;
    helpers `_t`/`_seed_label`/`_cuisine_label`/`_chapter_name`) → FR/EN/ES
    **identiques** (overlay vide), langue supplémentaire servie, non traduit → repli
    FR. Les **clés** sont construites par `api/i18n.py` et utilisées **aux deux
    bouts** (collecteur d'inventaire ET lookup SSR) : ne jamais forger une clé à la
    main d'un côté sans l'autre. **Libellés du `field_schema`** (labels de champs,
    valeurs de `select`, labels de groupes répétables) : **rendus dans le guide
    voyageur** → recensés au même titre (`field.<section>.<champ>`,
    `field.<section>.<groupe>.<champ>`, clés **scopées par section** — un même
    `key` porte un libellé différent selon la section, jamais de collision) et
    overlayés par `guide_page._render_fields` (via `_seed_label`, donc FR/EN/ES
    inchangés). Export/réimport relecteur : `ops/i18n_export.py --lang xx` (CSV) /
    `ops/i18n_import.py ui_xx.csv` (idempotent, refuse les clés inconnues).
    **Génération des propositions (volet 3)** : `ops/i18n_generate.py` peuple
    `ui_translations` via Claude (`enrich/translate_ui.py`, traducteur injectable),
    langue par langue, en imposant le **registre** (`languages.register_note`) ;
    cibles = registre **hors `api.i18n.SOURCE_LANGS`** (jamais fr/en/es) ;
    skip-existing par défaut (ne clobbe pas une correction relue), `--dry-run` sans
    appel API, coût dans `api_costs` (`operation='ui_translate'`, property NULL).
    Runbook : `docs/i18n.md`.

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
psql -d casaguide -f db/migrations/009_trial_model.sql   # subscriptions.trial_ends_at (essai 21 j, V2-18a)
psql -d casaguide -f db/migrations/010_trial_reminders.sql # reminder_7d/2d_sent_at (relances essai, V2-18a)
psql -d casaguide -f db/migrations/011_grille_addons_staff.sql # add-on + staff_grandfathered (grille V2-18b)
psql -d casaguide -f db/migrations/012_scheduled_plan_change.sql # downgrade programmé (scheduled_plan_id/_change_at, V2-18e)
psql -d casaguide -f db/migrations/013_poi_travel_mode.sql # mode de trajet par catégorie (poi_categories.travel_mode, V2-24)
psql -d casaguide -f db/migrations/014_calendar.sql # calendrier des séjours : bookings + property_calendars + heures standard (V2-23a)
psql -d casaguide -f db/migrations/015_booking_nature.sql # nature du séjour + bagages + bloc miroir ; status → cycle de vie (V2-23b, volet 0)
psql -d casaguide -f db/migrations/016_booking_guests.sql # nb de voyageurs + âges des enfants (V2-23b, volet 1)
psql -d casaguide -f db/migrations/017_care_rules.sql # règles d'entretien + catalogue de demandes (V2-23b, volet 1)
psql -d casaguide -f db/migrations/018_guest_contact_split.sql # téléphone/email/langue séparés + backfill (V2-23b, volet 3)
psql -d casaguide -f db/migrations/019_languages.sql # registre des langues du produit (draft/in_review/published) (V2-21a, volet 1)
psql -d casaguide -f db/migrations/020_ui_translations.sql # libellés statiques traduits (ui_translations) (V2-21a, volet 2)
psql -d casaguide -f db/migrations/021_stay_showcase_tokens.sql # lien de séjour (bookings.stay_token) + lien vitrine (properties.showcase_token) (V2-23c)

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
- **Cache des modules ES importés relativement (OPS-2, 27/07)** : le `?v=<sha>` ne
  buste QUE le point d'entrée (`app.js`) — un `import './views/x.js'` ne porte
  aucune query, donc un module modifié pouvait rester en cache navigateur après
  déploiement (constat prod : hotfix V2-18c invisible malgré la bonne version).
  **Remède = en-têtes HTTP** : `RevalidatingStaticFiles` sépare désormais le
  **code** (JS/MJS/CSS/HTML/manifeste → `Cache-Control: no-cache, must-revalidate`,
  revalidation ETag systématique, 304 si inchangé) des **images/polices**
  (`_LONG_CACHE_EXT` → `public, max-age=2592000`). Les statiques passant par
  FastAPI (Caddy ne fait que `reverse_proxy`), l'en-tête y est posé ; le
  `Caddyfile` le **garantit aussi au bord** (matcher `@code path *.js *.mjs *.css`
  → même en-tête, défense en profondeur). Les uploads (`routers/media.py`)
  gardent leur `private, max-age=3600`. **Ne jamais** revenir à un cache long sur
  le code ni retirer l'ETag. Vérif post-déploiement :
  `curl -sI https://holaguia.com/js/views/subscription.js | grep -i cache`
  → `Cache-Control: no-cache, must-revalidate`. Couvert par
  `test_api.py::test_es_modules_revalidate` / `test_static_images_cache_long`.
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
- Lecture seule d'essai côté front (V2-18a) : le refus d'écriture est un **403**
  `detail={"code":"trial_expired","message":<FR>}` (même forme objet que le 402
  quota). `js/quota.js` expose désormais `handlePlanLimit(err)` qui gère **les
  deux** codes (`quota_exceeded` **et** `trial_expired`) → encart vers
  `#/abonnement` ; `handleQuotaError` en est un **alias** (les vues existantes
  marchent sans changement). Toute nouvelle action d'écriture doit passer son
  erreur à `handlePlanLimit` avant tout `toast`. Le **bandeau global** de lecture
  seule (`app.js updateReadonlyBanner`) s'affiche sur `owner.trial_expired ===
  true` **strict** (jamais sur un champ absent — comme le bandeau de vérif email).
  L'état d'essai vient de `/me` (`on_trial`/`trial_expired`/`trial_ends_at`) et de
  `/api/subscription` (mêmes champs + compte à rebours). **V2-17/V2-18b** : partout
  où on affichait « Jusqu'à 5 langues » / « X/5 », le front dit « Toutes les langues
  disponibles ». Depuis V2-18b `features.langs='all'` (illimité) → **ne jamais**
  comparer `langs` numériquement (`'all' > 1` est `false` en JS !) : passer par
  `subscription.js isMultilingual` (`langs==='all' || Number(langs)>1`).
- Grille & add-on côté front (V2-18b) : `js/quota.js handlePlanLimit` gère
  désormais **trois** codes (`quota_exceeded` 402, `trial_expired` 403,
  `staff_locked` 402) → toute action d'écriture (dont l'enregistrement d'une
  section, staff comprise) doit y passer avant tout `toast`. Le **stepper**
  d'add-on (`views/subscription.js addonStepper`) n'apparaît que pour un **Pro
  actif** ; il **demande** la quantité à `POST /api/billing/addons` puis affiche
  « mise à jour en cours » (jamais de mutation locale de l'abonnement : le webhook
  est seul à poser `addon_qty`, invariant 11) et invite à actualiser. La carte
  **Gratuit** de « Changer d'offre » ne s'affiche **que** pour un compte déjà
  `plan.id==='free'` (un essai ne « descend » jamais vers le gratuit).
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
- **Checkout = ENTRER dans le payant, jamais y rester (V2-18d)** : un abonné
  **payant déjà actif** ne repasse **jamais** par `POST /api/billing/checkout`
  (mode subscription → il créerait un **second** abonnement Stripe, double
  facturation, temps déjà payé ignoré). Garde serveur : checkout renvoie **409
  `already_subscribed`** si `plans.has_active_paid_subscription` (statut
  active/past_due + `stripe_subscription_id` + prix>0). Le changement d'offre d'un
  abonné actif passe par `POST /api/billing/change-plan` qui **modifie
  l'abonnement Stripe EXISTANT** (`subscriptions.update` — swap du price
  principal, suppression des items d'add-on si la cible n'en a pas, proration
  `create_prorations` → le temps payé est crédité). Comme l'add-on (invariant
  11) : l'endpoint **demande** à Stripe et **n'écrit RIEN** en base ; `plan_id`/
  `addon_qty` reviennent par le webhook `customer.subscription.updated` (seule
  autorité, invariant 9). Corollaire front (`subscription.js`) : `isPaidActive`
  route les boutons « Passer en X » vers `change-plan` (+ `confirmDialog` de
  proration) et **Checkout uniquement** pour essai/free ; l'en-tête « Offre
  actuelle » affiche le **total effectif** (`base + addon_qty × unité`), pas le
  prix de base.
- **Downgrade programmé à l'échéance (V2-18e)** : un `change-plan` **n'est plus
  toujours immédiat** (cf. invariant 12). Le router route selon le SENS (prix de
  base cible vs actuel) : upgrade → `gateway.change_plan` (immédiat, prorata) ;
  downgrade → `gateway.schedule_downgrade` (Subscription Schedule, effet à
  `current_period_end`). **Ne jamais** faire un swap immédiat sur un downgrade
  (le client perdrait le temps déjà payé) ni écrire `scheduled_plan_id` hors du
  webhook. Côté webhook, `subscription_schedule.*` + la transition
  (`customer.subscription.updated` dont le plan appliqué == le programmé) posent
  ET effacent les colonnes `scheduled_*` — un `.released` (annulation ou
  complétion) efface aussi. Le `stripe_events` handler résout la **prochaine
  phase future** (`start_date > now`, la plus proche) : dans une phase de
  schedule, `item.price` est un **id (chaîne)**, pas un objet — le parser
  (`stripe_events._phase_main_plan`, gateway `_phase_price_id`) tolère les deux
  mais ne suppose jamais un objet. `effective_at`/`current_period_end` viennent
  de la base (posés par le webhook), jamais recalculés côté endpoint. Côté front
  (`subscription.js`) : `startChangePlan(plan, btn, sub)` choisit le discours
  (dates réelles FR via `formatDateFR`/`untilPhrase`) selon
  `plan.price_month_cts < sub.plan.price_month_cts` ; `scheduledChangeBanner`
  affiche le rappel persistant + « Annuler le changement programmé »
  (`api.cancelScheduledChange`) ; la carte de l'offre programmée montre
  « Programmée » (désactivée). **Live Stripe non exercé par les tests** (comme
  tout le code réseau Stripe : fakes injectés) → surface vérifiée par
  introspection (OPS-1b : `subscription_schedules.{create,retrieve,update,
  release}`, `subscriptions.{retrieve,update}`), validation 4242 en Test avec
  André en attente. **Ne jamais** deviner le mécanisme Stripe d'un downgrade « au
  fil de l'eau » (un `subscriptions.update` immédiat sans prorata ne repousse pas
  l'échéance — il faut un schedule).
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
- **Tests représentatifs — un composant se teste sur son ARBRE RENDU (V2-18c)** :
  corollaire front du piège OPS-1. Un composant DOM se vérifie via son sous-arbre
  effectivement **monté** (`querySelector` sur le rendu), **jamais** via une
  référence JS interne à un élément. Bug vécu (V2-18c) : `subscription.js
  addonStepper` **créait et câblait** le bouton « Confirmer » (listener, états
  `disabled`) mais le `return` **ne l'insérait pas** dans l'arbre → stepper sans
  moyen de confirmer en prod. Le test « headless » d'alors tenait la **référence**
  au bouton (câblée, donc verte) au lieu de le **chercher dans le DOM rendu** : un
  élément non monté restait « vert ». Règle : après avoir rendu le composant,
  localiser chaque élément interactif par `querySelector`/texte sur le nœud
  retourné et asserter sa présence **et** son comportement (ici : désactivé à
  l'ouverture, actif après changement de quantité). Couvert par
  `frontend-tests/subscription.test.mjs` (harnais Chrome headless
  `subscription-harness.html`, fetch simulé, verdict lu dans le DOM dumpé) — hors
  `frontend/` (jamais servi publiquement). NB : sur ce build (macOS,
  `--headless=new`), `--dump-dom` **écrit** le DOM sur stdout mais **ne quitte pas**
  proprement → le runner lit le flux et **tue** Chrome dès le verdict présent (ne
  jamais attendre la sortie du process, sinon 60 s de timeout et stdout perdu).
- **Noms de méthodes Stripe à vérifier contre le SDK réel (OPS-1b)** : le
  contrat à respecter n'est pas que le *comportement* des objets — c'est aussi la
  **surface** (noms de méthodes). Sur l'interface `StripeClient` (`client.v1.*`),
  la méthode d'écriture est **`update`**, **jamais `modify`** (`modify` est
  l'ancienne API par ressources, `stripe.Price.modify(...)`). Le script appelait
  `client.v1.products.modify(...)` → `AttributeError` (`KeyError 'modify'`) **en
  prod réelle** dès la branche de mise à jour (product existant retrouvé —
  l'idempotence du volet 1 marchait), mais **vert en test** parce que le fake
  `_Collection` exposait `modify`. Règle : ne jamais deviner un nom de méthode de
  mémoire ; vérifier par introspection contre le SDK installé (`dir(client.v1.
  products)` → `create/list/retrieve/update/delete/search`). Garde-fou :
  `test_stripe.py::test_fake_collection_matches_real_stripe_surface` vérifie que
  le fake n'expose **QUE** des méthodes du vrai `StripeClient` (aurait attrapé le
  bug). Tout nouvel appel Stripe dans un script/gateway : confirmer la signature
  réelle *avant* d'écrire, et refléter exactement cette surface dans les fakes.
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
- **SW & `/secrets` `no-store` (choix assumé)** : `sw.js` met en cache les réponses
  `/g|/b/…/secrets` (via `cache.put` en `networkFirst`) **malgré** leur en-tête HTTP
  `no-store` — c'est **voulu** : le mot de passe wifi doit rester lisible hors-ligne
  AVANT d'avoir le wifi (scénario porte d'entrée). Le `no-store` HTTP reste pour les
  **caches partagés/proxies** (le SW est un cache privé, sur l'appareil du voyageur).
  Même régime sur le lien de séjour `/b/` (branche miroir de `/g/`, v28).
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
  d'avant V2-08). Les emails partent en tâche de fond via **`auth._send_email_bg`**
  qui **avale toute exception** de l'envoi (best-effort, journalisée) — cf. piège
  générique ci-dessous : sans ce garde-fou, une panne SMTP annulait l'inscription.
  `BackgroundTasks` ne survit pas à un redémarrage d'uvicorn.
- **Exception d'une `BackgroundTask` = ROLLBACK de la requête (V2-16)** : une
  exception levée par une tâche `BackgroundTasks.add_task(...)` **remonte dans la
  pile de sortie de la requête** et fait un **rollback de la transaction** de la
  dépendance `get_conn` (`with db.connect() as conn`) — **y compris en FastAPI
  0.139**, alors que la réponse (ex. `201` + JWT) est **déjà partie**. Symptôme
  vécu en prod (24/07) : inscription avec email à domaine inexistant →
  `SMTPRecipientsRefused` dans la tâche d'envoi → **owner jamais committé** malgré
  le 201 → le `/me` suivant (jeton pourtant valide) renvoie 401 → éjection «
  session expirée » (ou « compte non créé »). Règle : **toute** tâche de fond
  déclenchée par une écriture DB doit être **best-effort** (try/except qui n'échoue
  jamais) — jamais laisser une `add_task(mailer.send, …)` nue. Reproduit et couvert
  par `test_auth_email.py::test_register_survives_verification_email_failure` (et
  `…forgot…`). Côté front, le corollaire : ne jamais faire confiance à un `201`
  seul ; le parcours pose le jeton **puis vérifie `/me`** avant tout appel
  authentifié (`js/authflow.js submitAuth`), et un `401` sur une **route publique**
  (inscription/connexion) ou la sonde de démarrage n'éjecte pas
  (`api.suppressUnauthorizedRedirect`).
- **BackgroundTasks retardent le commit de `get_conn` (V2-16b)** : suite directe
  du piège V2-16. Les `BackgroundTasks` s'exécutent **DANS le contexte de
  `get_conn`**, entre le return de l'endpoint et la sortie du générateur — donc
  le **commit** du `with db.connect() as conn` a lieu **APRÈS** elles. Une tâche
  d'envoi SMTP **lente** (domaine invalide/lent : plusieurs secondes avant
  d'échouer, même avalée) retarde donc la **VISIBILITÉ** des écritures de la
  requête, alors que la réponse (`201` + JWT) est déjà partie. Symptôme prod
  (25/07) : `register` renvoie 201, le front pose le jeton et appelle `/me` dans
  la foulée (**nouvelle connexion**) → owner pas encore committé → 401 « compte
  inconnu » → « session expirée » (journal : `register 201` puis `me 401` la même
  seconde ; le compte apparaît en base quelques secondes plus tard). Règle :
  **toute route qui ÉCRIT en DB PUIS programme une tâche de fond doit
  `conn.commit()` EXPLICITEMENT avant `return`** — jamais compter sur le commit
  du gestionnaire de contexte, qui attend la fin des tâches. Appliqué à
  `register`, `forgot`, `resend-verification` (`routers/auth.py`) ; le commit du
  contexte devient un no-op. Couvert par
  `test_auth_email.py::test_register_account_visible_before_slow_email_completes`
  (mailer qui BLOQUE sur un `threading.Event` : l'owner doit être visible depuis
  une 2e connexion pendant le blocage). NB : distinct de V2-16 (best-effort
  contre le *rollback*) — ici on corrige le *retard* de commit ; les deux gardes
  sont nécessaires ensemble.
- Erreurs de validation 422 lisibles (V2-16) : FastAPI renvoie un `detail`
  **liste** `[{loc,msg,type}]` (pas une chaîne ni `{code,message}`). `js/apierrors.js
  messageFromDetail(status, detail)` (module pur, testé hors navigateur) mappe
  chaque champ vers un libellé FR (email/password/full_name/token) ; `api.js`
  l'utilise dans `handleResponse`. Sans ce mapping, l'UI affichait « Erreur serveur
  (422) ». Toute nouvelle erreur de champ à traduire s'ajoute dans `fieldLabel`.
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
- Mode de trajet par catégorie + itinéraire par POI (V2-24) : `poi_categories.
  travel_mode` (`'driving'` | `'walking'` | NULL) porté par le **seed** (invariant 8 :
  jamais en dur) — `fuel`/`charging_station` → `'driving'` (toujours en voiture,
  même à 400 m : véhicule de location) ; `beach` → `'walking'` ; NULL = auto
  historique (à pied si ≤ 30 min, sinon voiture). **UN seul point de vérité de
  l'affichage** dupliqué en 3 endroits qu'il faut garder cohérents :
  `guide_page._fmt_dist` (SSR), `frontend/js/ui.js fmtDist` (back-office) et
  `frontend/guide/app.js fmtDist` (popups carte) — mode `'walking'` avec un
  **plafond `_WALK_MODE_MAX` (45 min)** au-delà duquel on rebascule en voiture
  (une plage à 90 min ne se fait pas à pied) ; repli propre si le temps du mode
  demandé manque (POI ancien → l'autre temps ; le prochain enrichissement ou
  `POST /recompute-distances` comble). Le mode ne touche JAMAIS les distances
  stockées, seulement l'affichage. `travel_mode` est porté par `repo.guide_pois`
  et `repo._POI_SELECT` (join `poi_categories`) **et** injecté dans `map_data`.
  **Boutons d'itinéraire sur TOUS les POI géolocalisés** (`_itinerary_links` →
  `.poi-nav` / `.route-link`) : Google Maps (`/maps/dir/?api=1&destination=lat,lon`),
  Waze (`waze.com/ul?ll=lat,lon&navigate=yes`), Apple Maps (`maps.apple.com/
  ?daddr=lat,lon`) — libellé Apple **localisé** (`maps_apple` : Plans/Apple Maps/
  Mapas), heading `go_there`. Simples liens `target=_blank` déclenchés par le
  voyageur → **invariant 4 intact** (aucun chargement/SDK auto). Ne pas confondre
  avec les **blocs de trajet** M-14 (airport/train → planification depuis le lieu,
  affordance distincte). Toute modif de `guide/*` (app.js, guide.css) → **bumper
  `sw.js VERSION`** (ici v15 → v16).
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
- Calendrier des séjours (V2-23a) : le parser iCal (`api/ical.py`) est **pur**
  (aucun réseau) ; le fetch (`api/calendars.fetch_ical`) est le seul point réseau
  et il est **injecté** (`deps.get_calendar_fetcher`) → toute la synchro se teste
  sans sortir (fakes dans `test_calendars.py`/`test_calendar_api.py`). **Sémantique
  de dates** : `ends_on` = jour du **départ** (= DTEND, exclusif pour les VEVENT
  journée entière) ; toute logique de chevauchement/rotation raisonne en intervalle
  **semi-ouvert** `[starts_on, ends_on)` — ne jamais la refaire en jours inclusifs
  (une rotation deviendrait un faux chevauchement). L'upsert de synchro ne touche
  QUE `starts_on`/`ends_on` (et réactive un `cancelled` qui réapparaît) : le
  `status` d'un séjour non annulé et les champs manuels survivent — **ne jamais**
  ajouter `guest_name`/`checkin_time`/`status` au `DO UPDATE SET` de
  `upsert_imported_booking`, sinon une promotion ou une complétion manuelle serait
  écrasée à la prochaine sync. `mask_url` masque l'URL iCal (secret) : ne jamais
  renvoyer `ical_url_enc` ni l'URL déchiffrée dans une réponse (`list_calendars`
  n'expose pas la colonne ; le router mask via `list_calendars_with_url`). Le
  bouton « Synchroniser maintenant » est rate-limité (cooldown 20 s, `429`) :
  l'ajout d'un flux synchronise déjà → un sync-now immédiat après ajout **répond
  429** (attendu). Timer systemd toutes les 4 h (`ops/sync_calendars.py` +
  `casaguide-sync-calendars.{service,timer}`, patron opsenv/OPS-1).
- Nature vs statut & avertissement à la saisie (V2-23b, volet 0, invariant 14) :
  depuis la migration 015, `bookings.status` est un **cycle de vie pur**
  (`active`/`cancelled`) et `bookings.nature` porte la sémantique. Le `DO UPDATE
  SET` de `upsert_imported_booking` ne doit contenir QUE `starts_on`/`ends_on`/
  `status` (réactivation `cancelled`→`active`) — **jamais** `nature` (une
  qualification manuelle serait écrasée à chaque sync). Les chevauchements/rotations
  filtrent sur `_is_occupied` (occupé = reservation/private, actif, non rattaché),
  plus sur `status='confirmed'` (valeur qui n'existe plus). **Duplication
  volontaire front/back de la règle d'intervalle** : `frontend/js/lib/overlaps.js`
  (module PUR, testé par `frontend-tests/overlaps.test.mjs` sans Chrome) reprend
  l'intervalle semi-ouvert + la notion d'occupation du backend pour **avertir à la
  saisie** (encart instantané, aucun aller-retour serveur). Le front **avertit**, le
  back **fait foi** (`GET /calendar` recalcule) — les deux DOIVENT rester alignés
  (si on change la règle d'un côté, changer l'autre + les deux tests). L'alerte
  n'est **jamais bloquante** ; seul le cas **rouge** (chevauchement d'une
  occupation) demande un 2e clic « Enregistrer quand même ». Un **bloc miroir**
  rattaché (`linked_booking_id`, §0.5) est filtré de `GET /calendar` (liste + vue
  purs) mais jamais supprimé. La vue est **ancrée sur aujourd'hui** (§0.7) :
  `todayISO()` sépare « à venir » (départ ≥ aujourd'hui, en cours compris) des
  passés/annulés repliés — le flux Abritel exporte aussi l'historique.
- Préparation des séjours & moteur d'interventions (V2-23b, volet 1, invariant 14) :
  le **moteur `api/care.py` est PUR** (aucune base, aucun réseau, `today` toujours
  passé) et **ne stocke jamais** : les interventions sont **calculées** comme les
  fenêtres. La **nature pilote** la préparation, pas le statut — `reservation` =
  tout, `private` = même entretien **sans** welcome pack, `works`/`unavailable`/
  `unqualified` = aucune intervention. Toute sortie est **quantifiée** par
  `guest_count` ; une quantité inconnue s'affiche (« nombre de voyageurs non
  renseigné »), jamais un trou. Le **nombre d'enfants se déduit** de
  `children_ages` (jamais une colonne redondante) ; les âges **suggèrent**
  l'équipement du catalogue (`suggest_equipment`) mais n'ajoutent **jamais** une
  demande d'office (la modale propose, le propriétaire confirme). Les **hommes-heures**
  de rotation vivent dans `care_rules` (invariant 8) et sont **laissés à `null`**
  tant qu'André ne les a pas mesurés → `turnaround_signal` renvoie `level='unknown'`
  plutôt qu'une valeur inventée ; ne **jamais** coder une charge en dur. `care_rules`
  est un **JSONB** : `create_property` pose le défaut (`care.default_care_rules`) et
  amorce le catalogue (`seed_request_types`) ; `update_property` le **sérialise**
  (`json.dumps`), jamais un objet brut. **Duplication volontaire front/back** (comme
  la règle d'intervalle) : les tranches d'âge & suggestions vivent dans
  `frontend/js/lib/care.js` (front, suggère à la saisie) ET `api/care.py` (back, fait
  foi/planning) — si l'une change, changer l'autre **et** les deux tests
  (`test_care.py`, `care.test.mjs`). La **relance active §0.6** (`missing_info`) ne
  signale que les séjours **occupés, actifs, non rattachés, non terminés** ; les
  coordonnées ne sont exigées que si une intervention **en cours de séjour** est
  prévue (elle suppose un rendez-vous). RGPD (à formaliser V2-25) : les âges
  d'enfants suivent le régime des coordonnées (visibles pour l'équipe, séjours en
  cours et à venir uniquement — appliqué au planning `/s/` du **volet 2**).

- Planning du cahier d'équipe & grammaire des signaux (V2-23b, volet 2) :
  - **L'entité du planning est la FENÊTRE, pas le séjour** — `care.build_planning`
    (pure, `today` passé, jamais stockée, comme les fenêtres/chevauchements) produit
    trois types d'entrées : `window` (préparation avant une arrivée occupée : « libre
    depuis / prochaine arrivée → fenêtre », deux échéances si `luggage_drop_time`,
    **longue vacance ancrée sur l'arrivée**), `midstay` (intervention en cours de
    séjour, maison **habitée** → rendez-vous), `idle` (séjour non occupé **grisé**,
    « rien à préparer » — ferme la question plutôt que laisser un trou). Rendu en tête
    du cahier par `guide_page._render_planning` (server-side, lisible sans JS).
  - **RGPD — coordonnées visibles pour les séjours en cours/à venir uniquement.**
    `care._show_contact(booking, today)` = `ends_on ≥ today` : un lien `/s/` partagé
    ne donne **jamais** accès au répertoire des locataires passés. Même régime pour
    les âges d'enfants. Le planning filtre déjà l'historique (n'affiche que l'à-venir)
    mais le gate reste posé par prudence. Le **gating Pro** du planning est **déjà
    assuré** par `staff_access` sur `/s/` (invariant 11) — ne pas le re-gater.
  - **Le signal de rotation part de l'ÉCHÉANCE LA PLUS PROCHE.** `turnaround_signal`
    (hommes-heures → neutre/ambre/rouge, recommandation d'effectif) est calculé sur
    la fenêtre qui se termine au **dépôt de bagages** s'il est renseigné, sinon à
    l'arrivée effective (`care._prep_deadline`). Un dépôt de bagages **avance**
    l'échéance et peut faire basculer une rotation confortable en serrée. Le signal
    s'affiche **des deux côtés** : `RotationOut.signal` (calendrier propriétaire,
    calculé dans `routers/calendars._rotation_signal`) **et** le planning `/s/`.
  - **Le travail à plusieurs n'est PAS parfaitement divisible.** La durée écoulée
    n'est **jamais** `charge / effectif` (division naïve = rotations intenables
    promises, la pire erreur possible) mais `charge / débit_effectif`, où
    `débit = 1 + (k-1) × parallel_efficiency` (`care._effective_throughput`).
    `care_rules.turnaround.parallel_efficiency` (défaut **0,75**, invariant 8, jamais
    en dur) — la 2ᵉ personne ne vaut que 0,75 d'un plein temps. Explicite et
    **configurable** dans « Réglages d'entretien », jamais appliqué en silence.
  - **Anti-saturation du signal (front).** L'incomplétude (voyageurs/coordonnées
    manquants, nature à qualifier) est **sobre** : pastille neutre `.cal-incomplete`
    « Incomplet » (jamais le triangle, **strictement réservé au danger** —
    chevauchement de deux occupations, rotation infaisable). Agrégée **en tête**
    (`.incomplete-box` « N séjours incomplets », dépliable) avec **saisie rapide en
    ligne** du nombre de voyageurs (`PATCH /bookings/{id}` — le champ manque sur la
    quasi-totalité des imports ; les remplir un par un via la modale est coûteux),
    plutôt qu'un badge répété. **Validation** : tout champ comptant des **personnes**
    (capacité, voyageurs à faible occupation, effectif de ménage, `guest_count`) est
    un **entier ≥ 1** ; seules les **heures** acceptent des décimales.
  - `_is_occupied` et `effective_times` sont **dupliqués** dans `care.py`
    (`_is_occupied`, `_effective_times`) pour garder le moteur **pur** et sans
    dépendance à `calendars.py` — même règle des deux côtés, à garder alignée.

- Coordonnées séparées & demande du voyageur (V2-23b, volet 3) :
  - **Le téléphone est la coordonnée qui compte pour l'opérationnel.** La relance
    (`care.missing_info`) signale « **téléphone manquant** » (code `phone_missing`,
    **plus** `contact_missing`) car une intervention en cours de séjour se cale par
    appel/WhatsApp — un email ne convient pas d'un rendez-vous le jour même. Le
    téléphone effectif = `care.effective_phone` (`guest_phone`, sinon legacy
    `guest_contact` **s'il ressemble à un téléphone** : ≥ 6 chiffres, pas d'`@` — même
    heuristique que le backfill de la migration 018). Idem `effective_email`. Le front
    (`calendar.js`) doit passer par le code `phone_missing` (le `contact_missing`
    n'existe plus).
  - **La liste des langues du locataire n'est jamais en dur** (§3.0, comme les
    quotas — invariant 8) : elle se lit dans `property.published_langs` **+** la
    langue source (`default_lang`), dédupliquées. Offrir une langue que le produit ne
    sait pas encore générer créerait une promesse intenable (« locataire
    néerlandophone » sans guide NL). `LANG_LABELS` ne fait qu'**embellir** un code
    présent ; un code inconnu s'affiche brut.
  - **Le libellé d'une demande du voyageur vient TOUJOURS du template**, jamais d'une
    valeur libre du POST (`repo.requestable_section_label` : la section doit être
    **visible**, `audience='guest'`, et porter `field_schema.request` sur CE guide) —
    le voyageur n'est pas authentifié, il ne choisit qu'une section + un message. La
    demande se rattache au séjour **occupé en cours** (à défaut le suivant,
    `repo.current_or_next_booking_by_guide_token`) : aucun séjour → **409** (rien à
    quoi rattacher, jamais de fuite). **Rate-limit par guide** (429,
    `guest_request_min_interval_s`), le staff n'a **jamais** de bouton de demande
    (invariant 7). **Invariant 4 intact** : le `POST /g/{token}/requests` est une
    **action** du voyageur, pas un appel au rendu.
  - **Commit AVANT la tâche de fond** (V2-16/V2-16b) : l'endpoint de demande écrit la
    `booking_requests` puis `conn.commit()` **explicitement** avant de programmer
    l'email de notification (`emails.guest_service_request_email`) via
    `_send_email_bg` (best-effort, avale toute exception → jamais de rollback ni de
    retard de visibilité). Ne jamais laisser une panne SMTP annuler/retarder la
    demande.
  - **Bumper `sw.js VERSION`** à toute modif de `frontend/guide/*` (ici v24 → v25 :
    `app.js initRequestService` + `guide.css .svc-request*`).

- Fenêtre « Envoyer le guide » & précédence de langue (V2-23c, volet 3) :
  - **Les tokens de séjour/vitrine se génèrent À LA DEMANDE, côté propriétaire,
    jamais côté public.** `repo.ensure_stay_token`/`ensure_showcase_token` utilisent
    la **même fabrique que `guide_token`** — **hex 128 bits
    `encode(gen_random_bytes(16),'hex')`** (jamais `token_urlsafe` : tout le système
    est en hex) — avec une **garde SQL idempotente/atomique** (`UPDATE … WHERE …
    stay_token IS NULL` **puis relecture**) : deux clics simultanés ne créent qu'un
    token (la garde tranche en base, pas en Python). Endpoints **authentifiés**
    (`routers/share.py`, `POST …/showcase-link` & `…/bookings/{bid}/stay-link`,
    gardés par `OwnedProperty`) → le token n'est **jamais** créé ni renvoyé par une
    route publique. Le lien **maison** n'a pas d'endpoint (le front le construit
    depuis le `guide_token` déjà connu — il SURVIT tel quel). `/b/` et `/v/` n'ont
    **pas de slug décoratif** (`_real_token` ne s'applique qu'à `/g/`).
  - **La fenêtre ne code AUCUN libellé d'envoi.** Les gabarits email/WhatsApp vivent
    dans l'inventaire i18n (clés **`ui.send_*`** — FR/EN/ES dans `guide_page._UI`,
    langues supplémentaires dans `ui_translations`) et sont lus résolus via
    l'endpoint public **`GET /send-templates?lang=`** (même overlay que le SSR). Le
    front (`js/components/sendmenu.js`) substitue `{property}`/`{name}` et compose le
    `mailto:`/`wa.me`. Ajouter une clé `ui.send_*` → régénérer `i18n/inventory.json`
    (`ops/i18n_inventory.py`, `--check` = gate) comme tout libellé du guide.
  - **§3.5 — précédence de langue sur `/b/` (une seule source de vérité).** Le SSR
    expose `data-guest-lang` sur le `<body>` du variant séjour **si et seulement si**
    `guest_lang` est une langue **offerte** par le guide (publiée + registre —
    invariant 15 ; sinon repli langue source, attribut absent). `frontend/guide/app.js
    initLang` lit ce seul attribut : présent → la fiche fait foi, la devinette M-09
    (`navigator.language`, préférence `localStorage` — souvent héritée d'un autre
    guide) **ne s'y superpose plus** (un clic explicite sur une puce gagne via
    `?lang=`) ; **absent → M-09 intact**, exactement comme sur `/g/` (là le serveur
    ne sait rien du visiteur, la devinette est une qualité). `/g/` n'est **jamais**
    touché.
  - **§3.5 amendement 2 — le choix de langue se retient SOUS LE GUEST, pas sous la
    navigation (clé `casaguide:lang:b`).** Une clé `localStorage` **dédiée aux liens de
    séjour**, `casaguide:lang:b`, **distincte** de la clé globale M-09 `casaguide:lang` :
    écrite **UNIQUEMENT depuis une page `/b/`** (clic sur une puce **et** branche
    `?lang=` explicite de `initLang`), **lue par tous les liens `/b/` nus** (tout
    logement, tout séjour), **jamais lue ni écrite par `/g/`**. Le marqueur « on est sur
    `/b/` » est le **préfixe de l'`apiBase`** (`apiBase.startsWith("/b/")` — UNE seule
    source de vérité, celle déjà en place `/g/`·`/b/`·`/v/`), **pas** `data-guest-lang`
    (qui ne dit que si la fiche connaît la langue). **Précédence sur un lien `/b/` nu**
    (sans `?lang=`) : **clé guest** si posée & offerte → **`guest_lang`** de la fiche →
    **M-09** (fiche muette). `?lang=` explicite gagne toujours et alimente la clé guest.
    Limites assumées : le choix ne suit pas d'un appareil à l'autre (le rappel serveur du
    guest récurrent, **périmètre owner strict — jamais inter-tenant**, est une mission à
    part adossée à V2-23d) ; un choix explicite du locataire prime une fiche corrigée
    après coup. `/g/` garde M-09 intact (le serveur ne sait rien du visiteur — la
    devinette est une qualité). Toute modif de `frontend/guide/*` (ici `app.js initLang`)
    → **bumper `sw.js VERSION`** (v29 → v30).
  - **Le manque devient une invitation (§3.3), jamais un `mailto:` vide** : un séjour
    sans email (resp. téléphone) désactive le canal et propose « Ajouter un email à
    ce séjour » (`calendar.openBookingOnLoad` + navigation → ouvre la modale du
    séjour). Les liens produits par la fenêtre ne portent **jamais** le `guide_token`
    hors le choix explicite « lien maison ».

- Migrations & amorçage — leçons de l'incident 015 (31/07, à ne jamais réapprendre) :
  1. **Une migration se teste contre l'ÉTAT ANTÉRIEUR RÉEL**, jamais en la rejouant
     sur son propre résultat ni sur une base neuve construite depuis `schema.sql`
     (où les contraintes sont **déjà** les nouvelles). La 015 échouait
     systématiquement en prod (`new row violates check constraint
     bookings_status_check`) parce que l'`UPDATE` des valeurs de statut s'exécutait
     **avant** le `DROP` de la contrainte héritée — invisible en test car la suite
     bâtit sa base depuis `schema.sql`. Règle : **tout changement de contrainte
     `CHECK` suit l'ordre `DROP → UPDATE → ADD`** (retirer l'ancienne contrainte,
     migrer les valeurs, poser la nouvelle) ; et une migration doit être exercée au
     moins une fois contre une base portant l'ancien schéma.
  2. **Tout état amorcé à la CRÉATION exige un BACKFILL** pour les lignes
     antérieures. Une migration SQL n'ajoute que la colonne/table (vide) : le
     CONTENU applicatif posé par le code de création (ex. `care_rules` par défaut,
     catalogue de demandes) n'existe pas sur les logements déjà en base. Fournir un
     script `ops/` idempotent qui **réutilise les mêmes fonctions que la création**
     (jamais une copie du JSON dans le SQL — invariant 8) et le documenter comme
     **étape post-migration** dans `docs/deploiement.md`. Exemple :
     `ops/backfill_care.py` (V2-23b) rejoue `care.default_care_rules()` +
     `repo.seed_request_types` sur les logements où `care_rules = {}`.
  3. **`deploy.sh` fait `git pull` (bascule du FRONT) AVANT les migrations** : une
     migration en échec laisse un front **neuf** devant une API **ancienne** (les
     statiques sont relus du disque, l'API reste en mémoire sur l'ancien code) →
     des symptômes qui désignent le mauvais coupable (champ manquant, `undefined`
     affiché). Devant un symptôme « front OK / données absentes » après déploiement,
     **vérifier d'abord le journal des migrations**, pas le code applicatif.

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
