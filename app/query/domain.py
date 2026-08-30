"""Query domain visibility-aware per km_engine (Iterazione A, WP-A4).

Estensioni additive per ``:Document``, ``:CanonicalTerm`` e ``:DomainPack``.
Tutte le letture applicano il filtro visibilità P4 (default-deny) tramite
``principal_visibility_context`` (unico ponte auth -> storage). Admin bypass;
nessun percorso di lettura non filtrato, inclusi full-text e storico.

Nota sullo storico: in questa iterazione ``:Document`` non ha ancora una catena
``VERSION_OF`` (arriva con l'estrattore WP-A6). Quando verrà aggiunta, ogni
lettura dello storico dovrà passare dallo stesso filtro qui sotto.
"""
from __future__ import annotations

from typing import Any

from neo4j import ManagedTransaction

from app.auth import Principal, principal_visibility_context
from app.storage.client import Neo4jClient
from app.storage.visibility import Visibility, is_visible

DOCUMENT_TITLE_FULLTEXT = "document_title_fulltext"
CANONICAL_TERM_LABEL_EN_FULLTEXT = "canonical_term_label_en_fulltext"


def _read(client: Neo4jClient, fn) -> Any:
    """Esegue una funzione di lettura in transazione."""
    with client.session() as session:
        return session.execute_read(fn)


def _jsonable(value: Any) -> Any:
    """Converte tipi temporali Neo4j in stringhe ISO (JSON-serializzabili)."""
    try:
        from neo4j.time import Date, DateTime, Time
    except ImportError:  # pragma: no cover
        return value
    if isinstance(value, (DateTime, Date, Time)):
        return value.iso_format()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _node_to_dict(node: Any) -> dict[str, Any]:
    """Converte un nodo Neo4j in dict JSON-serializzabile."""
    return _jsonable(dict(node))


def _visibility_from_props(props: dict[str, Any]) -> Visibility:
    """Legge la visibilità concreta da un dict di proprietà (default-deny)."""
    return Visibility(
        is_public=bool(props.get("is_public", False)),
        roles=tuple(props.get("roles") or ()),
        teams=tuple(props.get("teams") or ()),
    )


def _can_see(props: dict[str, Any], ctx: dict[str, object]) -> bool:
    """Applica il filtro visibilità P4 con la politica default-deny."""
    return is_visible(_visibility_from_props(props), **ctx)


def get_document(
    client: Neo4jClient,
    principal: Principal,
    doc_id: str,
) -> dict[str, Any] | None:
    """Ottieni un :Document per id, filtrato per visibilità.

    Ritorna None se il documento non esiste o non è visibile al principal.
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> dict[str, Any] | None:
        record = tx.run(
            "MATCH (d:Document {id: $doc_id}) RETURN d",
            doc_id=doc_id,
        ).single()
        if record is None:
            return None
        props = _node_to_dict(record["d"])
        if is_admin or _can_see(props, ctx):
            return props
        return None

    return _read(client, work)


def list_documents(
    client: Neo4jClient,
    principal: Principal,
) -> list[dict[str, Any]]:
    """Lista tutti i :Document visibili al principal."""
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run("MATCH (d:Document) RETURN d ORDER BY d.id")
        documents = []
        for record in result:
            props = _node_to_dict(record["d"])
            if is_admin or _can_see(props, ctx):
                documents.append(props)
        return documents

    return _read(client, work)


def get_document_by_entity(
    client: Neo4jClient,
    principal: Principal,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Documenti che contengono una :Entity (Entity-[:PART_OF_DOC]->Document).

    I documenti restituiti sono filtrati per visibilità. L'entity_id è solo la
    chiave di ingresso della query, non un dato restituito.
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run(
            """
            MATCH (e:Entity {id: $entity_id})-[:PART_OF_DOC]->(d:Document)
            RETURN d ORDER BY d.id
            """,
            entity_id=entity_id,
        )
        documents = []
        for record in result:
            props = _node_to_dict(record["d"])
            if is_admin or _can_see(props, ctx):
                documents.append(props)
        return documents

    return _read(client, work)


def list_canonical_terms(
    client: Neo4jClient,
    principal: Principal,
) -> list[dict[str, Any]]:
    """Lista tutti i :CanonicalTerm visibili al principal."""
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run("MATCH (t:CanonicalTerm) RETURN t ORDER BY t.id")
        terms = []
        for record in result:
            props = _node_to_dict(record["t"])
            if is_admin or _can_see(props, ctx):
                terms.append(props)
        return terms

    return _read(client, work)


def search_documents(
    client: Neo4jClient,
    principal: Principal,
    text: str,
) -> list[dict[str, Any]]:
    """Ricerca full-text su Document.title, filtrata per visibilità.

    Usa l'indice full-text ``document_title_fulltext`` (schema 002). Il filtro
    visibilità è applicato DOPO il retrieval, prima della restituzione: il
    full-text non deve far trapelare documenti non visibili.
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run(
            """
            CALL db.index.fulltext.queryNodes($index, $text)
            YIELD node, score
            RETURN node, score
            """,
            index=DOCUMENT_TITLE_FULLTEXT,
            text=text,
        )
        documents = []
        for record in result:
            props = _node_to_dict(record["node"])
            if is_admin or _can_see(props, ctx):
                props["score"] = record["score"]
                documents.append(props)
        return documents

    return _read(client, work)


def search_canonical_terms(
    client: Neo4jClient,
    principal: Principal,
    text: str,
) -> list[dict[str, Any]]:
    """Ricerca full-text su CanonicalTerm.label_en, filtrata per visibilità.

    Usa l'indice full-text ``canonical_term_label_en_fulltext`` (schema 002).
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run(
            """
            CALL db.index.fulltext.queryNodes($index, $text)
            YIELD node, score
            RETURN node, score
            """,
            index=CANONICAL_TERM_LABEL_EN_FULLTEXT,
            text=text,
        )
        terms = []
        for record in result:
            props = _node_to_dict(record["node"])
            if is_admin or _can_see(props, ctx):
                props["score"] = record["score"]
                terms.append(props)
        return terms

    return _read(client, work)
