"""FastAPI application per km_engine (WP5, Gate G5).

Monta il router /auth di app.auth.routes e fornisce endpoint REST per:
- GET /api/v1/entities
- GET /api/v1/entities/{id}
- GET /api/v1/entities/{id}/facts
- GET /api/v1/entities/{id}/relations
- GET /api/v1/search?q=...
- GET /api/v1/healthz (composito: bolt+psql)

Rate limiting: dependency applicata agli endpoint /auth (documentato come limite prototipo).
OpenAPI: generata automaticamente, salvata in docs/openapi.json.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from neo4j.exceptions import ServiceUnavailable as Neo4jServiceUnavailable

from app.auth import (
    Principal,
    auth_required,
)
from app.auth import (
    router as auth_router,
)
from app.query.engine import (
    get_entity_with_history,
    localize_response,
    query_entities,
    query_facts,
    query_relations,
    search,
)
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository


# ------------------------------------------------------------------ Rate limiting
@dataclass
class TokenBucket:
    """Token bucket in-memory per rate limiting (prototipo, per-istanza)."""

    tokens: float = field(default=10.0)
    last_update: float = field(default_factory=time.time)

    def consume(self, tokens: float = 1.0, refill_rate: float = 1.0, max_tokens: float = 10.0) -> bool:
        """Tenta di consumare token. Restituisce True se successo."""
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(max_tokens, self.tokens + elapsed * refill_rate)
        self.last_update = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class RateLimiter:
    """Rate limiter in-memory per IP (limite del prototipo)."""

    def __init__(self, default_limit: float = 10.0, auth_limit: float = 5.0):
        self._buckets: dict[str, TokenBucket] = defaultdict(TokenBucket)
        self.default_limit = default_limit
        self.auth_limit = auth_limit

    def get_bucket(self, ip: str) -> TokenBucket:
        return self._buckets[ip]

    def is_allowed(self, ip: str, is_auth_endpoint: bool = False) -> bool:
        bucket = self.get_bucket(ip)
        limit = self.auth_limit if is_auth_endpoint else self.default_limit
        return bucket.consume(tokens=1.0, refill_rate=limit, max_tokens=limit * 2)

    def check_rate_limit(self, ip: str, is_auth_endpoint: bool = False) -> None:
        """Controlla rate limit e solleva HTTPException se superato."""
        if not self.is_allowed(ip, is_auth_endpoint):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Rate limit exceeded.",
                headers={"Retry-After": "60"},
            )


# Singleton rate limiter (per-istanza, documentato)
_rate_limiter = RateLimiter(default_limit=10.0, auth_limit=5.0)


# ------------------------------------------------------------------ Dipendenze
def get_neo4j_client() -> Neo4jClient:
    """Dependency: client Neo4j dal contesto."""
    return Neo4jClient.from_env()


def get_repo(client: Annotated[Neo4jClient, Depends(get_neo4j_client)]) -> GraphRepository:
    """Dependency: repository GraphRepository."""
    return GraphRepository(client)



def get_response_lang(
    request: Request,
    lang: Annotated[str | None, Query(description="Lingua risposta (override)")] = None,
) -> str | None:
    """FR9: lingua risposta dal query param `lang` o dall'header Accept-Language."""
    if lang:
        return lang
    accept = request.headers.get("accept-language")
    if accept:
        return accept.split(",")[0].split(";")[0].strip() or None
    return None


def get_client_ip(request: Request) -> str:
    """Estrae IP client da X-Forwarded-For o remote_addr."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_auth(request: Request) -> None:
    """Dependency per rate limiting su endpoint /auth."""
    ip = get_client_ip(request)
    _rate_limiter.check_rate_limit(ip, is_auth_endpoint=True)


# ------------------------------------------------------------------ Applicazione
def create_app() -> FastAPI:
    """Factory per creare l'applicazione FastAPI."""

    app = FastAPI(
        title="km_engine API",
        description="Knowledge Management Engine API (WP5, Gate G5)",
        version="0.1.0",
        openapi_tags=[
            {"name": "auth", "description": "Autenticazione JWT (ADR-002)"},
            {"name": "entities", "description": "Gestione entità"},
            {"name": "search", "description": "Ricerca full-text"},
            {"name": "health", "description": "Health check"},
        ],
    )

    # Includiamo il router /auth di app.auth.routes con rate limiting
    # (prototipo: token bucket in-memory per istanza; piu' stretto su /auth/*)
    app.include_router(auth_router, dependencies=[Depends(rate_limit_auth)])

    # ------------------------------------------------------------------ Health check
    @app.get(
        "/api/v1/healthz",
        tags=["health"],
        summary="Health check composito (Neo4j + Postgres)",
    )
    async def healthz() -> dict[str, Any]:
        """Health check composito: verifica connettività Neo4j e Postgres.

        Returns:
            Dict con status per bolt (Neo4j) e psql (Postgres)
        """
        result = {"status": "healthy", "checks": {}}

        # Check Neo4j
        try:
            client = Neo4jClient.from_env()
            with client.session() as session:
                session.run("RETURN 1").single()
            result["checks"]["bolt"] = "ok"
        except Neo4jServiceUnavailable:
            result["checks"]["bolt"] = "unavailable"
            result["status"] = "unhealthy"
        except Exception as e:  # noqa: BLE001 - healthz non deve mai 500
            result["checks"]["bolt"] = f"error: {e!s}"
            result["status"] = "unhealthy"

        # Check Postgres
        try:
            from app.auth.config import get_auth_settings
            from app.auth.db import connect
            settings = get_auth_settings()
            with connect(settings) as conn:
                conn.execute("SELECT 1").fetchone()
            result["checks"]["psql"] = "ok"
        except psycopg.Error as e:
            result["checks"]["psql"] = f"error: {e!s}"
            result["status"] = "unhealthy"
        except Exception as e:  # noqa: BLE001 - healthz non deve mai 500
            result["checks"]["psql"] = f"error: {e!s}"
            result["status"] = "unhealthy"

        return result

    # ------------------------------------------------------------------ Entities
    @app.get(
        "/api/v1/entities",
        tags=["entities"],
        summary="Lista entità filtrate per visibilità",
        response_model=list[dict],
    )
    async def list_entities(
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        label: Annotated[str | None, Query(description="Filtro per label")] = None,
        type: Annotated[str | None, Query(description="Filtro per tipo")] = None,
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> list[dict]:
        """Ottieni lista entità visibili all'utente autenticato.

        - **Filtro visibilità**: applicato automaticamente in base a ruoli/teams
        - **Admin bypass**: gli admin vedono tutte le entità
        - **lang**: se specificata e != "en", i contenuti non tradotti hanno flag untranslated
        """
        entities = query_entities(client, principal, label=label, entity_type=type)

        # Localizzazione FR9
        if lang:
            entities = localize_response(entities, lang)

        return entities

    @app.get(
        "/api/v1/entities/{entity_id}",
        tags=["entities"],
        summary="Ottieni entità per ID",
        response_model=dict,
    )
    async def get_entity(
        entity_id: str,
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> dict:
        """Ottieni un'entità specifica con i suoi fatti correnti.

        - **404**: se l'entità non esiste o non è visibile
        - **Filtro visibilità**: applicato su entità e fatti
        """
        result = get_entity_with_history(client, principal, entity_id)

        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Entity {entity_id!r} not found or not visible",
            )

        # Localizzazione FR9
        if lang:
            result = localize_response(result, lang)

        return result

    @app.get(
        "/api/v1/entities/{entity_id}/facts",
        tags=["entities"],
        summary="Ottieni fatti di un'entità",
        response_model=list[dict],
    )
    async def list_facts(
        entity_id: str,
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        at_time: Annotated[datetime | None, Query(description="Timestamp per query storica 'al tempo T'")] = None,
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> list[dict]:
        """Ottieni i fatti di un'entità, con opzionale filtro temporale.

        - **at_time**: se specificato, restituisce i fatti validi a quel timestamp (FR5.3)
        - **Filtro visibilità**: applicato a ogni fatto
        """
        facts = query_facts(client, principal, entity_id=entity_id, at_time=at_time)

        if not facts:
            # Verifichiamo se l'entità esiste
            repo = GraphRepository(client)
            entity = repo.get_entity(entity_id)
            if entity is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Entity {entity_id!r} not found",
                )

        # Localizzazione FR9
        if lang:
            facts = localize_response(facts, lang)

        return facts

    @app.get(
        "/api/v1/entities/{entity_id}/relations",
        tags=["entities"],
        summary="Ottieni relazioni di un'entità",
        response_model=list[dict],
    )
    async def list_relations(
        entity_id: str,
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> list[dict]:
        """Ottieni le relazioni RELATES_TO di un'entità.

        - **Filtro visibilità**: applicato all'entità target di ogni relazione
        """
        relations = query_relations(client, principal, entity_id=entity_id)

        # Localizzazione FR9
        if lang:
            relations = localize_response(relations, lang)

        return relations

    # ------------------------------------------------------------------ Search
    @app.get(
        "/api/v1/search",
        tags=["search"],
        summary="Ricerca full-text",
        response_model=list[dict],
    )
    async def search_endpoint(
        q: Annotated[str, Query(description="Testo da cercare", min_length=1)],
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        label: Annotated[str | None, Query(description="Filtro per label")] = None,
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> list[dict]:
        """Ricerca full-text su label e value dei fatti.

        - **Implementazione**: CONTAINS (documentato; per performance usare indici Neo4j full-text)
        - **Filtro visibilità**: applicato a tutti i risultati
        - **match_type**: "entity" o "fact" indica il tipo di match
        """
        results = search(client, principal, text=q, label=label)

        # Localizzazione FR9
        if lang:
            results = localize_response(results, lang)

        return results

    # ------------------------------------------------------------------ Error handling
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    return app


# Creazione dell'app per uvicorn
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
