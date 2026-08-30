#!/usr/bin/env bash
# ============================================================================
# retention_audit.sh — audit della retention backup (WP-E4, NFR8)
#
# Elenca i backup cifrati in BACKUP_DIR e segnala quelli piu' vecchi di
# RETENTION_DAYS (default 7). Esce con codice 1 se trova backup scaduti non
# rimossi (utile come controllo schedulato dopo backup.sh).
#
# Uso:
#   scripts/retention_audit.sh
#   BACKUP_DIR=/mnt/backups RETENTION_DAYS=30 scripts/retention_audit.sh
# ============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "BACKUP_DIR non trovato: $BACKUP_DIR"
    exit 0
fi

expired=0
total=0
while IFS= read -r file; do
    total=$((total + 1))
    if [[ -n "$file" ]]; then
        echo "SCADUTO (oltre ${RETENTION_DAYS}g): $file"
        expired=$((expired + 1))
    fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -name 'km_engine_*.enc' -mtime +"$RETENTION_DAYS" -print)

echo "Retention audit: $total backup, $expired oltre ${RETENTION_DAYS} giorni."
if [[ "$expired" -gt 0 ]]; then
    echo "ESITO: KO — rimuovere i backup scaduti o verificare backup.sh."
    exit 1
fi
echo "ESITO: OK"
