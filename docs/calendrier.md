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
