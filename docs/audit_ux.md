# Audit UX de l'espace propriétaire — V2-31, volet 0

**Statut : brouillon v1 (05/08/2026, après-midi) — audit en cours.**
Écrans passés au crible : Mes logements, éditeur du guide (partiel), système
d'aide (exigences). Restent : calendrier, premier contact (inscription → écran
vide), fiche Informations, fenêtre d'envoi.

Ce document est la SOURCE des volets 1–3 de V2-31 : chaque volet y puisera son
périmètre. Il se relit avant chaque brief.

---

## 0. Principes directeurs (actés André, 05/08)

1. **« Si André cherche, tout le monde est perdu. »** Constat fondateur : le
   fondateur lui-même a demandé plusieurs fois « c'est où ? » pendant la semaine
   de validation (fenêtre d'envoi, photo de couverture, sigle maison découvert
   « en mode surprise »). La navigation par exploration a échoué pour l'auteur
   du produit — elle échouera pour tout propriétaire neuf.
2. **Le fil guide, le propriétaire décide.** Guidage SOUPLE acté : le parcours
   recommande et signale les vides, ne verrouille jamais — le guide est la
   responsabilité et le choix du propriétaire, il en reste maître. Seul
   l'absurde technique bloque (publier sans adresse : casse carte et
   distances). Cohérent avec la doctrine existante « la limite est une vitrine,
   pas un mur » (V2-22).
3. **Enjeu de CONVERSION, pas de confort** (André : « sans cela un proprio qui
   teste va se dire rapidement c'est trop compliqué, j'abandonne »). Ce
   programme précède la communication (V2-25) dans l'ordre des urgences.

---

## 1. Écran « Mes logements » (audité le 05/08 sur capture réelle)

### Constats

- **(a) La métrique « Complétude du guide » ment.** Villa Ballarin — publiée,
  validée en réel, guides envoyés, locataires servis — affiche « 2 % ». La
  métrique compte les bascules « Section complétée » (jamais cochées), pas le
  contenu réel. Pour un nouveau, ce chiffre est un jugement : « tu n'as presque
  rien fait ». Un indicateur qui contredit la réalité vécue est pire qu'aucun.
- **(b) Les badges oranges crient sans expliquer.** « 96 à valider » se lit
  « 96 problèmes » — il faut déjà savoir que l'IA suggère des POI pour
  comprendre. « 92 retenus » : retenus par qui, où ?
- **(c) Neuf actions de poids égal, aucune hiérarchie.** Guide Locataires /
  Équipe d'entretien / Calendrier / Suggestions / Enrichir / Envoyer le guide /
  Voir le guide / icône maison / corbeille. Rien ne dit par où commencer ni ce
  qui est fréquent vs rare. Trois entrées parlent du même objet (le guide :
  compléter/voir/envoyer), deux du même pipeline (Suggestions/Enrichir — la
  différence n'est écrite nulle part). **La corbeille — l'action la plus
  destructrice du produit — cohabite au même niveau que « Voir le guide ».**
- **(d) Vocabulaire non introduit** : « Enrichir » (quoi ?), « Cahier de
  préparation », « Suggestions », « à valider / retenus », le sigle maison.
- **(e) Ce qui marche** : la double porte Guide Locataires / Équipe d'entretien
  (deux publics, acquis V2-26) ; en-tête sobre ; « + Nouveau logement »
  trouvable.

### Directions (volets 1–2)

- Une **action principale par état** : Brouillon → « Compléter le guide » ;
  Publié → « Envoyer le guide ». Le reste replié dans un menu « ⋯ » ; la
  corbeille reléguée dedans, avec confirmation.
- Métrique remplacée par le **fil du parcours** (§3) : « Étape 4/7 : validez
  vos suggestions » — dit où on en est ET quoi faire ensuite.
- Badges reformulés en langage propriétaire (« 96 suggestions à examiner »),
  visuel neutre (une suggestion n'est pas une alerte).
- État vide (zéro logement) = écran d'accueil du parcours, pas une page blanche.
  (À auditer : ce qu'il affiche aujourd'hui.)

## 2. Éditeur du guide (audit partiel — la structure, pas encore l'écran)

Constat structurel (André) : rien ne distingue l'impératif du facultatif, ni ce
que le propriétaire doit fournir de ce que le système fournit. Le nouveau
devant 9 chapitres / 49 sections ne sait pas par où commencer.

→ Réponse : la CHAÎNE (§3) matérialisée dans l'éditeur — sections des étapes
1–2 marquées « impératif », le reste explicitement « à votre rythme ».
(Audit visuel de l'écran lui-même : à faire — densité de la barre latérale,
libellés des bascules, encarts IA.)

## 3. LA CHAÎNE — le parcours guidé en 7 étapes (acté André, 05/08)

Principe : une chaîne logique d'actions alternant CE QUE LE PROPRIÉTAIRE
FOURNIT et CE QUE LE SYSTÈME FOURNIT, avec navigation libre (revenir, corriger,
sauter). Chaque étape : état à faire/en cours/fait, cliquable, deep-link vers
l'écran existant.

1. **Le logement** *(vous fournissez — impératif)* : nom, ADRESSE PRÉCISE
   (pilote géocodage, distances, POI), contact voyageur, photo de couverture.
   « Le système ne peut pas deviner ces informations. »
2. **Les indispensables du séjour** *(vous — impératif)* : arrivée/départ,
   accès, boîte à clés + wifi (espace sécurisé). « Sans elles, un guide ne sert
   à rien à votre locataire. »
3. **Le système travaille** *(Holaguia fournit)* : enrichissement — POI autour
   de l'adresse, urgences, règles locales. « À partir de votre adresse, nous
   préparons les suggestions. »
4. **Vous validez et personnalisez** *(vous)* : accepter/écarter les
   suggestions (c'est ICI que « 96 à valider » prend sens) ; ajouter coups de
   cœur et lieux non trouvés.
5. **Le reste du guide** *(facultatif, à votre rythme)* : équipements,
   règlement, sections optionnelles ; masquer le non-pertinent.
6. **Publier et tester** : voir le guide comme un locataire, imprimer le QR.
7. **Envoyer** : fenêtre d'envoi, calendrier, automatique J-7.

Forme : **fil conducteur PERSISTANT** dans l'espace (pas un tunnel séparé qui
dupliquerait l'interface). Remplace la métrique « % » de la carte logement.
Guidage souple (principe 0.2).

## 4. L'AIDE — recherche par mots-clés (exigences actées André, 05/08)

Idée directrice (André) : pas un manuel statique — « une recherche par
mots-clés qui pointe sur le bon endroit, la bonne marche à suivre ou le bon
exemple ».

- **Un index unique** d'entrées (question, mots-clés + synonymes du terrain,
  réponse en 2–3 étapes, destination deep-link, exemple si utile), versé à
  l'inventaire i18n. Ce MÊME index nourrit trois surfaces : la recherche
  d'aide, les infobulles par champ (volet 1), la page « Premiers pas »
  linéaire. Trois habillages, un contenu — patron du registre des langues.
- **VETO ANDRÉ, couvert par test** : « une aide qui ne trouve pas un mot
  pourtant affiché à l'écran est un défaut bloquant. » Mécanique : script de
  couverture (patron i18n_inventory --check) — tout libellé de l'inventaire
  (menus, boutons, champs : les 320 clés) doit être mot-clé d'au moins une
  entrée d'aide, sinon la suite est ROUGE. Un futur bouton sans entrée d'aide
  ne peut plus passer.
- **Zéro-résultat n'existe pas comme écran** : correspondances approchées
  (fautes de frappe), puis repli « Premiers pas ». Toujours une porte de
  sortie.
- **Journal des recherches infructueuses** (requête + date, rien de personnel)
  → André voit ce que les propriétaires cherchent AVEC LEURS MOTS et qui
  manque. L'aide apprend de l'usage réel.
- **Corpus initial** : les questions réellement posées par André pendant le
  développement (« c'est où le bouton pour envoyer ? », « où est la photo de
  couverture ? », « le service livraison n'apparaît pas », « que veux-tu dire
  par modale »…) — chaque « c'est où ? » des sessions est une entrée d'index
  avec sa formulation naturelle.

## 5. Refonte de la modale séjour (constat d'origine, périmètre au tracker)

Volets suivant les QUESTIONS du propriétaire : LE SÉJOUR (dates, nature,
heures, bagages) / LES VOYAGEURS (nom, contact, langue, nombre, âges) / LA
PRÉPARATION (demandes, notes) / L'ACCÈS (code de boîte à clés, isolé). Premier
ouvert, les autres pliés visibles. Préserver : auto-promotion de nature (§0.3),
avertissement de chevauchement à la saisie (§0.4), suggestions par âges, garde
de surcharge non lue. Y loger l'opt-out J-7 par séjour (différé de V2-23d v2).

## 6. Reste à auditer (prochaines sessions de conversation)

- **Le calendrier** — l'écran le plus riche ; André y vit, il sait où ça pique.
- **Le premier contact absolu** : inscription → email de bienvenue → premier
  écran vide → création du premier logement.
- **La fiche Informations** et **la fenêtre d'envoi** (déjà bonnes en
  substance ; vérifier libellés et aides).
- L'audit visuel de l'éditeur (barre latérale, encarts IA, bascules).

## 7. Découpage pressenti des volets de code (à confirmer à la fin de l'audit)

- **Volet 1** : aides contextuelles par champ + reformulation des badges et
  libellés (petit, vite livré, corrige le pire de l'écran d'accueil).
- **Volet 2** : la carte logement (hiérarchie des actions, menu ⋯, corbeille) +
  le fil du parcours (7 étapes, remplace le %).
- **Volet 3** : la recherche d'aide (index, script de couverture, journal) +
  page « Premiers pas ».
- **Volet 4** : la modale séjour.

Chaque volet = une session Claude Code, un commit, hash en première ligne.
