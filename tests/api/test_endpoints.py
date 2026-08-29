"""Test delle API REST (WP5, Gate G5).

Copre:
- Auth su ogni endpoint (401/403)
- Endpoint /entities, /entities/{id}, /facts, /relations, /search
- Filtro visibilità via API
- Rate limiting (429)
- FR9 (Accept-Language)
- Health check
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.auth.db import connect as pg_connect
from app.auth.users import create_user
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility

TEST_PG_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get("KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"),
)

WP5_PREFIX = "wp5_"


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Pulizia nodi con prefisso wp5_."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=WP5_PREFIX,
        )


def cleanup_postgres(conn) -> None:
    """Pulizia utenti con prefisso wp5_."""
    with conn.transaction():
        conn.execute("DELETE FROM users WHERE username LIKE %s", (f"{WP5_PREFIX}%",))


@pytest.fixture
def app():
    """Applicazione FastAPI per test."""
    return create_app()


@pytest.fixture
def client(app):
    """TestClient per test."""
    with TestClient(app=app, base_url="http://test") as c:
        yield c


@pytest.fixture
def neo4j_client():
    """Neo4j client per setup dati."""
    c = Neo4jClient.from_env()
    c.verify_connectivity()
    cleanup_neo4j(c)
    yield c
    cleanup_neo4j(c)
    c.close()


@pytest.fixture
def repo(neo4j_client: Neo4jClient) -> GraphRepository:
    """GraphRepository per creare dati."""
    return GraphRepository(neo4j_client)


@pytest.fixture
def pg_conn():
    """Connessione Postgres per setup utenti."""
    settings = type("Settings", (), {
        "pg_dsn": TEST_PG_DSN,
        "jwt_secret": "wp5-test-secret-key-12345678901234567890",
        "admin_username": "wp5_admin",
        "admin_password": "wp5-admin-password-123",
    })()

    conn = pg_connect(settings)
    cleanup_postgres(conn)
    yield conn
    cleanup_postgres(conn)
    conn.close()


@pytest.fixture
def test_user(pg_conn):
    """Crea utente di test e ritorna credenziali."""
    user = create_user(
        pg_conn,
        f"{WP5_PREFIX}testuser",
        f"{WP5_PREFIX}test@example.com",
        "wp5-test-password-123",
        roles=("viewer",),
        teams=(f"{WP5_PREFIX}team_a",),
    )
    return {
        "username": f"{WP5_PREFIX}testuser",
        "password": "wp5-test-password-123",
        "user_id": str(user["id"]),
    }


@pytest.fixture
def admin_user(pg_conn):
    """Crea admin di test."""
    user = create_user(
        pg_conn,
        f"{WP5_PREFIX}admin",
        f"{WP5_PREFIX}admin@example.com",
        "wp5-admin-password-123",
        roles=("admin",),
    )
    return {
        "username": f"{WP5_PREFIX}admin",
        "password": "wp5-admin-password-123",
        "user_id": str(user["id"]),
    }


def login_user(client: TestClient, username: str, password: str) -> str:
    """Login e ritorna access token."""
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    data = response.json()
    return data["access_token"]


def auth_header(token: str) -> dict:
    """Crea header Authorization."""
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_login_success(self, client: TestClient, test_user) -> None:
        """Login con credenziali valide."""
        response = client.post(
            "/auth/login",
            json={"username": test_user["username"], "password": test_user["password"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    def test_login_invalid_credentials(self, client: TestClient) -> None:
        """Login con credenziali invalide: 401."""
        response = client.post(
            "/auth/login",
            json={"username": "nonexistent", "password": "wrongpassword"},
        )
        assert response.status_code == 401

    def test_endpoint_requires_auth(self, client: TestClient) -> None:
        """Endpoint senza token: 401."""
        response = client.get("/api/v1/entities")
        assert response.status_code == 401


class TestEntitiesEndpoint:
    def test_list_entities_authenticated(
        self, client: TestClient, test_user, neo4j_client: Neo4jClient, repo: GraphRepository
    ) -> None:
        """Lista entità con auth: 200."""
        token = login_user(client, test_user["username"], test_user["password"])

        # Crea entità pubblica
        repo.create_entity(
            entity_id=f"{WP5_PREFIX}api_entity1",
            label="TestClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        response = client.get(
            "/api/v1/entities",
            headers=auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(e["id"] == f"{WP5_PREFIX}api_entity1" for e in data)

    def test_list_entities_visibility_filter(
        self, client: TestClient, test_user, admin_user, repo: GraphRepository
    ) -> None:
        """Filtro visibilità: viewer vede solo entità visibili."""
        # Entità pubblica
        repo.create_entity(
            entity_id=f"{WP5_PREFIX}public_api",
            label="PublicClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        # Entità solo admin
        repo.create_entity(
            entity_id=f"{WP5_PREFIX}admin_only_api",
            label="AdminClass",
            type="code",
            visibility=Visibility(roles=("admin",)),
        )

        # Viewer vede solo pubblica
        token_viewer = login_user(client, test_user["username"], test_user["password"])
        response = client.get("/api/v1/entities", headers=auth_header(token_viewer))
        assert response.status_code == 200
        data = response.json()
        assert any(e["id"] == f"{WP5_PREFIX}public_api" for e in data)
        assert not any(e["id"] == f"{WP5_PREFIX}admin_only_api" for e in data)

        # Admin vede entrambe
        token_admin = login_user(client, admin_user["username"], admin_user["password"])
        response = client.get("/api/v1/entities", headers=auth_header(token_admin))
        assert response.status_code == 200
        data = response.json()
        assert any(e["id"] == f"{WP5_PREFIX}public_api" for e in data)
        assert any(e["id"] == f"{WP5_PREFIX}admin_only_api" for e in data)

    def test_get_entity_by_id(
        self, client: TestClient, test_user, repo: GraphRepository
    ) -> None:
        """GET /entities/{id}: 200 con entità esistente."""
        entity_id = f"{WP5_PREFIX}single_entity"
        repo.create_entity(
            entity_id=entity_id,
            label="TestClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            f"/api/v1/entities/{entity_id}",
            headers=auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["entity"]["id"] == entity_id

    def test_get_entity_not_found(
        self, client: TestClient, test_user
    ) -> None:
        """GET /entities/{id}: 404 se non esiste."""
        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            "/api/v1/entities/nonexistent",
            headers=auth_header(token),
        )
        assert response.status_code == 404

    def test_get_entity_facts(
        self, client: TestClient, test_user, repo: GraphRepository
    ) -> None:
        """GET /entities/{id}/facts: 200 con fatti."""
        entity_id = f"{WP5_PREFIX}entity_facts_api"
        repo.create_entity(
            entity_id=entity_id,
            label="TestClass",
            type="code",
            visibility=Visibility(is_public=True),
        )
        repo.create_fact(
            fact_id=f"{WP5_PREFIX}fact_api",
            entity_id=entity_id,
            property="description",
            value="Test value",
            visibility=Visibility(is_public=True),
        )

        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            f"/api/v1/entities/{entity_id}/facts",
            headers=auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_get_entity_relations(
        self, client: TestClient, test_user, repo: GraphRepository
    ) -> None:
        """GET /entities/{id}/relations: 200 con relazioni."""
        source_id = f"{WP5_PREFIX}source_api"
        target_id = f"{WP5_PREFIX}target_api"

        repo.create_entity(
            entity_id=source_id,
            label="SourceClass",
            type="code",
            visibility=Visibility(is_public=True),
        )
        repo.create_entity(
            entity_id=target_id,
            label="TargetClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        with repo.client.session() as session:
            session.run(
                """
                MATCH (s:Entity {id: $source_id})
                MATCH (t:Entity {id: $target_id})
                CREATE (s)-[:RELATES_TO {relation: "depends_on"}]->(t)
                """,
                source_id=source_id,
                target_id=target_id,
            )

        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            f"/api/v1/entities/{source_id}/relations",
            headers=auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(r["target_id"] == target_id for r in data)


class TestSearchEndpoint:
    def test_search_query(
        self, client: TestClient, test_user, repo: GraphRepository
    ) -> None:
        """GET /search?q=...: 200 con risultati."""
        repo.create_entity(
            entity_id=f"{WP5_PREFIX}search_api",
            label="MySearchClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            "/api/v1/search?q=MySearch",
            headers=auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(r["id"] == f"{WP5_PREFIX}search_api" for r in data)

    def test_search_requires_query(self, client: TestClient, test_user) -> None:
        """GET /search senza q: 422."""
        token = login_user(client, test_user["username"], test_user["password"])
        response = client.get(
            "/api/v1/search",
            headers=auth_header(token),
        )
        assert response.status_code == 422


class TestHealthz:
    def test_healthz(self, client: TestClient) -> None:
        """GET /api/v1/healthz: 200."""
        response = client.get("/api/v1/healthz")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "checks" in data


class TestFR9Localization:
    def test_accept_language_flag(
        self, client: TestClient, test_user, repo: GraphRepository
    ) -> None:
        """FR9: flag untranslated coerente con lingua utente e stato traduzione.

        - fatto francese (source_language=fr) con EN non pronta (pending):
          utente fr -> nessun flag (contenuto nativo disponibile)
          utente en -> untranslated=True (rappresentazione EN non pronta)
          utente de -> untranslated=True (traduzione DE non disponibile)
        """
        entity_id = f"{WP5_PREFIX}fr_entity"
        repo.create_entity(
            entity_id=entity_id,
            label="FrenchClass",
            type="code",
            visibility=Visibility(is_public=True),
        )

        # Fatto francese con traduzione EN non ancora pronta (FR9.2 pending)
        with repo.client.session() as session:
            session.run(
                """
                MATCH (e:Entity {id: $entity_id})
                CREATE (f:Fact {
                    id: $fact_id,
                    property: "description",
                    value: "Valeur en français",
                    translation_state: "pending",
                    source_language: "fr",
                    language: "en",
                    valid_from: datetime()
                })
                CREATE (e)-[:HAS_FACT]->(f)
                """,
                entity_id=entity_id,
                fact_id=f"{WP5_PREFIX}fr_fact",
            )

        token = login_user(client, test_user["username"], test_user["password"])

        # Utente francese: contenuto nativo, nessun flag
        response = client.get(
            f"/api/v1/entities/{entity_id}/facts",
            headers={**auth_header(token), "Accept-Language": "fr"},
        )
        assert response.status_code == 200
        data = response.json()
        assert all("untranslated" not in f for f in data)

        # Utente inglese: EN non pronta -> flag
        response_en = client.get(
            f"/api/v1/entities/{entity_id}/facts",
            headers={**auth_header(token), "Accept-Language": "en"},
        )
        assert response_en.status_code == 200
        data_en = response_en.json()
        assert any(f.get("untranslated") is True for f in data_en)

        # Utente tedesco: traduzione DE non disponibile -> flag
        response_de = client.get(
            f"/api/v1/entities/{entity_id}/facts",
            headers={**auth_header(token), "Accept-Language": "de"},
        )
        assert response_de.status_code == 200
        data_de = response_de.json()
        assert any(f.get("untranslated") is True for f in data_de)


class TestRateLimiting:
    def test_rate_limit_auth_endpoint(
        self, client: TestClient, app
    ) -> None:
        """Rate limiting su /auth/login: 429 dopo troppe richieste."""
        # Reset del rate limiter per test pulito
        from app.api.app import _rate_limiter
        _rate_limiter._buckets.clear()

        # Fai molte richieste rapide
        responses = []
        for _ in range(20):
            response = client.post(
                "/auth/login",
                json={"username": "fakeuser", "password": "fakepass"},
            )
            responses.append(response.status_code)

        # Dovremmo vedere almeno un 429
        assert 429 in responses, f"Rate limiting non ha funzionato: {responses}"
