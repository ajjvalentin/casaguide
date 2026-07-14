#!/usr/bin/env bash
# ============================================================================
# CasaGuide — déploiement en UNE commande (M-11)
# ----------------------------------------------------------------------------
# Idempotent. Exécuté par l'utilisateur applicatif `casaguide` sur le VPS :
#   /opt/casaguide/deploy.sh
#
# Étapes : git pull → pip (si requirements changé) → migrations SQL (idempotentes)
# → seed (idempotent) → version d'assets (SHA git, cache-busting) → restart du
# service systemd → healthcheck (/health, /docs, / → 200). Voir docs/deploiement.md.
# ============================================================================
set -euo pipefail

APP_DIR="/opt/casaguide"
BACKEND="$APP_DIR/backend"
VENV="$APP_DIR/.venv"
BRANCH="${CASAGUIDE_BRANCH:-main}"
DB="${CASAGUIDE_DB:-postgresql:///casaguide}"
SERVICE="casaguide"
PORT="${CASAGUIDE_PORT:-8000}"

cd "$APP_DIR"
log() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 1. Récupérer le code (empreinte requirements avant/après pour décider du pip)
log "git pull (origin/$BRANCH)"
req_before="$(sha1sum "$BACKEND/requirements.txt" 2>/dev/null | awk '{print $1}')"
git fetch --quiet origin "$BRANCH"
git checkout --quiet "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"
req_after="$(sha1sum "$BACKEND/requirements.txt" | awk '{print $1}')"

# 2. Dépendances Python (seulement si requirements a changé, ou venv absent)
if [ ! -d "$VENV" ]; then
  log "création du venv"
  python3 -m venv "$VENV"
  req_before=""            # venv neuf → forcer l'installation
fi
if [ "$req_before" != "$req_after" ]; then
  log "pip install -r requirements.txt (requirements modifié)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$BACKEND/requirements.txt"
else
  log "requirements inchangé — pip ignoré"
fi

# 3. Migrations SQL (idempotentes, IF NOT EXISTS) + seed (idempotent)
log "migrations + seed"
for f in "$APP_DIR"/db/migrations/*.sql; do
  [ -e "$f" ] || continue
  psql "$DB" -v ON_ERROR_STOP=1 -q -f "$f"
done
psql "$DB" -v ON_ERROR_STOP=1 -q -f "$APP_DIR/db/seed.sql"

# 4. Version des assets (cache-busting) : SHA git court → EnvironmentFile du service.
#    L'API l'expose en ?v=<sha> sur les balises JS/CSS et l'injecte dans le nom des
#    caches du service worker → chaque déploiement invalide les caches navigateur.
SHA="$(git rev-parse --short HEAD)"
printf 'CASAGUIDE_ASSET_VERSION=%s\n' "$SHA" > "$BACKEND/.env.deploy"
log "version des assets = $SHA"

# 5. Redémarrage du service (sudoers autorise ce restart sans mot de passe)
log "restart $SERVICE"
sudo systemctl restart "$SERVICE"

# 6. Healthcheck : l'API doit répondre 200 sur /health, /docs et /
log "healthcheck"
ok=0
for _ in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 1
done
[ "$ok" = "1" ] || die "l'API ne répond pas sur /health après 15 s (voir: journalctl -u $SERVICE -n 50)"
for path in /health /docs /; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$path" || true)"
  [ "$code" = "200" ] && printf '  %s → %s\n' "$path" "$code" \
                       || die "$path → $code (attendu 200)"
done

log "déploiement OK — version $SHA"
