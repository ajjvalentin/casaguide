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
