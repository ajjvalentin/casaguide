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

# 1. Récupérer le code
log "git pull (origin/$BRANCH)"
git fetch --quiet origin "$BRANCH"
git checkout --quiet "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"

# 2. Dépendances Python : (ré)installer dès que le venv ne reflète pas
#    requirements.txt. On compare le hash du requirements COURANT à un « stamp »
#    écrit dans le venv APRÈS le dernier pip réussi — et non les versions
#    avant/après le pull. Un pull no-op (code déjà présent : pull manuel antérieur
#    ou déploiement interrompu) sur un venv périmé déclenche donc quand même
#    l'install. (Bug vécu V2-05b : dépendance `stripe` ajoutée mais « pip ignoré »
#    car le pull ne changeait plus requirements → ModuleNotFoundError au restart,
#    API down.)
if [ ! -d "$VENV" ]; then
  log "création du venv"
  python3 -m venv "$VENV"
fi
STAMP="$VENV/.requirements.sha1"
req_hash="$(sha1sum "$BACKEND/requirements.txt" | awk '{print $1}')"
installed_hash="$(cat "$STAMP" 2>/dev/null || true)"
if [ "$installed_hash" != "$req_hash" ]; then
  log "pip install -r requirements.txt (venv non aligné : ${installed_hash:-aucun} → $req_hash)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$BACKEND/requirements.txt"
  printf '%s\n' "$req_hash" > "$STAMP"   # stamp uniquement après un pip réussi (set -e)
else
  log "requirements déjà installé ($req_hash) — pip ignoré"
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
if [ "$ok" != "1" ]; then
  printf '\033[1;31m✗ l'\''API ne répond pas sur /health après 15 s.\033[0m\n' >&2
  # Garde-fou : la panne la plus fréquente au restart est une dépendance Python
  # manquante (import qui casse uvicorn au démarrage → /health jamais 200). On
  # inspecte le journal et on propose systématiquement la remise à niveau du venv.
  logs="$(journalctl -u "$SERVICE" -n 50 --no-pager 2>/dev/null || true)"
  fix="\"$VENV/bin/pip\" install -r \"$BACKEND/requirements.txt\" && sudo systemctl restart $SERVICE"
  if printf '%s' "$logs" | grep -qiE 'ModuleNotFoundError|ImportError'; then
    printf '\033[1;33m➤ Cause détectée dans le journal : dépendance Python manquante.\n  Corrige avec :\n    %s\033[0m\n' "$fix" >&2
  else
    printf '\033[1;33m➤ Piste : si une dépendance a été ajoutée, remets le venv à niveau :\n    %s\033[0m\n' "$fix" >&2
  fi
  die "healthcheck /health échoué (journal : journalctl -u $SERVICE -n 50)"
fi
for path in /health /docs /; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$path" || true)"
  [ "$code" = "200" ] && printf '  %s → %s\n' "$path" "$code" \
                       || die "$path → $code (attendu 200)"
done

log "déploiement OK — version $SHA"
