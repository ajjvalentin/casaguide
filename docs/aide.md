# L'aide du back-office — recherche à couverture garantie (V2-31, volet 3a)

> Source : `docs/audit_ux.md` §0 (« Si André cherche, tout le monde est perdu »)
> et §4 (exigences actées). Le back-office est en français délibéré → l'aide vit
> en FR, hors de l'inventaire i18n voyageur (qui reste le domaine du guide).

## L'idée en une phrase

Un champ de recherche unique (bouton **« Aide »** de l'en-tête + raccourci **⌘K /
Ctrl+K**) trouve, pour n'importe quel mot du propriétaire, une réponse courte et un
bouton **« M'y emmener »** qui l'y conduit. **Aucun écran zéro-résultat** : à défaut
de correspondance franche, on propose les rubriques les plus proches, puis l'index
entier. Et une garantie mécanique : **tout libellé visible du back-office doit avoir
une entrée d'aide, sinon la suite de tests rougit.**

## Les pièces

| Fichier | Rôle |
|---|---|
| `frontend/js/help/index.js` | **LA source de vérité** : le tableau `HELP_INDEX` (de la donnée, pas du code). |
| `frontend/js/help/search.js` | Moteur PUR : normalisation, correspondance tolérante (trigrammes), `searchHelp`, `isCovered`, résolution des routes. |
| `frontend/js/help/panel.js` | Le panneau (bouton « Aide », ⌘K, rendu, « M'y emmener », journalisation, repli → « Premiers pas »). |
| `frontend/js/views/premierspas.js` | **La page « Premiers pas »** (volet 3b) : 7 étapes verbatim, CTAs, rubriques groupées par `step`. |
| `frontend/css/app.css` | Styles `.help-*` et `.pp-*`. |
| `backend/api/routers/help.py` | `POST /api/help/searches` — journal best-effort. |
| `db/migrations/027_help_searches.sql` | Table `help_searches` (métrique de santé de l'index). |

## L'index (`index.js`)

Chaque entrée est un objet :

```js
{
  id: "envoyer-guide",                 // slug stable (traduisible plus tard)
  step: 7,                             // rang dans la chaîne cible (1–7) ou null — cf. « Premiers pas »
  question: "Comment envoyer le guide à mon voyageur ?",
  keywords: ["envoyer le guide", "email", "whatsapp", "qr", ...],  // + synonymes du terrain
  steps: ["…", "…"],                   // 2 à 4 phrases, ton du modèle étape 1 de l'audit
  target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  exemple: "…"                         // optionnel
}
```

- **`keywords`** inclut les mots du TERRAIN, pas seulement le vocabulaire officiel :
  « clé wifi », « code boîte », « airbnb », « QR ». C'est là que se gagne la
  tolérance.
- **`steps`** : le ton = *une phrase qui dit ce que le système FAIT et rassure sur
  l'erreur* (audit §1, étape 1). Jamais un pavé.
- **`target.route`** : un **gabarit** de route existante. `:id` = le logement
  courant, résolu contre le hash actif ; sans logement courant, une route qui a
  besoin d'un `:id` retombe sur « Mes logements ». Toute route doit résoudre
  (vérifié par `help-search.test.mjs`).
- **`step`** (volet 3b) : le rang **1 à 7** de la chaîne cible (docs/audit_ux.md
  §2) auquel la rubrique se rattache, ou **`null`** si elle relève d'un usage HORS
  du chemin heureux (calendrier, abonnement, équipe d'entretien, suppression). La
  page « Premiers pas » range chaque rubrique sous son étape ; les `null` tombent
  dans le bloc final **« Et aussi »**. **Toute entrée porte un `step`** (1–7 ou
  `null`) — l'absence de champ est refusée par `help-search.test.mjs`.

## La page « Premiers pas » (`#/premiers-pas`, volet 3b)

La marche à suivre **linéaire** que le repli de la recherche promettait :
`frontend/js/views/premierspas.js` rend la chaîne cible en **7 étapes** (texte
**verbatim** de l'audit §2, ton pesé — jamais réécrit), chacune avec :

- son **numéro**, son **titre** et une **étiquette** (« Vous fournissez » /
  « Holaguia s'en charge » / « À votre rythme » / « Vous décidez » / …) qui dit
  **qui fait quoi** ;
- un **bouton d'action** (deep-link) qui **résout contre le logement courant**
  (le premier du compte) avec **repli « Mes logements »** — même patron que
  `target.route` du volet 3a (`resolveRoute`) ; l'étape 1 adapte son libellé
  (« Créer mon logement » sans logement, « Ouvrir la fiche Informations » sinon) ;
- dessous, **repliées et dépliables**, les **rubriques d'aide de l'étape** (celles
  dont `step` vaut ce rang) : question cliquable → étapes + « M'y emmener » (même
  rendu que le panneau ⌘K, classes `.help-*` partagées).

La page est **accessible sans logement** (un compte vierge doit pouvoir la lire).
Les rubriques `step: null` sont réunies en fin de page sous **« Et aussi »**.

**Branchements** : le repli **« Voir toutes les rubriques d'aide »** du panneau ⌘K
ouvre désormais cette page (au lieu de la liste brute) ; le **pied du panneau**
porte un lien **« Premiers pas »** à côté de l'astuce ⌘K ; et l'**état vide de
« Mes logements »** (zéro logement) devient un **accueil** — « Bienvenue sur
Holaguia » + le texte d'accueil en deux phrases + « Créer mon premier logement »
(primaire) + « Lire les Premiers pas » (secondaire).

### Choisir l'étape d'une nouvelle entrée

Rattachez la rubrique à l'étape de la chaîne qu'elle **sert** (1 le logement, 2 les
indispensables, 3 la recherche des lieux, 4 la validation, 5 le reste du guide, 6
publier/tester, 7 envoyer). En cas de doute, la meilleure boussole est la **cible**
`target.route` : une rubrique qui mène à `.../pois` sert l'étape 4, à l'éditeur les
étapes 1/2/5/6, au calendrier/abonnement → `step: null` (« Et aussi »). L'étape 3
(« Holaguia s'en charge ») peut rester **sans rubrique** : le système agit seul.
Aucune entrée ne reste sans `step` — sinon la suite rougit.

## La recherche (`search.js`)

Correspondance **tolérante**, documentée dans le fichier :

1. **Normalisation** : minuscules, accents retirés (NFD), ponctuation → espaces.
2. **Tokens** (mot à mot, mots-outils français ignorés) à trois niveaux : exact
   (1.0), préfixe/sous-chaîne (0.7), **proximité par trigrammes** (Dice ≥ 0.5) —
   c'est ce dernier qui rattrape les fautes (« wifii », « calendrer »).
3. **Bonus de sous-chaîne** si la requête entière apparaît dans une entrée.

Deux seuils : `GOOD_SCORE` (au-dessus = réponse « franche », sinon « approchée ») et
`COVERAGE_SCORE` (seuil de la couverture — cf. ci-dessous).

## La couverture garantie PAR TEST (le veto)

Le cœur du volet. `frontend-tests/help-coverage-harness.html` **rend les vraies
vues** du back-office (properties, calendrier, abonnement, éditeur, suggestions)
avec un fetch simulé et un **stub Leaflet** (aucun réseau), **collecte les libellés
réellement affichés** (boutons, entrées de menu, portes, titres d'écran/panneau) et
vérifie que **chacun est couvert** (`isCovered` → au moins une entrée au-dessus du
seuil). Un libellé non couvert → **suite ROUGE** (`help-coverage.test.mjs`). Le
harnais contient aussi **le test du test** : un libellé bidon *doit* rester non
couvert, sinon le mécanisme serait trop laxiste.

**Conséquence pour le développement** : ajouter un bouton/menu/titre au back-office
**sans** enrichir l'index fait échouer la suite. On corrige en ajoutant l'entrée (ou
un mot-clé sur une entrée existante).

### Les exclusions (l'exception, jamais le contournement)

Deux catégories, chacune **justifiée en commentaire** dans le harnais :

- **Verbes d'action universels** : « Annuler », « Fermer », « Enregistrer », « OK »,
  « Confirmer », « Copier »… — non rattachables à une rubrique précise.
- **Valeurs dynamiques / contenu data-driven** : nom du logement (répété en titre et
  fil), **titres de section de l'éditeur** et **catégories de POI** (contenu du guide
  issu du seed — 43 sections, 9 chapitres —, pas du chrome d'application ; l'aide par
  rubrique n'est pas le périmètre du volet 3a). Ces titres sont exclus **par portée**
  (`.section-panel`, `.cat-head`, `.prop-card`, `.chapters`, `.crumbs`).

**Trou honnêtement signalé** : les libellés qui n'apparaissent qu'après un clic
(menus d'envoi, menu « ⋯ » ouvert, modales) ne sont pas collectés au rendu initial ;
leurs concepts sont néanmoins présents dans l'index. La navigation par ancres
(`.crumbs`) et la barre latérale de l'éditeur (`.chapters`) sont exclues comme
navigation d'arbre.

## Le journal (`help_searches`, migration 027)

Chaque recherche est journalisée (`POST /api/help/searches`, **best-effort** : un
échec de journal ne casse jamais la recherche, qui est purement front). On stocke le
strict nécessaire : `owner_id`, `query`, `results_count`, `searched_at`.
`results_count = 0` marque une recherche **sans correspondance franche** — le **taux
de zéro-résultat est la métrique de santé de l'index**. Pas d'UI de consultation dans
ce volet ; `psql` suffit :

```sql
SELECT query, count(*) FROM help_searches
WHERE results_count = 0 GROUP BY query ORDER BY 2 DESC;
```

## Ajouter une entrée d'aide (recette)

1. Ajouter un objet à `HELP_INDEX` dans `frontend/js/help/index.js` (id unique,
   2–4 étapes, `target.route` existante, mots-clés riches en synonymes du terrain).
2. Lancer `node --test frontend-tests/help-search.test.mjs` (index bien formé,
   routes valides) puis `help-coverage.test.mjs` (couverture verte).
3. Si un nouveau bouton du back-office est signalé « non couvert », c'est
   normal : ajoutez son libellé aux `keywords` de l'entrée la plus pertinente (ou
   créez-en une). **Ne jamais** l'ajouter aux exclusions pour « faire passer » —
   l'exclusion est réservée aux verbes universels et aux valeurs dynamiques.

## Hors périmètre (volets suivants)

Le **méta-guide** (guide de démonstration « Villa Holaguia » — session de contenu,
pas de code), la **traduction** de l'aide, les **infobulles par champ** (volet 1 en
a posé quelques-unes), et le **fil persistant des 7 étapes** sur les cartes (volet
2 — la page « Premiers pas » en est la version lisible ; le volet 2 en fera la
version vivante).
