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

**Partage des gestes (doctrine, 06/08/2026).** `git push`, `ssh` vers le VPS et
`deploy.sh` sont les gestes d'ANDRÉ, exclusivement — jamais exécutés par Claude
Code, et jamais PROPOSÉS comme prochaine action (une suggestion « déploie sur
le VPS » affichée au prompt le 06/08 a déclenché une alerte de sécurité — la
frontière vaut aussi pour les suggestions). La mission de Claude Code se
termine au commit local, hash en première ligne du rapport ; la suite
appartient à André.

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

**La surcharge du code de boîte à clés MEURT AVEC LE LIEN (V2-23c volet 2,
migration 024).** Le logement porte le code par défaut (`property_secrets`) ; un
**séjour** peut le **remplacer** (`bookings.keybox_code_enc`, chiffré AES **comme
`property_secrets`** — mêmes helpers `api/crypto`, clé hors base, invariant 5 ;
jamais une seconde implémentation). C'est la « surcharge welcome pack » (V2-23b)
appliquée aux secrets : le logement porte le défaut, le séjour peut le remplacer,
le moteur lit à travers (`guide.py _secrets_payload(keybox_override=…)`, le repli
vit **côté serveur, une seule fois**). Seul `GET /b/{t}/secrets` sert la surcharge
→ elle **meurt à J+8 avec la page** (`_resolve_stay`), comme le reste des secrets du
séjour. Le lien **maison** `/g/{t}/secrets` **ignore** les surcharges (acté : le QR
imprimé sert toujours le code du logement) ; la vitrine `/v/` n'a **aucune** route
secrets. La surcharge ne transite **JAMAIS** par `GET /calendar`/`BookingOut`/une
liste (absente de `repo._BOOKING_COLS` — même régime que les secrets du logement) :
l'unique lecture d'édition est `GET .../bookings/{bid}/keybox` (propriétaire,
patron des secrets). NULL = pas de surcharge → code du logement (zéro backfill).

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
16. **Le rattachement absorbe la substance du bloc — jamais de perte silencieuse
    (V2-23f)** : rattacher un **bloc miroir** à un séjour **maître** (`linked_
    booking_id`, §0.5) le masque de la vue (invariant 14) mais un bloc peut porter
    des données réelles — des **demandes** de locataire (`booking_requests`), des
    coordonnées saisies. Le masquer sans les déplacer serait une **perte
    silencieuse** (bug prod 05/08, cas Tracy Russel). Au **point UNIQUE** où
    `linked_booking_id` est posé (`repo.absorb_block_into_master`, appelé par
    `routers/calendars.update_booking` — **les deux chemins d'UI**, modale §0.5 et
    bandeau de chevauchement §0.4, convergent sur ce même `PATCH`), le maître
    **absorbe** le bloc, dans la même transaction : (1) **toutes** les demandes
    migrent (`UPDATE booking_requests SET booking_id={maître} WHERE booking_id=
    {bloc}`), `pending` **comme** `accepted` (ces dernières nourrissent
    `plan_interventions` — le planning doit rester vrai) ; (2) les champs de fiche
    (`guest_phone`, `guest_email`, `guest_lang`, `guest_count`, `children_ages`,
    `keybox_code_enc`, `luggage_drop_time`, `notes`) sont repris **seulement si le
    maître est vide/NULL** (`COALESCE` + `NULLIF` sur les vides) — une valeur déjà
    présente du maître n'est **JAMAIS** écrasée (esprit invariant 13) ;
    `keybox_code_enc` se copie tel quel (bytea chiffré, aucun déchiffrement,
    invariant 5). Le **détachement** (`linked_booking_id`→NULL) ne rejoue **rien**
    à l'envers : le transfert est un **acte, pas un miroir** (aucune migration
    inverse). L'opération est **idempotente** (re-rattacher = zéro double). Aucun
    changement de schéma, aucun backfill (les rattachements passés se corrigent à
    la main). Couvert par `test_calendar_api.py` (absorption pending+accepted,
    champs vides repris, champs garnis intacts, détachement sans retour,
    idempotence, bloc étranger→404).
17. **Le flux possède les dates SAUF marqueur explicite (V2-23g, migration 026)** :
    par défaut la synchro iCal ne rafraîchit que `starts_on`/`ends_on` (invariant
    13) — mais dès que le propriétaire **déplace réellement** une date d'un séjour
    **importé** (`external_uid` non NULL) via le PATCH, `bookings.dates_overridden`
    passe **automatiquement** TRUE (l'intention EST la modification ; posé
    UNIQUEMENT si une date **change**, car le front renvoie toujours les deux dates
    — sans quoi éditer un nom figerait les dates). Marqueur TRUE → l'upsert de
    synchro **ne rafraîchit plus** les dates (comme le cas Tracy : arrivée avancée
    au 07.08 revenait au 08.08 toutes les 4 h). `dates_overridden` rejoint la liste
    des champs protégés dans l'esprit de l'invariant 13. Les dates **saisies**
    restent la source de vérité de TOUT l'aval — chevauchements, rotations, planning
    `/s/`, **envoi J-7** (`care.select_auto_sends` lit `starts_on`). La synchro
    **mémorise** à chaque passage les dernières dates du flux
    (`feed_starts_on`/`feed_ends_on`, écrites toujours) — impossibles à recalculer
    au rendu (`GET /calendar` n'a aucun accès réseau au flux) — d'où le **signal de
    divergence** (fiche + calendrier, ton **sobre**, jamais le triangle) quand elles
    diffèrent des dates saisies : protéger sans signaler masquerait une vraie
    modification côté plateforme. **« Reprendre les dates du flux »** (`POST
    …/bookings/{id}/reset-dates`, `repo.reset_booking_to_feed_dates`) réaligne sur le
    flux et **rend la main** (`dates_overridden`→FALSE) — chemin **dédié**, jamais le
    PATCH (qui reposerait le marqueur). Une saisie directe n'a pas de flux → jamais
    de marqueur, jamais rien à reprendre (404). Le marqueur et les dates du flux
    **survivent** au cycle de vie (annulation/réapparition). DEFAULT FALSE = aucun
    backfill. Couvert par `test_calendar_api.py` (marqueur posé & survit à la
    synchro, divergence exposée, reprise du flux, saisie directe sans marqueur,
    édition sans changement de dates, cancel/réapparition, chevauchement+rattachement
    sur dates ajustées) + harnais front `calendar-harness.html`.
18. **Une succession n'hérite JAMAIS du registre d'envois (V2-23h)** : une
    modification de réservation côté plateforme (Vrbo/Abritel constaté 06/08) réémet
    l'événement iCal sous un **nouvel `external_uid`** → l'ancien passe `cancelled`
    avec toute sa fiche (invariant 13), un séjour **vierge** naît du nouvel uid, et
    le lien `/b/` déjà envoyé est **mort** (résolution refuse les annulés). On
    **détecte au rendu** (aucun état de plus, aucun rapprochement dans la synchro —
    `care.find_succession_candidate`, pure : import actif à **fiche pauvre** — ni
    nom, ni email, ni téléphone — + prédécesseur `cancelled` importé d'**autre uid**,
    dates chevauchantes, porteur de substance ; meilleur = recouvrement max puis plus
    récent) et on **propose** (jamais l'automatisme, doctrine 0.4) : bandeau fiche +
    signal calendrier au ton **attention** (ambre, **jamais** le triangle),
    `BookingOut.succession = {source_id, source_label, message}` **construit côté
    serveur** (langage humain, **jamais d'uid**). L'action `POST
    …/bookings/{id}/inherit` (`repo.inherit_booking_fiche`) réutilise l'absorption
    **V2-23f** (mêmes champs + `guest_name` ; copie **vers le vide seulement**,
    `COALESCE`+`NULLIF` ; demandes `pending`+`accepted` migrées ; `keybox_code_enc`
    tel quel ; idempotent) avec DEUX différences **gravées** : **(a)** pas de
    `linked_booking_id` (succession, pas bloc miroir — l'ancien reste annulé) ;
    **(b)** le registre **`guide_sends` n'est JAMAIS hérité** — c'est le cœur : le
    nouveau séjour est **vierge d'envoi**, donc la fenêtre d'envoi et le J-7
    renverront un lien **VIVANT** au **nouveau** token (le `stay_token` de l'ancien
    n'est pas repris non plus). Le signal **disparaît** après reprise **ou** saisie
    manuelle (le séjour cesse d'être à fiche pauvre). Aucun schéma à migrer.
    Back-office seul (aucun bump SW). Couvert par `test_care.py` (détection : fiche
    pauvre+cancelled substantiel→candidat, fiche garnie→silence, multiples→le
    meilleur, sans substance→silence) + `test_calendar_api.py` (signal exposé,
    inherit remplit le vide/garnis intacts/demandes migrées/**guide_sends non
    hérité**/nouveau token≠ancien/source étrangère→404/idempotence, signal disparaît)
    + harnais front `calendar-harness.html`.

    **Note V2-32 volet 1 — le registre arbitre les canaux d'envoi, le premier servi
    gagne (migration 028).** Le J-7 assisté WhatsApp (`docs/calendrier.md §9.5`) ajoute
    un troisième `guide_sends.origin` : **`whatsapp_assisted`** (« Marquer envoyé ✓ »,
    geste **déclaratif** — wa.me ne confirme rien ; `POST …/bookings/{id}/mark-sent`,
    authentifié, multi-tenant, idempotent). Le moteur `care.select_auto_sends` gagne
    `AutoSendPlan.to_whatsapp` (mêmes règles J-7 que `to_send` — natures, fenêtre,
    publié+`auto_send_guide`, non déjà envoyé — au seul changement près : **téléphone
    effectif présent**, email indifférent). **Propriété émergente** : un séjour avec
    email ET téléphone est **dans les deux listes** (`to_send`+`to_whatsapp`) tant que
    rien n'est parti ; le premier canal servi (email auto de 9 h **ou** envoi WhatsApp)
    pose une ligne `guide_sends` kind='stay' qui met `already_sent` à vrai et le retire
    des **deux** au passage suivant — **aucun doublon possible par construction, aucun
    second verrou**. Le registre est déjà le verrou (invariant 18) : c'est la **même**
    idée, étendue au choix du canal. Corollaire §4 : le motif `auto_send_email_missing`
    de `missing_info` s'éteint dès qu'une ligne stay existe (tout origin,
    `guide_already_sent`). Back-office seul (aucun bump SW). Couvert par `test_care.py`
    (to_whatsapp : téléphone/fenêtre/natures/opt-out, deux-listes, retrait par registre)
    + `test_calendar_api.py` (mark-sent : enregistre/idempotence sans doublon/404/422/
    retrait de la file ; **test croisé** : l'email auto est supprimé après un mark-sent
    WhatsApp) + harnais front `calendar-harness.html`.
19. **Aucun libellé visible sans entrée d'aide — couverture PAR TEST, suite rouge
    sinon (V2-31 volet 3a)** : le back-office porte une recherche d'aide (bouton
    « Aide » + ⌘K, `frontend/js/help/`) dont l'index (`help/index.js`) est **la
    source de vérité, de la donnée pas du code**. Le veto d'André est **mécanique**,
    pas une discipline : `frontend-tests/help-coverage-harness.html` **rend les
    vraies vues** du back-office (fetch simulé + stub Leaflet, aucun réseau),
    **collecte les libellés réellement affichés** (boutons, entrées de menu, portes,
    titres d'écran/panneau) et vérifie que **chacun est couvert** par l'index
    (`isCovered`) — un libellé sans entrée fait **ROUGIR** `help-coverage.test.mjs`
    (+ « le test du test » : un libellé bidon doit rester non couvert). **Ajouter un
    bouton/menu/titre au back-office sans enrichir l'index casse la suite.** Les
    **exclusions** (verbes universels « Annuler/Fermer/Enregistrer », valeurs
    dynamiques comme le nom du logement, contenu data-driven du guide : titres de
    section de l'éditeur, catégories de POI) sont **explicites et justifiées** dans le
    harnais — l'exception, **jamais** le contournement (ne JAMAIS ajouter un libellé
    aux exclusions pour « faire passer »). **Zéro-résultat interdit** à l'écran
    (approches + repli « Voir toutes les rubriques »). Chaque recherche est
    **journalisée** (`help_searches`, migration 027, best-effort : un échec de journal
    ne casse jamais la recherche ; `results_count = 0` = santé de l'index).
    Back-office FR délibéré → **hors inventaire i18n voyageur**. Runbook :
    `docs/aide.md`. Back-office seul (aucun bump SW).
20. **Le fil mesure la SUBSTANCE, jamais la déclaration (V2-31 volet 2)** : chaque
    logement expose un « fil des 7 étapes » (`docs/audit_ux.md` §2) — `PropertyOut.
    journey`, calculé par la **fonction PURE** `api/journey.py` (patron `care`, aucune
    base/réseau, aucune colonne/migration : tout se déduit de l'existant). L'ancien
    « Complétude X % » **disparaît** (carte + éditeur), remplacé par « Étape k/7 ·
    *action* » cliquable. Le calcul se fait **exclusivement sur la substance réelle**
    (contenu de section non vide, secret posé par `IS NOT NULL` — **jamais déchiffré**,
    POI validés, `guide_sends`), **jamais** sur la bascule déclarative « Section
    complétée » — c'est elle qui produisait le pourcentage MENSONGER (Villa Ballarin,
    publiée et servie, affichait 4 %). Critères : É1 adresse+contact/couverture ; É2
    arrivée+accès/keybox+wifi ; É3 des POI existent ; É4 ≥1 retenu ET 0 en attente ;
    É5 **jamais binaire** (compte informatif, hors « k/7 » — on ne culpabilise pas le
    facultatif) ; É6 publié ; É7 ≥1 envoi ; au bout « Guide envoyé ✓ ». La bascule
    « Section complétée » est **retirée** de l'éditeur (colonne `completed`
    **conservée** en base, non destructif, aucune migration) ; « Visible dans le
    guide » **intouchée**. Seule l'**adresse absente** bloque la publication (guidage
    souple, audit 0.2 : avertir, jamais un mur). Duplication front/back **assumée**
    (`frontend/js/journey.js` `sectionHasSubstance`/`ESSENTIAL_CODES` miroir du back,
    comme `js/lib/care.js`). Runbook : `docs/parcours.md`. Back-office seul (aucun
    bump SW). Couvert par `test_journey.py` (fonction pure + cas Villa Ballarin réel)
    et `test_api.py::test_property_exposes_journey_measuring_substance`.
21. **Un fait de zone déclaré se rend même si la section n'a JAMAIS été enregistrée
    (V2-07 volet 1bis)** : `repo.guide_sections` ne renvoyait que les sections ayant
    une ligne `property_sections` existante — une section jamais sauvée par le
    propriétaire n'a pas de ligne, le guide la sautait, et l'`area_fact` adossé
    (`food_delivery`, mais aussi `waste_rules`/`noise_rules` depuis toujours) restait
    **invisible** (bug prouvé en prod 11/08 : encart livraison servi seulement après
    un « Enregistrer » vide). Désormais `guide_sections` **part des templates guest**
    en LEFT JOIN : une section **sans ligne** (`virtual=TRUE`) n'entre que si elle
    **déclare un fait de zone** (`jsonb_exists(field_schema,'area_facts')`) — jamais
    une coquille pour les 40+ autres sections. Le tri final des **vraies coquilles
    vides** (fait absent/vide) se fait au rendu, **une seule source de vérité par type
    de fait** : `guide_page._prune_virtual_sections` garde une section virtuelle
    **seulement si** `_render_section_facts` (via `_FACT_INLINE`) rend un encart non
    vide — jamais de logique de vacuité dupliquée en SQL. L'élagage est appelé **au
    point d'assemblage** (`routers/guide._assemble_guide`, pour que `/data` ET le SSR
    listent EXACTEMENT les mêmes sections) **et** en tête de `render_guide`
    (idempotent, garde-fou d'un appel direct). Un `is_visible = FALSE` **explicite**
    masque **toujours** (`COALESCE(ps.is_visible, TRUE)=TRUE` dans la requête), fait
    présent ou non — le choix du propriétaire prime (miroir de l'invariant de
    visibilité des médias). **Corollaire tuile (Pièce 2)** : un fait de zone n'a pas
    de POI → aucune porte d'entrée dans la grille de services ; `_service_fact_tiles`
    ajoute une tuile (icône `bike` du seed, libellé `name_i18n` du template — 7 langues
    déjà là, zéro clé i18n neuve) **seulement si** l'encart se rendrait (≥1 plateforme),
    au même rang et dans la même grammaire que les catégories → correspondance
    tuiles ↔ blocs 1:1 (V2-12) préservée. La section rendue rejoint automatiquement
    l'index de recherche (V2-33, moisson DOM) → une requête « livraison » aboutit.
    **Comportement de la tuile** : `href="#{code}"` est l'**ancre native** (repli sans
    JS ; `.sec-card{scroll-margin-top:64px}` la décolle de la barre d'onglets collante
    — V2-07 1quater) ; `data-fact="{code}"` fait ouvrir à `app.js` (`initServiceFacts`)
    le **MÊME mode filtré** que les tuiles de catégorie (V2-07 1quinquies) — la section
    ciblée seule à l'écran + « Retour aux services », `setServiceFilter` étendu aux
    `.sec-card[id]` (classe `.filter-target` + `.fact-filtered` qui masque la carte,
    pas de POI). Le tap filtre **directement** puis pose l'ancre en `pushState` (aucun
    hashchange → le filtre tient) ; un **deep-link nu** `#{code}` reste une ancre
    (section visible dans la page). La recherche (V2-33) **lève** un filtre actif avant
    de révéler (`reveal` → `window._setServiceFilter("")`, généralise la levée du filtre
    cuisine). Généricité : tout futur fait de zone doté d'une tuile en hérite sans
    nouveau code. Back-office/SSR seul, **sauf** V2-07 1quater/1quinquies (guide.css +
    app.js précachés → bump SW) ; aucune migration (`virtual` déduit à la lecture).
    Couvert par `test_guide_page.py` (section virtuelle rendue/non-coquille, section
    réelle jamais élaguée, tuile présente/absente/localisée, `data-fact`) et
    `test_api.py::test_zone_fact_renders_even_without_saved_section` (bout en bout contre
    l'état réel : rend sans ligne, `is_visible=FALSE` masque) ; front `guide-anchor-offset`
    (scroll-margin) et `guide-fact-filter` (mode filtré, retour, non-régression V2-12,
    recherche, deep-link).

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
psql -d casaguide -f db/migrations/022_guide_sends.sql # traçage des envois du guide par le backend (guide_sends) (V2-23d)
psql -d casaguide -f db/migrations/023_property_cover.sql # photo de couverture du logement (properties.cover_media_id) (V2-30)
psql -d casaguide -f db/migrations/024_booking_keybox_override.sql # surcharge du code de boîte à clés par séjour (bookings.keybox_code_enc, chiffré) (V2-23c volet 2)
psql -d casaguide -f db/migrations/025_auto_send_guide.sql # envoi auto du guide à J-7 : properties.auto_send_guide + guide_sends.origin (V2-23d volet 2)
psql -d casaguide -f db/migrations/026_booking_dates_override.sql # dates ajustées à la main protégées de la synchro : bookings.dates_overridden + feed_starts_on/feed_ends_on (V2-23g)
psql -d casaguide -f db/migrations/027_help_searches.sql # journal des recherches d'aide (santé de l'index) (V2-31 volet 3a)
psql -d casaguide -f db/migrations/028_guide_send_whatsapp_assisted.sql # origine d'envoi 'whatsapp_assisted' (J-7 assisté WhatsApp, V2-32 volet 1)
psql -d casaguide -f db/migrations/029_poi_weekday.sql # jour du marché : pois.weekday + weekday_note (+ backfill des marchés préfixés) (V2-33 volet 1)
psql -d casaguide -f db/migrations/030_poi_completion_meta.sql # provenance de la complétion auto des fiches de service (pois.completion_meta) (V2-07 volet 2)
psql -d casaguide -f db/migrations/031_guide_reminders.sql # registre des relances du planificateur d'envoi (idempotence « une par séjour/motif ») (V2-36 pièce 1)

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
- **Recherche dans le guide voyageur (V2-33 volet 2)** : un champ de recherche
  hors-ligne (`/g`, `/b`, `/v`) trouve sections, POI et infos par mots-clés,
  tolérant aux fautes, dans la langue du guide. Le **cœur de correspondance**
  (`normalize`/`tokenize`/`buildIndex`/`search`, trigrammes de Dice) est **extrait
  et partagé** : `frontend/js/lib/matchcore.js` — PUR et **paramétrable** (l'appelant
  fournit ses `stopwords` et seuils). `frontend/js/help/search.js` (aide ⌘K) en est
  désormais un simple consommateur → **comportement de l'aide strictement inchangé**
  (mêmes stopwords FR, mêmes seuils ; ne pas y toucher sans relancer la couverture).
  `frontend/guide/search.js` (`initSearch`) **construit l'index depuis le DOM rendu**
  au chargement (titres/corps de sections, noms/desc/commentaires de POI, badge du
  jour de marché) — **aucun réseau**. **Secrets JAMAIS indexés** : le wifi et le code
  de boîte à clés déchiffrés sont injectés **après coup** par `initSecrets` dans des
  `.secret-slot` → `initSearch` s'exécute **AVANT** `initSecrets` **et** retire
  `.secret-slot`/`.secret-card` à la moisson (double garde) ; seuls les libellés
  **publics** (titres « Wifi », « Boîte à clés ») sont trouvables et mènent à la
  section. **Jamais d'écran vide** (veto André, hérité du 3a) : sous le seuil franc,
  on propose les meilleures approches. **Normalisation** : `ß→ss` **avant** le filtre
  `[^a-z0-9]` (sinon l'eszett allemand disparaît) — le NFD couvre fr/es/it/nl/sq.
  **Sans JS** : le champ est injecté par JS → **absent** sans JS, page intacte (M-08).
  **PIÈGE PWA (principal)** : `matchcore.js` vit **hors `/guide/`** → il n'est **pas**
  couvert par la branche `/guide/` du SW → il **doit** être dans `SHELL_ASSETS` **et**
  servi par la branche `SHELL_EXTRA` de `sw.js` (sinon la recherche **meurt
  hors-ligne**) ; `/guide/search.js` est aussi précaché ; toute modif → **bumper
  `VERSION`** (ici v31 → v32). Les 3 libellés (`ui.search_*`, fr/en/es dans `_UI`,
  overlay nl/de/it/sq à venir) passent par l'inventaire i18n (régénérer + `--check`)
  et sont rendus en `data-search-*` sur le `<body>`.
- **SW & `/secrets` `no-store` (choix assumé)** : `sw.js` met en cache les réponses
  `/g|/b/…/secrets` (via `cache.put` en `networkFirst`) **malgré** leur en-tête HTTP
  `no-store` — c'est **voulu** : le mot de passe wifi doit rester lisible hors-ligne
  AVANT d'avoir le wifi (scénario porte d'entrée). Le `no-store` HTTP reste pour les
  **caches partagés/proxies** (le SW est un cache privé, sur l'appareil du voyageur).
  Même régime sur le lien de séjour `/b/` (branche miroir de `/g/`, v28).
- Serveurs OSM publics : 1 req/s max, User-Agent obligatoire → en production,
  prévoir OSRM auto-hébergé et/ou un fournisseur géré.
- **Overpass 406 — la cause était l'en-tête `Accept`, PAS l'User-Agent (OPS-4,
  12/08)** : deux runs Ballarin ont vu ~19-21/26 catégories refusées en **406 Not
  Acceptable** par overpass-api.de (mais pas toutes → pas une absence d'UA).
  **Reproduit** : la MÊME requête (celle que `overpass._build_query` construit)
  reçoit par intermittence un 406 dont le corps est une **page Apache générique**
  (`Server: Apache/2.4.68`, `text/html` — PAS un message Overpass). **A/B décisif**
  (15 requêtes chacune) : `Accept: application/json` → **8/15 en 406** ;
  `Accept: */*` ou pas d'`Accept` → **0/15**. Cause : la **négociation de contenu
  Apache** (mod_negotiation) de l'endpoint interpreter n'offre aucune variante
  `application/json` → 406. Le format de sortie est décidé par `[out:json]` **dans**
  la requête, jamais par l'en-tête HTTP → **`overpass._post_overpass` envoie
  `Accept: */*`**. En défense : **backoff sur 406/429/503/504** (`_RETRYABLE_STATUS`,
  `overpass_max_attempts`/`overpass_backoff_s`) avec bascule miroir puis nouvelle
  passe ; un 4xx **non** transitoire (400 requête invalide) lève tout de suite ; le
  **corps complet** du refus est **logué** (`log.warning`) et **tronqué proprement**
  côté `steps` (`_short`, coupe sur un mot + « … », fini le « For more informatio »
  à cru) via `OverpassError` dont le `str()` reste court. L'User-Agent par défaut est
  passé à un contact réel (`Holaguia/1.0 (+https://holaguia.com; …)`) par conformité,
  mais ce n'était **pas** la cause (l'A/B l'a montré). **Repro pour le VPS** (avant/
  après, l'IP peut différer de la machine de dév) :
  `curl -sS -o /dev/null -w "%{http_code}\n" -A "$CASAGUIDE_UA" -H "Accept: application/json" --data-urlencode 'data=[out:json][timeout:15];(nwr["amenity"="pharmacy"](around:2000,37.9262,-0.7233););out center tags;' https://overpass-api.de/api/interpreter`
  (répéter ~10×) → des 406 ; remplacer `-H "Accept: application/json"` par
  `-H "Accept: */*"` → plus de 406.
- **Pipeline OBSERVABLE & sortie propre (OPS-4)** : (Pièce 2) `enrichment_jobs.steps`
  est le **journal de vérité** — chaque étape IA y figure avec `ok`/erreur + compteurs
  + coût : `area_facts`, `food_delivery` (`platforms`), `describe_pois` (`described`),
  `service_complete` (`by_category` + `completed`), `babysitter` (`created`), en plus
  de `geocode`/`overpass`/`distances`/`claude`. (Pièce 3) `pipeline._progress(msg)`
  imprime **avec `flush=True`** (+ `log.info`) à chaque étape (début/fin, compteurs,
  coût) → un run de 5-30 min donne signe de vie ; le **résumé final** du CLI est
  étendu aux complétions/créations et liste les **catégories en échec en clair**.
  (Pièce 4 — sortie propre) le client **Anthropic créé DANS `run()`/`_retry_failed`**
  (chemin CLI, `anthropic_client=None`) est **fermé explicitement** dans un `finally`
  (`ai.close()`) — son pool httpx laissé ouvert **bloquait la sortie du process**
  (~1 h de terminal muet le 12/08). Un client **passé** par l'appelant (API/tests) n'est
  **jamais** fermé par le pipeline (il lui appartient — `owns_ai = anthropic_client is
  None`). Couvert par `test_pipeline.py` (`steps` 4c/4d/food_delivery, en-têtes/backoff/
  troncature Overpass, fermeture du client Anthropic).
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
- **Livraison de repas par zone (V2-07 volet 1)** : la section `E_food_delivery`
  est remplie automatiquement, pour **n'importe quel pays**, par
  `claude_enrich.fetch_food_delivery` — un appel Claude **SÉPARÉ** de `_AREA_PROMPT`
  (il a besoin de l'**outil `web_search`** de l'API Anthropic et d'une cadence de
  rafraîchissement propre). **Deux niveaux** : (1) pays → **marque locale** (Just Eat
  Takeaway = Just Eat ES/FR/IT, Lieferando DE/AT, Thuisbezorgd NL ; Delivery Hero =
  Glovo/Foodora ; Uber Eats ; Wolt ; Deliveroo) — cette connaissance entre dans le
  **prompt comme CONTEXTE, jamais comme réponse** ; (2) **vérification de couverture**
  par recherche web pour la **commune** du logement. **Seules** les plateformes avec
  **preuve** (URL + date) sont retenues ; une **liste vide est un résultat valide**
  (« N'invente jamais », doctrine du prompt existant). Réponse en **JSON strict validé
  avant insertion** (même contrat que l'existant ; un JSON malformé lève et **n'écrit
  rien**). **Stockage mutualisé** : nouveau `area_facts.fact_type = 'food_delivery'`
  (clé pays+commune) → deux logements d'Orihuela partagent le résultat, un logement de
  Berlin obtient Lieferando. **Fraîcheur pilotée par `fetched_at`** via
  `db.area_fact_fresh(..., 'food_delivery', settings.food_delivery_max_age_days)` (90 j
  par défaut) : **aucun nouvel appel** dans la fenêtre. **Restitution** dans
  `E_food_delivery` selon le motif area_facts (`guide_page._fact_food_delivery` dans
  `_FACT_INLINE`, section du seed déclarant `area_facts:["food_delivery"]`) : noms de
  marques **neutres** (jamais traduits) + lien de preuve, prose minimale. **Branchement
  pipeline** : appel best-effort (SAVEPOINT — un échec n'annule pas le reste de
  l'enrichissement ni le job) ; `api_costs` (opération `'food_delivery'`, **unités
  web_search incluses**, ~10 $/1000 requêtes) ; quota d'enrichissement respecté (dans
  le même job). `food_delivery` a **quitté** `CLAUDE_ONLY_CATEGORIES` (liste morte) ;
  **`babysitter` y reste** (volet 2, avec la complétion des coordonnées de service).
  Pas de migration : `area_facts.fact_type` n'a aucun `CHECK`. Mocks de test : refléter
  la **surface réelle** du SDK web_search (blocs `server_tool_use`/`web_search_tool_
  result` + `usage.server_tool_use.web_search_requests`, `stop_reason` `pause_turn`) —
  leçon OPS-1b.
- **Complétion des fiches de service (V2-07 volet 2)** : tel/site (catégories où
  l'action est APPELER : `taxi`/`doctor`/`veterinary`/`pharmacy`/`police`/`rental`/
  `babysitter`) et horaires+site (où ils conditionnent la visite : `supermarket`/`mall`/
  `bakery`/`laundry`/`post_office`/`pharmacy`) sont complétés par Claude + recherche web
  AVEC PREUVE (`claude_enrich.complete_service_pois`, périmètre par catégorie via
  `service_fields`). **Compléter, jamais écraser** : seules les fiches **retenues**
  (`approved`/`edited`) et seulement leurs champs **NULL** sont visées
  (`db.pois_needing_completion` filtre `status IN ('approved','edited')` + au moins un
  champ NULL ; `db.apply_poi_completion` remplit en **COALESCE** — une valeur du
  propriétaire n'est même pas demandée — et garde `WHERE status IN ('approved','edited')`).
  La complétion **ne change ni `status` ni `source`** (ce n'est pas une édition
  propriétaire) : la **provenance** vit dans `pois.completion_meta` JSONB (migration 030),
  par champ (`{source_url, verified_on}`) + un marqueur `_checked_on` (dernière tentative,
  même infructueuse → jamais de re-appel en boucle sur un champ introuvable ; cadence
  `service_complete_max_age_days`). **Preuve ou rien** (même contrat que le volet 1) :
  une entrée sans `source_url` est écartée, JSON malformé → ValueError (aucune écriture),
  vide accepté. Les **horaires** (donnée périssable) reçoivent la mention
  `HOURS_INDICATIVE_SUFFIX` (« · Horaires indicatifs », précédent des notes de marché,
  i18n hors périmètre V2-29) et jamais de source de plus d'un an (consigne du prompt).
  **Coût MAÎTRISÉ** (point central) : **un appel web par catégorie/commune** (les N fiches
  incomplètes en un lot, jamais un appel par POI), plafond `service_complete_max_searches`,
  et le filtre NULL+`_checked_on` réduit le lot. **Baby-sitting** : `fetch_babysitters`
  **crée** des POI (`db.insert_service_poi`, `source='claude'`, `status='suggested'` →
  validation propriétaire ; position = celle du logement, service TÉLÉPHONIQUE ; idempotent
  par `source_ref='claude:babysitter:<slug>'`), vide assumé, cadence propre par logement
  (`db.recent_operation('babysitter', …)` — mémoire via `api_costs`, un vide n'est pas
  re-cherché). `babysitter` a **quitté** `CLAUDE_ONLY_CATEGORIES` (désormais **vide** — les
  deux catégories claude-only sont traitées ailleurs). Étapes best-effort SAVEPOINT dans le
  pipeline (4c/4d), `api_costs` (`operation='service_complete'`/`'babysitter'`, unités web
  incluses). NB : sur un run NEUF tous les POI sont `suggested` → 4c ne fait rien (elle vise
  les retenus) ; la complétion opère au **ré-enrichissement**, après arbitrage. Migration 030
  = `ADD COLUMN IF NOT EXISTS completion_meta JSONB` (idempotente, DEFAULT NULL, aucun
  backfill). Mocks : surface web_search réelle (OPS-1b).
- **Marchés hebdomadaires par zone (V2-07 volet 3)** : DÉCOUVERTE **mutualisée** par
  (pays, commune) via `claude_enrich.fetch_markets` (Claude + web, sources mairie/
  office de tourisme/presse), mise en cache **area_facts** sous `MARKET_FACT_TYPE =
  'markets'` (fraîcheur `market_max_age_days`, 90 j — deux logements d'une commune
  partagent la découverte, **zéro appel dans la fenêtre**) ; **MATÉRIALISATION** en POI
  `market` **par logement** (pipeline 4e, `db.get_area_fact` → `db.insert_market_poi`,
  `source='claude'`, `status='suggested'`, idempotent par `source_ref='claude:market:
  <slug>:<weekday>'`, distances calculées). **`weekday` OBLIGATOIRE** (entier 1-7 ;
  un marché sans jour valide n'est **pas** créé — `fetch_markets` l'écarte, jamais
  `bool`/chaîne) ; `weekday_note` = horaires (suffixe **« · Horaires indicatifs »**,
  précédent V2-33/volet 2) + caractère + « (activité à confirmer) » si `doubtful` ;
  preuve `source_url`+`verified_on` en `completion_meta._market`. **Trois écueils du
  prototype, chacun son mécanisme** : (1) **Doublons** — `market_matches_existing`
  (PUR) contre les POI market existants **tous statuts** (un `edited` jamais recréé,
  un `rejected` jamais ressuscité) : **même jour** + (nom trigramme ≥ 0,6 OU même
  place ≤ 250 m) → doublon, **ou** nom quasi-identique (≥ 0,82) seul ; deux marchés
  même place mais **jours différents** restent distincts. Pré-dédup par **nom** AVANT
  géocodage (idempotence par `source_ref` d'abord → pas de re-géocodage au ré-run).
  (2) **Catégorisation** — le prompt **exclut** explicitement commerces fixes,
  supérettes, marchés couverts quotidiens (seulement mercadillos/rastros hebdo).
  (3) **Données mortes** — preuve **récente** exigée ; activité incertaine → `doubtful`
  (note prudente), jamais affirmé. **Position** (`pipeline._resolve_market_position`) :
  coordonnées de la source si **plausibles** (≤ `MARKET_MAX_DIST_M` 25 km du logement,
  garde anti-hallucination), sinon **géocodage** de l'adresse — accepté **seulement si
  la précision n'est PAS « city »** (`geocode` reconnaît square/marketplace/pedestrian
  → « street ») ; **jamais un marqueur ville** ; sans position fiable → **sauté +
  journalisé** (`steps.markets.skipped_position`). Best-effort SAVEPOINT, `api_costs`
  `operation='markets'`, journal `steps.markets` (discovered/created/skipped_*) +
  progression (OPS-4). **Aucune migration** (réutilise `weekday`/`weekday_note` de la
  029, `completion_meta` de la 030). Contenu FR (traductions par le circuit existant,
  relancées par André après validation). Mocks : surface web_search réelle (OPS-1b).
- **Robustesse de `_ask_web_search_json` (V2-07 volet 3bis)** : constat prod 12/08 —
  `fetch_markets` a échoué sur un JSON long **tronqué** (`Expecting ',' delimiter`),
  zéro écriture (garde-fou OK) mais **un seul essai** et **coût non enregistré** (il ne
  l'était qu'au succès). Correctifs, au **motif commun** (tous les volets en héritent) :
  (1) **max_tokens** — l'appel marchés relève son plafond de SORTIE (`market_max_tokens`
  8000 ; défaut 2000 tronquait une commune riche) ; le `stop_reason` du dernier essai est
  **journalisé dans l'erreur** (`WebSearchJSONError`, `stop_reason=max_tokens` = smoking
  gun). (2) **Retry borné** — sur JSON invalide, la réponse est **RÉGÉNÉRÉE une fois**
  (jamais un re-parse ; `web_search_max_attempts`=2) : `_one_web_call` = un appel logique
  (boucle `pause_turn` incluse), `_ask_web_search_json` boucle dessus. (3) **Comptabilité
  à la réponse, pas au succès** — chaque essai porte son coût (`meta["attempts"]`, liste) ;
  `db.record_costs` écrit **une ligne `api_costs` PAR essai**. Sur ÉCHEC de parsing,
  `WebSearchJSONError` (sous-classe `ValueError` → gardes best-effort inchangées) **porte
  les coûts** ; `pipeline._record_web_failure_cost` les comptabilise dans le `except`,
  **APRÈS le rollback du SAVEPOINT** (donc sur la transaction principale, `conn.commit()`)
  → **coût enregistré, zéro donnée écrite**. Corollaire assumé : un `food_delivery`/
  `markets` malformé enregistre désormais **2 lignes** `api_costs` (essai + retry), area_fact
  toujours non écrit. NB : les échecs de validation **structurelle** APRÈS parsing réussi
  (`'platforms' doit être une liste`) restent un `ValueError` nu sans coût porté — hors
  périmètre (le parsing avait réussi). Couvert par `test_markets.py` (troncature→erreur
  mentionne max_tokens, retry 1er malformé+2e valide→2 coûts, succès=1 essai) et
  `test_pipeline.py` (double malformé marchés→0 écriture+2 coûts ; food_delivery malformé
  →2 coûts, area_fact non écrit).
- **Parité de robustesse `_ask_json` + descriptions best-effort (V2-37 volet 1bis)** :
  constat prod Ardon (16/08) — `describe_pois` → `_ask_json` → `JSONDecodeError: Expecting
  value: char 0` → **job ENTIER en échec**. Deux trous comblés. (1) **Parité** : le motif
  du 3bis est **factorisé** dans `_json_retry(one_call, *, label, max_tokens)` — un SEUL
  motif pour les deux chemins ; `_ask_json` (via `_one_plain_call`) en hérite **exactement**
  (isolation d'un JSON encadré de prose/fences par `_parse_strict_json`, **un retry**
  régénéré sur JSON invalide, coût `api_costs` **par essai** même en échec, `stop_reason`
  du dernier essai **gravé dans l'erreur** → une réponse vide se diagnostique enfin :
  max_tokens ? refus ? bloc non-texte ?). L'exception est renommée **`ClaudeJSONError`**
  (neutre ; `WebSearchJSONError` reste un **alias** rétrocompat). (2) **Descriptions
  best-effort** : l'étape 4b passe sous **SAVEPOINT** comme 4c/4d — son échec est journalisé
  (`steps.describe_pois` `ok:false` + `described:0` + `error`), le coût des essais
  comptabilisé (`_record_failed_call_cost`, renommé — générique), et le job **continue**
  (complétions, baby-sitting, marchés, save) et finit **`done`**. *Une description manquante
  est un manque ; un job tué en est cent.* NB : `area_facts` (aussi `_ask_json`) **gagne la
  robustesse du retry** mais reste dans le flux principal (non best-effort — foundational).
  (3) **max_tokens descriptions** : un lot éditorial peut compter ~40 POI (5 catégories × 8)
  × 25 mots ⇒ ~2 400 tokens de sortie > défaut 2000 → `describe_max_tokens=6000`
  (`describe_pois` le passe). Couvert par `test_quality_prompts.py` (`_ask_json` : prose→parsé,
  retry→2 coûts, double invalide→`ClaudeJSONError`+stop_reason+2 coûts, alias) et
  `test_pipeline.py` (échec descriptions → job `done`, autres étapes exécutées, `steps` porte
  l'échec motivé, restaurant sans description = manque pas corruption).
- **Passe qualité des prompts — interdiction du REMPLISSAGE (V2-35)** : « n'invente
  jamais » **étendu au style** — le remplissage est une invention polie (constat 14/08,
  « La Marquesa » : « Site à visiter à Orihuela, accessible aux vacanciers intéressés
  par la culture locale » = zéro information). **(a) Descriptions** : `_POI_PROMPT`
  impose de renvoyer **`""`** si aucune connaissance PROPRE au lieu, exige **≥1 fait
  spécifique par phrase**, bannit des tournures explicites (« à visiter à X »,
  « accessible aux vacanciers », « pour un repas sur place », « durant votre séjour »,
  « à découvrir », et la famille) et interdit la paraphrase du nom/catégorie. À la
  RÉCEPTION, `describe_pois` **accepte une description vide** (absente du résultat →
  l'upsert n'écrit rien, **JAMAIS de repli générique** ; NULL pour un POI neuf, COALESCE
  conserve l'existant) **et** rejette une réponse de remplissage (`is_filler_description`
  / `FILLER_MARKERS`, garde-fou si le modèle désobéit). Les descriptions **déjà en base
  ne sont pas touchées** (COALESCE) ; le script **lecture seule** `ops/list_filler_
  descriptions.py` les **recense par logement** (mêmes `FILLER_MARKERS`) — **André décide**
  quoi purger (aucune écriture). **(b) Tags de cuisine** : `overpass._norm_cuisine`
  valide la **forme** (1 à 3 mots, jamais une phrase) — au-delà de 3 mots → tag **IGNORÉ**
  (None), **jamais tronqué** (couper « Modern, … » à « modern » inventerait un tag). NB :
  la cuisine est **OSM-sourcée** (aucun prompt ne la produit), d'où la validation à la
  réception. **(c) Site web « en savoir plus »** : `SERVICE_SITE_CATEGORIES = (sight,
  family_activity, sport)` ajoutées à `service_fields`/`SERVICE_COMPLETE_CATEGORIES` en
  **site SEUL** (ni téléphone ni horaires) — même mécanique volet 2 (preuve URL+date,
  batché par catégorie/commune, champs NULL, `_checked_on`) ; ~3 appels web de plus par
  run. Couvert par `test_quality_prompts.py` (interdictions au prompt, vide→écrit vide
  jamais générique, remplissage→rejeté/factuel gardé, cuisine phrase→ignorée, 3 catégories
  site-seul) + `test_pipeline.py` (script ops lecture seule). **Le vrai test des
  descriptions est « sortie de boîte » sur un logement NEUF** (les fiches existantes
  gardant leur texte) → juge de paix = premier enrichissement de Villa Ardon (+ première
  mesure du taux de retouche). Hors périmètre : purge auto (l'humain décide), i18n (V2-29).
- **Qualité sortie de boîte — marqueurs v2, commune jamais inventée, restaurants (V2-37
  volet 1)** : revue réelle des restaurants d'Ardon (16/08, taux de retouche ~7 %). **(a)
  FILLER_MARKERS v2** : quatre variantes avaient échappé (« où les vacanciers peuvent
  prendre leurs repas », « pouvant convenir aux vacanciers cherchant un repas », « une
  option de restauration », « pour les visiteurs »). Plutôt qu'empiler des littéraux,
  `is_filler_description` teste aussi `_FILLER_PATTERNS` (regex) qui captent le MOTIF —
  un **public générique** (`vacanciers|visiteurs|touristes|voyageurs`) + un **but/verbe
  creux** (peuvent prendre, pouvant convenir, « pour les X », « option de restauration »).
  **Ancré sur le mot de public** (ou « option de … ») pour NE PAS sur-bloquer le factuel
  (« … apprécié depuis Chaplin », « saut à l'élastique de 190 m » ne matchent jamais) ; le
  script ops **hérite** des marqueurs. **(b) La commune jamais inventée** : `describe_pois`
  affirmait « restaurant à Ardon » pour un lieu de Vétroz (le prompt supposait la commune
  du logement). Correctifs : la **localité RÉELLE** du POI (`addr:city` OSM, portée par
  `overpass._element_to_poi` en champ `locality`, en mémoire, non stockée) est **passée au
  prompt par POI** ; le prompt **interdit d'affirmer toute commune non fournie** (sans
  localité → aucune mention de lieu, jamais « à {city} » par défaut). Bonus recensement :
  `ops/list_filler_descriptions.py` gagne une section **« communes suspectes »**
  (`find_suspect_communes` : la ville du logement apparaît dans la description mais pas dans
  le nom, et l'adresse OSM porte une AUTRE ville — le motif-filler ne voit pas ce défaut ;
  lecture seule, l'humain tranche). **(c) Complétion des restaurants** : `restaurant`, `bar`,
  `cafe` réintégrés dans la complétion en **téléphone + site SEULS** (`SERVICE_TELSITE_
  CATEGORIES`) — les **horaires restent EXCLUS** (trop volatils) et ne sont **jamais écrits**
  même si la réponse web en propose (garantie par le filtre `perim = service_fields(cat)` de
  `complete_service_pois`). Même mécanique volet 2 (preuve, batch, NULL, `_checked_on`) ;
  surcoût d'un re-run ≈ 3 catégories bien peuplées (~30-45 ct/logement). Couvert par
  `test_quality_prompts.py` (4 échappées matchent, factuel épargné Chaplin+Bungy, règle
  localité + localité par POI au prompt, restauration tél+site) + `test_service_complete.py`
  (restaurant : horaires jamais écrits même si proposés) + `test_pipeline.py` (ops :
  communes suspectes, lecture seule). Hors périmètre : sélecteur de catégorie (V2-37 volet 2).
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
- Jour du marché (V2-33, migration 029) : le jour d'un marché vit dans
  `pois.weekday` (SMALLINT `1..7`, 1=lundi ISO) + `pois.weekday_note` (précision
  libre), **jamais dans le nom** (l'ancien compromis « Samedi · … », illisible en
  anglais, est défait par le backfill de la 029 — ciblé `category_code='market'`,
  idempotent). Le champ est **à nous, pas à OSM** : proposé dans la modale
  Ajouter/Modifier **seulement** pour la catégorie `market` (`PoiCreateIn`/
  `PoiEditIn.weekday` bornés `1..7` → 422 sinon). **Aucune clé i18n pour le nom du
  jour** : le badge est rendu par **CLDR** des deux côtés — Babel côté serveur
  (`guide_page._weekday_label`, `Babel` en dépendance) et `Intl.DateTimeFormat`
  côté client (`guide/app.js`, popups carte), sur une **date pivot** (2024-01-01 =
  lundi) → même mot, 7 langues gratuites, **casing naturel** de la langue (fr
  « samedi », en « Saturday » — ne pas forcer la capitale, elle malmènerait
  l'albanais). La catégorie `market` se trie **par jour croissant** (jour absent en
  dernier) **puis** distance dans `_render_pois` — les autres catégories restent
  triées par distance (coup de cœur en tête). `map_data` porte `weekday`/
  `weekday_note` pour l'alignement client. Toute modif de `guide/*` → **bumper
  `sw.js VERSION`** (ici v30 → v31).
- Requalifier une catégorie depuis « Modifier » (V2-37 volet 2) : la modale
  Modifier porte un **sélecteur de catégorie** (`frontend/js/views/pois.js`
  `buildCategorySelect`, MÊME liste que la modale Ajouter — chargée via
  `api.poiCategories`, **jamais en dur**, invariant 8), pré-rempli sur
  `p.category_code` ; il **pilote** le champ « Jour du marché » (visible ⇔ catégorie
  choisie = `market`, comme dans Ajouter). Côté back, `PoiEditIn.category_code` est
  **validé contre `poi_categories`** (422 `Catégorie inconnue` sinon) et ajouté à
  `_POI_EDITABLE` → l'édition classe le lieu « Modifié » (un champ de plus). **Effets
  À CONSTATER, jamais recodés** : le **mode de trajet** suit la catégorie (join
  `poi_categories`, V2-24) ; la tuile/section du guide suit **au prochain rendu** ;
  une fiche requalifiée vers une catégorie de complétion (ex. sight → restaurant/bar)
  **entre dans le périmètre tél/site au prochain run** (V2-37 vol 1). **Invariant
  critique** : une catégorie **éditée** n'est **jamais** révertie par un
  ré-enrichissement — les POI `edited`/`approved` sont hors upsert
  (`WHERE pois.status='suggested'`), la clé de conflit `(property_id, source,
  source_ref)` n'inclut pas la catégorie → node/333 revient en `restaurant` sous OSM
  mais le POI reste `bar`/`edited`. En **quittant `market`**, `weekday`/`weekday_note`
  restent **STOCKÉS mais inertes** (le front ne les envoie pas, `edit_poi` ignore les
  champs absents ; seul le rendu `market` les affiche) — choix assumé, aucune
  effacement. Back-office seul (aucun bump SW, aucune migration : colonne existante).
  Couvert par `test_api.py::test_edit_poi_recategorises_and_rejects_unknown_category`
  (422 + persistance + statut Modifié + métadonnée qui suit),
  `test_pipeline.py::test_edited_category_survives_reenrichment_and_enters_completion`
  (non-réversion + périmètre de complétion) et le harnais `pois-harness.html`
  (sélecteur présent+pré-rempli, toggle du jour, PATCH porte `category_code`).
- Horaires au rendu (V2-34) : la donnée `pois.opening_hours` **STOCKÉE ne bouge
  pas** ; **au rendu SSR** (`guide_page._render_opening_hours`, appelé par
  `_render_pois`), tout horaire s'affiche en **texte humain localisé** + une mention
  **« Horaires indicatifs » LOCALISÉE et SYSTÉMATIQUE** (toute source — OSM comme
  complété IA). Le **normaliseur `_normalize_hours` est PUR** : il parse le
  sous-ensemble courant de la syntaxe OSM (`Mo-Sa 09:00-21:30`, listes/plages de
  jours, plages multiples, `off`/`closed`, `24/7`) et rend les **jours par CLDR**
  (`_weekday_label`, Babel — même source que les badges de marché V2-33, jamais de
  liste de jours en dur) et l'**heure au format du locale** (`_fmt_time`, Babel
  `format_time` `short` — l'anglais gagne son AM/PM). **RÈGLE D'OR : jamais dégrader**
  — toute valeur non parsée (PH, règles complexes, **prose héritée du volet 2**,
  saisie libre) s'affiche **telle quelle** (un horaire brut vaut mieux qu'un horaire
  déformé) ; sans Babel, repli brut aussi. La mention stockée par le volet 2
  (`· Horaires indicatifs`) est **retirée** avant d'apposer la mention localisée
  (`_STORED_HOURS_MENTION`, **jamais deux**) ; une valeur vide (ou une mention seule)
  → **ni horaire ni mention**. La mention est un **libellé d'interface, PAS du CLDR** :
  deux clés `ui.hours_indicative`/`ui.hours_closed` (fr/en/es dans `_UI`, overlay
  nl/de/it/sq au régime existant, repli FR) **versées à l'inventaire**
  (`i18n/inventory.json` régénéré, `--check` vert) — **dette i18n consignée à V2-29**
  comme les `ui.search_*`. **SSR SEUL** : les popups de carte ne rendent PAS
  `opening_hours` (absent de `map_data`) → **aucun bump SW**. HORS PÉRIMÈTRE (dettes
  ouvertes) : faire produire de la syntaxe OSM par la complétion du volet 2 (→ V2-35,
  passe prompts) et traduire les valeurs en prose héritées (→ V2-29). Couvert par
  `test_guide_page.py` (normaliseur pur fr/en/de, 24/7, multi-plages, `Su off`, règle
  complexe→brut, prose→brut+mention dédupliquée, vide→rien ; DOM rendu carte fr/en).
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

- « Envoyer par Holaguia » — l'email HTML du guide part du backend (V2-23d, volet 1) :
  - **L'envoi est SYNCHRONE et le token est assuré côté serveur.** `POST /api/
    properties/{id}/send-guide` (`routers/send.py`, gardé par `OwnedProperty` +
    `CurrentOwner`, patron `share.py`) assure le token (`ensure_stay_token`/
    `ensure_showcase_token`) puis envoie l'email HTML localisé (`emails.guide_stay_
    email`/`guide_showcase_email`) **avant** de répondre (le propriétaire attend la
    confirmation). Le client **n'envoie JAMAIS d'URL** — le `guide_token` éternel ne
    figure ni dans le lien (`/b/`·`/v/`) ni dans le corps (couvert par test). Une
    **panne SMTP → 502 propre** (jamais un 500 nu) et **aucune** ligne `guide_sends`
    (la trace n'est écrite qu'APRÈS un envoi réussi). Comme les routes qui écrivent
    puis programment (V2-16b) : `conn.commit()` **explicite** après la trace.
  - **Les emails d'envoi sont LOCALISÉS** (contrairement aux emails owner restés FR) :
    copies FR/EN/ES dans `emails._EMAIL`, langues supplémentaires (nl/de/it/sq) via
    l'overlay `ui_translations` (nouveau domaine d'inventaire **`email.*`**,
    `i18n.email_key`, repli FR sans trou — même mécanique que `guide_page._t`). Toute
    clé `email.*` ajoutée → régénérer `i18n/inventory.json` (`ops/i18n_inventory.py`,
    `--check` = gate). La langue d'envoi est **validée contre le registre** (invariant
    15) → 422 si non offerte par le guide (jamais une promesse intenable).
  - **`/send-templates` est kind-aware** (`?kind=stay|showcase|house`) : séjour &
    vitrine partagent les copies de l'email HTML (`email.*`, texte brut adapté pour
    mailto/wa.me) ; la **maison garde** le gabarit utilitaire générique `ui.send_*`
    (lien du QR, pas d'email backend). Côté front (`sendmenu.js`), « Envoyer par
    Holaguia » est le **premier** canal du séjour et de la vitrine (états : envoi →
    « Envoyé ✓ » → erreur), la vitrine exige un **email saisi** (bouton désactivé
    sinon), le reste passe en « ou via votre messagerie ». `guide_sends` mémorise le
    dernier envoi (`GET …/last-send`, affiché dans la fenêtre) — c'est le socle du
    **J-7 automatique (volet 2)**.
- Envoi automatique du guide à J-7 (V2-23d, volet 2, migration 025) :
  - **Le registre `guide_sends` EST le verrou d'idempotence.** La sélection
    (`api.care.select_auto_sends`, PURE) supprime tout séjour ayant déjà une ligne
    `guide_sends` kind='stay' — **manuelle OU auto** (`already_sent`). Corollaire
    **assumé** : un envoi manuel à J-60 supprime l'automatique (le guide est déjà
    chez le locataire). Ne jamais ajouter un second verrou (flag sur `bookings`, job
    qui basculerait des lignes…) : le registre suffit, un re-run le même jour
    n'envoie rien deux fois. La trace `origin='auto'` est écrite **APRÈS** l'envoi
    réussi + `conn.commit()` explicite ; un échec SMTP ne trace RIEN (ré-essai
    demain, séjour encore dans la fenêtre) et **n'arrête pas la boucle** (V2-16).
  - **La fenêtre est un `≤`, pas un `== J-7`** (rattrapage) : `today ≤ starts_on ≤
    today+7`. Un timer en panne deux jours ne saute personne. Le `≥ today` borne
    l'arrivée (on n'envoie pas pour un séjour déjà commencé).
  - **Voie unique manuel ↔ auto** : la construction de l'email de séjour vit dans
    `api/guidesend.py` (`build_stay_email` + helpers langue/vignette/lien/overlay),
    partagée par `routers/send.py` (manuel) et `run_auto_send` (auto). Ne jamais
    dupliquer la construction — toute évolution (langue, image, lien) se fait là.
  - **Cible : natures `reservation`/`private` seules**, `status='active'`, non
    rattaché (`_is_occupied`), logement `auto_send_guide` vrai **et** publié.
    L'interrupteur `properties.auto_send_guide` est **par logement, défaut TRUE**
    (le DEFAULT couvre l'existant → **aucun rattrapage post-migration**). Un envoi
    MANUEL reste permis sur toute nature (le propriétaire sait ce qu'il fait) sauf
    un séjour **annulé** → **422** (lien mort, garde ajoutée au volet 2).
  - **Timer : fuseau EXPLICITE** (`OnCalendar=… 09:00:00 Europe/Madrid`). Sans
    suffixe, `OnCalendar` est en heure **locale du serveur** (VPS souvent UTC) → le
    guide partirait à la mauvaise heure. Ne jamais retirer le suffixe de fuseau.
  - **Relance §0.6** : `care.missing_info(..., auto_send_guide=…)` ajoute le motif
    `auto_send_email_missing` UNIQUEMENT si le logement a l'option active ET que le
    séjour éligible entre dans la fenêtre J-7 SANS email — pastille sobre, jamais un
    email de relance séparé.
- Filets de l'envoi automatique J-7 (V2-36, migration 031) : premier envoi auto réel
  (16/08, Tracy Taylor) : succès technique mais `guest_lang` **vide** → email parti en
  **français** à une Britannique. Trois filets/règles.
  - **Relance « langue non précisée » (pièce 1)** : `care.select_lang_reminders` (PURE,
    même famille que `to_remind`) relance tout séjour éligible, non envoyé, à `guest_lang`
    vide, sur une fenêtre **élargie d'UN jour** (`today ≤ starts_on ≤ today+8`) — le `+1`
    prévient **la veille** de l'entrée dans la fenêtre d'envoi (arrivée = `today+8`,
    relancée mais **jamais envoyée** : `select_auto_sends` re-borne à `today+7`) ; le
    reste couvre le **rattrapage** (déjà dans la fenêtre). `run_auto_send` calcule les
    relances **AVANT la passe d'envoi** en élargissant le fetch (`horizon+1`). **Jamais
    bloquante** : le repli langue-du-logement demeure (un guide en français vaut mieux
    que pas de guide). **Idempotence** par le **registre `guide_reminders`** (migration
    031, `UNIQUE (booking_id, code)`) : `repo.record_reminder` = INSERT … ON CONFLICT DO
    NOTHING RETURNING → une relance **par séjour et par motif**, **pas une par jour** (le
    run reste silencieux si déjà émise ; `conn.commit()` après chaque relance neuve).
    **Pastille calendrier** `auto_send_lang_missing` dans `care.missing_info` (même
    grammaire que `auto_send_email_missing`, fenêtre `+1` aussi) — c'est un **état**
    calculé au rendu (n'a **pas** besoin du registre : elle disparaît quand la langue est
    complétée ou le guide parti). Le front la rend **génériquement** (`m.message`, aucun
    code neuf côté JS). Compteur `AutoSendReport.lang_reminders` (relances **neuves**) au
    bilan du run.
  - **Cci propriétaire (pièce 2)** : l'envoi **automatique** met le propriétaire en Cci
    (email du compte, `owner_email` joint dans `list_auto_send_candidates`) — preuve de
    service rendu. Le `Mailer.send` gagne un kwarg **`bcc`** optionnel ; `_build_message`
    pose l'en-tête `Bcc` que `smtplib.send_message` inclut dans l'enveloppe SMTP puis
    **retire du corps transmis** (le locataire ne voit jamais la copie). **Périmètre :
    auto seul** (l'envoi manuel, c'est le propriétaire qui clique). Les fakes de test
    d'un mailer nourri par `run_auto_send` doivent accepter `send(self, to, email, *,
    bcc=None)` (sinon `TypeError` — seuls `run_auto_send` passe `bcc`).
  - **§3.5 ACTÉ (pièce 3)** — précédence de langue de l'email + lien `/b/`, **point de
    décision unique `guidesend.stay_natural_lang`** : (1) `guest_lang` renseignée **et
    offerte** gagne **partout** (email + `/b/` figé par `data-guest-lang`) ; (2)
    `guest_lang` **vide** → **langue du logement** (`default_lang`) pour l'email
    (comportement du 16/08, désormais **documenté et compensé** par la pièce 1), et `/b/`
    nu à la devinette M-09 ; (3) renseignée mais **non offerte** → langue du logement
    aussi (invariant 15), **sans** relance (l'écart n'est pas un oubli). Orthogonal : un
    **clic explicite** du visiteur gagne et est retenu (`?lang=`, clé `casaguide:lang:b`,
    §3.5 amendement 2). Couvert : `test_care.py` (`select_lang_reminders` bornes/natures/
    opt-out + pastille `missing_info`), `test_api.py` (Cci dans l'enveloppe, relance J+8
    tracée+pastille+non envoyée+idempotente, sélection de langue les **deux** branches),
    `test_mailer.py` (en-tête Bcc + ConsoleMailer). Back-office/ops seul → **aucun bump
    SW**.
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
