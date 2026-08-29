#!/usr/bin/env bash
# ============================================================================
# recycle_unhealthy.sh — healthcheck recycle (ADR-003 D7, WP7/G8)
#
# `restart: unless-stopped` riavvia i container che escono, ma NON quelli che
# restano vivi e non-healthy. Questo script riavvia i container del progetto
# il cui healthcheck risulta "unhealthy" (es. km-api con DB irraggiungibile).
#
# Schedulazione consigliata in produzione (cron, ogni minuto):
#   * * * * * /path/to/km_engine/scripts/recycle_unhealthy.sh >> /var/log/km-recycle.log 2>&1
# ============================================================================
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
PROJECT_NAME="${PROJECT_NAME:-km-engine-prod}"

restarted=0
for c in $(docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" ps -q 2>/dev/null); do
    name="$(docker inspect -f '{{.Name}}' "$c" | tr -d '/')"
    status="$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo 'none')"
    if [[ "$status" == "unhealthy" ]]; then
        echo "$(date -Iseconds) recycle $name (unhealthy)"
        docker restart "$c"
        restarted=$((restarted + 1))
    fi
done
if [[ "$restarted" -gt 0 ]]; then
    echo "$(date -Iseconds) riavviati $restarted container non-healthy"
fi
exit 0
