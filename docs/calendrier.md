# Calendrier des séjours (V2-23a) — exploitation

Le calendrier importe les séjours d'un logement depuis les **flux iCal** des
plateformes (Airbnb, Vrbo/Abritel, Booking) et accepte les **saisies directes**.
Il détecte les **chevauchements** (alerte, jamais un blocage) et met en évidence
les **rotations** (départ = arrivée le même jour, avec la fenêtre de préparation).

Ce document couvre : où trouver l'URL d'export iCal sur chaque plateforme, la
mise en place du timer de synchronisation, et le runbook de validation réelle
(volet 3) avec le flux Vrbo/Abritel de **Villa Ballarin**.

> Conception complète : `docs/conception_V2-23_calendrier.md`.
> Invariants & pièges : voir `CLAUDE.md` (invariant 13, section « Pièges connus »).

## 1. Où trouver l'URL d'export iCal (à coller dans « Flux de calendrier »)

L'URL iCal est un **secret** : elle donne accès au calendrier complet du bien.
Holaguia la chiffre en base (AES) et ne l'affiche plus qu'en clair masqué
(`airbnb.com/…/…a1b2.ics`). Ne la partagez pas.

### Airbnb
1. Site web Airbnb → **Annonces** → ouvrir l'annonce.
2. Onglet **Calendrier** → panneau **Disponibilité** → **Connecter un autre
   site web / Synchroniser les calendriers**.
3. **Exporter le calendrier** → copier l'URL `.ics` proposée.

### Vrbo / Abritel
1. Tableau de bord hôte → **Calendrier** → **Importer/Exporter** (ou
   **Synchronisation des calendriers**).
2. Section **Exporter le calendrier** → copier le lien iCal de l'hébergement
   (pour Villa Ballarin : hébergement **n° 1281695**).

### Booking.com
1. Extranet → **Tarifs et disponibilités** → **Synchroniser les calendriers**
   (Calendar sync / iCal).
2. **Exporter** → copier l'URL iCal de la chambre/du logement concerné.

> Chaque plateforme reformule régulièrement ses libellés ; l'idée reste la même :
> « Calendrier » → « Synchroniser/Exporter » → un lien qui finit par `.ics`.

## 2. Synchronisation périodique (timer systemd, toutes les 4 h)

L'ajout d'un flux et le bouton « Synchroniser maintenant » synchronisent à la
demande ; le timer maintient l'ensemble à jour en tâche de fond (les plateformes
elles-mêmes ne rafraîchissent pas plus vite).

```bash
# Sur le serveur, après un deploy.sh (migration 014 + pip icalendar appliqués) :
sudo cp /opt/casaguide/ops/casaguide-sync-calendars.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now casaguide-sync-calendars.timer

# Vérifier :
systemctl list-timers | grep sync-calendars
# Test manuel (sans écriture) :
sudo -u casaguide /opt/casaguide/.venv/bin/python \
    /opt/casaguide/ops/sync_calendars.py --dry-run
```

Le script charge `backend/.env` lui-même (OPS-1). Il exige `CASAGUIDE_SECRET_KEY`
(les URL iCal sont chiffrées) ; sans elle, il s'arrête proprement (code 3).
L'échec d'un flux (plateforme lente, lien invalide) est enregistré sur le flux
(`last_sync_status='error'`, `sync_error`) et **n'empêche jamais** les autres.

## 3. Runbook de validation réelle (volet 3, à dérouler avec André)

Sur **Villa Ballarin**, avec le flux iCal réel Vrbo/Abritel (hébergement
**n° 1281695**) :

1. **Récupérer l'URL** d'export iCal dans le dashboard Vrbo (§1).
2. Back-office → carte du logement → **Calendrier** → section « Flux de
   calendrier » → choisir **Vrbo / Abritel**, coller l'URL → **Ajouter &
   vérifier**. Attendu : « N séjours importés » (ou une erreur claire si l'URL
   est mauvaise).
3. **Vérifier les séjours réels** : les plages importées correspondent au
   calendrier Vrbo ; les blocs « indisponibles » apparaissent grisés.
4. **Compléter un bloc** : ouvrir un séjour importé, saisir le nom du locataire
   et éventuellement des heures ajustées, enregistrer (promotion possible d'un
   bloc « indisponible » en « confirmé »).
5. **Saisir une location directe** : « Nouveau séjour », dates + nom, créer.
6. **Provoquer/constater une rotation ou un chevauchement** : créer un séjour
   dont l'arrivée tombe le jour du départ d'un autre (rotation, fenêtre horaire
   affichée) ; ou dont les dates recouvrent un autre confirmé (bandeau d'alerte).
7. **Synchroniser maintenant** : relancer la synchro (cooldown ~20 s après un
   ajout). Vérifier que rien n'est dupliqué (idempotence) et que les champs
   saisis à la main survivent.
8. **Supprimer le séjour de test** : une location directe est réellement
   supprimée ; un séjour importé serait seulement marqué « annulé » (conservé).

Post-validation : si des retouches sont nécessaires, elles font l'objet du
commit 3 ; sinon la mission V2-23a est close et V2-23b (préparation par séjour,
planning staff dans `/s/`) prend la suite.

## 4. Nature d'un séjour vs. cycle de vie (V2-23b, volet 0)

Depuis la migration 015, un séjour porte **deux axes indépendants** :

- **`nature`** — la **sémantique**, celle qui pilote la préparation par l'équipe
  (jamais le statut). La bonne question n'est pas « est-ce loué » mais « est-ce
  **occupé** » : quelqu'un dort ici → il y a une préparation.

  | nature | occupée ? | préparation | welcome pack | occupation locative |
  |---|---|---|---|---|
  | `reservation` | oui | complète | oui | oui |
  | `private` (proprio / famille / amis) | oui | complète | non | **non** |
  | `works` (travaux, maintenance) | non | aucune | non | non |
  | `unavailable` (fermé, ne pas louer) | non | aucune | non | non |
  | `unqualified` (défaut à l'import) | inconnu | « à qualifier » | — | non |

- **`status`** — le **cycle de vie** : `active` | `cancelled`. La synchro passe un
  séjour disparu du flux en `cancelled` (conservé, jamais supprimé) et le réactive
  s'il réapparaît.

**Chevauchement = occupé ↔ occupé** (`nature IN ('reservation','private')`) : une
occupation privée qui recouvre une réservation est une double réservation, comme
deux réservations. L'alerte existe désormais **à la saisie** (encart sous les
dates, calculé côté navigateur, jamais bloquant ; seul le cas rouge demande un
2e clic « Enregistrer quand même »).

**Import iCal** : un événement dont le flux donne un nom arrive en `reservation` ;
un bloc de fermeture arrive en `unqualified` (à qualifier par le propriétaire). La
synchro ne touche **jamais** la nature saisie à la main (invariant 13).

**Auto-promotion** : saisir un nom sur un séjour « à qualifier » bascule
visiblement la nature sur « Réservation » (réversible).

**Bloc miroir** (§0.5) : une location directe est souvent re-bloquée sur la
plateforme pour éviter la double vente → deux lignes pour un même séjour. On
**rattache** le bloc importé au séjour direct (`linked_booking_id`) : il disparaît
de la liste et du planning (une seule arrivée pour l'équipe) mais n'est **jamais**
supprimé (la synchro le recréerait). Le rattachement se pose depuis la modale du
séjour **ou** depuis le bandeau de chevauchement (§0.4) ; les deux chemins d'UI
passent par le **même** `PATCH /bookings/{id}`.

**Le rattachement absorbe la substance du bloc (V2-23f)** — car un bloc peut
porter des données réelles (des demandes de locataire, des coordonnées saisies) et
le masquer sans les déplacer serait une **perte silencieuse** (constaté en prod le
05/08, cas Tracy Russel : des `booking_requests` en attente évanouies de la fiche,
du bandeau « demandes en attente » et du planning staff). Au point unique où
`linked_booking_id` est posé, le **maître absorbe** le bloc :

- **Demandes** (`booking_requests`) : **toutes** migrent vers le maître — les
  `pending` comme les `accepted` (ces dernières nourrissent le planning
  d'interventions, il doit rester vrai). Un transfert, jamais une création ni une
  destruction.
- **Champs de fiche** (`guest_phone`, `guest_email`, `guest_lang`, `guest_count`,
  `children_ages`, `keybox_code_enc`, `luggage_drop_time`, `notes`) : repris
  **seulement si le champ du maître est vide/NULL** et celui du bloc renseigné. Une
  valeur déjà présente sur le maître n'est **jamais** écrasée (esprit invariant 13).
  Le code de boîte à clés se copie tel quel (bytea chiffré, aucun déchiffrement —
  invariant 5).

**Détachement** (`linked_booking_id`→NULL) : le transfert est un **acte, pas un
miroir** — les demandes et champs migrés **restent** sur le maître (aucune
migration inverse). Le bloc réapparaît dans la vue, vidé de ce qu'il a cédé.
L'opération est **idempotente** : re-rattacher le même bloc ne double aucune
demande et ne réécrit aucun champ.

**Bagages** : `luggage_drop_time` (dépôt avant l'entrée : la maison doit être
accessible et présentable pour cette heure-là) est saisissable ; `luggage_until_time`
est prévu en base (cas symétrique, affiché seulement s'il est renseigné).

## 5. Préparation des séjours (V2-23b, volet 1)

### 5.1 Voyageurs — la checklist est QUANTIFIÉE

Le flux iCal ne transporte **pas** le nombre de voyageurs : il est saisi à la main
dans la modale du séjour (`bookings.guest_count`, enfants compris). Sans lui, la
préparation dirait *quoi* faire mais pas *combien* — c'est pourquoi un séjour occupé
sans `guest_count` est **relancé** dans le calendrier (§0.6, encart ambre).

Les **âges des enfants à l'arrivée** (`bookings.children_ages`, ex. `{1,3,14}`) sont
saisis en chips ; le nombre d'enfants s'en déduit (pas de colonne redondante). Le
libellé « âge à l'arrivée » évite toute mécanique de date de naissance. Les âges
**suggèrent** l'équipement du catalogue (lit bébé, chaise haute, lit d'appoint) —
la modale propose, le propriétaire confirme : rien n'est jamais ajouté d'office
(un bébé dont les parents ont leur propre lit parapluie n'exige aucune préparation).

> RGPD : les âges d'enfants suivent le régime des coordonnées — donnée strictement
> nécessaire à la préparation matérielle, à exposer à l'équipe pour les séjours en
> cours et à venir uniquement (à formaliser en V2-25).

### 5.2 Règles d'entretien par logement (`properties.care_rules`)

Réglables dans « Réglages d'entretien » (bouton du calendrier). Jamais en dur
(invariant 8) — défaut posé à la création du logement (`api/care.default_care_rules`,
valeurs de Villa Ballarin) :

- **`linen_change_from_day`** — draps changés tous les N jours en cours de séjour
  (8 par défaut). Un séjour de 14 nuits → draps à J+8 ; un séjour de 5 nuits → aucun.
- **`midstay_cleaning`** — `included` | `on_request` | `none`.
- **`welcome_pack`** — `free` | `paid` | `none` (jamais sur une occupation privée).
- **`age_bands`** — tranches d'âge → équipement suggéré (personnalisables).
- **`turnaround`** — effort de rotation en **hommes-heures** : la CHARGE suit
  l'occupation (interpolée entre faible et pleine occupation), la RESSOURCE (combien
  de personnes viennent) est une décision d'équipe. ⚠ Les hommes-heures sont laissés
  à `null` tant qu'André ne les a pas mesurés (jamais inventés) → tant qu'ils
  manquent, le signal de fenêtre reste « non configuré » plutôt que faux.

### 5.3 Moteur d'interventions (`api/care.py`, fonction pure)

À partir d'un séjour (nature, dates, voyageurs) et des `care_rules`, il **calcule**
(ne stocke jamais) les interventions datées et quantifiées :

- **La nature pilote** (invariant 14) : `reservation` → tout ; `private` → même
  entretien **sans** welcome pack ; `works`/`unavailable`/`unqualified` → aucune
  intervention automatique.
- Sortie quantifiée à partir de `guest_count` (« Draps pour 6 », « Welcome pack
  pour 6 ») ; une quantité inconnue s'affiche explicitement (« nombre de voyageurs
  non renseigné »), jamais un trou silencieux.
- Une **demande acceptée** (catalogue, `booking_requests`) rejoint la préparation.
- Le signal de fenêtre de rotation (§1.1) dit **quoi faire** : neutre / ambre
  (« prévoir 2 personnes ») / rouge (« infaisable, même à deux »).

API propriétaire : `GET .../bookings/{id}/interventions`. Le **planning de l'équipe**
dans `/s/` (frise des fenêtres) est livré au **volet 2**.

### 5.4 Catalogue de demandes particulières (§1.2)

`property_request_types` (lit bébé, chaise haute, parasol, lit d'appoint) amorcé à
la création, personnalisable (ajout / désactivation — jamais de suppression qui
casserait l'historique). Une demande rattachée à un séjour (`booking_requests`)
porte une `origin` (`owner` ; `guest` au volet 3) et un `status`
(`pending`/`accepted`/`declined`).

> **Duplication volontaire front/back** (comme la règle d'intervalle) : les tranches
> d'âge et les suggestions vivent des DEUX côtés — `backend/api/care.py` (fait foi,
> planning) et `frontend/js/lib/care.js` (suggère à la saisie, sans aller-retour).
> Si l'une change, changer l'autre **et** les deux tests (`test_care.py`,
> `care.test.mjs`).


## 6. Planning de l'équipe & grammaire des signaux (V2-23b, volet 2)

### 6.1 Le planning dans le cahier `/s/` — la FENÊTRE, pas le séjour

Le cahier d'équipe `/s/{staff_token}` porte désormais, **en tête**, une **frise
chronologique** des préparations à venir (`care.build_planning`, pure ; rendue par
`guide_page._render_planning`). Le planning ne répond qu'à trois questions :
*depuis quand la maison est libre*, *pour quand elle doit être prête*, *quoi faire*.
Trois types d'entrées :

- **Fenêtre de préparation** (avant une arrivée occupée) : « libre depuis sam. 01.08
  à 10:00 · prochaine arrivée 15:00 → fenêtre 5 h », la check-list **quantifiée**
  (draps/serviettes/welcome pack pour N), et le **signal de rotation** (§6.2). Un
  **dépôt de bagages** annoncé affiche deux échéances (accessible/présentable à
  l'heure de dépôt ; tout fini à l'entrée officielle). **Longue vacance** : la
  fenêtre s'ancre sur l'arrivée (« libre depuis 12 jours », sans urgence).
- **Intervention en cours de séjour** (draps à J+8, ménage demandé) : la maison est
  **habitée** → rendez-vous, donc **nom + téléphone** du locataire sur la fiche.
- **Séjour non occupé** (travaux/indisponible/à qualifier) : **grisé**, « rien à
  préparer » — un trou inexpliqué fait téléphoner, une ligne grise ferme la question.

**Gating Pro** : le cahier `/s/` est déjà réservé à l'offre Pro (`staff_access`,
invariant 11) — le planning qu'il contient l'est donc de fait.

**RGPD (minimisation)** : les coordonnées ne s'affichent que pour les séjours **en
cours ou à venir** (`care._show_contact` : `ends_on ≥ aujourd'hui`), jamais sur
l'historique — un lien `/s/` partagé ne donne pas accès au répertoire des locataires
passés. Même régime pour les âges d'enfants. Mention à ajouter à la politique de
confidentialité (V2-25).

### 6.2 Signal de rotation gradué (hommes-heures, effectif)

La rotation se mesure en **hommes-heures**, pas en heures : la **charge** suit
l'occupation (`care.turnaround_person_hours`, interpolée par `guest_count`), la
**ressource** (nombre de personnes envoyées) est une décision d'équipe. Le signal
(`care.turnaround_signal`) est donc une **recommandation d'effectif** :

| Fenêtre disponible | Signal |
|---|---|
| ≥ charge à 1 personne + marge de confort | neutre — rien à signaler |
| trop court à 1 personne, tenable à plusieurs | **ambre ⇄** — « prévoir 2 personnes » |
| infaisable même avec `max_cleaners` | **rouge ⚠** |

Le triangle est **strictement réservé au danger** (rotation infaisable, chevauchement
de deux occupations) — jamais à l'incomplétude. Le calcul part de l'**échéance la
plus proche** : un dépôt de bagages avance l'échéance et peut faire basculer une
rotation confortable en rotation serrée. Le signal s'affiche **des deux côtés** :
sur le calendrier du propriétaire (`RotationOut.signal`, aide à décider avant
d'accorder une arrivée anticipée) **et** sur le planning de l'équipe.

**Le travail à plusieurs n'est pas parfaitement divisible** (`care_rules.turnaround.
parallel_efficiency`, défaut 0,75) : la 2ᵉ personne ne rend qu'une fraction d'un
plein temps (mesure André pour Villa Ballarin : ~6 h à 1, ~4 h à 2). Deux personnes
valent donc **1,75** fois une seule, pas deux — diviser naïvement par le nombre de
personnes promettrait des rotations intenables. Le rendement est **explicite et
configurable** dans l'écran « Réglages d'entretien », jamais appliqué en silence.

⚠ Les hommes-heures restent **à `null`** tant qu'André ne les a pas mesurés →
`turnaround_signal` renvoie `level='unknown'` (rien de signalé) plutôt qu'une valeur
inventée.

### 6.3 Grammaire des signaux du calendrier propriétaire (anti-saturation)

L'**incomplétude** (voyageurs/coordonnées manquants, nature à qualifier) est un
signal **sobre** (pastille neutre « Incomplet »), **jamais le triangle**. Le
calendrier agrège l'incomplétude **en tête** (« N séjours incomplets », dépliable)
plutôt qu'un badge répété sur chaque ligne, avec une **saisie rapide en ligne** du
nombre de voyageurs (le champ manque sur la quasi-totalité des imports). Tout champ
comptant des **personnes** (capacité, voyageurs, effectif de ménage) est un **entier
≥ 1** ; seules les **heures** acceptent des décimales.


## 7. Coordonnées séparées & boucle « demande du voyageur » (V2-23b, volet 3)

### 7.1 Téléphone, email et langue séparés (§3.0, migration 018)

Le champ unique « Contact (téléphone / email) » est remplacé par trois colonnes
(`bookings.guest_phone` / `guest_email` / `guest_lang`). La séparation n'est pas
cosmétique :

- **Le téléphone est une ACTION.** Une intervention en cours de séjour se cale par
  appel ou WhatsApp → le planning `/s/` offre des liens `tel:` et **WhatsApp**
  cliquables depuis le mobile de la personne qui fait le ménage. Encourager la
  **forme internationale** (`+33…`) : la clientèle est FR/BE/NL/DE/CH, l'équipe
  compose depuis l'Espagne.
- **L'email sert autre chose** : lien `mailto:` (lien du guide, mot de bienvenue).
- **La langue** aide l'équipe à aborder le locataire et permettra d'envoyer le lien
  du guide dans la bonne langue (`?lang=xx`). La liste proposée dans la modale se
  lit dans les **langues publiées** du logement (+ sa langue source) — jamais une
  liste en dur : offrir une langue non générée créerait une promesse intenable.

`guest_contact` **n'est pas supprimée** (aucune perte). La migration 018 **backfille**
heuristiquement (`@` → email ; ≥ 6 chiffres → téléphone ; ambigu → rien, l'ancienne
valeur reste affichée en repli). La **relance** (§0.6) dit désormais « **téléphone
manquant** » (et non « coordonnées ») : seul le téléphone cale un rendez-vous le jour
même. RGPD : les trois champs suivent le régime des coordonnées (séjours en cours et à
venir uniquement — visibles sur le planning `/s/`, jamais sur l'historique).

### 7.2 La demande du voyageur atterrit dans le planning (§3.1)

Le cahier voyageur annonce déjà le ménage supplémentaire et les draps sur demande. La
demande doit atterrir dans le **planning** plutôt que dans un SMS oublié :

- Les sections « sur demande » portent `field_schema.request` dans le seed
  (`B_cleaning`, `E_services`) → le guide publié affiche un bouton **« Demander ce
  service »** (rendu serveur, lisible sans JS, enrichi en petit formulaire par
  `frontend/guide/app.js`).
- `POST /g/{token}/requests` crée une `booking_requests` en `origin='guest'`,
  `status='pending'`, rattachée au séjour **en cours** à la date du jour (à défaut au
  **suivant**). Le libellé vient **toujours** du template de la section, jamais d'une
  valeur libre du voyageur ; celui-ci peut joindre un message.
- **Anti-abus** : cadence minimale par guide (`CASAGUIDE_GUEST_REQUEST_MIN_INTERVAL_S`,
  60 s par défaut → 429). Le voyageur n'est pas authentifié.
- Le propriétaire est **notifié par email** (best-effort, `no-reply@holaguia.com`) et
  voit un **badge** dans son calendrier (bandeau agrégé + pastille de ligne). Il
  **accepte ou refuse** depuis la fiche du séjour ; une demande **acceptée** devient
  une intervention visible par l'équipe (moteur `care.plan_interventions`).
- **Invariant 4 préservé** : le POST est une **action** du voyageur, aucun appel
  externe automatique au rendu du guide.

## 8. Les trois liens & la fenêtre « Envoyer le guide » (V2-23c)

Le guide se partage désormais par **trois liens** à la grammaire distincte (préfixes
publics réservés, cf. CLAUDE.md — `/g/` maison · `/s/` équipe · `/v/` vitrine ·
`/b/` séjour) :

| Lien | Pour qui | Secrets | Expiration |
|---|---|---|---|
| **Maison** `/g/{guide_token}` | occupant sur place (QR imprimé) | réels | aucune |
| **Séjour** `/b/{stay_token}` | locataire réservé | réels (**code de boîte à clés surchargeable par séjour**, §8.4), meurent avec la page | **J+7 après le départ** |
| **Vitrine** `/v/{showcase_token}` | prospect / annonce / démo | **valeurs d'exemple** (jamais réelles) | aucune |

### 8.1 Génération des tokens à la demande (volets 1/1bis + 3)

Les tokens de séjour et de vitrine sont **générés au premier usage** par la fenêtre
d'envoi, jamais par une route publique :

- `POST /api/properties/{id}/bookings/{bid}/stay-link` → `{token, url}` (lien `/b/…`) ;
- `POST /api/properties/{id}/showcase-link` → `{token, url}` (lien `/v/…`).

Fabrique **hex 128 bits `encode(gen_random_bytes(16),'hex')`** — la même que
`guide_token`/`staff_token` depuis le premier jour. La génération est **idempotente
et atomique** (`repo.ensure_stay_token`/`ensure_showcase_token` : garde SQL
`UPDATE … WHERE … AND stay_token IS NULL` puis relecture) : deux clics simultanés ne
créent qu'un token, un séjour garde le sien. Endpoints **propriétaire authentifiés**
(garde-fou multi-tenant) — le token n'est **jamais** créé ni renvoyé côté public.

### 8.2 La fenêtre « Envoyer le guide » (volet 3)

Bouton **« Envoyer le guide »** sur la carte du logement (page « Mes logements »,
`frontend/js/components/sendmenu.js`) — LA surface d'envoi. On choisit **quoi
envoyer** :

1. **un séjour** (à venir + en cours, jamais l'historique ni les annulés) : le
   destinataire EST le séjour — langue **pré-sélectionnée** depuis `guest_lang`
   (modifiable), email et téléphone lus de la fiche ;
2. **la vitrine** (langue au choix, défaut = langue du logement) ;
3. **le lien maison** (partage multilingue actuel, pour réimprimer le QR).

Puis un **canal** : copier le lien · QR téléchargeable (PNG nommé proprement,
`guide-{logement}-{prénom}.png`) · **email** (`mailto:` pré-adressé, sujet/corps
localisés) · **WhatsApp** (`wa.me/{téléphone}`, même message). Les gabarits sont
**localisés via l'inventaire i18n** (clés `ui.send_*` — FR/EN/ES portées par le
code, langues supplémentaires par `ui_translations` ; endpoint public résolveur
`GET /send-templates?lang=`). **Aucun lien produit par la fenêtre ne contient le
`guide_token`**, sauf le choix explicite « lien maison ».

**Le manque devient une invitation (§3.3)** : un séjour sans email (resp. sans
téléphone) désactive le canal et propose **« Ajouter un email à ce séjour »** —
qui ouvre la modale du séjour dans le calendrier — jamais un `mailto:` vide.

### 8.3 Précédence de langue sur `/b/` (§3.5, acté 02/08)

Sur un lien de **séjour** dont la fiche connaît la langue du locataire, `guest_lang`
**fait foi** : le SSR l'expose au DOM (`data-guest-lang`, **une seule source de
vérité**) et `frontend/guide/app.js initLang` **cesse d'y superposer** la devinette
M-09 (ni `navigator.language`, ni la préférence mémorisée d'un autre guide). Un
**clic explicite** du visiteur sur une puce gagne (via `?lang=`) et est retenu.
`guest_lang` **vide** → M-09 **intact**, exactement comme sur `/g/` (le serveur ne
sait rien du visiteur — la devinette est alors une qualité). Une `guest_lang` non
publiée au registre retombe sur la langue source (invariant 15) et n'expose pas
`data-guest-lang`.

### 8.4 Surcharge du code de boîte à clés par séjour (volet 2, migration 024)

Le logement porte **un** code de boîte à clés par défaut (`property_secrets.
keybox_code_enc`). Un **séjour** peut le **remplacer** le temps de sa fenêtre —
c'est la « surcharge welcome pack » (V2-23b) appliquée aux secrets : le logement
porte le défaut, le séjour peut le remplacer, le moteur lit à travers.

- **Modèle** : `bookings.keybox_code_enc` (BYTEA), chiffrée **AES** exactement comme
  `property_secrets` (mêmes helpers `api/crypto`, clé hors base, invariant 5). NULL =
  pas de surcharge → code du logement (comportement d'origine, zéro backfill).
- **Saisie** : champ facultatif de la modale séjour, « Code de boîte à clés pour ce
  séjour — laisser vide pour utiliser celui du logement ». Écrit par l'API séjour
  authentifiée (`POST`/`PATCH .../bookings/{id}`, `keybox_code` en clair → chiffré
  **côté serveur** avant insertion) ; **vider le champ remet NULL**.
- **Lecture pour l'édition** : le code déchiffré n'est accessible au propriétaire
  que par un **chemin réservé** — `GET /api/properties/{id}/bookings/{bid}/keybox`
  (le même patron que l'édition des secrets du logement). Il ne transite **JAMAIS**
  par `GET /calendar` ni `BookingOut` ni aucune liste (couvert par test :
  `keybox_code_enc` est absent de `_BOOKING_COLS`).
- **Rendu** : `GET /b/{stay_token}/secrets` sert la surcharge si le séjour du token
  en porte une, **sinon** le code du logement (le repli vit **côté serveur, une
  seule fois** dans `guide.py _secrets_payload(keybox_override=…)`). Le lien
  **maison** `/g/{token}/secrets` **ignore** les surcharges (acté : le QR imprimé
  sert toujours le code du logement). La **vitrine** `/v/` n'a aucune route secrets.
- **Invariant — la surcharge meurt avec le lien** : `/b/{t}/secrets` est gardé par
  `_resolve_stay` (token mort → 404). La surcharge disparaît donc **à J+8 avec la
  page**, exactement comme les codes du logement servis via `/b/` : c'est le progrès
  de sécurité du lien de séjour, étendu au code personnalisé.

*Hors périmètre V2-23c : la surcharge du **wifi** par séjour (non demandée) et
l'envoi automatique J-7 dans la langue du locataire (V2-23d).*

## 9. « Envoyer par Holaguia » — l'email HTML part du backend (V2-23d, volet 1)

Les `mailto:`/`wa.me` de la fenêtre (§8.2) dépendent de la messagerie du
propriétaire et sont peu vendeurs. **V2-23d ajoute un canal principal** : un email
HTML soigné (gabarit transactionnel V2-08 — sable/encre/mer, vignette du logement,
bouton d'action) **envoyé depuis le serveur**.

### 9.1 L'endpoint (`routers/send.py`)

`POST /api/properties/{id}/send-guide` (authentifié, garde multi-tenant
`OwnedProperty`) — corps `SendGuideIn` : `kind` (`stay`|`showcase`), `booking_id`
(requis si `stay`), `lang` (validée contre le **registre**, invariant 15 → 422
sinon), `recipient` (défaut : email de la fiche pour un séjour, `care.effective_email` ;
**requis** pour la vitrine). Le **token est assuré côté serveur**
(`ensure_stay_token`/`ensure_showcase_token`) — le client n'envoie **jamais**
d'URL, et le `guide_token` éternel ne figure ni dans le lien (`/b/`·`/v/`) ni dans
le corps. **Envoi SYNCHRONE** (le propriétaire attend la confirmation) ; une panne
SMTP → **502 propre** (jamais un 500 nu) et **aucune** trace écrite.
`GET …/last-send?kind=&booking_id=` renvoie le dernier envoi (« déjà envoyé le… »).

### 9.2 Gabarits localisés (`emails.guide_stay_email` / `guide_showcase_email`)

Contrairement aux emails owner (restés FR), ces deux emails partent **vers le
voyageur/prospect** → ils sont **localisés**. Copies FR/EN/ES dans `emails._EMAIL` ;
langues supplémentaires (nl/de/it/sq) via l'overlay `ui_translations` — nouveau
domaine d'inventaire **`email.*`** (`i18n.email_key`), repli FR sans trou (même
mécanique que `guide_page._t`). Le bouton pointe vers `/b/{stay_token}?lang=` ou
`/v/{showcase_token}?lang=`. Version texte jointe (multipart, comme V2-08). Copies
FR actées (André, 02/08).

Le `mailto:`/`wa.me` de secours se différencie **aussi** par cible :
`GET /send-templates?kind=stay|showcase|house` — séjour/vitrine partagent les copies
de l'email (`email.*`, texte brut) ; la **maison** garde le gabarit utilitaire
générique `ui.send_*`.

### 9.3 Traçage (`guide_sends`, migration 022 + `origin` migration 025)

Une ligne par envoi **réussi** (`id`, `property_id`, `booking_id` NULL pour la
vitrine, `kind`, `lang`, `recipient`, **`origin`** `'manual'|'auto'`, `sent_at`),
écrite **après** l'envoi SMTP. C'est la mémoire de l'affichage « envoyé le… » de la
fenêtre **et le verrou d'idempotence de l'automatisation J-7** (§9.4 : « déjà
envoyé ? »). Aucun backfill (le `DEFAULT 'manual'` couvre les lignes du volet 1).

Côté front (`sendmenu.js`), « Envoyer par Holaguia » est le **premier** canal du
séjour et de la vitrine (états : envoi en cours → « Envoyé ✓ » → erreur) ; la
vitrine exige un **email saisi** ; le reste passe en « ou via votre messagerie ».
La **maison** reste inchangée (lien utilitaire, pas d'email backend).

### 9.4 Envoi automatique du guide à J-7 (volet 2, migration 025)

Sept jours avant l'arrivée, le guide du séjour part **tout seul** par email (l'hôte
le lit au petit-déjeuner). Timer systemd quotidien **09:00 Europe/Madrid**
(`ops/send_guides.py` + `casaguide-send-guides.{service,timer}`, patron
`sync_calendars`) → **cœur testable** `api.guidesend.run_auto_send`.

**Sélection PURE** (`api.care.select_auto_sends`, testable sans base) :

- logement `properties.auto_send_guide` vrai (**interrupteur par logement, défaut
  activé**, migration 025) **et** publié ;
- séjour de nature **`reservation`/`private`**, `status='active'`, non rattaché
  (`_is_occupied`) — jamais `unqualified`/`works`/`unavailable`/annulé ;
- **fenêtre d'arrivée** `today ≤ starts_on ≤ today+7`. Le `≤` (et non `== J-7`)
  fait le **rattrapage** : un timer en panne deux jours ne saute personne, tout
  séjour encore dans la fenêtre non servi reste candidat ;
- **pas déjà envoyé** : une ligne `guide_sends` kind='stay' (manuelle **ou** auto)
  fait office de verrou → un re-run le même jour n'envoie rien deux fois.
  *Conséquence assumée : un envoi manuel à J-60 supprime l'automatique* (le guide
  est déjà dans la boîte du locataire) ;
- **email effectif absent** (`care.effective_email`) → pas candidat à l'envoi mais
  candidat à la **relance** (sortie séparée `AutoSendPlan.to_remind`).

**Envoi** par la MÊME voie que le volet 1 (voie unique `api/guidesend.py`, partagée
avec le canal manuel) : `ensure_stay_token` + `emails.guide_stay_email` (langue =
`guest_lang` si offerte, sinon langue du logement — invariant 15). La trace
`guide_sends origin='auto'` est écrite **APRÈS un envoi réussi** avec un
`conn.commit()` explicite. Un échec SMTP sur un séjour est journalisé, **non tracé**
(ré-essai au passage du lendemain — encore dans la fenêtre) et **n'arrête pas la
boucle** (best-effort, leçon V2-16). `--dry-run` : plan sans envoi.

**Relance §0.6** : quand `auto_send_guide` est actif, un séjour éligible entrant
dans la fenêtre J-7 **sans email** ajoute le motif `missing_info` **`auto_send_
email_missing`** (« Séjour dans 7 jours — email manquant pour l'envoi automatique
du guide ») — pastille calendrier sobre, jamais un email de relance séparé.

*Hors périmètre volet 2 : opt-out par séjour (V2-31), suivi d'ouverture, pièces
jointes, envoi automatique de la vitrine (n'a pas de sens).*

### 9.5 Le J-7 assisté WhatsApp — la file des guides à envoyer (V2-32 volet 1, migration 028)

Le **numéro de téléphone du propriétaire a la valeur** : il n'existe aucune API pour
les comptes WhatsApp personnels, donc tout s'automatise **sauf le dernier appui**. Le
moteur J-7 (§9.4) détecte déjà qui doit recevoir le guide ; ce canal-ci lui fait
**présenter les envois WhatsApp prêts**, un appui par locataire.

**La file** (`AutoSendPlan.to_whatsapp`, même moteur PUR `care.select_auto_sends`) —
mêmes règles que le J-7 email (natures `reservation`/`private`, actif, non rattaché,
logement publié + `auto_send_guide`, **fenêtre** `today ≤ starts_on ≤ today+7`, **pas
déjà envoyé**), au seul changement près : **téléphone effectif présent**
(`care.effective_phone`), que l'email soit là **ou non** (indifférent). Elle est
exposée par `GET /calendar` → `CalendarViewOut.whatsapp_queue[]`
(`{booking_id, guest_name, starts_on, ends_on, lang, phone}`, `lang` = langue
effective : `guest_lang` si offerte, sinon langue du logement).

**L'arbitrage émergent du registre — le premier canal servi gagne, aucun doublon
possible par construction.** Un séjour avec **email ET téléphone** apparaît **dans les
deux listes** (`to_send` email prêt + `to_whatsapp`) tant que rien n'est parti :

- si l'email automatique part à 9 h → ligne `guide_sends` kind='stay' origin='auto' ;
- si le propriétaire envoie par WhatsApp avant → ligne origin='whatsapp_assisted'.

Dans les **deux** cas, dès le passage suivant `already_sent` passe à vrai et le séjour
**quitte les deux listes**. Le registre (verrou d'idempotence, invariant 18) tranche :
il n'y a **aucun état de plus à maintenir**, aucun second verrou, aucune course.

**« Marquer envoyé ✓ »** — `POST /api/properties/{id}/bookings/{bid}/mark-sent`
(authentifié, garde multi-tenant). wa.me n'offre **aucune confirmation technique**
(ouvrir WhatsApp ≠ envoyer) : le geste est **déclaratif**. Il pose une ligne
`guide_sends` kind='stay', **origin='whatsapp_assisted'**, `lang`=langue effective,
`recipient`=le téléphone. **Idempotent** : une ligne stay existe déjà (tout origin) →
`200 {already:true}` sans doublon. Sans téléphone → 422 ; séjour étranger → 404.

**La surface calendrier** — un signal gradué **NEUTRE** (une opportunité, pas une
alerte, jamais le triangle) : « N guides à envoyer par WhatsApp », déplié en une ligne
par séjour (« Tracy Taylor · 23.08–01.09 · 🇬🇧 ») avec **« Envoyer par WhatsApp »** (le
wa.me pré-rempli **réutilisé** de la fenêtre d'envoi §8.2 — mêmes gabarits kind-aware,
même `ensure_stay_token`, même langue effective, `frontend/js/components/sendmenu.js`
`stayWhatsAppLink`) **et** « Marquer envoyé ✓ ». **Deux gestes distincts** : ouvrir le
lien ne marque **pas** envoyé — une ligne d'aide le dit (« Après l'envoi dans WhatsApp,
marquez-le ici pour que Holaguia n'envoie pas de doublon »).

**Cohérence des relances (§4)** : la pastille « email manquant » (`auto_send_email_
missing`) s'**éteint** dès qu'une ligne `guide_sends` stay existe pour le séjour (tout
origin, via `missing_info(..., guide_already_sent=…)`) — le guide est arrivé, on ne
harcèle plus pour le canal. Back-office seul (aucun bump de service worker).

*Hors périmètre : l'API WhatsApp Business (volet 2), tout envoi automatique par
WhatsApp, notification push/digest au propriétaire (la pastille calendrier suffit).*


## 10. Photo de couverture du logement (V2-30)

**Constat (validation V2-23d volet 1).** La vignette des emails (§9) et l'og-image
des liens partagés prenaient la **première photo du guide** (`_first_photo_path` :
photos de niveau logement d'abord, puis 1re section visible). Chez Villa Ballarin,
c'était la boîte à clés vue de la rue — utile au locataire *dans sa section*,
désastreuse comme image de vente. On désigne désormais **une photo de couverture
par logement**, servie partout où le logement se montre **hors du guide**.

### 10.1 Le modèle (migration 023)

`properties.cover_media_id UUID REFERENCES media(id) ON DELETE SET NULL`. **Aucun
backfill** : `NULL` = comportement actuel à l'identique — le repli est le contrat.
`ON DELETE SET NULL` : supprimer le média rend la couverture `NULL` (repli gracieux),
jamais une référence morte. Idempotente (rejeu sûr). Dans `schema.sql`, l'`ALTER`
suit la table `media` (car `properties` la précède).

Une couverture peut être une **photo existante** d'une section **ou** une **photo
dédiée sans section** (`media.section_id` NULL — média du logement). Un média sans
section **ne figure dans aucun rendu de section** du guide (`render_guide` ne reçoit
que les médias de section ; les médias du logement n'alimentent que l'og) : la
couverture est une image de **façade commerciale**, pas un contenu du guide.

### 10.2 L'endpoint (`PUT /api/properties/{id}/cover`)

`routers/properties.py`, garde `require_write_access` + `OwnedProperty`. Corps
`{media_id}` (ou `null` pour **retirer**). **Validation** : `media_id` doit
référencer une **photo de CE logement** — sinon **422** (garde multi-tenant +
cohérence). `repo.set_cover_media` est encore borné par `owner_id`. Retirer la
couverture **ne supprime jamais** le média.

### 10.3 Consommation — la couverture d'abord, repli inchangé

Ladder à trois étages **partout** : **couverture → première photo → image générée**.

- **Vignette des emails** (§9) : `routers/send.py` `_target_image_url` sert la
  couverture en premier (si servable), via le préfixe de la cible (`/b/…`·`/v/…`).
- **og-image des trois préfixes** `/g/`·`/b/`·`/v/` (carte de prévisualisation
  WhatsApp/iMessage) : `routers/guide.py` `_first_photo_path` préfère la couverture
  (`_cover_photo_url`) — servable **seulement** si c'est un média du logement ou
  d'une **section visible** ; sinon repli. La couverture est servie par les routes
  médias publiques des trois préfixes (`get_public_media`/`get_showcase_media`
  servent déjà les médias sans section — les gardes `_resolve_*` n'exigent pas de
  section).

### 10.4 Back-office

Bloc **« Photo de couverture »** (`frontend/js/components/cover.js`) monté dans la
fiche du logement (`propertyinfo.js`) : aperçu de l'actuelle (ou mention
« première photo du guide »), **téléverser** (upload existant, **sans**
`section_code`), **choisir parmi les photos du guide** (picker), **retirer**. Le
bloc agit **immédiatement** (`api.setCover`) et remonte le logement via `onChange`.

*Le guide voyageur n'est pas touché (og/emails côté serveur, bloc côté back-office)
→ aucun bump du service worker. Hors périmètre : recadrage/redimensionnement,
couverture par séjour, galerie.*


## 11. Dates ajustées à la main protégées de la synchro (V2-23g)

**Le contrat des dates.** Par défaut, *le flux possède les dates* d'un séjour
importé : l'upsert de synchronisation ne rafraîchit QUE `starts_on`/`ends_on`
(invariant 13). Utile — une plateforme peut prolonger un séjour et on veut le
suivre — mais **dangereux quand le propriétaire a corrigé une date à la main** :
elle revenait à celle du flux à la synchro suivante, silencieusement, toutes les
4 h (constaté deux fois en 24 h : Tracy Russel, arrivée avancée au 07.08 →
ramenée au 08.08). Les **heures** avaient déjà leur grammaire « (ajustée) » ; les
**dates** l'ont désormais.

Un séjour importé porte donc **un marqueur** `bookings.dates_overridden` :

- **La pose est automatique** — dès que le propriétaire **déplace réellement** une
  date d'un séjour **importé** via le PATCH, le marqueur passe TRUE (l'intention
  EST la modification, aucun geste en plus). *Réellement* : le front renvoie
  toujours `starts_on`/`ends_on` dans le PATCH ; le marqueur n'est posé que si une
  date **change** par rapport à l'existant — éditer le nom ou une heure « ajustée »
  ne fige pas les dates. Une **saisie directe** (sans flux) n'est jamais concernée :
  rien ne l'écrase.
- **La synchro respecte le marqueur** — tant que `dates_overridden` est TRUE, le
  flux ne rafraîchit plus les dates. Mais il **mémorise** à chaque passage les
  dernières dates annoncées (`feed_starts_on`/`feed_ends_on`) — la seule trace de
  ce que la plateforme dit désormais. Les dates **saisies** restent la source de
  vérité de tout l'aval : chevauchements, rotations, planning `/s/`, et **l'envoi
  automatique J-7** (`care.select_auto_sends` lit `starts_on` → le guide part sur
  les vraies dates, pas sur celles, périmées, du flux).
- **Le signal de divergence** — protéger sans signaler serait dangereux (une vraie
  modification côté plateforme doit rester visible). Quand `dates_overridden` ET
  les dates du flux **diffèrent** des dates saisies : la fiche du séjour affiche
  « Le flux Vrbo/Abritel indique désormais 08–22.08 (vous avez saisi 07–22.08) » et
  le calendrier porte un signal **sobre** (« Dates ≠ flux », ton neutre-attention,
  **jamais** le triangle réservé au danger). Un badge sobre « Dates ajustées »
  reste sur la fiche même sans divergence.
- **« Reprendre les dates du flux »** (`POST …/bookings/{id}/reset-dates`) — réaligne
  les dates saisies sur les dernières dates du flux et **rend la main à la synchro**
  (`dates_overridden` → FALSE). Chemin **dédié**, distinct du PATCH (qui reposerait
  le marqueur puisque écrire des dates VAUT prise de possession). Réservé aux séjours
  importés (une saisie directe n'a pas de flux → 404).

**Modèle (migration 026)** : trois colonnes ajoutées à `bookings` —
`dates_overridden BOOLEAN NOT NULL DEFAULT FALSE` (aucun backfill : FALSE =
comportement d'avant), `feed_starts_on`/`feed_ends_on DATE` (NULL tant qu'aucune
synchro n'a eu lieu depuis la migration). On ne peut pas recalculer les dates du
flux au rendu (`GET /calendar` n'a aucun accès réseau au flux — l'événement iCal
n'existe qu'à l'instant de la synchro), d'où leur stockage. Le marqueur et les
dates du flux **survivent au cycle de vie** : un séjour ajusté qui disparaît du
flux (`cancelled`) puis réapparaît (réactivé) garde ses dates protégées.

**Interactions vérifiées** : le bandeau de chevauchement (§0.4) et le rattachement
d'un bloc miroir (§0.5) fonctionnent sur les dates **ajustées** (le cas Tracy EST
la combinaison des deux) ; l'absorption (V2-23f) et la protection des dates
coexistent. *Hors périmètre : protection d'autres champs du flux ; le rapprochement
d'UID changeants côté Vrbo est traité au §12.*


## 12. Succession d'identifiants : hériter la fiche d'un prédécesseur (V2-23h)

**Le comportement Vrbo/Abritel à connaître.** Quand un locataire (ou la
plateforme) **modifie** une réservation — dates, occupants, parfois rien de
visible —, Vrbo/Abritel **réémet l'événement iCal sous un NOUVEL `UID`** au lieu de
mettre à jour l'ancien. Airbnb ne fait généralement pas cela ; Vrbo si. Pour notre
import, l'ancien `UID` **disparaît du flux** → il passe `cancelled` (invariant 13,
jamais supprimé) en **gardant toute sa fiche** (nom, email, langue, code de boîte à
clés, demandes), et un séjour **vierge** naît du nouvel `UID`, aux mêmes dates.

Résultat brut, sans traitement : le propriétaire voit une réservation « annulée »
et une « nouvelle » vide, re-saisit tout sans comprendre — **et le lien `/b/` déjà
envoyé au locataire est mort** (la résolution `/b/` refuse les séjours annulés,
V2-23c). C'est l'incident réel du **06/08** (Villa Ballarin) : `f221c1cf` (Tracy
Russel, `cancelled`, fiche complète) et `571cf102` (créée 06/08 12:20, vierge,
mêmes dates 07–22.08) ; le lien envoyé à 9 h 00 était mort chez la cliente. **Ce
scénario rejouera à chaque modification de réservation plateforme, chez chaque
client.**

**Détection — au RENDU, jamais dans la synchro.** On ne stocke aucun état de plus
et on ne rapproche rien dans l'import (pas d'heuristique fragile côté synchro) :
`GET /calendar` calcule la situation à chaque affichage via une fonction **pure**
`care.find_succession_candidate`. Pour chaque séjour **importé, actif, à fiche
pauvre** (ni nom, ni email, ni téléphone — sinon le propriétaire a déjà repris la
main), on cherche dans le même logement un séjour **`cancelled`, importé d'un autre
`UID`, aux dates identiques ou chevauchantes, porteur de substance** (au moins un
champ de fiche non vide, ou des demandes). Plusieurs candidats → **le meilleur**
(recouvrement maximal, puis le plus récemment créé). Deux séjours seulement
**adjacents** (départ = arrivée : rotation) ne se chevauchent pas → pas de
succession.

**La proposition, jamais l'automatisme** (doctrine 0.4 : le propriétaire décide).
Le séjour vierge porte un **bandeau** sur sa fiche ET un **signal calendrier** au
ton *attention* (ambre — une situation à résoudre, **jamais** le triangle rouge du
danger) : « Ce séjour remplace peut-être « TRACY RUSSEL · 07–22.08 », annulé par le
flux Vrbo / Abritel. Reprendre sa fiche ? » — **langage humain, jamais d'UID**
(principe 0.4). Le message est construit **côté serveur** (une seule source de
vérité) et exposé dans `BookingOut.succession = {source_id, source_label, message}`.

**L'action « Reprendre la fiche »** — `POST …/bookings/{nouveau}/inherit`
(authentifié, `source_id` validé : même logement, `cancelled`, importé →
404/422 sinon). Elle réutilise la mécanique d'absorption **V2-23f**
(`repo.inherit_booking_fiche`) : mêmes champs (**`guest_name` en plus**), **copie
seulement vers les champs vides** du nouveau (`COALESCE`+`NULLIF`, jamais
d'écrasement — esprit invariant 13), migration des demandes `pending` **et**
`accepted`, `keybox_code_enc` copié tel quel (bytea chiffré, aucun déchiffrement —
invariant 5). **Idempotent** (re-jouer ne remplit que du vide, les demandes migrées
ne matchent plus).

Deux différences **gravées** avec le rattachement d'un bloc miroir (V2-23f) :

- **(a) Pas de `linked_booking_id`** — l'ancien reste `cancelled` et invisible ; ce
  n'est PAS un bloc miroir (le double d'un séjour présent) mais une **succession**
  (l'`UID` a changé, l'ancien est mort). Aucune migration inverse au « détachement »
  (il n'y a pas de rattachement).
- **(b) Le registre `guide_sends` n'est JAMAIS hérité** — c'est le **cœur** de la
  mission et c'est voulu (**invariant 18**) : le nouveau séjour est **vierge
  d'envoi**, donc la fenêtre d'envoi (§8.2) et le J-7 automatique (§9.4) renverront
  un lien **VIVANT** au NOUVEAU token. Hériter le registre aurait pérennisé le bug
  du 06/08 (lien mort jamais renvoyé). Le `stay_token` de l'ancien n'est pas repris
  non plus.

Le **signal disparaît** dès que le nouveau séjour cesse d'être « à fiche pauvre » —
après la reprise (il porte alors le nom hérité) **ou** une saisie manuelle du
propriétaire (email/téléphone/nom). Aucun schéma à migrer : tout se lit dans
l'existant. Back-office seul (aucun bump de service worker). *Hors périmètre :
héritage automatique sans confirmation, rapprochement en amont dans la synchro.*
