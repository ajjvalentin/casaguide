# Mission V2-23c — Le lien de séjour, le lien vitrine et la fenêtre « Envoyer le guide »

**Statut** : brief rédigé le 02/08/2026. Conception actée avec André (01–02/08),
détail au tracker (entrée V2-23c). Trois volets, **un commit par volet**, suite de
tests verte avant chaque commit. Une session Claude Code devrait suffire ; couper
après le volet 2 si elle s'essouffle.

> **AMENDEMENT 02/08 (après relecture du volet 1 livré, non commité)** : la forme
> `/g/{guide_token}?s={stay_token}` du lien de séjour était une **faute de
> sécurité** — le token éternel de la maison voyageait dans l'URL envoyée au
> locataire ; passé J+7, retirer `?s=` suffisait à retrouver les vrais codes.
> Décision actée par André : **préfixe dédié `/b/{stay_token}`** (b = booking),
> résolution du logement côté serveur, le `guide_token` ne quitte plus la maison.
> Le domaine livré est BON et presque entièrement agnostique à la forme d'URL :
> la reprise est un **refactor ciblé de la couche de routage seule** (§ « Reprise
> 02/08 » en fin de document), pas une relivraison.

**Rituels non négociables** (leçons de la semaine, cf. CLAUDE.md) :
- migration testée contre l'**état antérieur réel** (jamais contre son propre
  résultat ni une base neuve) ;
- le rapport de livraison **cite le hash du commit** — un rapport sans hash n'est
  pas une livraison ;
- **`git status` propre vérifié** en fin de mission.

---

## Le problème en une phrase

Le guide n'a qu'un lien de **logement** — un token éternel, identique pour tous,
qui montre les vrais secrets à quiconque le possède et rattache les demandes de
service *au séjour en cours à l'instant du clic* (devinette). Depuis que le guide
est interactif (V2-23b volet 3), c'est intenable : envoyer le guide à l'avance au
voyageur du 12.09 accroche ses demandes au séjour de quelqu'un d'autre, le code de
boîte à clés ne peut pas changer d'un séjour à l'autre, et rien ne peut être
montré à un prospect sans lui donner les codes de la maison.

## La grammaire à TROIS liens (actée)

| Lien | Pour qui | Secrets (boîte à clés, wifi) | Demandes de service | Expiration |
|---|---|---|---|---|
| **Vitrine** | prospect, annonce, démo | **masqués — valeurs d'EXEMPLE marquées comme telles** | désactivées | **aucune** (acté) |
| **Séjour** | locataire réservé | réels, **surchargeables par séjour** | rattachement **certain** au séjour du token | **J+7 après le départ** (acté) |
| **Maison** (QR imprimé) | occupant sur place | réels | règle actuelle (en cours → suivant) | aucune |

Principes actés par André :
1. **Le lien de logement SURVIT tel quel** — c'est le QR imprimé dans la maison.
   Le lien de séjour est un raffinement, pas un remplacement. Le QR maison reste
   sans personnalisation ni surcharge (le voyageur sur place a déjà les clés).
2. **Expiration du lien de séjour : 7 jours après le départ** — un ancien
   locataire ne garde pas l'accès au code de la boîte. Progrès de sécurité par
   rapport au lien éternel.
3. **Le lien vitrine n'expire pas** et ne montre JAMAIS un secret réel — c'est à
   la fois un outil de vente (annonce Airbnb, prospect qui hésite ; rejoint
   V2-19) et un progrès de sécurité (aujourd'hui, montrer le guide = donner les
   codes).
4. **Envoi manuel d'abord** — l'envoi automatique J-7 dans la langue du locataire
   (email, SMTP existant) est V2-23d ; il avait besoin de cette fondation.

---

## Volet 1 — Le modèle et les trois rendus

### 1.1 Migration 021

```sql
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS stay_token TEXT UNIQUE;          -- ≥ 128 bits, NULL tant que non généré
ALTER TABLE properties
  ADD COLUMN IF NOT EXISTS showcase_token TEXT UNIQUE;      -- lien vitrine, NULL tant que non généré
```

- Tokens générés **à la demande** (premier usage dans la fenêtre d'envoi), même
  fabrique que `guide_token`/`staff_token` (secrets.token_urlsafe, ≥ 128 bits).
  Aucun backfill nécessaire (NULL = jamais partagé) — mais le vérifier contre
  l'état antérieur réel comme d'habitude.
- Le token de séjour vit sur le séjour : il meurt avec lui (annulation → 404
  comme un token inconnu ; on ne révèle rien).

### 1.2 Résolution d'URL (amendée 02/08 — préfixe dédié `/b/`)

- `/g/{guide_token}` : inchangé (lien maison, QR imprimé à vie — INTOUCHABLE).
- `/b/{stay_token}` : lien de **séjour**. Le logement est résolu **côté serveur**
  depuis le séjour du token ; le `guide_token` n'apparaît nulle part dans l'URL
  envoyée au locataire. Plus de paramètre `?s=`, et le contrôle « même logement »
  disparaît (sans objet avec un seul token).
- `/v/{showcase_token}` : lien vitrine (préfixe distinct — un token vitrine ne
  doit jamais pouvoir être confondu avec un guide réel, ni côté code ni dans les
  journaux).
- `_real_token()` (slugs décoratifs `/g/{slug}-{token}`, M-25) ne s'applique
  **pas** à `/b/` : un lien de séjour est envoyé, jamais retapé — décision
  explicite, couverte par un test.
- Tous les cas morts des **nouveaux** préfixes (`/b/` inconnu / séjour annulé /
  expiré, `/v/` inconnu) servent la **même page neutre**. Ne pas rétrofitter
  `/g/`, en prod avec `render_not_found`.
- **Table des préfixes réservés** (à graver dans CLAUDE.md) :
  `/g/` maison · `/s/` équipe · `/v/` vitrine · `/b/` séjour.

### 1.3 Rendu SSR — lien de séjour

- **Accueil personnalisé** : « Bonjour {prénom}, votre séjour du {arrivée} au
  {départ} » (le nom vient du séjour ; s'il manque, accueil générique).
- **Langue par défaut = `guest_lang` du séjour** (le `?lang=` explicite garde la
  priorité ; repli = comportement actuel). Une `guest_lang` non publiée au
  registre → repli, jamais une langue non publiée (invariant 15).
- **Demandes de service** : le POST des demandes reçoit le `stay_token` →
  rattachement **certain** au séjour du token, **sans dépendre du
  `guide_token`** — plus aucune devinette. Sans `stay_token` (lien maison), la
  règle actuelle (en cours → suivant) reste le repli documenté.
- **La page et l'endpoint des demandes meurent ensemble** (déjà implémenté au
  volet 1, on le grave ici) : un `stay_token` mort — inconnu, séjour annulé,
  J+8 — donne la page neutre côté GET **et** 404 côté POST. Jamais de
  rattachement deviné sur un token mort, on ne révèle rien.
- **Expiration** : `today > ends_on + 7 jours` → page neutre « Ce lien a expiré —
  demandez le lien à jour à votre hôte » (aucune donnée du logement, pas même le
  nom). Le seuil (7) vit dans une constante nommée, pas un littéral éparpillé.
- Avant/pendant le séjour : pleinement valable (J-7 comme J-60).

### 1.4 Rendu SSR — lien vitrine

- Même gabarit que le guide (c'est le but : montrer le vrai produit), avec :
  - **secrets remplacés par des valeurs d'exemple explicitement marquées**
    (« 1234 — exemple », « MotDePasseWifi — exemple ») — le rendu ne DOIT jamais
    toucher `property_secrets` : le remplacement se fait en amont, les vraies
    valeurs ne transitent pas ;
  - **bouton « Demander ce service » absent** (pas de séjour à rattacher) ;
  - un bandeau discret « Aperçu du guide » (honnêteté vis-à-vis du prospect, et
    le propriétaire voit d'un coup d'œil quel lien il a envoyé).
- Pas de compteur de vues spécifique pour l'instant (guide_views existant
  suffit ; V2-02 affinera).

### 1.5 Tests clés du volet (refaits 02/08)

- `/b/{token}` inconnu, séjour annulé, J+8 après départ → **la même page
  neutre** ; J+6 → guide servi.
- **Préfixes croisés** : un `guide_token` sous `/b/`, un `stay_token` sous
  `/v/`, un `showcase_token` sous `/b/` → même page neutre, jamais un guide.
- **Vitrine — le test utile vise l'ENDPOINT, pas le HTML** : il n'existe aucune
  route secrets sous `/v/` (`GET /v/{token}/secrets` → 404 de l'API), et un
  chemin `/v/…` non capté ne tombe pas en silence dans l'attrape-tout de la SPA.
  (Rappel d'architecture : les vrais secrets ne transitent jamais par le SSR —
  le HTML du guide contient des slots vides remplis côté client via
  `GET /g/{token}/secrets` ; la vitrine ne pose aucun slot et n'a aucune route
  secrets → fuite impossible **par construction**. Un test du HTML passerait au
  vert pour rien.)
- `/b/` est capté par l'API, **pas** par l'attrape-tout de la SPA.
- Pas de slug décoratif sur `/b/` : `/b/{slug}-{token}` → page neutre (test
  explicite de la non-application de `_real_token()`).
- Demande de service portant un `stay_token` mort → 404, jamais de rattachement.
- `guest_lang='de'` → guide servi en allemand sans `?lang=`.

---

## Volet 2 — Surcharge du code de boîte à clés par séjour

- `bookings.keybox_code_enc` (migration du volet, chiffrée **AES comme
  `property_secrets`** — jamais en clair, invariant existant).
- Saisie : champ facultatif dans la modale séjour (« Code de boîte à clés pour ce
  séjour — laisser vide pour utiliser celui du logement »).
- Rendu : le lien de **séjour** affiche la surcharge si elle existe, sinon le
  code du logement. Le lien **maison** ignore les surcharges (acté). Le lien
  **vitrine** n'affiche jamais rien de réel.
- RGPD/sécurité : même régime que les secrets du logement ; la surcharge n'est
  jamais renvoyée par les API de liste (uniquement au rendu du guide du séjour
  concerné et dans la modale d'édition du propriétaire).
- Patron : c'est la **surcharge welcome pack** (V2-23b) appliquée aux secrets —
  le logement porte le défaut, le séjour peut le remplacer, le moteur lit à
  travers.

---

## Volet 3 — La fenêtre « Envoyer le guide »

Un bouton **« Envoyer le guide »** sur la carte du logement (page Mes logements),
qui ouvre une fenêtre unique — LA surface d'envoi du produit :

### 3.1 Choisir quoi envoyer

1. **Un séjour** de la liste (à venir + en cours, jamais l'historique) :
   « Tracy Russel · 08–22.08 · 🇬🇧 ». Le destinataire EST le séjour — rien à
   retaper : langue **pré-sélectionnée** depuis `guest_lang` (modifiable), email
   et téléphone lus de la fiche.
2. **Le lien vitrine** (langue au choix, défaut = langue du logement).
3. **Le lien maison** (l'actuel partage multilingue, pour réimprimer le QR).

Le token (séjour ou vitrine) est **généré au premier usage** puis réutilisé.

### 3.2 Canaux

Pour la sélection courante :
- **Copier le lien** ;
- **QR téléchargeable** (PNG, nommé proprement : `guide-ballarin-tracy.png`) ;
- **Email** : `mailto:` pré-adressé (`guest_email`), sujet/corps pré-remplis dans
  la langue choisie (gabarits courts, i18n via l'inventaire V2-21a — nouvelles
  clés `ui.send_*` à ajouter à l'inventaire, générées puis relues comme le
  reste) ;
- **WhatsApp** : `wa.me/{guest_phone}` pré-rempli, même message.

### 3.3 Le manque devient une invitation

Séjour sélectionné sans email → le bouton Email est désactivé avec « Ajouter un
email à ce séjour » qui ouvre la modale du séjour (même chose pour WhatsApp sans
téléphone). Esprit §0.6 : le manque est montré au moment où il coûte, jamais en
reproche générique.

### 3.4 Tests clés du volet

- La fenêtre liste les séjours à venir, pas les passés.
- Sélection d'un séjour 🇩🇪 → lien `/b/…`, langue pré-sélectionnée `de`, mailto
  vers l'email de la fiche avec corps en allemand.
- Vitrine → lien `/v/…` ; **aucun lien produit par la fenêtre ne contient jamais
  le `guide_token`**, sauf le choix explicite « lien maison ».
- Sans email/téléphone → boutons désactivés + invitation, jamais un mailto vide.
- Harnais headless (patron calendar-harness) pour la fenêtre.

---

## Livrables transverses

- `docs/calendrier.md` §8 (les trois liens, l'expiration, la surcharge) ;
  `docs/i18n.md` si nouvelles clés d'inventaire (les régénérer + `--check`).
- `CLAUDE.md` : invariant « un lien vitrine ne rend jamais un secret réel » +
  migration 021 dans les commandes + **table des préfixes réservés**
  (`/g/` maison · `/s/` équipe · `/v/` vitrine · `/b/` séjour).
- `project_tracker.html` mis à jour après chaque volet.
- SW du guide : bump si CSS/JS du guide touchés.
- Hors périmètre (explicitement) : l'envoi automatique J-7 (V2-23d), la consigne
  de bagages après départ, tout compteur de vues vitrine.

---

## Reprise 02/08 — refactor ciblé du volet 1 (`?s=` → `/b/`)

**Contexte** : le volet 1 est livré dans l'arbre de travail en forme `?s=`,
**rien n'est commité** (`main` = 6eec6f1). Le domaine est bon ; seule la couche
de routage porte la faute de sécurité. **C'est une demi-session, pas une
relivraison** — ne PAS réécrire ce qui suit.

### À GARDER TEL QUEL (ne pas toucher, sauf mention)

- `db/migrations/021_stay_showcase_tokens.sql` — le SQL est parfait
  (idempotent, UNIQUE multi-NULL, zéro backfill). **Seul l'en-tête est à
  réécrire** (il documente la forme `?s=`) — déjà fait par André/Claude le
  02/08, vérifier et ne pas y revenir.
- `backend/api/guide_page.py` — intégralement : `render_stay_expired` (aucune
  donnée, noindex), `_stay_welcome_html` (repli générique), cartes d'exemple
  vitrine rendues côté serveur sans slot de secrets, variant `showcase` sans
  bouton de demande, i18n FR/EN/ES des nouveaux libellés.
- Dans `guide.py` : `STAY_EXPIRY_DAYS`, `_stay_expired`, la logique de
  `_resolve_stay` (3 cas morts), le garde du `stay_token` sur le POST des
  demandes (404 si token mort, jamais de rattachement deviné), toutes les
  routes `/v/` (page, og-image, médias).
- `frontend/guide/sw.js` — bump v26 déjà fait.

### À REFACTORER (la couche de routage seule)

1. Nouvelle route `GET /b/{stay_token}` : résout le séjour PUIS le logement
   côté serveur ; rend le guide en variant `stay` (accueil personnalisé,
   `guest_lang` par défaut, `?lang=` prioritaire).
2. Supprimer le paramètre `?s=` de `GET /g/{guide_token}` et le contrôle
   « même logement » dans `_resolve_stay` (sans objet avec un seul token — la
   signature change : plus besoin de `prop`).
3. Le POST des demandes garde `stay_token` comme rattachement certain **sans
   dépendre du `guide_token`** (vérifier que la résolution ne passe plus par le
   contrôle de logement supprimé ; le repli lien-maison est inchangé).
4. `_real_token()` ne s'applique **pas** à `/b/` — décision explicite dans le
   code (commentaire) + test.
5. Page neutre **identique** pour tous les cas morts des nouveaux préfixes
   (`/b/` et `/v/`) ; ne pas rétrofitter `/g/` (en prod, `render_not_found`).
6. Adapter les éventuels renvois au lien de séjour dans `guide_page.py`
   (`stay_ctx`, liens internes) si — et seulement si — ils encodent la forme
   `?s=`.

### Tests à écrire (§1.5 amendé fait foi)

Préfixes croisés → même page neutre ; aucune route secrets sous `/v/` ; `/b/`
capté par l'API et non par l'attrape-tout SPA ; pas de slug sur `/b/` ; POST
demandes sur token mort → 404 ; J+8 → neutre, J+6 → servi ; `guest_lang=de`
sans `?lang=`.

### Livrables de la reprise

- Table des préfixes réservés dans `CLAUDE.md`
  (`/g/` maison · `/s/` équipe · `/v/` vitrine · `/b/` séjour).
- Suite de tests verte, **UN commit** (volet 1 refactoré entier), **hash cité
  au rapport**, `git status` propre vérifié en fin de mission.

> **Livré le 02/08, commit b749a92** — conforme aux 6 items, MAIS la relecture
> a montré que le correctif de sécurité est incomplet : voir volet 1bis.

---

## Volet 1bis — le guide_token ne doit pas non plus quitter la maison par le DOM

**Le problème (constaté sur b749a92, ligne à ligne)** : la route `/b/` est bonne,
mais la PAGE `/b/{stay_token}` embarque encore le `guide_token` éternel dans son
HTML — `data-token` (lu par `app.js` ligne 12 pour appeler `/g/{token}/secrets`),
chaque URL de média (`media_base=/g/{token}`), le meta og. « Afficher la source »
suffit à le récupérer ; passé J+7, `/b/` meurt mais `/g/{guide_token}` sert les
vrais codes à vie. Aggravant : le SW (portée `/`) met la page en cache sur le
téléphone du locataire — le token y survit sans même ouvrir la source. La faute
d'origine (`?s=`) est simplement déplacée de l'URL vers le DOM.

**Le patron du correctif existe déjà dans le code : c'est la vitrine.** `/v/` a
ses propres sous-routes précisément pour que « le vrai guide_token ne transite
jamais ». Le 1bis applique le même geste à `/b/`.

### À GARDER — tout b749a92

Routes `/b/` et `/v/`, `_resolve_stay(conn, stay_token)`,
`_render_guide_html`, table des préfixes CLAUDE.md, les 9 tests §1.5.
On ÉTEND, on ne réécrit pas.

### À FAIRE

0. **Inventaire d'abord** : recenser dans `guide_page.render_guide` et
   `frontend/guide/app.js` TOUTES les occurrences du token / du préfixe `/g/`
   qui atterrissent dans le rendu du variant `stay` (data-token, data-stay-token,
   URLs média, og, liens internes, appels fetch : secrets, data, requests,
   manifest). Lister avant de coder — c'est la checklist de sortie.
1. **Sous-routes `/b/{stay_token}/…`**, toutes gardées par `_resolve_stay`
   (token mort → 404 API, rien révélé) :
   `GET /b/{t}/data` · `GET /b/{t}/secrets` · `GET /b/{t}/media/{id}` ·
   `GET /b/{t}/og-image.png` · `POST /b/{t}/requests`.
   Conséquence assumée et VOULUE : **les secrets meurent à J+8 avec la page**
   (« page et endpoints meurent ensemble » étendu aux secrets — c'est LE progrès
   de sécurité du lien de séjour). `/b/{t}/secrets` sert les secrets du logement
   (la surcharge par séjour = volet 2, pas maintenant) avec la même logique de
   mode d'accès que `/g/{t}/secrets`.
2. **Rendu stay sans guide_token** : `_render_guide_html` paramétré pour que le
   variant `stay` utilise `media_base=/b/{stay_token}`, og sur `/b/…`, et
   `data-token={stay_token}`. `app.js` déduit son préfixe d'API (`/g/` ou `/b/`)
   du pathname ou d'un `data-base` — au choix du plus simple, mais UNE seule
   source de vérité. Le POST des demandes du variant stay part sur
   `POST /b/{stay_token}/requests` ; la branche `stay_token` du
   `POST /g/…/requests` et le champ du schema disparaissent si plus aucun
   appelant (vérifier app.js — pas de code mort).
3. **`manifest=False` sur `/b/`** (comme la vitrine) : une PWA installée sur un
   lien qui meurt à J+7 serait cassée ; l'installation reste le métier du QR
   maison. Aucune route manifest sous `/b/`.
4. **guide_page.py — libellé de la page neutre** (arbitré 02/08, non livré) :
   « Ce lien n'est plus valable — demandez un lien à jour à la personne qui vous
   l'a envoyé », même ton en EN/ES (elle sert désormais `/b/` ET `/v/`, tous cas
   morts confondus — une vitrine n'« expire » pas et un prospect n'a pas
   d'hôte). Toujours aucune donnée du logement, noindex, 404.
5. **Bump SW** (le JS du guide change).

### Tests du 1bis

- **LE test de la mission** : le HTML d'une page `/b/{stay_token}` ne contient
  le `guide_token` NULLE PART (rendu réel, recherche du token dans le corps
  complet — pas un test d'endpoint cette fois : c'est précisément le DOM qu'on
  corrige).
- `/b/{t}/secrets` : sert pendant le séjour (J-7, J+6), **404 à J+8** et sur
  séjour annulé/inconnu.
- Médias et data servis via `/b/…` sur la page séjour ; og aussi.
- `POST /b/{t}/requests` : rattachement certain au séjour du token ; token mort
  → 404, zéro rattachement (reprendre le test existant, déplacé sur la nouvelle
  route).
- Aucun manifest sous `/b/`.
- Libellé neutre : la même page (contenu identique) sur `/b/` mort et `/v/`
  inconnu.
- Les 9 tests §1.5 restent verts (adapter ceux qui postaient `stay_token` sur
  `/g/`).

### Livrables du 1bis

UN commit, hash au rapport, `git status` propre, `project_tracker.html` à jour
(V2-23c volet 1 = livré après 1bis, pas avant), `docs/calendrier.md` §8 si déjà
rédigé — sinon au volet 3 comme prévu.
