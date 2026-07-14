#!/usr/bin/env bash
# ============================================================================
# CasaGuide — sauvegarde nocturne (M-11)
# ----------------------------------------------------------------------------
# Dump PostgreSQL (format custom, compressé) + archive des médias, avec rotation.
# Déclenché par casaguide-backup.timer (03:30). Manuel : ops/casaguide-backup.sh
# Restauration : ops/casaguide-restore.sh (procédure dans docs/deploiement.md).
# ============================================================================
set -euo pipefail

BACKUP_DIR="${CASAGUIDE_BACKUP_DIR:-/opt/casaguide/backups}"
MEDIA_DIR="${MEDIA_ROOT:-/opt/casaguide/backend/var/media}"
DB="${CASAGUIDE_DB:-postgresql:///casaguide}"
KEEP_DAYS="${CASAGUIDE_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

# 1. Base de données — format custom (-Fc) : restauration sélective possible.
pg_dump "$DB" -Fc -f "$BACKUP_DIR/casaguide-$STAMP.dump"

# 2. Médias (photos/PDF des sections). Copie tar si le répertoire existe.
if [ -d "$MEDIA_DIR" ]; then
  tar -czf "$BACKUP_DIR/media-$STAMP.tar.gz" \
      -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")"
fi

# 3. Rotation : supprimer les sauvegardes de plus de KEEP_DAYS jours.
find "$BACKUP_DIR" -type f -name 'casaguide-*.dump'  -mtime "+$KEEP_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'media-*.tar.gz'    -mtime "+$KEEP_DAYS" -delete

echo "Sauvegarde OK : $BACKUP_DIR/casaguide-$STAMP.dump"
