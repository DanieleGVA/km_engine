"""Fixture condivise per i test domain (Iterazione A, WP-A4: T7/T8)."""
from __future__ import annotations

from typing import Any

import pytest

from app.auth import Principal
from app.storage.client import Neo4jClient

TEST_PREFIX = "ia4_"


def clean_test_data(client: Neo4jClient) -> None:
    """Elimina tutti i nodi domain/MVP creati dai test con prefisso ia4_."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:CanonicalTerm OR n:DomainPack
                   OR n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=TEST_PREFIX,
        )


@pytest.fixture
def client() -> Neo4jClient:
    """Neo4j client connesso, con pulizia prima e dopo ogni test."""
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    clean_test_data(neo4j_client)
    yield neo4j_client
    clean_test_data(neo4j_client)
    neo4j_client.close()


@pytest.fixture
def principal_viewer() -> Principal:
    """Viewer del team ia4_team_a, nessun ruolo speciale."""
    return Principal(
        user_id="ia4_viewer",
        roles=("viewer",),
        teams=("ia4_team_a",),
        tenant="default",
        jti="ia4_jti_viewer",
    )


@pytest.fixture
def principal_other_team() -> Principal:
    """Viewer di un team diverso (ia4_team_b)."""
    return Principal(
        user_id="ia4_other",
        roles=("viewer",),
        teams=("ia4_team_b",),
        tenant="default",
        jti="ia4_jti_other",
    )


@pytest.fixture
def principal_no_team() -> Principal:
    """Viewer senza team e senza ruoli speciali."""
    return Principal(
        user_id="ia4_noteam",
        roles=("viewer",),
        teams=(),
        tenant="default",
        jti="ia4_jti_noteam",
    )


@pytest.fixture
def principal_admin() -> Principal:
    """Admin con bypass visibilità."""
    return Principal(
        user_id="ia4_admin",
        roles=("admin",),
        teams=(),
        tenant="default",
        jti="ia4_jti_admin",
    )


def create_document(
    client: Neo4jClient,
    doc_id: str,
    *,
    title: str | None = None,
    is_public: bool | None = None,
    roles: list[str] | None = None,
    teams: list[str] | None = None,
    **extra: Any,
) -> str:
    """Crea un :Document di test con visibilità controllata."""
    props: dict[str, Any] = {
        "id": doc_id,
        "title": title or doc_id,
        "lang": extra.pop("lang", "en"),
        "source_lang": extra.pop("source_lang", "it"),
        "canonical_hash": extra.pop("canonical_hash", f"hash-{doc_id}"),
        "verification_level": extra.pop("verification_level", "L1"),
        "translation_state": extra.pop("translation_state", "native"),
        "source_language": extra.pop("source_language", "it"),
    }
    if is_public is not None:
        props["is_public"] = is_public
    if roles is not None:
        props["roles"] = list(roles)
    if teams is not None:
        props["teams"] = list(teams)
    props.update(extra)
    with client.session() as session:
        session.run("CREATE (d:Document $props)", props=props)
    return doc_id


def create_canonical_term(
    client: Neo4jClient,
    term_id: str,
    *,
    namespace: str = "ia4_glossary",
    label_en: str | None = None,
    is_public: bool | None = None,
    roles: list[str] | None = None,
    teams: list[str] | None = None,
    **extra: Any,
) -> str:
    """Crea un :CanonicalTerm di test con visibilità controllata."""
    props: dict[str, Any] = {
        "id": term_id,
        "namespace": namespace,
        "term_id": extra.pop("term_key", term_id.rsplit(":", 1)[-1]),
        "label_en": label_en or term_id,
        "label_it": extra.pop("label_it", label_en or term_id),
    }
    if is_public is not None:
        props["is_public"] = is_public
    if roles is not None:
        props["roles"] = list(roles)
    if teams is not None:
        props["teams"] = list(teams)
    props.update(extra)
    with client.session() as session:
        session.run("CREATE (t:CanonicalTerm $props)", props=props)
    return term_id
