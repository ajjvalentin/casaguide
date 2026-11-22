# Supervision & watchdog Holaguia (OPS-3)

Runbook de supervision. Né de l'**incident du 01/09** : `holaguia.com` injoignable
~36 min pour le propriétaire pendant qu'il approuvait des fiches, et à chaud
personne ne savait si le serveur était tombé ou si c'était la box. La leçon, quelle
qu'ait été la cause : **voir de l'extérieur**, et **guérir seul** quand c'est bien le
serveur.

Trois couches, du plus proche au plus lointain :

1. **`/health` qui dit vrai** — une sonde profonde et bon marché dans l'API.
2. **Watchdog local** — un timer systemd qui redémarre le service gelé et alerte André.
3. **Supervision externe** — UptimeRobot, l'œil depuis Internet (ce document, §3).

---

## 1. `/health` — la sonde profonde

`GET https://holaguia.com/health` (public, sans authentification, appelable chaque
minute) :

- fait un `SELECT 1` sur la base → **503** avec la cause en clair si la base est
  injoignable (jamais le DSN ni un secret : seulement le type d'erreur) ;
- renvoie la **version déployée** (SHA des assets) et un **horodatage UTC**.

Réponse saine (200) :

```json
{"status": "ok", "version": "a4900c2", "time": "2026-09-01T10:12:04.512+00:00"}
```

Base injoignable (503) :

```json
{"status": "error", "reason": "database: OperationalError", "version": "a4900c2", "time": "…"}
```

Le mot-clé **`ok`** dans le corps est ce que la supervision externe surveille (§3) —
ne pas le renommer.

---

## 2. Watchdog local — le service gelé

`ops/watchdog.py`, déclenché **chaque minute** par `casaguide-watchdog.timer`.

- Sonde `/health` **par Caddy** (`https://holaguia.com/health`, le chemin réel du
  client — Caddy compris), délai court.
- Après **3 échecs consécutifs** → `systemctl restart casaguide` (et si **Caddy**
  lui-même ne répond pas localement → `restart caddy`).
- **Cooldown de 10 min** après un redémarrage : jamais de boucle. Compteur remis à
  zéro au premier succès.
- **Email à André** après un redémarrage (mailer Infomaniak existant) : « Holaguia
  s'est relancé seul à 12:03 UTC : 3 échecs (…) ». La panne se lit dans la boîte.

Journal type (`journalctl -u casaguide-watchdog`) :

```
watchdog : 3 échecs consécutifs /health (503 database: OperationalError) → casaguide redémarré
watchdog : alerte de redémarrage envoyée à andre@…
```

### Configuration

Dans `/opt/casaguide/backend/.env` (jamais committé) :

```
CASAGUIDE_OPS_EMAIL=andre@exemple.com     # destinataire des alertes (sinon : journalisé seulement)
```

Réglages facultatifs (défauts entre parenthèses) : `CASAGUIDE_WATCHDOG_THRESHOLD`
(3), `CASAGUIDE_WATCHDOG_COOLDOWN_S` (600), `CASAGUIDE_WATCHDOG_TIMEOUT_S` (8),
`CASAGUIDE_WATCHDOG_URL` (`https://holaguia.com/health`),
`CASAGUIDE_WATCHDOG_CADDY_URL` (`http://127.0.0.1:80/`),
`CASAGUIDE_WATCHDOG_STATE` (`/run/casaguide-watchdog.json`).

### Installation

`deploy.sh` synchronise les unités (`sync_systemd_units`) : il copie
`ops/casaguide-watchdog.{service,timer}` vers `/etc/systemd/system/`, recharge
systemd et active le timer — **si** il dispose du sudo non-interactif. Le sudoers
applicatif étant restreint (`systemctl restart casaguide` seul), `deploy.sh` affiche
sinon les commandes à lancer **une fois** à la main :

```bash
sudo cp /opt/casaguide/ops/*.service /opt/casaguide/ops/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now casaguide-watchdog.timer
```

> Le watchdog tourne en **root** (il doit pouvoir redémarrer `casaguide` ET `caddy`).

Vérifier : `systemctl list-timers | grep watchdog`. Test manuel (root) :
`/opt/casaguide/.venv/bin/python /opt/casaguide/ops/watchdog.py`.

### Juge de paix (gel simulé)

```bash
pgrep -af 'uvicorn'                 # repérer le PID
sudo kill -STOP <pid>              # geler uvicorn (ni mort ni sain : gelé)
```

En ≤ 3-4 min : le journal du watchdog signale 3 échecs, `casaguide` est redémarré,
un email arrive. (`sudo kill -CONT <pid>` pour dégeler si le restart n'a pas déjà
recréé le process.)

---

## 3. Supervision externe (UptimeRobot) — le vrai œil

Le watchdog local ne voit pas une panne qui l'empêcherait lui-même de tourner
(serveur éteint, réseau coupé). Il faut un **œil depuis Internet**. UptimeRobot
gratuit suffit (50 moniteurs, intervalle 5 min, alertes email ; bonus : expiration
TLS et domaine). **La création du compte et la configuration sont le geste d'André**,
pas celui de Code. Dix minutes, pas à pas :

1. **Compte** — créer un compte gratuit sur https://uptimerobot.com (email d'André).
   Confirmer l'email.

2. **Contact d'alerte** — *My Settings → Alert Contacts* : vérifier que l'email
   d'André est bien un contact actif (il l'est par défaut). Optionnel : ajouter
   l'app mobile UptimeRobot (notification push) — *Add Alert Contact → Mobile*.

3. **Moniteur `/health`** — *Dashboard → + New Monitor* :
   - **Monitor Type** : `HTTP(s)` — mais on veut vérifier le CONTENU, donc choisir
     **`Keyword`** si proposé (sinon HTTP(s), puis activer le mot-clé plus bas).
   - **Friendly Name** : `Holaguia /health`.
   - **URL (or IP)** : `https://holaguia.com/health`.
   - **Keyword type** : `exists` (alerte si le mot-clé **manque**).
   - **Keyword value** : `ok` (présent dans `{"status": "ok", …}`).
     Ainsi un **503** — base KO, corps `{"status": "error", …}` — déclenche l'alerte
     alors même que le serveur HTTP répond : on surveille la *santé*, pas juste le port.
   - **Monitoring Interval** : `5 minutes` (offre gratuite ; 1 min sur les offres
     payantes si un jour besoin).
   - **Alert Contacts To Notify** : cocher l'email d'André (+ l'app mobile si ajoutée).
   - **Create Monitor**.

4. **Certificat TLS & domaine** — dans les réglages du moniteur (ou *Settings →
   SSL*), activer **SSL/TLS certificate expiry** (alerte ~30 j avant expiration) et,
   si disponible, **Domain expiry**. La même sonde surveille donc HTTPS + validité du
   certificat Let's Encrypt et l'échéance de `holaguia.com`.

5. **Tableau de bord vert** — après quelques minutes, le moniteur doit être **Up**
   (vert). C'est la ligne de base.

6. **Test d'alerte** — deux façons de provoquer une notification pour vérifier la
   chaîne :
   - *Dashboard → le moniteur → Pause* pendant ~2 min puis *Resume* (certaines
     offres envoient un « paused/started ») ; **ou**
   - la façon franche : sur le serveur, `sudo systemctl stop casaguide` 2 min → le
     `/health` renvoie 502/503, le mot-clé `ok` disparaît → UptimeRobot alerte ;
     `sudo systemctl start casaguide` pour rétablir. (Le watchdog local le
     relancerait de toute façon en ≤ 4 min.)

À la première alerte reçue (email + push), la boucle est bouclée : **la prochaine
fois que le site « ne charge pas », on saura en trente secondes de quel côté est la
panne** — et, la plupart du temps, le watchdog l'aura déjà relancé.

---

### Où regarder quand ça sonne

| Symptôme | Commande |
|---|---|
| UptimeRobot alerte, watchdog silencieux | `curl -s https://holaguia.com/health` (depuis ailleurs) ; probable réseau/box côté serveur |
| Watchdog a redémarré (email reçu) | `journalctl -u casaguide-watchdog -n 50` puis `journalctl -u casaguide -n 100` |
| `/health` = 503 « database » | `systemctl status postgresql` ; `journalctl -u postgresql -n 50` |
| Caddy muet | `systemctl status caddy` ; `journalctl -u caddy -n 50` |

---

## 4. Journal des modifications (V2-92)

Chaque modification du contenu visible d'un guide est **datée, rattachée à sa source
et réversible** : table `content_changes` (migration 043), écrite par le déclencheur
PostgreSQL `trg_pois_journal` — jamais par le code applicatif, si bien qu'aucun chemin
d'écriture sur `pois` ne peut y échapper.

### Ce qui est tracé

| Événement | Ligne | Condition |
|---|---|---|
| Changement de statut | `status` (`{status, status_by, status_at}` avant/après) | toujours |
| Champ modifié | `update`, une ligne par champ, `old_value`/`new_value` | fiche retenue (`approved`/`edited`) avant OU après, valeur réellement changée |
| Création | `create` + instantané complet | fiche née retenue (lieu saisi à la main) |
| Suppression | `delete` + instantané complet (de quoi la recréer) | fiche retenue |
| Annulation | `revert`, `reverts_id` = la ligne annulée | toujours |

Champs suivis : ceux de `enrich.locks.LOCKABLE_FIELDS` (éditables + `geom`, noté
`{"lat","lon"}`). **Pas** tracés : distances, `fetched_at`, `updated_at`,
`completion_meta`, `human_fields`, `status_by`, `status_at`, ni les colonnes
de contrôle `last_checked_at` / `source_state` / `source_state_since` (V2-93, bruit). Une fiche `suggested`
rafraîchie à chaque moisson n'écrit **rien**. Hors périmètre : traductions
(dérivées), sections (écrites par le seul propriétaire), `area_facts` (mission à
venir : leur rafraîchissement passe par des appels IA) et
**jamais** `property_secrets`. Supprimer un logement supprime son journal.

### Les quatre auteurs (`actor`)

Le journal établit **qui** a écrit — y compris à des fins fiscales : le contenu est
modifié par le système ou par le propriétaire, jamais par l'exploitant de sa propre
initiative.

| Auteur | Qui | Posé par |
|---|---|---|
| `system` | le pipeline, les tâches automatiques (défaut si rien n'est déclaré) | `journal.bind` (pipeline : + `job_id`, un `batch_id` par exécution) |
| `judge` | le juge IA (offre Guide Voyageur) | `db.apply_judge_verdict` |
| `owner` | le propriétaire (édition, création, Approuver/Rejeter) | `repo.edit_poi`, `create_manual_poi`, `set_poi_status` |
| `support` | l'exploitant, **à la demande expresse d'un propriétaire** | `ops/revert_changes.py` |

L'auteur est une variable de **transaction** (`holaguia.actor`, `holaguia.batch_id`,
`holaguia.job_id`) — jamais de session.

### Consulter

```sql
SELECT id, created_at, action, field, old_value, new_value, actor, batch_id
FROM content_changes WHERE property_id = '<uuid>' ORDER BY id DESC LIMIT 50;
```

Côté propriétaire : `GET /api/properties/{id}/changes?poi_id=&limit=&before_id=`.
L'identifiant du lot d'une exécution du pipeline figure dans la ligne de progression
`▶ Enrichissement … — job <uuid>, lot <uuid>`.

### Annuler

```bash
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --batch <uuid> --dry-run
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --batch <uuid>
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --change <id>
```

`--dry-run` joue exactement la même logique puis ROLLBACK : le plan affiché est celui
qui serait appliqué. Règles : seules les lignes `system`/`judge` s'annulent (une saisie
`owner` est refusée) ; **rien n'est restauré** si la valeur a changé depuis
(`value_changed`) ou si le champ est passé dans `human_fields` (`human_locked`) ; une
ligne ne s'annule qu'une fois (`already_reverted`, garanti par un index unique) ; un
lot s'annule du plus récent au plus ancien, en entier, conflits compris. Annuler une
suppression recrée la fiche avec son identifiant d'origine. Code de sortie 4 s'il
reste des conflits ou refus.

---

## 5. Revérification nocturne des lieux (V2-93)

Un horaire corrigé dans OpenStreetMap atteint désormais un guide publié. Chaque nuit
(`casaguide-refresh-pois.timer`, 02:15 Europe/Madrid), `ops/refresh_pois.py` relit
chaque lieu retenu **auprès de sa source d'origine, par identifiant**, et reporte les
trois champs simples. Aucun appel IA (aucune ligne `api_costs`).

### Ce qui est revérifié

| | Périmètre |
|---|---|
| Logements | `status='published'`, hors `guest_guide` (instantané acheté à vie) et hors `demo` |
| Fiches | `approved` / `edited`, `source` = `osm` ou `overture`, `source_ref` non NULL |
| Champs | `phone`, `website`, `opening_hours` — rien d'autre (nom, adresse, position, description, coup de cœur intouchés) |
| Non revérifiables | fiches `web`, `claude`, `owner` (aucun identifiant à réinterroger) : comptées, jamais touchées |

Un logement est **dû** quand une de ses fiches n'a jamais été contrôlée ou l'a été il y
a plus de `CASAGUIDE_REFRESH_MAX_AGE_DAYS` (7 j) : le timer tourne chaque nuit mais ne
traite que les dus, la charge s'étale d'elle-même.

**Sources.** OSM : Overpass `node(id:…)`/`way(id:…)`/`relation(id:…)`, paquets de
`CASAGUIDE_REFRESH_BATCH_SIZE` (100 — un logement typique de ~125 fiches en deux
requêtes ; une requête par identifiant ne coûte presque rien au serveur, la borne tient
à la taille de la réponse et au coût d'un paquet perdu), miroirs et backoff habituels.
Une réponse 200 est **refusée** (miroir suivant, puis échec du logement s'il n'y en a
aucune d'utilisable) si elle porte une erreur d'exécution (`remark` : résultat partiel)
ou si les données du miroir ont plus de `CASAGUIDE_REFRESH_MAX_SOURCE_LAG_HOURS` (48 h)
de retard (`osm3s.timestamp_osm_base`) : un miroir périmé fait passer un lieu récent
pour disparu et un lieu supprimé pour présent — constaté le 22/11/2026 sur un des
miroirs publics. Overture : relu
par identifiant GERS **dans le cache de zone local uniquement** — hors zone en cache,
la fiche est « non contrôlée » (jamais de lecture S3).

### La règle de report (`enrich/locks.plan_refresh`)

Pour chaque champ : verrouillé par un humain (`human_fields`) ⇒ rien ; source muette ⇒
rien (une source muette n'efface jamais) ; valeur identique ⇒ rien ; sinon ⇒ la valeur
de la source. Un téléphone venu d'Overture ou de la complétion web sur une fiche OSM est
remplacé si OSM en donne un aujourd'hui, conservé si OSM reste muet. **Même règle** au
ré-enrichissement à la demande (`db.upsert_pois`). Un champ **vidé** par le propriétaire
est verrouillé : le système ne le remplit plus.

### Lieu fermé ou disparu

**Constaté, jamais retiré** : `pois.source_state` = `closed` (OSM `is_closed` : fermé,
désaffecté, date de fin passée…) ou `missing` (l'élément n'existe plus) ;
`source_state_since` = depuis quand ; la fiche reste `approved` et servie. Aucun champ
n'est reporté depuis un élément fermé ou disparu. Le retrait passe par le juge et le
propriétaire (V2-94).

```sql
SELECT name, source_ref, source_state, source_state_since FROM pois
WHERE property_id = '<uuid>' AND source_state IN ('closed', 'missing');
```

### Lire un passage

Un job `enrichment_jobs` par logement (`trigger='refresh'`, **hors quota** du
propriétaire) ; bilan dans `steps.refresh` : `checked`, `modified` (fiches),
`modified_by_field`, `closed`, `missing`, `locked_skipped` (la source dit autre chose mais
le champ est verrouillé), `not_recheckable`, `not_checked` (Overture hors cache). Chaque
modification est une ligne `update` du journal (§ 4), auteur `system`, avec le lot et
le job du passage. `last_checked_at` est posé sur toute fiche contrôlée ; une écriture
qui ne change que ces colonnes ne bouge pas `updated_at`.

```bash
# Passage à blanc (lectures réseau réelles, AUCUNE écriture, bilan identique) :
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/refresh_pois.py --dry-run --max-properties 3
# Un logement, à la main (quel que soit l'âge de ses contrôles) :
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/refresh_pois.py --property <uuid>
```

**Échec.** Un échec réseau sur un logement n'arrête pas les suivants : son job passe
`failed` avec la cause, rien n'est écrit sur ses fiches (`last_checked_at` non posé), il
est repris la nuit suivante. Code de sortie 1 s'il y a eu au moins un échec (visible
dans `systemctl status casaguide-refresh-pois`).

### Annuler un passage — et pourquoi la valeur revient

```bash
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --batch <lot> --dry-run
/opt/casaguide/.venv/bin/python /opt/casaguide/ops/revert_changes.py --batch <lot>
```

Le retour arrière restaure l'ancienne valeur, **mais le passage suivant la remodifie** :
c'est attendu, la source dit toujours la nouvelle valeur et le champ n'est pas
verrouillé. Pour figer l'ancienne valeur, c'est au **propriétaire** de la saisir (son
édition verrouille le champ). Un retour arrière sert donc à réparer une erreur
ponctuelle (source vandalisée, bug), pas à refuser durablement une donnée de la source.

