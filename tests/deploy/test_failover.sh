#!/usr/bin/env bash
# ============================================================================
# WP7, gate G8 — test failover: kill container km-api -> restart automatico
# (restart: unless-stopped) -> healthcheck healthy (ADR-003 D4/D7).
#
# Scelto come script bash (non pytest): il chaos test richiede di uccidere un
# container dello stack prod e osservare il ciclo di recovery di Docker —
# operazione di sistema, non di unit test.
#
# Richiede lo stack prod attivo (deploy/docker-compose.yml). Se non attivo lo
# script termina con SKIP (exit 0) e rimanda al runbook: il test manuale e'
# documentato in docs/runbook.md §5.
#
# Uso:
#   tests/deploy/test_failover.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
PROJECT_NAME="${PROJECT_NAME:-km-engine-prod}"
BASE_URL="${BASE_URL:-http://localhost}"

if ! docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" ps --status running --format '{{.Name}}' 2>/dev/null | grep -q 'km-api'; then
    echo "SKIP: stack prod non attivo — eseguire 'docker compose -f deploy/docker-compose.yml up -d --build --scale km-api=2' (docs/runbook.md §2), poi rilanciare questo test."
    exit 0
fi

echo "==> 1. Baseline: /healthz healthy"
curl -fsS "$BASE_URL/api/v1/healthz" | grep -q '"status": "healthy"' || { echo "FAIL: healthz non healthy a inizio test"; exit 1; }

echo "==> 2. Kill di un'istanza km-api (chaos)"
API_CONTAINER="$(docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" ps -q km-api | head -1)"
echo "    container: $API_CONTAINER"
docker kill "$API_CONTAINER"

echo "==> 3. Attesa restart automatico + healthcheck healthy (max 5 min)"
recovered=0
for i in $(seq 1 30); do
    sleep 10
    status="$(docker inspect -f '{{.State.Health.Status}}' "$API_CONTAINER" 2>/dev/null || echo 'missing')"
    echo "    t+$((i * 10))s: $status"
    if [[ "$status" == "healthy" ]]; then
        recovered=1
        break
    fi
done
[[ "$recovered" == "1" ]] || { echo "FAIL: il container non e' tornato healthy"; exit 1; }

echo "==> 4. Verifica servizio end-to-end"
curl -fsS "$BASE_URL/api/v1/healthz" | grep -q '"status": "healthy"' || { echo "FAIL: healthz non healthy dopo recovery"; exit 1; }

echo "PASS: failover ok (kill -> restart automatico -> healthy)"
