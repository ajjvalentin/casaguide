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
supprimé (la synchro le recréerait).

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
