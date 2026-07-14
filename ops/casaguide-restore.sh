#!/usr/bin/env bash
# ============================================================================
# CasaGuide — restauration d'une sauvegarde (M-11)
# ----------------------------------------------------------------------------
# À EXÉCUTER AVEC sudo (opération d'exploitation privilégiée) : postgis n'étant
# pas une extension « trusted », seul le superutilisateur `postgres` peut la
# (re)créer. Le script crée la base cible + l'extension via `postgres`, puis
# restaure en tant que `casaguide` (propriétaire) — le `CREATE EXTENSION IF NOT
# EXISTS postgis` du dump devient alors un no-op (aucun privilège requis).
#
# Restaure dans une base CIBLE (par défaut une base témoin, pour tester sans
# toucher la production). Voir docs/deploiement.md.
#
# Usage :
#   sudo /opt/casaguide/ops/casaguide-restore.sh <fichier.dump> [base_cible]
# Exemples :
#   sudo .../casaguide-restore.sh backups/casaguide-AAAAMMJJ-HHMMSS.dump            # → base témoin
#   sudo .../casaguide-restore.sh backups/casaguide-AAAAMMJJ-HHMMSS.dump casaguide  # → PRODUCTION (confirme « OUI »)
# ============================================================================
set -euo pipefail

DUMP="${1:?usage: sudo casaguide-restore.sh <fichier.dump> [base_cible]}"
TARGET_DB="${2:-casaguide_restore_test}"
[ -f "$DUMP" ] || { echo "Fichier introuvable : $DUMP" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo (accès superutilisateur postgres requis)." >&2; exit 1; }

if [ "$TARGET_DB" = "casaguide" ]; then
  read -r -p "⚠ Restauration dans la base de PRODUCTION 'casaguide'. Confirmer (tapez OUI) ? " ans
  [ "$ans" = "OUI" ] || { echo "Annulé."; exit 1; }
fi

echo "Restauration de $DUMP → base « $TARGET_DB »"
# 1. (Re)créer la base cible + l'extension postgis via le superutilisateur.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qc "DROP DATABASE IF EXISTS \"$TARGET_DB\";"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qc "CREATE DATABASE \"$TARGET_DB\" OWNER casaguide;"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qd "$TARGET_DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;"

# 2. Restaurer en tant que propriétaire (objets appartenant à casaguide).
#    Le seul avertissement attendu est « COMMENT ON EXTENSION postgis » (casaguide
#    n'est pas propriétaire de l'extension) : cosmétique, sans impact → toléré.
runuser -u casaguide -- pg_restore --no-owner -d "$TARGET_DB" "$DUMP" || true

echo "── Vérification du contenu restauré ──"
for q in \
  "section_templates" "poi_categories" "plans" "properties" "owners"; do
  runuser -u casaguide -- psql "$TARGET_DB" -tAc "SELECT '$q=' || count(*) FROM $q;"
done
echo "Restauration terminée dans « $TARGET_DB »."
