# Mission V2-23d — Volet 2 : envoi automatique du guide à J-7

**Statut** : brief rédigé le 05/08/2026, décisions actées par André le jour même.
Fondation = volet 1 (livré 50dbfdd, validé en réel : gabarits HTML localisés,
table `guide_sends`, tokens à la demande). **UN commit. La PREMIÈRE LIGNE du
rapport est le hash du commit.**

**Rituels** : migration testée contre l'état antérieur réel ; `git status`
propre vérifié ; hash en première ligne.

---

## Décisions actées (André, 05/08)

1. **Déclencheur** : timer systemd quotidien (patron `sync_calendars`), **09:00
   Europe/Madrid** — l'email d'hôte se lit au petit-déjeuner.
2. **Cible** : séjours de nature **`reservation` et `private`** uniquement
   (`unqualified` jamais — pas qualifié = pas d'envoi automatique ;
   works/unavailable évidemment exclus), `status='active'`, non rattachés
   (`linked_booking_id IS NULL`).
3. **Sans email sur la fiche** : l'envoi ne part pas → **relance au
   propriétaire** dans le calendrier, esprit §0.6 (« séjour dans 7 jours —
   email manquant pour l'envoi automatique »), via le mécanisme `missing_info`
   existant. Pas d'email de relance séparé (éviter le bruit ; la pastille
   calendrier est le canal des manques).
4. **Opt-out** : interrupteur **par logement** « Envoi automatique du guide à
   J-7 » — défaut **activé**. (Surcharge par séjour : différée à V2-31, la
   modale est déjà saturée — décision de séquence.)

## À livrer

### 1. Modèle — migration 025

- `properties.auto_send_guide BOOLEAN NOT NULL DEFAULT TRUE` (le DEFAULT couvre
  l'existant — pas de backfill séparé, mais vérifier contre l'état antérieur
  réel comme toujours).
- `guide_sends.origin TEXT NOT NULL DEFAULT 'manual'` (`'manual'` | `'auto'`) —
  backfill trivial par le DEFAULT (toutes les lignes existantes sont manuelles).
- Idempotente, rejeu vérifié, `schema.sql` à jour.

### 2. Le moteur de sélection (fonction PURE, patron `care.py`)

`select_auto_sends(bookings, today, …)` — testable sans base :
- fenêtre : `starts_on <= today + 7` **et** `starts_on >= today` (rattrapage :
  un timer en panne deux jours n'oublie personne — tout séjour entrant dans la
  fenêtre non encore servi est candidat, pas seulement `starts_on == J-7`) ;
- natures `reservation`/`private`, actif, non rattaché ;
- logement `auto_send_guide` vrai **et** logement publié ;
- **suppression si déjà envoyé** : une ligne `guide_sends` kind='stay' existe
  pour ce `booking_id` (manuel OU auto) → pas de renvoi. Le registre des envois
  EST le verrou d'idempotence — un re-run du script le même jour n'envoie rien
  deux fois. (Conséquence assumée, à documenter : un envoi manuel à J-60
  supprime l'automatique — le guide est déjà dans la boîte du locataire.)
- email effectif absent (`care.effective_email`) → pas candidat à l'envoi, mais
  candidat à la **relance** (sortie séparée de la fonction).

### 3. Le script — `ops/send_guides.py` + timer

- Patron `ops/sync_calendars.py`/opsenv : `--dry-run` (plan sans envoi),
  idempotent par construction (le ledger), journalisé (N envoyés / N relances /
  N ignorés et pourquoi).
- Envoi par la MÊME voie que le volet 1 : `ensure_stay_token` +
  `emails.guide_stay_email` (langue = `guest_lang` si renseignée ET offerte,
  sinon langue du logement — invariant 15), trace `guide_sends` origin='auto'
  écrite APRÈS envoi réussi, `conn.commit()` explicite. Échec SMTP sur un
  séjour : journalisé, PAS de trace → re-tenté au passage du lendemain (le
  séjour est encore dans la fenêtre), les autres séjours continuent (un échec
  n'arrête pas la boucle).
- **Garde serveur (écart mineur consigné au volet 1)** : la sélection refuse
  annulés/works/unavailable/unqualified CÔTÉ MOTEUR — et tant qu'on y est,
  ajouter la même garde de bon sens à l'endpoint MANUEL `send-guide` (séjour
  annulé → 422 explicite ; les natures restent permises en manuel, le
  propriétaire sait ce qu'il fait, mais un annulé produit un lien mort).
- Timer systemd : quotidien **09:00 Europe/Madrid** (attention : `OnCalendar`
  est en heure LOCALE du serveur — vérifier le fuseau du VPS et fixer
  explicitement, pas d'implicite). Fichiers unit/timer dans ops/ comme
  `sync_calendars`, documentés dans `docs/deploiement.md` (installation = geste
  André, comme le timer de synchro).

### 4. La relance (§0.6)

`care.missing_info` (ou le point d'intégration équivalent) gagne un motif :
« email manquant pour l'envoi automatique du guide » — UNIQUEMENT quand le
logement a `auto_send_guide` vrai ET que le séjour est dans la fenêtre J-7 ET
qu'il est éligible par nature. Pastille calendrier, même grammaire visuelle que
les relances existantes (neutre — c'est une incomplétude, pas un danger).

### 5. L'interrupteur

- Backend : `auto_send_guide` sur `PropertyOut` + éditable (PATCH properties,
  patron care_rules).
- Front : bascule dans la fiche Informations du logement, sous le contact
  voyageur (l'envoi du guide est un geste d'accueil), libellé « Envoi
  automatique du guide à J-7 » + ligne d'aide une phrase.

### 6. Tests

Moteur pur : fenêtre + rattrapage, natures, opt-out, non publié, déjà-envoyé
(manuel supprime l'auto), sans email → relance pas envoi, re-run = zéro double.
Intégration : bout-en-bout avec mailer injecté (trace origin='auto', langue
guest_lang/repli, commit), échec SMTP → pas de trace + boucle continue,
endpoint manuel séjour annulé → 422. Front : bascule visible et persistée.
`missing_info` : le motif n'apparaît que dans les bonnes conditions.

### 7. Livrables

`docs/calendrier.md` §9 étendu (l'automatique), `docs/deploiement.md` (timer),
`project_tracker.html` (entrée V2-23d EXISTANTE → DONE, volet 2 dans la proof —
ne pas créer de doublon), `CLAUDE.md` (migration 025 + piège fuseau du timer si
constaté). Suite verte, UN commit, hash EN PREMIÈRE LIGNE du rapport,
`git status` propre.

**Hors périmètre** : opt-out par séjour (V2-31), suivi d'ouverture, pièces
jointes, envoi automatique de la vitrine (n'a pas de sens), V2-23e.
