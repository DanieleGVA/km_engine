#!/usr/bin/env bash
# ============================================================================
# backup.sh — backup giornaliero Neo4j + Postgres, cifrato, retention 7gg
# (WP7, gate G8 — ADR-003 D5/D6, NFR4 RPO 24h, NFR7 cifratura at-rest)
#
# Procedura:
#   1. Neo4j: dump offline coerente (neo4j-admin database dump) — il servizio
#      viene fermato brevemente (finestra notturna, ADR-003 D5 punto aperto 4)
#   2. Postgres: pg_dump -Fc (online, coerente)
#   3. Tar + cifratura AES-256 (openssl enc -pbkdf2) — NFR7/GDPR
#   4. Retention: i backup piu' vecchi di RETENTION_DAYS (default 7) vengono
#      eliminati
#
# Uso (dev):
#   COMPOSE_FILE=docker-compose.yml PROJECT_NAME=km-engine \
#     BACKUP_PASSPHRASE=... scripts/backup.sh
# Uso (prod):
#   BACKUP_PASSPHRASE=... scripts/backup.sh
#
# Variabili (override via env):
#   COMPOSE_FILE      file compose (default deploy/docker-compose.yml)
#   PROJECT_NAME      nome progetto compose (default km-engine-prod)
#   BACKUP_DIR        destinazione (default ./backups; in prod: mount off-server)
#   BACKUP_PASSPHRASE passphrase cifratura AES-256 (OBBLIGATORIA)
#   NEO4J_DB          database Neo4j (default neo4j)
#   POSTGRES_USER     utente Postgres (default km)
#   POSTGRES_DB       database Postgres (default km_engine)
#   RETENTION_DAYS    retention in giorni (default 7)
# ============================================================================
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
PROJECT_NAME="${PROJECT_NAME:-km-engine-prod}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"
NEO4J_DB="${NEO4J_DB:-neo4j}"
POSTGRES_USER="${POSTGRES_USER:-km}"
POSTGRES_DB="${POSTGRES_DB:-km_engine}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
LOG_FILE="${LOG_FILE:-$BACKUP_DIR/backup.log}"

if [[ -z "$BACKUP_PASSPHRASE" ]]; then
    echo "ERRORE: BACKUP_PASSPHRASE non impostata (cifratura AES-256 obbligatoria, NFR7)." >&2
    exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
STAMP="$(date -Iseconds)"
mkdir -p "$BACKUP_DIR"
WORK="$(mktemp -d "$BACKUP_DIR/.work_XXXXXX")"
# il container neo4j scrive /backups come utente neo4j: dir temporanea scrivibile
chmod 777 "$WORK"

log() { echo "[$STAMP] $*" | tee -a "$LOG_FILE"; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

log "=== Backup avviato (project=$PROJECT_NAME, compose=$COMPOSE_FILE) ==="

# --- 1. Neo4j: dump offline coerente ----------------------------------------
log "Neo4j: stop container (finestra breve)..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" stop neo4j

log "Neo4j: dump database '$NEO4J_DB'..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" run --rm --no-deps \
    -v "$WORK:/backups" neo4j \
    neo4j-admin database dump "$NEO4J_DB" --to-path=/backups --overwrite-destination

log "Neo4j: start container..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" start neo4j

# --- 2. Postgres: pg_dump -Fc (online) --------------------------------------
log "Postgres: pg_dump -Fc..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$WORK/km_engine.pg"

# --- 3. Cifratura AES-256 (NFR7) --------------------------------------------
log "Cifratura AES-256 (openssl enc -aes-256-cbc -pbkdf2)..."
tar -C "$WORK" -cf - "$NEO4J_DB.dump" km_engine.pg | \
    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -pass env:BACKUP_PASSPHRASE \
        -out "$BACKUP_DIR/km_engine_$TS.enc"

# --- 4. Retention -----------------------------------------------------------
log "Retention: rimozione backup piu' vecchi di ${RETENTION_DAYS} giorni..."
find "$BACKUP_DIR" -maxdepth 1 -name 'km_engine_*.enc' -mtime +"$RETENTION_DAYS" -delete

log "=== Backup completato: $BACKUP_DIR/km_engine_$TS.enc ==="
ls -lh "$BACKUP_DIR/km_engine_$TS.enc" | tee -a "$LOG_FILE"
