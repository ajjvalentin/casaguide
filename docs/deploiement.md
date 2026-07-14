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
- **URL provisoire** : `http://179.237.85.250` (et `https://` en cert auto-signé).

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
CASAGUIDE_PUBLIC_BASE_URL=http://179.237.85.250 # QR imprimables / liens (provisoire)
CASAGUIDE_CORS_ORIGINS=http://179.237.85.250
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

## 4. Bascule vers un nom de domaine (Let's Encrypt) — le jour venu

1. Faire pointer le domaine (`A`/`AAAA`) vers `179.237.85.250`.
2. Dans `/etc/caddy/Caddyfile` (cf. commentaires de `ops/Caddyfile`) : remplacer
   le bloc `http://…, https://179.237.85.250 { tls internal … }` par
   `guide.mondomaine.fr { … }` (Caddy obtient le certificat automatiquement),
   supprimer `auto_https disable_redirects` et **décommenter l'en-tête HSTS**.
3. `backend/.env` : `CASAGUIDE_PUBLIC_BASE_URL=https://guide.mondomaine.fr` et
   `CASAGUIDE_CORS_ORIGINS=https://guide.mondomaine.fr`.
4. `sudo systemctl reload caddy && sudo -u casaguide /opt/casaguide/deploy.sh`.

HTTPS de confiance → le service worker / PWA devient pleinement actif (hors-ligne).

---

## 5. Sauvegarde & restauration

- **Quoi** : `pg_dump -Fc` (base) + `tar` des médias, dans `/opt/casaguide/backups`,
  **rotation 14 jours** (`ops/casaguide-backup.sh`, timer 03:30).
- **Restauration testée** dans une base témoin (ne touche pas la production) :

```bash
sudo -u casaguide /opt/casaguide/ops/casaguide-restore.sh \
     /opt/casaguide/backups/casaguide-AAAAMMJJ-HHMMSS.dump
# → recrée la base « casaguide_restore_test », restaure, et affiche les compteurs
#   (section_templates, poi_categories, properties, owners) pour vérification.
```

- **Restauration en production** (sinistre) — arrêter l'API, restaurer, redémarrer :

```bash
sudo systemctl stop casaguide
sudo -u casaguide /opt/casaguide/ops/casaguide-restore.sh <fichier.dump> casaguide   # demande « OUI »
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
