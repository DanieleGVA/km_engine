"""WP7, gate G8 — test del healthcheck composito (ADR-003 D4).

Richiede i container dev attivi (km-neo4j, km-postgres, docker-compose.yml di
root): verifica che /api/v1/healthz risponda 200 con status "healthy" e che
entrambi i check (bolt Neo4j, psql Postgres) siano "ok".

Nota: il test del comportamento "unhealthy" (dipendenza giu') e' coperto dal
test bash tests/deploy/test_failover.sh (richiede lo stack prod) e dal runbook.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def test_healthz_returns_200(client: TestClient) -> None:
    """/api/v1/healthz risponde sempre 200 (mai 500)."""
    r = client.get("/api/v1/healthz")
    assert r.status_code == 200


def test_healthz_healthy_with_bolt_and_psql(client: TestClient) -> None:
    """Healthcheck composito: bolt (Neo4j) e psql (Postgres) entrambi ok."""
    r = client.get("/api/v1/healthz")
    body = r.json()
    assert body["status"] == "healthy"
    assert body["checks"]["bolt"] == "ok"
    assert body["checks"]["psql"] == "ok"


def test_healthz_shape(client: TestClient) -> None:
    """Contratto del body: status + checks con chiavi bolt/psql."""
    r = client.get("/api/v1/healthz")
    body = r.json()
    assert set(body.keys()) == {"status", "checks"}
    assert set(body["checks"].keys()) == {"bolt", "psql"}
