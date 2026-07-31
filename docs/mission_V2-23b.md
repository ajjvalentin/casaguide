# Mission V2-23b — Préparation des séjours & planning d'équipe

**Statut** : brief rédigé le 31/07/2026. Prérequis : V2-23a livrée et validée en réel
(flux Vrbo/Abritel de Villa Ballarin).

**Principe directeur, à garder en tête sur tous les volets** :

> Le cahier d'équipe ne répond qu'à trois questions : *depuis quand la maison est-elle
> libre*, *pour quand doit-elle être prête*, *qu'est-ce que je dois faire*. Tout ce qui
> ne sert pas ces trois questions n'a rien à faire sur le planning.

Corollaire de modélisation : **la bonne question n'est pas « est-ce loué » mais « est-ce
occupé »**. L'axe commercial (ça rapporte ou non → statistiques d'occupation) et l'axe
opérationnel (quelqu'un dort ici → il y a une préparation) sont indépendants et ne
doivent pas être portés par le même champ.

---

## Découpage

Quatre volets, **un commit par volet**, suite de tests verte avant chaque commit.
Prévoir **deux sessions Claude Code** : volets 0–1, puis volets 2–3.
André pousse lui-même (`git push`) — Claude Code ne pousse jamais.

---

## Volet 0 — Ergonomie du calendrier (constats terrain du 31/07)

### 0.1 Migration 015 — nature du séjour

Le champ `bookings.status` porte aujourd'hui deux choses à la fois : le cycle de vie
(annulé ou non) et la sémantique (réservation / bloc). On les sépare.

```sql
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS nature TEXT NOT NULL DEFAULT 'unqualified'
    CHECK (nature IN ('reservation','private','works','unavailable','unqualified'));
```

Backfill (idempotent) : `status='confirmed'` → `nature='reservation'` ;
`status='blocked'` → `nature='unqualified'`.
Puis `status` est ramené à un cycle de vie pur : `'active' | 'cancelled'`
(UPDATE des valeurs existantes, puis remplacement de la contrainte CHECK).

Sémantique des cinq natures et profil de préparation associé :

| nature | occupée ? | préparation | welcome pack | comptée en occupation locative |
|---|---|---|---|---|
| `reservation` | oui | complète | oui | oui |
| `private` (propriétaire, famille, amis) | oui | complète | non | **non** |
| `works` (travaux, maintenance) | non | aucune préparation d'accueil | non | non |
| `unavailable` (fermé, ne pas louer) | non | aucune | non | non |
| `unqualified` (défaut à l'import) | inconnu | signalée « à qualifier » | — | non |

**Conséquence sur la détection de chevauchement** : la règle actuelle
(`confirmed` ↔ `confirmed`) devient **occupée ↔ occupée**, c'est-à-dire
`nature IN ('reservation','private')`. Une occupation privée qui recouvre une
réservation est une double réservation, exactement comme deux réservations.

Le moteur de synchronisation ne doit **jamais** écraser une nature saisie à la main
(prolongement de l'invariant 13). Un événement iCal dont le flux donne un nom arrive en
`reservation` ; sinon en `unqualified`.

### 0.2 Autres colonnes de la migration 015

```sql
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS luggage_drop_time  TIME,   -- dépôt de bagages avant l'entrée
  ADD COLUMN IF NOT EXISTS luggage_until_time TIME,   -- consigne de bagages après le départ
  ADD COLUMN IF NOT EXISTS linked_booking_id  UUID REFERENCES bookings(id) ON DELETE SET NULL;
```

`linked_booking_id` porte le **rattachement** (§0.5). `luggage_until_time` est prévu en
base et affiché uniquement s'il est renseigné (cas symétrique non encore confirmé par
André).

### 0.3 Auto-promotion dans la modale « Compléter »

Constat : le sélecteur reste sur « Bloqué » même quand un nom est saisi, sans rien
signaler — un séjour complété reste invisible aux alertes.

Dès qu'un nom non vide est saisi sur un séjour `unqualified`, le sélecteur bascule
**visiblement** sur « Réservation », réversible d'un clic. Ligne d'aide sous le
sélecteur : *« Un séjour occupé déclenche les alertes de chevauchement et la préparation
par l'équipe. »*

### 0.4 Avertissement de chevauchement **à la saisie**

Aujourd'hui l'alerte n'existe qu'a posteriori, dans la liste. On saisit à l'aveugle et on
découvre l'erreur après coup.

À chaque modification des dates dans la modale, un encart apparaît sous les champs et
liste ce que la période recouvre, **calculé côté navigateur** sur les séjours déjà
chargés (instantané, aucun aller-retour serveur) :

- **rouge** — chevauchement avec une occupation (`reservation` ou `private`)
- **neutre** — recouvrement d'un `works` / `unavailable` / `unqualified`
- **info** — rotation (départ = arrivée) avec la fenêtre horaire calculée

**Jamais bloquant.** Seul le cas rouge demande un second clic (« Enregistrer quand
même »), pour que la double réservation soit un choix conscient.

Implémentation : module pur `frontend/js/lib/overlaps.js`, avec son propre test headless,
qui reprend la règle d'intervalle semi-ouvert du backend (rotation ≠ chevauchement).
**Duplication volontaire** : le front avertit, le back fait foi — à noter dans
`CLAUDE.md`, sinon les deux divergeront en silence.

### 0.5 Rattachement d'un bloc miroir

Réalité du métier : les locations directes ne passent pas par la plateforme, et le
propriétaire bloque ensuite ces dates sur Abritel pour éviter la double vente. Il y a donc
**systématiquement** deux lignes pour un même séjour.

Quand une saisie recouvre un bloc importé, l'encart d'avertissement propose
« rattacher ce bloc au séjour ». Le bloc rattaché disparaît de la liste et du planning
(une seule arrivée pour l'équipe) mais **n'est jamais supprimé** : la synchronisation
suivante le recréerait. Rattachement, pas suppression.

### 0.6 Relance sur les coordonnées manquantes

Le flux iCal ne transporte jamais le téléphone. Tout séjour dont la durée déclenche une
intervention en cours de séjour (§1.3) et dont `guest_contact` est vide est signalé au
propriétaire dans le calendrier : *« séjour de 14 jours · draps à J+8 · coordonnées
manquantes »*. Pendant opérationnel de l'état « à qualifier ».

### 0.7 Ancrage de la vue sur aujourd'hui

Le flux Abritel exporte aussi l'historique : la liste s'ouvre aujourd'hui sur les séjours
de 2025. « À venir » par défaut, passés repliés dans une section discrète.

---

## Volet 1 — Règles d'entretien, demandes particulières, welcome pack

### 1.0 Migration 016 — nombre de voyageurs

```sql
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS guest_count   INT,     -- total, enfants compris ; NULL = non renseigné
  ADD COLUMN IF NOT EXISTS children_ages INT[];  -- âges À L'ARRIVÉE, ex. '{1,3,14}'
```

**Pourquoi c'est structurant** : la checklist d'équipe n'est pas une liste, c'est une
liste **quantifiée**. Préparer un welcome pack et des draps pour 8 personnes n'a rien à
voir avec le faire pour 2. Sans ce nombre, le planning dit *quoi* faire mais pas *combien*
— et c'est précisément ce que la personne sur place doit savoir avant de charger sa
voiture.

Le flux iCal ne transporte pas le nombre de voyageurs : comme les coordonnées, il est
renseigné par le propriétaire. Il rejoint donc la **relance active** du §0.6 : un séjour
sans `guest_count` est signalé dans le calendrier.

Les champs sont ajoutés à la modale de séjour (le volet 0 ayant déjà été livré sans eux)
et entrent en **entrée du moteur d'interventions** (§1.3) : toutes les quantités de la
checklist en découlent.

**Le nombre d'enfants ne suffit pas — il faut les âges.** Préparer pour un enfant de 3 ans
et pour un adolescent de 15 n'a rien de commun : l'un demande un lit parapluie et une
chaise haute, l'autre compte comme un adulte pour les draps, les serviettes et le welcome
pack. Le champ est libellé **« âge à l'arrivée »** : une réservation posée huit mois à
l'avance verrait sinon un « 1 an » devenir faux le jour du séjour, et le libellé règle le
problème sans aucune mécanique de date de naissance. Le nombre d'enfants se déduit de la
longueur du tableau : pas de colonne redondante, pas d'incohérence possible entre les deux.

**Tranches d'âge définies dans `care_rules`, jamais en dur** (invariant 8) — le
propriétaire doit pouvoir les ajuster. Valeurs par défaut proposées :

```json
"age_bands": [
  {"code": "baby",   "max_age": 2,  "suggests": ["lit_parapluie", "chaise_haute"], "counts_as_adult": false},
  {"code": "child",  "max_age": 12, "suggests": ["lit_appoint"],                  "counts_as_adult": false},
  {"code": "teen",   "max_age": null, "suggests": [],                              "counts_as_adult": true}
]
```

**Les âges SUGGÈRENT les équipements du catalogue (§1.2), ils ne les ajoutent jamais
d'office** : un enfant d'un an dont les parents voyagent avec leur propre lit parapluie ne
doit pas générer une préparation inutile. La modale propose, le propriétaire confirme.

**RGPD** : les âges d'enfants suivent exactement le régime des coordonnées (§2) — visibles
par l'équipe pour les séjours en cours et à venir uniquement, jamais sur l'historique.
Mention à intégrer à la politique de confidentialité (V2-25), avec sa justification :
donnée strictement nécessaire à la préparation matérielle du logement.

### 1.1 Règles d'entretien par logement

`properties.care_rules JSONB NOT NULL DEFAULT '{}'` (jamais en dur — invariant 8) :

```json
{
  "linen_change_from_day": 8,
  "midstay_cleaning": "on_request",
  "welcome_pack": "free",
  "welcome_pack_note": "…",
  "welcome_pack_media_id": null
}
```

Valeurs par défaut proposées à la création = celles de Villa Ballarin
(draps inclus dès le 8e jour ; ménage en cours de séjour non inclus / sur demande).

### 1.2 Catalogue de demandes particulières

Table `property_request_types (id, property_id, code, label, sort_order, is_active)`,
amorcée à la création du logement avec : lit bébé, chaise haute, parasol, lit d'appoint.
Personnalisable par le propriétaire.

Table `booking_requests (id, booking_id, request_type_id, quantity, note, origin, status)`
avec `origin IN ('owner','guest')` et `status IN ('pending','accepted','declined')`.
`origin='guest'` sert au volet 3.

### 1.3 Moteur d'interventions — fonction pure

`api/care.py` : à partir d'un séjour (nature, dates, durée) et des `care_rules` du
logement, produire la liste des interventions avec leur date et leur libellé.
Rien n'est stocké : les interventions sont **calculées**, comme les fenêtres.

Règles d'application par nature :
`reservation` → tout ; `private` → même entretien (ménage, draps, interventions
calculées) **sans** welcome pack ni prestations commerciales ; `works` /
`unavailable` → aucune intervention automatique.

**Toute sortie du moteur est QUANTIFIÉE** à partir de `guest_count` / `children_count`
(§1.0) : « draps pour 6 », « welcome pack 6 personnes », « 6 jeux de serviettes ». Une
quantité inconnue s'affiche explicitement comme telle (« nombre de voyageurs non
renseigné ») plutôt que de disparaître en silence — l'équipe doit voir le trou, pas le
deviner sur place.

**Surcharge par séjour toujours possible** (cocher le welcome pack pour des amis qu'on
veut gâter). La nature donne le défaut intelligent, jamais une camisole.

Tests : un séjour de 14 jours produit un changement de draps à J+8 ; un séjour de 5 jours
n'en produit aucun ; une occupation privée de 14 jours produit les draps mais pas le pack.

---

## Volet 2 — Le planning dans le cahier d'équipe `/s/`

**L'entité affichée est la fenêtre, pas le séjour.** Une frise chronologique portant deux
types d'entrées :

**(i) Fenêtres de préparation**, calculées entre deux occupations consécutives :

> **Villa Ballarin** — libre depuis **sam. 01.08 à 10:00** · prochaine arrivée
> **sam. 01.08 à 15:00** → **fenêtre 5 h**
> **Dépôt de bagages annoncé à 11:30** — accessible et présentable pour cette heure-là
> À préparer : ménage complet · draps (2 doubles + 1 appoint) · lit bébé · welcome pack

Deux échéances distinctes quand `luggage_drop_time` est renseigné : l'heure de dépôt (la
maison doit être **accessible et présentable**, même si tout n'est pas fini) et l'heure
d'entrée officielle (jouissance, tout est fini).

**Longue vacance** : quand la fenêtre dure plusieurs jours, l'intervention s'ancre sur
**l'arrivée** (rafraîchissement la veille) et non sur le départ — affichage « libre depuis
12 jours », sans urgence. Vaut aussi pour la remise en état après travaux.

**(ii) Interventions en cours de séjour** — draps à J+8, ménage demandé.
Différence de nature : **la maison est habitée**. L'intervention suppose un rendez-vous,
donc le nom et le téléphone du locataire sur la fiche. C'est ce qui fonde la décision
« le staff voit tout » (27/07) : nécessité opérationnelle, pas confort.

Les cinq natures apparaissent toutes sur la frise — les non-occupées grisées, mention
« rien à préparer ». Un trou inexpliqué fait téléphoner ; une ligne grise ferme la
question.

**Minimisation RGPD** : les coordonnées ne s'affichent que pour les séjours **en cours et
à venir**, jamais sur l'historique. Un lien `/s/` partagé ne doit pas donner accès au
répertoire des locataires de 2025. Mention à ajouter à la politique de confidentialité
(V2-25).

**Gating Pro** : le planning est un joyau de l'offre Pro (`staff_access`). Signalétique
V2-22/V2-26 — vitrine badgée, jamais mur au clic.

---

## Volet 3 — Boucler la boucle : demande du locataire → planning

Le cahier voyageur annonce déjà le ménage supplémentaire et les draps sur demande. La
demande doit atterrir dans le planning plutôt que dans un SMS oublié.

- Action « Demander ce service » dans les sections concernées du guide publié.
- **Rattachement au séjour** : le lien du guide est propre au logement, pas au séjour —
  la demande se rattache au séjour **en cours** à la date du jour ; à défaut, au suivant.
- Crée un `booking_requests` en `origin='guest'`, `status='pending'`.
- Le propriétaire est notifié (SMTP existant, `no-reply@holaguia.com`) et voit un badge
  dans son calendrier ; il accepte ou refuse. Une demande acceptée devient une
  intervention planifiée, visible par l'équipe.
- Anti-abus : limitation de débit par jeton de guide (le voyageur n'est pas authentifié).
- Invariant 4 préservé : aucun appel externe automatique côté voyageur.

---

## Livrables transverses

- `docs/calendrier.md` complété (natures, fenêtres, interventions, boucle guide).
- `CLAUDE.md` : invariant « la nature pilote la préparation, jamais le statut » ;
  invariant « coordonnées visibles pour les séjours en cours et à venir uniquement » ;
  piège de la duplication front/back de la règle d'intervalle.
- `project_tracker.html` mis à jour après chaque volet.
- Migration 015 idempotente, appliquée automatiquement par `deploy.sh`.
