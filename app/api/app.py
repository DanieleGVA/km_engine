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

WP-B5: ``get_neo4j_client`` e' un singleton per processo (un client per
richiesta creava un driver + pool di connessioni mai chiuso, degradando la
latenza sotto carico). Le query Neo4j restano sincrone: su un singolo worker
la capacita' e' ~30-50 req/s (misurato nel load test WP-B5); in produzione
2 repliche + nginx coprono il rate realistico (NFR1).
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from neo4j.exceptions import ServiceUnavailable as Neo4jServiceUnavailable
from pydantic import BaseModel, Field

# ------------------------------------------------------------------ Rate limiting
# WP-E1: il limiter delega lo stato a uno store condivisibile (in-memory di
# default, Redis opzionale). La classe vive in app/api/rate_limit.py.
from app.api.rate_limit import build_rate_limiter
from app.api.ui import router as ui_router
from app.auth import (
    Principal,
    auth_required,
)
from app.auth import (
    router as auth_router,
)
from app.conflict import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    ConflictResolutionError,
    InvalidChoiceError,
    approve_conflict,
    list_conflicts,
    reject_conflict,
)
from app.domain.embedding import DeterministicEmbedding
from app.domain.recompose import recompose_document
from app.invalidation import (
    InvalidationError,
    SourceNotFoundError,
    invalidate_source,
)
from app.query.domain import get_document
from app.query.engine import (
    get_entity_with_history,
    localize_response,
    query_entities,
    query_facts,
    query_relations,
    search,
)
from app.rag.rag import (
    build_embedding_from_graph,
    glossary_query,
    localize_document,
    rag_query,
)
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

# Singleton rate limiter (per-istanza con store in-memory; Redis se configurato
# via KM_RATE_LIMIT_REDIS_URL e pacchetto redis installato).
_rate_limiter = build_rate_limiter(default_limit=10.0, auth_limit=5.0)


# ------------------------------------------------------------------ Dipendenze
_neo4j_client: Neo4jClient | None = None
_neo4j_client_lock = threading.Lock()


def get_neo4j_client() -> Neo4jClient:
    """Dependency: client Neo4j condiviso (singleton per processo).

    WP-B5: un client per richiesta creava un driver + pool di connessioni
    mai chiuso; sotto carico le connessioni accumulate degradavano la
    latenza (NFR1). Il driver Neo4j è thread-safe, quindi un singleton per
    processo è sicuro e riusa il pool di connessioni.
    """
    global _neo4j_client
    if _neo4j_client is None:
        with _neo4j_client_lock:
            if _neo4j_client is None:
                _neo4j_client = Neo4jClient.from_env()
    return _neo4j_client


def get_repo(client: Annotated[Neo4jClient, Depends(get_neo4j_client)]) -> GraphRepository:
    """Dependency: repository GraphRepository."""
    return GraphRepository(client)


def get_embedding_service(
    client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
) -> DeterministicEmbedding:
    """Dependency: embedding deterministico costruito da pack + grafo."""
    return build_embedding_from_graph(client)


def get_pg_conn():
    """Dependency: connessione Postgres per workflow conflitti/invalidazione."""
    from app.auth.config import get_auth_settings
    from app.auth.db import connect

    settings = get_auth_settings()
    conn = connect(settings)
    try:
        yield conn
    finally:
        conn.close()


async def auth_required_for_body(request: Request) -> Principal:
    """Body-free auth dependency for POST endpoints with a JSON body.

    ``app.auth.deps.auth_required`` has a non-Annotated ``settings`` parameter;
    FastAPI 0.141 would treat it as a body field and embed the request body.
    This wrapper calls it directly and keeps the dependency signature
    body-free.
    """
    return await auth_required(request)


def require_roles_for_body(*required: str):
    """RBAC dependency for POST endpoints that carry a JSON body.

    FastAPI 0.141 treats non-Annotated dependency parameters as body fields;
    ``app.auth.deps.require_roles`` has such a parameter (``settings``), so it
    would embed the request body. This local wrapper calls ``auth_required``
    directly and keeps the dependency signature body-free.
    """
    required_set = frozenset(required)

    async def dependency(request: Request) -> Principal:
        principal = await auth_required(request)
        if not required_set.intersection(principal.roles):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Accesso negato: richiesto uno dei ruoli {sorted(required_set)},"
                    f" l'utente ha {sorted(principal.roles)}."
                ),
            )
        return principal

    return dependency


class ApproveConflictRequest(BaseModel):
    """Body per POST /api/v1/conflicts/{id}/approve."""

    choice: Literal["a", "b"]


class InvalidateSourceRequest(BaseModel):
    """Body per POST /api/v1/sources/{source_id}/invalidate."""

    reason: str = Field(min_length=1)
    max_depth: int | None = Field(default=None, ge=0, le=10)


class RagQueryRequest(BaseModel):
    """Body per POST /api/v1/rag/query."""

    query: str = Field(min_length=1)
    lang: str | None = Field(
        default=None, description="Lingua utente per boost/localizzazione"
    )
    limit: int | None = Field(default=5, ge=1, le=50)


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
            {"name": "conflicts", "description": "Conflict check e workflow (FR6)"},
            {"name": "invalidation", "description": "Fact invalidation e truth-maintenance (FR7)"},
            {"name": "rag", "description": "Retrieval ibrido RAG (WP-B1)"},
            {"name": "documents", "description": "Documenti canonici (WP-B1/B4)"},
            {"name": "glossary", "description": "Query strutturate dal glossario (WP-B2)"},
            {"name": "ui", "description": "Web UI minima di adjudication (WP-E6)"},
        ],
    )

    # Includiamo il router /auth di app.auth.routes con rate limiting
    # (prototipo: token bucket in-memory per istanza; piu' stretto su /auth/*)
    app.include_router(auth_router, dependencies=[Depends(rate_limit_auth)])

    # Web UI minima di adjudication (WP-E6)
    app.include_router(ui_router)

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

    # ------------------------------------------------------------------ Conflicts
    @app.get(
        "/api/v1/conflicts",
        tags=["conflicts"],
        summary="Lista conflitti (workflow FR6)",
        response_model=list[dict],
    )
    async def list_conflicts_endpoint(
        principal: Annotated[Principal, Depends(auth_required)],
        conn: Annotated[psycopg.Connection, Depends(get_pg_conn)],
        status: Annotated[str | None, Query(description="Filtro stato: pending/approved/rejected")] = None,
    ) -> list[dict]:
        """Elenco dei conflitti, opzionalmente filtrati per stato."""
        if status is not None and status not in ("pending", "approved", "rejected"):
            raise HTTPException(
                status_code=422,
                detail="status must be one of pending, approved, rejected",
            )
        return list_conflicts(conn, status=status)

    @app.post(
        "/api/v1/conflicts/{conflict_id}/approve",
        tags=["conflicts"],
        summary="Approva un conflitto scegliendo a o b",
        response_model=dict,
    )
    async def approve_conflict_endpoint(
        conflict_id: int,
        body: Annotated[ApproveConflictRequest, Body()],
        principal: Annotated[Principal, Depends(require_roles_for_body("admin", "editor"))],
        repo: Annotated[GraphRepository, Depends(get_repo)],
        conn: Annotated[psycopg.Connection, Depends(get_pg_conn)],
    ) -> dict:
        """Applica il valore scelto e invalida l'altro fatto."""
        try:
            return approve_conflict(
                repo, conn, conflict_id, body.choice, principal.user_id
            )
        except ConflictNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictAlreadyResolvedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidChoiceError, ConflictResolutionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/conflicts/{conflict_id}/reject",
        tags=["conflicts"],
        summary="Rifiuta un conflitto senza modificare il grafo",
        response_model=dict,
    )
    async def reject_conflict_endpoint(
        conflict_id: int,
        principal: Annotated[Principal, Depends(require_roles_for_body("admin", "editor"))],
        conn: Annotated[psycopg.Connection, Depends(get_pg_conn)],
    ) -> dict:
        """Rifiuta un conflitto pending."""
        try:
            return reject_conflict(conn, conflict_id, principal.user_id)
        except ConflictNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictAlreadyResolvedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ------------------------------------------------------------------ Invalidation
    @app.post(
        "/api/v1/sources/{source_id}/invalidate",
        tags=["invalidation"],
        summary="Invalida una sorgente e propaga ai fatti dipendenti",
        response_model=dict,
    )
    async def invalidate_source_endpoint(
        source_id: str,
        body: Annotated[InvalidateSourceRequest, Body()],
        principal: Annotated[Principal, Depends(require_roles_for_body("admin", "editor"))],
        repo: Annotated[GraphRepository, Depends(get_repo)],
        conn: Annotated[psycopg.Connection, Depends(get_pg_conn)],
    ) -> dict:
        """Invalida i fatti DERIVED_FROM la sorgente e propaga ai dipendenti."""
        try:
            return invalidate_source(
                repo,
                conn,
                source_id,
                reason=body.reason,
                user_id=principal.user_id,
                max_depth=body.max_depth,
            )
        except SourceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------------ RAG (WP-B1)
    @app.post(
        "/api/v1/rag/query",
        tags=["rag"],
        summary="Retrieval ibrido RAG (vettoriale + grafo)",
        response_model=list[dict],
    )
    async def rag_query_endpoint(
        body: Annotated[RagQueryRequest, Body()],
        principal: Annotated[Principal, Depends(auth_required_for_body)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        embedding: Annotated[DeterministicEmbedding, Depends(get_embedding_service)],
    ) -> list[dict]:
        """Ricerca vettoriale su Document.embedding + ranking deterministico.

        - **Filtro visibilità**: applicato prima della restituzione (default-deny)
        - **Ranking**: cosine * (1 + boost_lang) * (1 + boost_verification)
        - **Nessun ranking LLM**
        """
        hits = rag_query(
            client,
            principal,
            body.query,
            lang=body.lang,
            limit=body.limit or 5,
            embedding=embedding,
        )
        return [hit.to_dict() for hit in hits]

    # ------------------------------------------------------------------ Documents (WP-B1/B4)
    @app.get(
        "/api/v1/documents/{document_id}",
        tags=["documents"],
        summary="Documento canonico + metadati",
        response_model=dict,
    )
    async def get_document_endpoint(
        document_id: str,
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        lang: Annotated[str | None, Depends(get_response_lang)] = None,
    ) -> dict:
        """Restituisce il canonical.md ricomposto e i metadati del documento.

        - **404**: se il documento non esiste o non è visibile
        - **lang**: localizzazione FR9.3 (flag ``untranslated`` se applicabile)
        """
        document = get_document(client, principal, document_id)
        if document is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document {document_id!r} not found or not visible",
            )
        canonical_md = recompose_document(client, document_id)
        localized = localize_document(document, lang)
        return {**localized, "canonical_md": canonical_md}

    # ------------------------------------------------------------------ Glossary (WP-B2)
    @app.get(
        "/api/v1/glossary/query",
        tags=["glossary"],
        summary="Query strutturate dal glossario (tecnica/ingrediente/stato)",
        response_model=list[dict],
    )
    async def glossary_query_endpoint(
        principal: Annotated[Principal, Depends(auth_required)],
        client: Annotated[Neo4jClient, Depends(get_neo4j_client)],
        term_id: Annotated[
            str | None, Query(description="CanonicalTerm id completo")
        ] = None,
        technique: Annotated[
            str | None, Query(description="Termine tecnica (id o label)")
        ] = None,
        ingredient: Annotated[
            str | None, Query(description="Termine ingrediente (id o label)")
        ] = None,
        state: Annotated[
            str | None, Query(description="Termine stato (id o label)")
        ] = None,
    ) -> list[dict]:
        """Query domain-aware sui percorsi CanonicalTerm<-Entity->Document.

        Specificare esattamente uno tra ``term_id``, ``technique``,
        ``ingredient`` e ``state``.
        """
        selectors = (term_id, technique, ingredient, state)
        if sum(selector is not None for selector in selectors) != 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Specify exactly one of term_id, technique, ingredient, state"
                ),
            )
        return glossary_query(
            client,
            principal,
            term_id=term_id,
            technique=technique,
            ingredient=ingredient,
            state=state,
        )

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
