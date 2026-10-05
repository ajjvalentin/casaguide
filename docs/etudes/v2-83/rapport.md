# V2-83 — Réduire la dépendance aux sources en temps réel

*Étude, 06/10/2026. Aucun code de production. Mesures reproductibles : `mesure.py`
(couverture, → `resultats.json`), `cout.py` (exploitation, → `cout.json`).*

## Résumé

| Question | Réponse courte |
|---|---|
| 1. Foursquare vs Overture | **Non mesuré.** Foursquare OS Places est passé en accès **contrôlé** (Hugging Face, compte + acceptation des conditions + jeton) ; le bucket S3 public ne contient plus que `LICENSE`/`NOTICE`. Il faut un jeton pour conclure. |
| 2. Overture peut-il remplacer Overpass ? | **Non pour le factuel, oui pour le commercial.** Overture n'a **aucun horaire** (le champ n'existe pas), et ses catégories hôpital / police / plage sont bruitées. En revanche ses pharmacies et ses commerces sont fiables et mieux renseignés qu'OSM. |
| 3. Coût d'une base locale | **Négligeable pour Overture** : 9 à 12 Mo par zone (Bali, province d'Alicante, Gironde), extraction en < 1 min, requête en quelques **millisecondes** (contre 15 à 92 s en lecture S3 directe). **Modéré pour OSM** : extrait régional de 140 Mo (Valence) à 1,7 Go (Indonésie), lisible localement en ~1 s, **sans serveur Overpass**. |

**Recommandation** : ne pas choisir entre les sources, mais **déplacer le temps réel vers
des copies locales**. D'abord un cache régional Overture (2-3 jours), puis une lecture OSM
locale depuis les extraits Geofabrik pour nos zones actives, Overpass restant en repli
(1,5 à 2 semaines). Foursquare ensuite seulement, si la mesure (avec jeton) montre un apport.

## Terrains et méthode

Trois terrains de densités opposées : **Seminyak** (Bali, très dense, hors Europe),
**La Zenia** (Costa Blanca, dense), **Bégadan** (Médoc, village). Pour chacune de nos 30
catégories :

- **OSM** : la moisson de production réelle (`overpass.fetch_grouped` : paliers
  dense-first, plafond 8 par catégorie) → présence, lieu le plus proche, % téléphone /
  site / horaires parmi les lieux retenus ;
- **Overture** : release `2026-09-23.1` (S3), bbox 25 km, lieux dans le **rayon de
  préférence** de la catégorie, classés par notre table de production + des mots-clés de
  taxonomie *pour la mesure* (vitaux, transports, géographie — que la production exclut à
  dessein) → nombre, plus proche, % téléphone / site.

Les chiffres bruts complets sont dans `resultats.json`. Ci-dessous, les lignes qui
tranchent.

## 2. Autonomie : ce qu'on perdrait sans Overpass

### Ce qu'Overture fait mieux qu'OSM (le commercial)

| Terrain · catégorie | OSM (retenus, tél.) | Overture (dans le rayon, tél.) |
|---|---|---|
| Seminyak · restaurant | 8 · 12 % | 1 686 · 88 % |
| Seminyak · bar | 8 · 12 % | 400 · 82 % |
| Seminyak · location | 8 · 12 % | 440 · 96 % |
| La Zenia · restaurant | 8 · 0 % | 320 · 92 % |
| La Zenia · supermarché | 8 · 0 % | 37 · 81 % |
| **Bégadan · pharmacie** | **la plus proche à 6,2 km** | **Pharmacie Puyjoursain à 337 m** (confiance 0,91) |

Le cas Bégadan est celui que V2-74 rattrapait par une recherche web payante : Overture
l'avait déjà.

### Ce qu'Overpass seul apporte (le factuel)

1. **Les horaires d'ouverture.** Overture n'a **aucun champ d'horaires** dans son schéma
   (`places`, release 2026-09). OSM en porte une partie : La Zenia pharmacies 43 %,
   supermarchés 38 % ; Seminyak boulangeries 38 % ; carburant 75 % sur les trois terrains.
   Sans OSM : **zéro horaire**. C'est la perte la plus nette.
2. **Les catégories vitales et géographiques, en qualité.** Échantillon des lieux Overture
   les plus proches :
   - « hôpital » = **cliniques et cabinets** (*Bhaktivedanta Medical* à Seminyak ;
     *Centro Médico*, *Clínica Los Altos* à La Zenia). Le vrai hôpital (Torrevieja) est
     dans OSM.
   - « police » **bruité** : un cabinet d'avocats (*Aroca Seiquer & Asociados*, La Zenia),
     un bureau d'immatriculation (*Kantor Samsat*, Seminyak).
   - « plage » **pollué** par des locations (*Apartment Zenia Beach*, *Leilighet til leie
     på La Zenia Beach*).
   - **Arrêts de bus, parkings, bornes de recharge** : absents d'Overture sur les trois
     terrains.
   - Pharmacies : **fiables** dans Overture (Farmacia Zenia Boulevard, Guardian…).
3. **Le factuel géographique** (plages réelles, sentiers, points de vue, gares) reste le
   domaine d'OSM, déjà acté par la décision de sources du 09/09 (V2-48).

**Conclusion 2.** Une base Overture (avec ou sans Foursquare, qui n'a pas d'horaires non
plus selon sa documentation — à vérifier avec le jeton) ne peut pas rendre Overpass
inutile. Elle peut le rendre **inutile pour le commercial**, et la dépendance restante
(factuel + horaires) peut elle-même sortir du temps réel par un extrait OSM local (voir 3).

### Fiabilité constatée d'Overpass (motif de l'étude)

Pendant cette journée de mesure, la moisson Overpass a pris **285 à 344 s par terrain**,
avec des expirations sur les miroirs `kumi.systems` et `private.coffee` et des 504 sur
`overpass-api.de` (la durée « normale » n'a pas été mesurée ici). Les recettes de V2-79b,
V2-79c et V2-80 ont toutes rencontré la même saturation. La dépendance est réelle.

## 3. Coût d'exploitation

### Overture — base locale par zone (mesuré)

| Zone | Lieux | Taille (parquet zstd) | Extraction depuis S3 | Requête bbox 25 km (local) |
|---|---|---|---|---|
| Bali (île) | 117 475 | 12,4 Mo | 57 s | 6 ms |
| Province d'Alicante | 90 766 | 10,4 Mo | 53 s | 4 ms |
| Gironde | 73 993 | 8,9 Mo | 40 s | 3 ms |

- Thème `places`, monde entier : **11,0 Go** (16 fichiers) par release.
- Cadence : **mensuelle** (visibles sur S3 : 2026-08-19.0, 2026-09-23.0, 2026-09-23.1 —
  le bucket ne garde que les deux dernières).
- Aujourd'hui, chaque génération lit S3 en direct : **15 à 92 s** selon la zone, et cette
  lecture est tombée en panne le 05/10 (V2-79c). En local : quelques millisecondes.
- Une zone par destination active (dizaines de zones) = **quelques centaines de Mo**,
  rafraîchis une fois par mois.

### OSM — extrait régional lu sans serveur (mesuré sur la Comunitat Valenciana)

| Étape | Mesure |
|---|---|
| Téléchargement Geofabrik | 140 Mo, 51 s |
| Lecture des nœuds à étiquettes POI (DuckDB `ST_ReadOSM`) | 294 519 nœuds, 0,6 s |
| Polygones (hôpitaux, police, pharmacies) + centroïdes | 297 voies reconstruites, 1,1 s |
| Requête bbox ~20 km autour de La Zenia | 12 ms : 685 restaurants (72 avec horaires), 106 pharmacies, 9 police (nœuds) + hôpitaux en polygones |

Tailles d'extraits : Comunitat Valenciana 140 Mo, Nouvelle-Aquitaine 296 Mo, **Indonésie
entière 1,7 Go** (pas d'extrait Bali seul chez Geofabrik). Geofabrik publie chaque jour ; un
rafraîchissement **hebdomadaire** suffit à un guide.

Ce chemin évite **un serveur Overpass auto-hébergé** (base de plusieurs fois la taille du
`.pbf` et une maintenance lourde — ordre de grandeur, non mesuré ici) : DuckDB lit le `.pbf`
directement, déjà installé pour Overture.

**Inconnue à lever** : la place disque et la mémoire du VPS ne sont documentées nulle part
dans le dépôt (`df -h` et `free -h` sur le serveur suffisent). Ordre de grandeur nécessaire
pour l'option complète sur nos marchés actuels (ES, FR, ID + quelques autres) : **5 à 10 Go**.

## 1. Foursquare OS Places — ce qui manque pour conclure

- Distribution actuelle : Hugging Face `foursquare/fsq-os-places`, **accès contrôlé**
  (`gated: auto` : approuvé automatiquement après acceptation des conditions, mais il faut
  un compte et un jeton). Accès anonyme : HTTP 401.
- Releases **mensuelles** (22 visibles, dernière 2026-09-15) ; **11,6 Go** monde (100
  fichiers parquet) — même ordre de grandeur qu'Overture.
- Licence Apache 2.0 : stockage et redistribution permis, **attribution obligatoire**
  (fichier NOTICE à conserver, mention Foursquare dans notre documentation).
- Overture agrège déjà une partie de Foursquare : l'apport net est précisément ce qu'il
  faut mesurer, sur la même grille (`mesure.py` accepte une troisième colonne).

**Pour finir la question 1** : un jeton Hugging Face (compte gratuit, conditions du jeu de
données acceptées) en variable `HF_TOKEN`. La mesure prend ensuite une heure.

## Recommandation et ordre de grandeur

1. **Cache régional Overture** — *2 à 3 jours.* Extraction par zone au premier guide d'une
   zone neuve, puis rafraîchissement mensuel (timer, patron des autres timers `ops/`).
   `overture.fetch_places` lit le fichier local s'il couvre la bbox, sinon S3 (comportement
   actuel). Gains : 15-92 s → millisecondes par génération, plus de panne S3 au moment
   d'un achat, quelques centaines de Mo. Risque faible : la chaîne en aval ne change pas.
2. **OSM local pour nos zones actives** — *1,5 à 2 semaines.* Lecture des extraits Geofabrik
   via DuckDB ; les sélecteurs `CATEGORY_TAGS` et les filtres `category_matches` existants
   (ils prennent un dictionnaire d'étiquettes) se réutilisent ; il faut réécrire la logique
   dense-first et les relations multipolygones. Overpass devient le **repli** pour une zone
   pas encore téléchargée. Gains : Overpass sort du chemin critique sur nos marchés ; les
   horaires et le factuel restent. Risque : premier guide d'une région neuve (télécharger
   1,7 Go pour l'Indonésie ≈ 10 min) — d'où le repli Overpass.
3. **Foursquare** — *après mesure.* À décider sur chiffres (jeton requis). L'intégration
   elle-même serait courte (même forme qu'Overture : parquet par zone, même fusion).

Ce qui ne bouge pas : la décision du 09/09 (OSM le factuel, Overture le commercial, le web
le qualitatif). L'étude ne change pas **qui fait foi**, seulement **d'où on lit** : des copies
locales plutôt que des serveurs publics au moment de l'achat.
