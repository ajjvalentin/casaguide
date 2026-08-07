# Le fil des 7 étapes — la substance, jamais la déclaration (V2-31, volet 2)

> Source : `docs/audit_ux.md` §1 (étape 2 : « c'est ici que la chaîne casse »),
> §2 (la chaîne actée en 7 étapes) et principe 0.2 (« le fil guide, le
> propriétaire décide — rien ne bloque jamais »).

## L'idée en une phrase

Chaque logement sait **où il en est** dans la chaîne cible en 7 étapes, et le
**dit** : « Étape k/7 · *prochaine action* » sur la carte et dans l'en-tête de
l'éditeur, cliquable vers l'action. Le fil **remplace** l'ancien « Complétude
X % ».

## Le cœur : la SUBSTANCE, pas la déclaration

L'ancien pourcentage se calculait sur la bascule **« Section complétée »** — une
case que personne ne comprenait. D'où le mensonge de l'audit : **Villa Ballarin,
publiée et servie, affichait 4 %.** Le fil ne calcule **jamais** sur cette
déclaration : il regarde ce qui est **réellement rempli** (contenu de section,
secret posé, POI validés, guide envoyé). Une rubrique « complétée » mais vide ne
compte pas ; une rubrique remplie compte, cochée ou non.

**La bascule « Section complétée » a été retirée** (elle ne pilotait plus rien) ;
« Visible dans le guide » reste. La colonne `property_sections.completed` demeure
en base (non destructif, aucune migration) mais n'alimente plus aucune surface.

## Les critères (source unique : `backend/api/journey.py`, fonction PURE)

Le calcul vit **côté serveur** (comme `api/care`) et est exposé sur
`PropertyOut.journey` (liste **et** détail). Aucune colonne nouvelle, aucune
migration : tout se déduit de l'existant.

| Étape | « Faite » si… |
|---|---|
| **1 — Votre logement** | adresse géocodée **ET** (un contact voyageur **OU** une photo de couverture). |
| **2 — Les indispensables** | arrivée renseignée (contenu `A_checkin` **ou** heure standard) **ET** accès/boîte à clés (contenu `A_access`/`A_keybox` **ou** code posé dans `property_secrets`) **ET** wifi posé. Les secrets sont testés par `IS NOT NULL` — **jamais déchiffrés**. |
| **3 — Holaguia cherche** | un enrichissement a produit des lieux (POI présents, **tout statut**). |
| **4 — Vous validez** | au moins un lieu retenu (approved/edited) **ET** zéro en attente. « en cours (N à examiner) » tant que des suggestions attendent. |
| **5 — Le reste** | **jamais un état binaire** — affiche un compte informatif de rubriques facultatives garnies. N'entre **pas** dans le « k/7 » (on ne culpabilise pas le facultatif). |
| **6 — Publier** | le guide est publié. |
| **7 — Envoyer** | au moins une ligne `guide_sends` (tout kind/origin). |

**Étape courante** = la première non faite de la séquence `1,2,3,4,6,7` (l'É5 est
exclue). Au bout du chemin (É7 faite) : le fil devient **« Guide envoyé ✓ »** — la
carte d'un vétéran ne fait pas la leçon.

**Progression k/7** : calculée sur 6 jalons (l'É5 n'a pas d'état) mais **affichée
en /7** pour rester dans le vocabulaire du parcours ; par choix assumé, l'É5
compte comme faite dès que l'É6 l'est.

## Les surfaces

- **Carte « Mes logements »** : le fil (pill + barre) remplace « Complétude X % ».
  Les pastilles de POI restent (issues de `/stats`).
- **Éditeur** : l'en-tête affiche le fil au lieu du « X % complété » ; la barre
  latérale marque les sections **essentielles** d'un badge sobre **« Essentiel »**
  (`journey.ESSENTIAL_CODES` : `A_checkin`, `A_checkout`, `A_keybox`, `A_access`,
  `B_wifi`). L'absence de badge dit « à votre rythme ». Le décompte par chapitre et
  la coche par section reflètent désormais la **substance** (rubrique garnie).
- **Publier le guide** : la confirmation **liste les manques** en langage humain
  (« Le wifi n'est pas renseigné — votre voyageur le cherchera ») avec **« Publier
  quand même »** toujours disponible. Seule l'**adresse absente** bloque réellement
  (guidage souple, audit 0.2).
- **Premiers pas** (`#/premiers-pas`) : chaque étape gagne sa **coche d'état** pour
  le logement courant — la page devient un tableau de bord.

## Duplication front/back assumée

Le front juge la « substance » d'une section pour la barre latérale de l'éditeur
(`frontend/js/journey.js sectionHasSubstance` + `ESSENTIAL_CODES`), miroir de
`backend/api/journey.py` (`section_has_substance`, `ESSENTIAL_CODES`) — comme
`js/lib/care.js`. Si l'une change, changer l'autre **et** les tests
(`backend/tests/test_journey.py`, harnais front).

## Tests

- `backend/tests/test_journey.py` — la fonction pure sur tous les scénarios +
  le cas **Villa Ballarin** (publiée+validée+servie → au-delà de l'étape 6 :
  le test anti-mensonge).
- `backend/tests/test_api.py::test_property_exposes_journey_measuring_substance`
  — le fil est exposé sur la liste & le détail, et avance avec de vraies données.
- Harnais front (help-coverage, editor-context, premierspas) — le fil rendu.
