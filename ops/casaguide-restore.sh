#!/usr/bin/env bash
# ============================================================================
# CasaGuide — restauration d'une sauvegarde (M-11)
# ----------------------------------------------------------------------------
# Restaure un dump dans une base CIBLE (par défaut une base témoin, pour tester
# sans toucher la production). Voir la procédure complète dans docs/deploiement.md.
#
# Usage :
#   ops/casaguide-restore.sh <fichier.dump> [base_cible]
# Exemples :
#   ops/casaguide-restore.sh backups/casaguide-20260715-033000.dump          # → casaguide_restore_test (témoin)
#   ops/casaguide-restore.sh backups/casaguide-20260715-033000.dump casaguide  # → PRODUCTION (danger)
# ============================================================================
set -euo pipefail

DUMP="${1:?usage: casaguide-restore.sh <fichier.dump> [base_cible]}"
TARGET_DB="${2:-casaguide_restore_test}"
[ -f "$DUMP" ] || { echo "Fichier introuvable : $DUMP" >&2; exit 1; }

if [ "$TARGET_DB" = "casaguide" ]; then
  read -r -p "⚠ Restauration dans la base de PRODUCTION 'casaguide'. Confirmer (tapez OUI) ? " ans
  [ "$ans" = "OUI" ] || { echo "Annulé."; exit 1; }
fi

echo "Restauration de $DUMP → base « $TARGET_DB »"
# (Re)créer la base cible. postgis est recréé par le dump (CREATE EXTENSION).
psql postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$TARGET_DB\";"
psql postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$TARGET_DB\" OWNER casaguide;"

# --no-owner : les objets appartiennent à l'utilisateur courant (casaguide, peer).
pg_restore --no-owner --if-exists --clean -d "$TARGET_DB" "$DUMP" || true

echo "── Vérification du contenu restauré ──"
psql "$TARGET_DB" -tAc "SELECT 'section_templates=' || count(*) FROM section_templates;"
psql "$TARGET_DB" -tAc "SELECT 'poi_categories='   || count(*) FROM poi_categories;"
psql "$TARGET_DB" -tAc "SELECT 'properties='        || count(*) FROM properties;"
psql "$TARGET_DB" -tAc "SELECT 'owners='            || count(*) FROM owners;"
echo "Restauration terminée dans « $TARGET_DB »."
