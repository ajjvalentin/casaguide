# Déploiement production CasaGuide (M-11)

Runbook du déploiement sur VPS Infomaniak (Ubuntu 24.04, UE). Architecture
volontairement **simple, sans Docker** pour le MVP : PostgreSQL local, API
uvicorn gérée par systemd, Caddy en frontal.

```
Internet ──▶ Caddy (:80/:443, TLS)  ──▶ uvicorn 127.0.0.1:8000 (systemd)
                                              │
                                              ▼
                                    PostgreSQL 16 + PostGIS (socket local)
```

- **Serveur** : `ubuntu@179.237.85.250` (sudo sans mot de passe).
- **Utilisateur applicatif** : `casaguide` (non-root), code dans `/opt/casaguide`.
- **Dépôt** : `github.com/ajjvalentin/casaguide` (privé, deploy key en lecture seule).
- **URL de production** : `https://holaguia.com` (marque **Holaguia**, adresse
  canonique — M-28, HTTPS de confiance Let's Encrypt). `www.holaguia.com`,
  `holaguia.ch`, l'ancien domaine technique `guide.holaquetalimmo.es` (M-27) et
  l'ancienne adresse par IP `http(s)://179.237.85.250` redirigent tous en **301
  permanent** vers `holaguia.com` (liens/QR déjà partagés préservés). `holaguia.es`
  suivra dès que la délégation .es sera publiée (bloc prêt-commenté dans le
  Caddyfile ; cf. §4).

---

## 1. Provisionnement initial (une fois)

Tout se fait depuis `ssh -i ~/.ssh/casaguide_vps ubuntu@179.237.85.250`.

### 1.1 Système à jour + sécurité de base

```bash
sudo apt-get update && sudo apt-get -y upgrade        # applique les MàJ en attente
sudo apt-get -y install ufw fail2ban unattended-upgrades curl git

# Mises à jour de sécurité automatiques
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

# Pare-feu : SSH + HTTP + HTTPS uniquement
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw --force enable

# fail2ban : protection SSH (jail sshd activée par défaut sur Ubuntu 24.04)
sudo systemctl enable --now fail2ban
```

### 1.2 Utilisateur applicatif

```bash
sudo adduser --system --group --home /opt/casaguide --shell /bin/bash casaguide
```

### 1.3 PostgreSQL 16 + PostGIS (accès local uniquement)

```bash
sudo apt-get -y install postgresql-16 postgresql-16-postgis-3 postgresql-client-16
```

PostgreSQL n'écoute que sur le socket local par défaut (`listen_addresses =
'localhost'`) — **jamais exposé**. Rôle + base (peer auth via l'utilisateur
système `casaguide`) :

```bash
sudo -u postgres createuser --createdb casaguide   # CREATEDB : nécessaire aux restaurations témoin
sudo -u postgres createdb --owner=casaguide casaguide
sudo -u postgres psql -d casaguide -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

La DSN applicative est `postgresql:///casaguide` (socket local, peer auth : le
service tourne sous l'utilisateur `casaguide` → mappé sur le rôle `casaguide`,
aucun mot de passe en base ni en clair).

### 1.4 Clé de déploiement GitHub (lecture seule)

Générer une paire dédiée sur le serveur, en tant que `casaguide` :

```bash
sudo -u casaguide ssh-keygen -t ed25519 -N '' -f /opt/casaguide/.ssh/id_ed25519 -C casaguide-vps
sudo -u casaguide cat /opt/casaguide/.ssh/id_ed25519.pub
```

➡ **Ajouter cette clé publique sur GitHub** : dépôt `casaguide` → *Settings →
Deploy keys → Add deploy key* (laisser « Allow write access » **décoché**).

Fiabiliser l'hôte GitHub :

```bash
sudo -u casaguide bash -c 'ssh-keyscan github.com >> /opt/casaguide/.ssh/known_hosts'
```

### 1.5 Clone du dépôt

```bash
sudo -u casaguide git clone git@github.com:ajjvalentin/casaguide.git /opt/casaguide/repo-tmp
# /opt/casaguide est le home ; on veut le code À la racine → on déplace le contenu
sudo -u casaguide bash -c 'shopt -s dotglob && mv /opt/casaguide/repo-tmp/* /opt/casaguide/ && rmdir /opt/casaguide/repo-tmp'
```

> Alternative propre : cloner directement dans `/opt/casaguide` s'il est vide
> (`git clone … /opt/casaguide`). Le home ayant déjà `.ssh`, on passe par un
> sous-dossier temporaire.

### 1.6 Python : venv + dépendances

```bash
sudo apt-get -y install python3-venv python3-pip
sudo -u casaguide python3 -m venv /opt/casaguide/.venv
sudo -u casaguide /opt/casaguide/.venv/bin/pip install --upgrade pip
sudo -u casaguide /opt/casaguide/.venv/bin/pip install -r /opt/casaguide/backend/requirements.txt
```

### 1.7 Schéma + seed + migrations

```bash
cd /opt/casaguide
sudo -u casaguide psql -d casaguide -v ON_ERROR_STOP=1 -f db/schema.sql
sudo -u casaguide psql -d casaguide -v ON_ERROR_STOP=1 -f db/seed.sql
for f in db/migrations/*.sql; do sudo -u casaguide psql -d casaguide -v ON_ERROR_STOP=1 -f "$f"; done
```

### 1.8 Configuration `backend/.env`

```bash
sudo -u casaguide cp /opt/casaguide/backend/.env.example /opt/casaguide/backend/.env
```

Éditer `/opt/casaguide/backend/.env` avec les valeurs de production :

```ini
CASAGUIDE_DB=postgresql:///casaguide
CASAGUIDE_JWT_SECRET=<openssl rand -hex 32 — généré sur le serveur>
CASAGUIDE_SECRET_KEY=<openssl rand -hex 32 — généré sur le serveur>
ANTHROPIC_API_KEY=REMPLACER_A_LA_MAIN          # ← fourni séparément par l'exploitant
MEDIA_ROOT=/opt/casaguide/backend/var/media
CASAGUIDE_PUBLIC_BASE_URL=https://holaguia.com  # QR imprimables / liens (M-28)
CASAGUIDE_CORS_ORIGINS=https://holaguia.com
```

Les clés JWT et SECRET sont générées **sur le serveur** (`openssl rand -hex 32`),
jamais committées. `ANTHROPIC_API_KEY` reste un **placeholder** tant que
l'exploitant ne l'a pas renseignée à la main (l'enrichissement IA est le seul à
en dépendre ; le reste de l'API fonctionne sans).

> `.env` (permissions 600, propriété `casaguide`) et `.env.deploy` (écrit par
> `deploy.sh`, contient `CASAGUIDE_ASSET_VERSION`) sont **hors dépôt** (.gitignore).

### 1.9 Service systemd (uvicorn)

```bash
sudo cp /opt/casaguide/ops/casaguide.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now casaguide
sudo systemctl status casaguide --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health   # → 200
```

Autoriser `casaguide` à redémarrer **uniquement** son service sans mot de passe
(nécessaire à `deploy.sh`) :

```bash
echo 'casaguide ALL=(root) NOPASSWD: /usr/bin/systemctl restart casaguide, /usr/bin/systemctl reload caddy' \
  | sudo tee /etc/sudoers.d/casaguide
sudo chmod 440 /etc/sudoers.d/casaguide
sudo visudo -c
```

### 1.10 Caddy (reverse proxy)

```bash
sudo apt-get -y install debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get -y install caddy

sudo cp /opt/casaguide/ops/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Vérifier depuis l'extérieur : `curl -sI http://179.237.85.250/health`.

> **HTTPS auto-signé** : `https://179.237.85.250` répond avec un certificat
> `tls internal` (avertissement navigateur attendu, sans domaine). Le mode PWA
> hors-ligne complet (service worker) nécessite un HTTPS **de confiance** →
> arrivera avec le domaine + Let's Encrypt (§4).

### 1.11 Sauvegardes nocturnes

```bash
sudo cp /opt/casaguide/ops/casaguide-backup.service /etc/systemd/system/
sudo cp /opt/casaguide/ops/casaguide-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now casaguide-backup.timer
sudo systemctl list-timers casaguide-backup.timer --no-pager
sudo systemctl start casaguide-backup.service      # test immédiat
ls -lh /opt/casaguide/backups/
```

---

## 2. Déploiement courant (à chaque mise à jour)

**Une seule commande**, en tant que `casaguide` :

```bash
sudo -u casaguide /opt/casaguide/deploy.sh
```

`deploy.sh` (idempotent) : `git pull` → `pip install` **si** `requirements.txt`
a changé → migrations SQL nouvelles (idempotentes) → `seed.sql` (idempotent) →
calcule la **version d'assets** (SHA git court) écrite dans `backend/.env.deploy`
→ `systemctl restart casaguide` → **healthcheck** (`/health`, `/docs`, `/` → 200,
échoue bruyamment sinon).

---

## 3. Versionnage des assets (cache-busting automatique)

Dette connue traitée : plus besoin de `Cmd+Option+R` ni de bump manuel du
service worker. À chaque déploiement, `deploy.sh` stampe `CASAGUIDE_ASSET_VERSION`
= SHA git court. L'API (`backend/api/assets.py`) :

1. sert `index.html` et les pages guide/staff avec `?v=<sha>` sur les balises
   JS/CSS locales (busting positif) ;
2. sert tous les fichiers statiques du front en `Cache-Control: no-cache`
   (revalidation ETag → un module ES modifié est toujours re-téléchargé, même
   les imports relatifs sans `?v`) ;
3. injecte `<sha>` dans le **nom des caches du service worker** (`/guide/sw.js`,
   placeholder `__ASSET_VERSION__`) → chaque déploiement réactive le SW et purge
   les anciens caches.

En local/dev (variable absente), la version vaut `dev` (comportement stable).

---

## 4. Bascule vers un nom de domaine (Let's Encrypt) — FAIT le 17/07/2026 (M-27)

**Domaine technique provisoire** : `guide.holaquetalimmo.es` (sous-domaine créé
chez Infomaniak → `A` vers `179.237.85.250` ; **aucun** autre enregistrement du
domaine touché — le site immobilier `holaquetalimmo.es` vit dessus). Le jour où la
**marque définitive** sera choisie, elle redirigera ici de la même façon (le bloc
« domaine définitif » est décrit en tête de `ops/Caddyfile`).

Déroulé réellement appliqué (le runbook théorique tenait, à deux améliorations près
notées ci-dessous) :

1. **DNS** : vérifier la propagation avant tout — `dig +short guide.holaquetalimmo.es`
   doit renvoyer `179.237.85.250` (confirmé aussi via `@8.8.8.8` / `@1.1.1.1`).
2. **Caddyfile** (`ops/Caddyfile`, copié dans `/etc/caddy/Caddyfile`) :
   - bloc de site `guide.holaquetalimmo.es { … }` → Caddy obtient et renouvelle le
     cert Let's Encrypt **automatiquement** (challenge `tls-alpn-01`), et active la
     redirection **http→https** automatiquement (plus besoin de la gérer à la main) ;
   - `email av@weemo.ch` dans le bloc global (contact Let's Encrypt) ;
   - `auto_https disable_redirects` **supprimé** ;
   - HSTS **activé** dans le snippet `(securite)` : `max-age=15552000; includeSubDomains`
     (180 j pour commencer, **sans preload** — le preload est irréversible) ;
   - **[écart au runbook, amélioration]** l'ancienne adresse par IP est **conservée**
     en **redir 301** (`http://` **et** `https://179.237.85.250 { tls internal }`) vers
     `https://guide.holaquetalimmo.es{uri}` : les liens/QR déjà partagés (l'ancien
     `CASAGUIDE_PUBLIC_BASE_URL`) continuent de mener au guide sans page morte.
   - Validation + normalisation : `sudo caddy validate --config … --adapter caddyfile`
     puis `sudo caddy fmt --overwrite /etc/caddy/Caddyfile` (le dépôt est resynchronisé
     sur cette version formatée).
3. **Reload + obtention du cert** : `sudo systemctl reload caddy`, puis vérifier les
   logs (`sudo journalctl -u caddy`) : `certificate obtained successfully`. Un
   avertissement `failed to install root certificate` peut apparaître : il concerne
   **uniquement** la CA locale du `tls internal` (IP), pas Let's Encrypt — cosmétique.
4. **`backend/.env`** : `CASAGUIDE_PUBLIC_BASE_URL=https://guide.holaquetalimmo.es`
   et `CASAGUIDE_CORS_ORIGINS=https://guide.holaquetalimmo.es`, puis
   `sudo systemctl restart casaguide`. (Au passage, un doublon de la ligne
   `CASAGUIDE_PUBLIC_BASE_URL` a été nettoyé ; permissions `600` / propriété
   `casaguide` reconfirmées.) Les futurs QR PDF et liens copiés portent le domaine.
5. **Validation curl** (toutes vertes le 17/07/2026) :
   - cert : `openssl s_client -connect guide.holaquetalimmo.es:443 -servername
     guide.holaquetalimmo.es | openssl x509 -noout -subject -issuer -dates` →
     `issuer=Let's Encrypt`, `subject=CN=guide.holaquetalimmo.es`, `notAfter=Oct 15 2026` ;
   - `curl -s -w '%{http_code} ssl_verify=%{ssl_verify_result}' https://guide.holaquetalimmo.es/health`
     → `200 ssl_verify=0` (chaîne de confiance OK) ; `/docs` → `200` ;
   - en-têtes sur `/` : `strict-transport-security`, `x-content-type-options`,
     `x-frame-options`, `referrer-policy` présents, `Server` masqué ;
   - guide publié réel : `GET /g/{token}` → `200` (titre `Villa Ballarin — Guide du
     logement`), `/g/{token}/data` → `200 application/json; charset=utf-8` ;
   - redirections : `http://guide.holaquetalimmo.es/…` → `308` vers `https://…` ;
     `http(s)://179.237.85.250/…` → `301` vers `https://guide.holaquetalimmo.es{uri}`.

**Renouvellement automatique** : géré **en interne par Caddy** (pas de cron/timer à
prévoir) ; cert + clé + méta ACME persistés sous
`/var/lib/caddy/.local/share/caddy/certificates/…/guide.holaquetalimmo.es/`.

HTTPS de confiance → le prérequis du **mode hors-ligne PWA (service worker, M-10)**
est désormais **levé**.

### 4bis. Bascule vers la marque **Holaguia** — FAIT le 19/07/2026 (M-28)

`holaguia.com` devient l'adresse **canonique** du produit ; tout le reste y redirige
en 301. Le domaine technique `guide.holaquetalimmo.es` (M-27) devient une simple
redirection — les liens/QR déjà partagés survivent.

**DNS (Infomaniak, `A` → `179.237.85.250`)** — au moment de la bascule, RÉSOLVAIENT :
`holaguia.com`, `www.holaguia.com`, `holaguia.ch`, `guide.holaquetalimmo.es`.
**Ne résolvait PAS encore** : `holaguia.es` (délégation .es non publiée par le
registrar) → laissé **prêt-commenté** dans le Caddyfile (voir plus bas).

Déroulé appliqué :
1. **Caddyfile** (`ops/Caddyfile`) : bloc de site canonique `holaguia.com { … }`
   (reverse_proxy `127.0.0.1:8000`) ; un bloc de redir 301 groupé
   `www.holaguia.com, holaguia.ch, guide.holaquetalimmo.es` ; l'IP (http+https)
   redirige aussi vers `holaguia.com`. Chaque nom qui résout obtient son cert
   Let's Encrypt (tls-alpn-01) automatiquement.
2. **Déploiement** : `git pull` sur `/opt/casaguide`, puis
   `sudo cp /opt/casaguide/ops/Caddyfile /etc/caddy/Caddyfile`,
   `sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`,
   `sudo systemctl reload caddy`. Vérifier `journalctl -u caddy` :
   `certificate obtained successfully` pour chaque nom.
3. **`backend/.env`** : `CASAGUIDE_PUBLIC_BASE_URL=https://holaguia.com` et
   `CASAGUIDE_CORS_ORIGINS=https://holaguia.com`, puis
   `sudo systemctl restart casaguide`.
4. **Validation** : cert `subject=CN=holaguia.com`, `issuer=Let's Encrypt` ;
   `https://holaguia.com/health` → `200 ssl_verify=0` ; chaque redirection
   (`www`, `.ch`, `guide.holaquetalimmo.es`, IP) → `301` vers `https://holaguia.com`.

**`holaguia.es` — geste restant (délégation .es en attente)** : son bloc est
**prêt-commenté** en bas de `ops/Caddyfile`. Tant que `holaguia.es` ne résout pas,
le **laisser commenté** — activé, Caddy échouerait en boucle sur le challenge
tls-alpn-01 (aucun cert possible sans DNS pointant sur ce serveur). Dès que
`dig +short holaguia.es` renvoie `179.237.85.250` :

```bash
# décommenter le bloc `holaguia.es { … }` dans ops/Caddyfile, puis :
sudo cp /opt/casaguide/ops/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy   # Caddy obtient le cert .es automatiquement
```

Aucune autre modif : `.es` n'est qu'une redirection de plus, pas de changement de
`CASAGUIDE_PUBLIC_BASE_URL`.

---

## 5. Sauvegarde & restauration

- **Quoi** : `pg_dump -Fc` (base) + `tar` des médias, dans `/opt/casaguide/backups`,
  **rotation 14 jours** (`ops/casaguide-backup.sh`, timer 03:30).
- **Restauration testée** dans une base témoin (ne touche pas la production). Le
  script s'exécute **avec `sudo`** : postgis n'étant pas une extension « trusted »,
  seul le superutilisateur `postgres` peut la (re)créer ; la restauration elle-même
  se fait ensuite en tant que `casaguide` (propriétaire des objets).

```bash
sudo /opt/casaguide/ops/casaguide-restore.sh \
     /opt/casaguide/backups/casaguide-AAAAMMJJ-HHMMSS.dump
# → recrée la base « casaguide_restore_test », restaure, et affiche les compteurs
#   (section_templates, poi_categories, plans, properties, owners) pour vérification.
# Le seul avertissement attendu (COMMENT ON EXTENSION postgis / spatial_ref_sys)
# est cosmétique : l'extension pré-créée fournit déjà ces objets.
```

- **Restauration en production** (sinistre) — arrêter l'API, restaurer, redémarrer :

```bash
sudo systemctl stop casaguide
sudo /opt/casaguide/ops/casaguide-restore.sh <fichier.dump> casaguide   # demande « OUI »
# Médias : tar -xzf backups/media-AAAAMMJJ-HHMMSS.tar.gz -C /opt/casaguide/backend/
sudo systemctl start casaguide
```

---

## 6. Exploitation courante

| Besoin | Commande |
|---|---|
| État du service | `sudo systemctl status casaguide` |
| Logs applicatifs | `sudo journalctl -u casaguide -f` |
| Logs d'accès Caddy | `sudo tail -f /var/log/caddy/access.log` |
| Redémarrer l'API | `sudo systemctl restart casaguide` |
| Recharger Caddy | `sudo systemctl reload caddy` |
| Déployer | `sudo -u casaguide /opt/casaguide/deploy.sh` |
| Sauvegarde manuelle | `sudo systemctl start casaguide-backup.service` |
| Bannissements fail2ban | `sudo fail2ban-client status sshd` |
| MàJ auto en attente | `sudo unattended-upgrade --dry-run` |

---

## 7. Sécurité — points clés

- PostgreSQL **jamais exposé** (socket local, peer auth, aucun mot de passe).
- ufw : seuls 22/80/443 ouverts ; fail2ban sur SSH ; unattended-upgrades.
- Secrets applicatifs (`.env`, 600, propriété `casaguide`) hors dépôt ;
  clé de chiffrement des `property_secrets` distincte, hors base.
- Deploy key GitHub en **lecture seule**.
- `casaguide` a un sudo **restreint** (restart de son service, reload Caddy).
- Guide public : `noindex`, tokens ≥ 128 bits, secrets jamais exposés (invariants).
