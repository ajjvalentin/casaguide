# Mission V2-21a — Socle des langues (registre + inventaire + outillage)

**Statut** : brief rédigé le 01/08/2026. Objectif : après cette mission, ajouter une
langue au produit n'est plus un développement mais une **opération** (générer → faire
relire → réimporter → publier). Les langues cibles (NL, DE, IT, SQ) seront traitées en
missions légères V2-21b…n, **une par une** — une langue ne se publie que lorsque sa
relecture est terminée, et l'italien ne doit pas attendre l'albanais.

**Principe directeur** :

> Le produit n'offre JAMAIS que les langues **publiées**. Aucune liste de langues codée
> en dur, nulle part (invariant 8 étendu aux langues). Une langue en brouillon ou en
> relecture est invisible pour tout utilisateur.

Trois volets, **un commit par volet**, suite de tests verte avant chaque commit.
Une session Claude Code devrait suffire ; couper après le volet 2 si elle s'essouffle.
André pousse lui-même (`git push`).

---

## Volet 1 — Le registre des langues

### 1.1 Migration 019

```sql
CREATE TABLE IF NOT EXISTS languages (
    code        TEXT PRIMARY KEY,          -- 'fr', 'en', 'es', 'nl', 'de', 'it', 'sq'
    name_native TEXT NOT NULL,             -- 'Français', 'Nederlands', 'Shqip'…
    status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','in_review','published')),
    sort_order  INT  NOT NULL DEFAULT 0,
    register_note TEXT                     -- consigne de registre imposée au modèle
);
```

Seed idempotent (ON CONFLICT DO UPDATE sur name_native/sort_order, **jamais sur
status** — le statut est un état d'exploitation, pas une donnée de seed ; un rejeu de
seed ne doit pas dépublier ni republier une langue) :

- `fr`, `en`, `es` → insérés en `published` (état actuel du produit) ; le DO UPDATE ne
  touche pas leur statut.
- `nl`, `de`, `it`, `sq` → insérés en `draft`.

`register_note` porte la décision de registre, tranchée UNE FOIS par langue et imposée
au modèle à chaque génération (cohérence > qualité ponctuelle). Défauts alignés sur le
ton actuel du guide (le FR vouvoie : « Votre guide de séjour ») :

| langue | registre par défaut |
|---|---|
| de | vouvoiement (Sie) |
| nl | vouvoiement (u) |
| it | voi (le guide s'adresse au groupe de voyageurs) |
| sq | forme de politesse (ju) |

André peut ajuster ces notes en base ; elles ne sont jamais en dur dans le code.

### 1.2 Câblage — le registre devient la source unique

Recenser puis remplacer **toutes** les listes de langues en dur par une lecture du
registre (filtre `status='published'`, tri `sort_order`) :

- sélecteur de langue du guide publié (SSR) et détection `navigator.language`
  (une langue non publiée détectée dans le navigateur → repli sur la langue par
  défaut du logement, jamais sur une langue non publiée)
- menu de partage `?lang=xx` (V2-10)
- sélecteur « langue du locataire » de la modale séjour (V2-23b §3.0 — lit déjà « les
  langues publiées » : vérifier qu'il lit bien LE REGISTRE et non une constante)
- langues proposées à l'éditeur pour la traduction (bouton Traductions)
- `properties.published_langs`, gabarits d'emails, et tout endroit révélé par le
  recensement — `grep -rn` sur les codes de langue dans backend/ et frontend/ fait foi,
  pas la mémoire

Endpoint léger `GET /languages` (publiques : code, name_native, published) pour le
front ; le SSR lit la base directement.

**Test clé** : passer une langue de `published` à `draft` en base la fait disparaître
partout (sélecteur, partage, détection, modale séjour) sans redéploiement.

### 1.3 Ce que le volet 1 ne fait PAS

Aucune traduction. Le produit reste identique pour l'utilisateur (3 langues publiées).

---

## Volet 2 — Inventaire des libellés + outillage export/réimport

### 2.1 Extraction de l'inventaire

Recenser tous les libellés statiques du produit dans un **format unique** :
`i18n/inventory.json` (ou équivalent), chaque entrée portant : clé stable, contexte
(« bouton », « titre de section », « email », …), texte source FR, textes EN/ES
existants. Périmètre :

- dictionnaires `_UI` du SSR (`guide_page.py`) — guide voyageur ET cahier `/s/`
- `name_i18n` + `description_i18n` du seed (~49 sections, 27 catégories, chapitres)
- libellés cuisine, libellés de la grammaire des signaux (rotation, « À qualifier »…)
- gabarits d'emails (`emails.py`)
- libellés du bouton « Demander ce service » et du formulaire de demande

L'inventaire est **généré par script** (`ops/i18n_inventory.py`) depuis les sources,
jamais maintenu à la main — sinon il divergera dès la mission suivante. Le script est
rejouable et signale les clés nouvelles/disparues (c'est l'outil de non-régression i18n
des futures missions).

**Hors périmètre** : le contenu des guides (sections remplies, POI) — déjà couvert par
le pipeline de traduction existant, par logement.

### 2.2 Export relecteur / réimport

- `ops/i18n_export.py --lang de` → un fichier **CSV** (lisible dans Excel/Numbers par
  un bénévole non technique) : clé, contexte, source FR, traduction proposée, colonne
  vide « correction ».
- `ops/i18n_import.py fichier.csv` → réimporte les corrections (la colonne correction,
  si remplie, remplace la proposition), idempotent, refuse les clés inconnues avec un
  rapport clair.

Les traductions importées vivent en base (table `ui_translations (lang, key, text)` ou
directement dans les structures existantes selon ce qui est le plus simple — au choix
de l'implémentation, mais UNE seule source de vérité, jamais deux).

### 2.3 Livraison au produit

Le SSR et le seed lisent les libellés traduits pour toute langue **publiée**. Pour
FR/EN/ES, l'inventaire reprend l'existant à l'identique (aucun changement visible —
c'est le test de non-régression du volet).

---

## Volet 3 — Génération des quatre langues (en brouillon)

Pour `nl`, `de`, `it`, `sq` :

1. Génération Claude de l'inventaire complet, avec la `register_note` de la langue
   imposée en consigne système + un GLOSSAIRE fixé en amont pour les termes récurrents
   (check-in, check-out, welcome pack, cahier d'équipe…) — la cohérence terminologique
   est le vrai risque, pas la grammaire.
2. Coût tracé dans `api_costs` (provider anthropic, operation 'i18n_static').
3. Les langues **restent en `draft`** — rien ne change pour l'utilisateur.
4. Produire les quatre fichiers d'export relecteur dans `i18n/review/` :
   - `it` → destiné à André (niveau 1 : auto-vérification)
   - `de`, `nl` → relecteurs natifs (niveau 2 : libellés d'interface + section
     urgences ; la contre-lecture DeepL des points de divergence reste un geste manuel
     d'André, hors code)
   - `sq` → relecteurs albanophones (niveau 3 : relecture complète)

La publication d'une langue (import des corrections → `status='published'`) est
l'objet des missions V2-21b…n, une par langue, quelques lignes chacune.

---

## Livrables transverses

- `docs/i18n.md` : le cycle de vie d'une langue (draft → in_review → published), le
  rituel d'ajout d'une langue future (PT, CA…), les commandes.
- `CLAUDE.md` : invariant « aucune liste de langues en dur — le registre fait foi » +
  commande de vérification.
- `project_tracker.html` mis à jour après chaque volet.
- Migration 019 idempotente ; **backfill** : les logements existants et leurs
  `published_langs` ne doivent pas bouger (vérifier sur l'état antérieur réel — leçon
  015/017).
- Rituel de fin de mission : suite verte, commit créé, **`git status` propre vérifié**
  (leçon du volet 3 de V2-23b).
