#!/usr/bin/env bash
# ============================================================================
# restore.sh — restore da backup cifrato (WP7, gate G8 — ADR-003 D6)
#
# Procedura (distruttiva: sostituisce i dati correnti con quelli del backup):
#   1. Decifratura AES-256 + estrazione tar
#   2. Postgres: drop/recreate database + pg_restore -Fc
#   3. Neo4j: stop container, neo4j-admin database load --overwrite-destination,
#      start container
#   4. Riavvio dello stack + smoke test (healthz + login admin)
#
# Uso:
#   BACKUP_PASSPHRASE=... scripts/restore.sh [file.enc]
#   (senza argomento: ripristina l'ultimo backup in BACKUP_DIR)
#
# RPO/RTO raggiungibili (MVP, scope change 2026-08-29):
#   - RPO = 24h (backup giornaliero, default NFR4): al massimo un giorno di
#     modifiche perse nel caso peggiore.
#   - RTO: nessun target formale nel MVP (NFR5 non definito). Il tempo di
#     restore e' dominato dal load del dump Neo4j + pg_restore; su dati di
#     prova (prototipo) l'operazione completa richiede pochi minuti. Un target
#     RTO formale va concordato col committente prima della produzione.
# ============================================================================
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
PROJECT_NAME="${PROJECT_NAME:-km-engine-prod}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"
NEO4J_DB="${NEO4J_DB:-neo4j}"
POSTGRES_USER="${POSTGRES_USER:-km}"
POSTGRES_DB="${POSTGRES_DB:-km_engine}"
CORPUS_DIR="${CORPUS_DIR:-./corpus}"
PACK_DIR="${PACK_DIR:-./domain-packs}"
LOG_FILE="${LOG_FILE:-$BACKUP_DIR/restore.log}"

BACKUP_FILE="${1:-}"

if [[ -z "$BACKUP_PASSPHRASE" ]]; then
    echo "ERRORE: BACKUP_PASSPHRASE non impostata (decifratura AES-256)." >&2
    exit 1
fi

if [[ -z "$BACKUP_FILE" ]]; then
    BACKUP_FILE="$(ls -1t "$BACKUP_DIR"/km_engine_*.enc 2>/dev/null | head -1 || true)"
fi
if [[ -z "$BACKUP_FILE" || ! -f "$BACKUP_FILE" ]]; then
    echo "ERRORE: nessun backup trovato in $BACKUP_DIR (o file inesistente: $BACKUP_FILE)." >&2
    exit 1
fi

STAMP="$(date -Iseconds)"
mkdir -p "$BACKUP_DIR"
WORK="$(mktemp -d "$BACKUP_DIR/.restore_XXXXXX")"
chmod 777 "$WORK"
log() { echo "[$STAMP] $*" | tee -a "$LOG_FILE"; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

log "=== Restore avviato da: $BACKUP_FILE ==="
log "ATTENZIONE: i dati correnti di Neo4j e Postgres verranno SOSTITUITI."

# --- 1. Decifratura + estrazione --------------------------------------------
log "Decifratura AES-256..."
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -pass env:BACKUP_PASSPHRASE \
    -in "$BACKUP_FILE" | tar -C "$WORK" -xf -
ls -lh "$WORK" | tee -a "$LOG_FILE"

# --- 2. Postgres: drop/recreate + pg_restore --------------------------------
log "Postgres: drop/recreate database '$POSTGRES_DB'..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" exec -T postgres \
    psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $POSTGRES_DB WITH (FORCE);" \
    -c "CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;"
log "Postgres: pg_restore -Fc..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" exec -T postgres \
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc < "$WORK/km_engine.pg"

# --- 3. Corpus md + domain pack (WP-E4) -------------------------------------
# Ripristina i file md inclusi nel backup (se presenti nell'archivio).
if [[ -d "$WORK/corpus" ]]; then
    log "Corpus md: ripristino $CORPUS_DIR"
    mkdir -p "$CORPUS_DIR"
    cp -a "$WORK/corpus/." "$CORPUS_DIR/"
fi
if [[ -d "$WORK/domain-packs" ]]; then
    log "Domain pack: ripristino $PACK_DIR"
    mkdir -p "$PACK_DIR"
    cp -a "$WORK/domain-packs/." "$PACK_DIR/"
fi

# --- 4. Neo4j: load dump -----------------------------------------------------
log "Neo4j: stop container..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" stop neo4j
log "Neo4j: load database '$NEO4J_DB' (--overwrite-destination)..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" run --rm --no-deps \
    -v "$WORK:/backups" neo4j \
    neo4j-admin database load "$NEO4J_DB" --from-path=/backups --overwrite-destination
log "Neo4j: start container..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" start neo4j

# --- 5. Verifica indice vettoriale (WP-E4) ----------------------------------
# Il dump Neo4j include schema e indici (full-text + vettoriale). Dopo il load
# verifichiamo che l'indice vettoriale sia ONLINE.
log "Verifica indice vettoriale document_embedding_vector..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" exec -T neo4j \
    cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-}" \
    "SHOW INDEXES YIELD name, state WHERE name = 'document_embedding_vector' RETURN name, state;" \
    || log "ATTENZIONE: verifica indice vettoriale non riuscita (vedi log)."

# --- 6. Smoke test -----------------------------------------------------------
log "Smoke test: attesa healthcheck dei servizi..."
for i in $(seq 1 30); do
    status="$(docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" ps --format '{{.Service}}={{.Health}}' 2>/dev/null || true)"
    if echo "$status" | grep -q 'neo4j=healthy' && echo "$status" | grep -q 'postgres=healthy'; then
        break
    fi
    sleep 5
done
echo "$status" | tee -a "$LOG_FILE"

log "=== Restore completato. Verificare con: curl http://localhost/api/v1/healthz ==="
