#!/usr/bin/env bash
# ============================================================================
# WP7, gate G8 — test roundtrip backup/restore su container dev
# (km-neo4j, km-postgres del docker-compose.yml di root).
#
# Scelto come script bash (non pytest) perche' il roundtrip richiede:
#   - stop/start del container Neo4j (dump offline, ADR-003 D5)
#   - drop/recreate del database Postgres (restore distruttivo)
# Operazioni di sistema che non hanno senso dentro pytest e che vanno eseguite
# in isolamento (nessun altro test in parallelo).
#
# Flusso: crea dati wp7_* -> backup cifrato -> verifica cifratura -> restore
# -> verifica parita' -> cleanup.
#
# Uso:
#   tests/deploy/test_backup_restore.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
export PROJECT_NAME="${PROJECT_NAME:-km-engine}"
export BACKUP_DIR="${BACKUP_DIR:-./backups-test}"
export BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-wp7-test-passphrase-0123456789}"
export NEO4J_AUTH="${NEO4J_AUTH:-neo4j/km_dev_password}"
export POSTGRES_USER="${POSTGRES_USER:-km}"
export POSTGRES_DB="${POSTGRES_DB:-km_engine}"

NEO4J_USER="${NEO4J_AUTH%%/*}"
NEO4J_PASS="${NEO4J_AUTH#*/}"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "==> 1. Verifica container dev attivi"
docker compose ps --format '{{.Name}} {{.Health}}' | grep -E 'km-(neo4j|postgres)' | grep -q healthy \
    || fail "container dev non healthy — avviare: docker compose up -d"

echo "==> 2. Crea dati di test wp7_*"
docker compose exec -T neo4j cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASS" \
    "CREATE (e:Entity {id:'wp7_entity_1', label:'wp7_roundtrip_entity', type:'test'}) RETURN e.id;" \
    || fail "creazione entita' Neo4j"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "INSERT INTO users (username, email, password_hash, active) VALUES ('wp7_roundtrip_user', 'wp7@test.local', 'x', true) ON CONFLICT (username) DO NOTHING;" \
    || fail "creazione utente Postgres"

echo "==> 3. Backup (scripts/backup.sh)"
scripts/backup.sh || fail "backup.sh"
BACKUP_FILE="$(ls -1t "$BACKUP_DIR"/km_engine_*.enc | head -1)"
[[ -n "$BACKUP_FILE" && -s "$BACKUP_FILE" ]] || fail "nessun file .enc prodotto"

echo "==> 4. Verifica cifratura (nessun plaintext nel file)"
if grep -q "wp7_roundtrip_entity" "$BACKUP_FILE"; then
    fail "il backup contiene plaintext (cifratura non applicata)"
fi
echo "    ok: file cifrato"

echo "==> 5. Restore (scripts/restore.sh)"
scripts/restore.sh "$BACKUP_FILE" || fail "restore.sh"

echo "==> 6. Verifica parita' post-restore"
LABEL="$(docker compose exec -T neo4j cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASS" \
    "MATCH (e:Entity {id:'wp7_entity_1'}) RETURN e.label;" 2>/dev/null || true)"
echo "$LABEL" | grep -q "wp7_roundtrip_entity" || fail "entita' Neo4j non ripristinata"
COUNT="$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM users WHERE username='wp7_roundtrip_user';" 2>/dev/null || true)"
[[ "$COUNT" == "1" ]] || fail "utente Postgres non ripristinato (count=$COUNT)"

echo "==> 7. Cleanup"
docker compose exec -T neo4j cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASS" \
    "MATCH (n) WHERE n.id STARTS WITH 'wp7_' DETACH DELETE n;" >/dev/null 2>&1 || true
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "DELETE FROM users WHERE username LIKE 'wp7\\_%';" >/dev/null 2>&1 || true
rm -rf "$BACKUP_DIR"

echo "PASS: backup/restore roundtrip ok (RPO 24h raggiungibile con backup giornaliero)"
