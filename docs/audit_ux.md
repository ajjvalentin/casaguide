# Audit UX de l'espace propriétaire — V2-31, volet 0

**Version 2 (05/08/2026 au soir) — inclut le PARCOURS COMPLET effectué par Claude
lui-même dans le navigateur (session réelle, compte André, Villa Ardon en
brouillon 0 % comme substitut du « propriétaire neuf »), dans la logique
demandée par André : suivre la chaîne étape par étape, avec les yeux et la
logique d'un humain qui découvre, en jugeant l'enchaînement intuitif avant
tout.**

Ce document est la SOURCE des volets 1–3 de V2-31.

---

## 0. Principes directeurs (actés André, 05/08)

1. **« Si André cherche, tout le monde est perdu. »** Le fondateur a demandé
   plusieurs fois « c'est où ? » pendant la semaine (fenêtre d'envoi, photo de
   couverture, sigle maison, rattachement). La navigation par exploration a
   échoué pour l'auteur — elle échouera pour tout nouveau.
   Cas d'école du 05/08 : le RATTACHEMENT (bandeau §0.4 « Rattacher ce bloc »)
   existait, proposait même le bon séjour automatiquement — ni André ni Claude
   ne savaient qu'il existait. **Le produit contient déjà de bons gestes que
   personne ne connaît : rendre visible l'existant compte autant qu'ajouter.**
2. **Le fil guide, le propriétaire décide** (guidage souple ; seul l'absurde
   technique bloque — publier sans adresse).
3. **Enjeu de CONVERSION** — précède la communication (V2-25).
4. **Aucune surface lue par un humain ne parle en identifiants techniques**
   (constat 05/08 : le journal du J-7 dit « séjour 3ccdc040… » ; André : « une
   référence pour un programmeur »). Vaut pour les journaux ops aussi.

---

## 1. LE PARCOURS OBSERVÉ, étape par étape (session navigateur du 05/08)

Grille de lecture pour chaque étape : *ce que le propriétaire voit / ce qu'il
comprend sans aide / ce qui manque pour que l'enchaînement soit intuitif.*

### Étape 0 — Connexion
- **Vu** : page sobre, deux onglets Connexion/Inscription, mot de passe oublié.
- **Verdict : BON.** Rien à signaler. (L'inscription elle-même n'a pas été
  rejouée — pas de création de compte en audit ; le flux V2-16 est réputé
  validé.)

### Étape 1 — Créer le logement (« + Nouveau logement »)
- **Vu** : modale courte — nom, adresse structurée, pays pré-sélectionné
  Espagne, et UNE phrase d'aide exemplaire : « L'adresse sert à localiser le
  logement et à suggérer l'environnement. Vous pourrez ajuster le point exact
  ensuite. »
- **Verdict : TRÈS BON — c'est le MODÈLE.** Peu de champs, une promesse claire
  de ce que le système fera de l'adresse, une porte de sortie (ajuster après).
  Cette phrase d'aide est le TON à généraliser partout.
- **Manque** : rien à cette étape. Le problème commence APRÈS la création.

### Étape 2 — Retour sur « Mes logements » : et maintenant ?
- **Vu** (carte Villa Ardon, brouillon 0 %) : « Complétude du guide 0 % »,
  badges « 96 à valider / 7 retenus » (orange = alarme), deux portes (Guide
  Locataires / Équipe d'entretien), quatre boutons (Calendrier, Suggestions,
  Enrichir, + maison, + corbeille).
- **Ce qu'un nouveau comprend** : qu'il n'a « rien fait » (0 %), qu'il y a
  « 96 problèmes » (orange), et HUIT chemins possibles sans ordre.
- **Verdict : C'EST ICI QUE LA CHAÎNE CASSE.** L'étape 1 était guidée ; à
  l'étape 2 le fil disparaît. Rien ne dit « prochaine étape : … ».
- Constats détaillés conservés du §Mes logements v1 : métrique mensongère
  (Ballarin publiée/validée = « 2 % »), badges qui crient sans expliquer,
  neuf actions de poids égal, corbeille exposée, vocabulaire non introduit
  (Enrichir, Suggestions, retenus, sigle maison).

### Étape 3 — L'éditeur du guide (porte « Guide Locataires »)
- **Vu** : atterrissage direct sur la section « Venir depuis votre
  emplacement » (pourquoi celle-là ? aucun texte ne le dit) ; barre latérale
  9 chapitres / 46 sections, tous à 0/n ; « Publier le guide » en pleine
  lumière dès 0 % ; bascule « Équipe d'entretien » en tête de la barre.
- **Points FORTS relevés** : les descriptions de section sont bonnes
  (« Itinéraires générés automatiquement (GPS…) . Ajoutez ici vos conseils
  personnels : péages, sorties, pièges à éviter… ») ; le badge
  « Pré-remplissable IA » + l'encart « Cette rubrique est alimentée par des
  suggestions automatiques (POI). Valider les suggestions → » est exactement
  le bon patron (dire qui fournit quoi + un lien qui y va) ; la section
  Boîte à clés est exemplaire — badge « Sensible », zone sécurisée encadrée,
  phrase « Le code lui-même est saisi dans l'espace sécurisé et chiffré ».
- **Ce qui manque pour l'intuition** : (a) la distinction IMPÉRATIF /
  FACULTATIF n'existe nulle part — 46 sections d'apparence égale, le nouveau
  ne sait pas que Check-in + Boîte à clés + Wifi sont vitaux et que
  « Excursions » attendra ; (b) aucun ordre suggéré ; (c) les deux bascules du
  pied de section (« Visible dans le guide » / « Section complétée ») ne sont
  expliquées nulle part — or « Section complétée » est PRÉCISÉMENT ce qui
  nourrit le % mensonger de l'étape 2 : personne ne coche une case dont il
  ignore le rôle ; (d) « Publier le guide » sans état de préparation (à 0 %,
  le bouton devrait dire ce qui manque — guidage souple : avertir, pas
  bloquer).

### Étape 4 — Les suggestions (« Suggestions à valider »)
- **Vu** : UNE FOIS ARRIVÉ, l'écran est BON — filtres À valider 96 / Retenus 7
  / Rejetés 12 / Tous 115 (le vocabulaire s'explique enfin par la
  juxtaposition), groupes par catégorie avec « Tout approuver », carte à
  droite, distances déjà calculées, Approuver / Rejeter / Modifier par lieu,
  « + Ajouter un lieu » pour les coups de cœur (l'étape 4 de la chaîne
  d'André existe donc intégralement).
- **Le problème n'est PAS cet écran, c'est d'y ARRIVER en comprenant** : le
  badge orange de la carte n'explique rien ; seul l'encart vert d'une section
  de l'éditeur fait le lien. Le chemin intuitif serait : l'enrichissement se
  raconte (« nous avons trouvé 115 lieux autour de votre adresse ») → CTA
  « Examinez-les ».
- **Mineur** (qualité de données, à lister pour V2-24/enrichissement) : un bar
  de Vétroz avec préfixe téléphonique +49 (Allemagne) ; « +4127 346 00 09 »
  mal espacé — la validation humaine attrape, mais un lot de données douteuses
  décrédibilise la suggestion.

### Étape 5 — Le reste du guide (facultatif)
- Couvert par l'étape 3 : sans marquage impératif/facultatif, cette étape
  n'existe pas comme étape — c'est un tas.

### Étape 6 — Publier et tester
- Non rejoué (cliquer aurait publié Ardon à 0 %). Constat de l'existant : le
  bouton est là, sans état de préparation ni récapitulatif avant publication.

### Étape 7 — Envoyer
- Validée en réel toute la semaine (fenêtre d'envoi, email Holaguia, J-7).
  Les surfaces sont bonnes ; leur DÉCOUVERTE a nécessité Claude (principe 0.1).

### Conclusion du parcours
**L'étape 1 prouve que le produit sait guider. Les étapes 2–5 prouvent qu'il
a cessé de le faire dès que le logement existe.** La matière est là (encarts
IA, écran Suggestions, descriptions de sections) — il manque le FIL qui
enchaîne, et le marquage impératif/facultatif. Rien de structurel à
reconstruire : c'est un travail de liaison et de libellés.

---

## 2. LA CHAÎNE cible — 7 étapes (actée André, 05/08, inchangée depuis v1)

1. **Le logement** *(vous — impératif)* : nom, adresse, contact, couverture.
2. **Les indispensables** *(vous — impératif)* : check-in/out, accès, boîte à
   clés + wifi.
3. **Le système travaille** *(Holaguia)* : enrichissement raconté simplement.
4. **Vous validez et personnalisez** *(vous)* : suggestions + coups de cœur.
5. **Le reste, à votre rythme** *(facultatif)*.
6. **Publier et tester** (aperçu locataire, QR).
7. **Envoyer** (fenêtre, calendrier, J-7).

Fil PERSISTANT (état par étape, deep-links, navigation libre), remplace le
« % » ; guidage souple. Le parcours observé (§1) donne le point d'ancrage de
chaque étape dans l'interface EXISTANTE — aucune refonte lourde requise.

## 3. L'AIDE — recherche par mots-clés (exigences actées, inchangées v1)

Index unique (i18n) → 3 surfaces (recherche, infobulles, « Premiers pas ») ;
**veto André couvert par test** : tout libellé affiché doit être mot-clé d'une
entrée, sinon suite rouge ; zéro-résultat interdit (approché + repli) ;
journal des recherches infructueuses ; corpus initial = les « c'est où ? »
réels des sessions.

## 4. Modale séjour (périmètre inchangé, au tracker V2-31)

## 5. Constats CALENDRIER (05/08, session réelle + navigateur)

- (a) **Références techniques** : journal J-7 en UUID (principe 0.4).
- (b) **Rattachement invisible** (principe 0.1) — la marche à suivre CORRECTE,
  constatée par André : modifier les dates du maître → le bandeau de
  chevauchement propose « Rattacher ce bloc » avec le bon séjour → confirmer.
  Entrée d'aide toute prête.
- (c) **V2-23f livrée** (le rattachement absorbe demandes + champs).
- (d) **DATES vs synchro — mission V2-23g À BRIEFER** : les dates d'un séjour
  importé appartiennent au FLUX (commentaire du code : « la synchro ne touche
  QUE les dates ») ; une date ajustée à la main est écrasée à la synchro
  suivante (constaté en réel : Tracy 07.08 → revenue 08.08, synchro passée
  4 min avant l'examen). À livrer : dates marquées « ajustées » (grammaire des
  heures) → protégées + SIGNAL DE DIVERGENCE quand le flux annonce autre
  chose + action « reprendre les dates du flux ». Décision d'aller (calibre,
  séquence) : ANDRÉ, en attente.
- (e) Les signaux du calendrier (incomplets agrégés, rotations graduées)
  fonctionnent — c'est l'acquis anti-saturation de V2-23b ; la « jungle »
  restante tient aux points a–d plus qu'à l'écran lui-même.

## 6. Reste à auditer

Premier contact absolu (inscription réelle → email de bienvenue → espace
vide — non rejouable en audit connecté), fiche Informations en revue de
libellés, fenêtre d'envoi en revue de libellés, cahier /s/ côté équipe.

## 7. Découpage des volets de code (affiné après le parcours)

- **Volet 1 — libellés & liaisons** (petit, fort rendement) : badges de carte
  reformulés (« 96 lieux suggérés à examiner », visuel neutre) ; « Enrichir »
  renommé/expliqué ; corbeille → menu ⋯ + confirmation ; aides une-phrase sur
  les bascules « Visible » / « Section complétée » et champs orphelins (ton du
  modèle étape 1) ; journal J-7 en langage humain (0.4).
- **Volet 2 — le fil des 7 étapes** : composant persistant carte+éditeur,
  marquage impératif/facultatif des sections, état « prêt à publier » sur le
  bouton Publier (avertit, ne bloque pas), remplace le %.
- **Volet 3 — la recherche d'aide** (index, couverture testée, journal,
  « Premiers pas »).
- **Volet 4 — la modale séjour.**
- Hors programme mais nés de l'audit : **V2-23g** (dates ajustées vs flux),
  qualité de données des suggestions (téléphones aberrants → signalement).
