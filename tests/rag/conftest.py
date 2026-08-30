"""Shared fixtures for Iteration B RAG tests (WP-B1/B2/B4).

Data prefix: ``ib_``. Cleanup removes only ``ib_`` nodes/rows.

The Domain Pack is loaded from a clean ``git archive HEAD`` snapshot so the
RAG tests stay deterministic even while sibling workers edit the working-tree
pack in progress.
"""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import psycopg
import pytest

from app.auth import Principal
from app.storage.client import Neo4jClient

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "domain-packs" / "ricette"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_ricette"
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "rag_golden.json"

PREFIX = "ib_"
TEST_DSN = "postgresql://km:km_dev_password@localhost:5432/km_engine"

# The 15 committed Iteration-A recipes (pinned, deterministic).
GOLDEN_CORPUS_FILES = (
    "ric-001-pomodoro.md",
    "ric-002-risotto.md",
    "ric-003-torta.md",
    "ric-004-pane.md",
    "ric-005-pollo.md",
    "ric-006-insalata.md",
    "ric-007-zuppa.md",
    "ric-008-frittata.md",
    "ric-009-tiramisu.md",
    "ric-010-sugo.md",
    "ric-011-crepes.md",
    "ric-012-polpette.md",
    "ric-101-asparagi-burro.md",
    "ric-102-fregola-vongole.md",
    "ric-103-amaretti.md",
)


def extract_committed_pack(dest: Path) -> Path:
    """Extract ``domain-packs/ricette`` from git HEAD into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "HEAD", "domain-packs/ricette"],
        check=True,
        stdout=subprocess.PIPE,
    )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        tar.extractall(dest)
    return dest / "domain-packs" / "ricette"


@pytest.fixture(scope="session")
def pack_dir(tmp_path_factory) -> Path:
    """Clean committed pack directory (independent of working-tree edits)."""
    return extract_committed_pack(tmp_path_factory.mktemp("pack"))


@pytest.fixture(scope="session")
def pack(pack_dir: Path):
    from app.domain import load_domain_pack

    return load_domain_pack(pack_dir)


def read_golden_corpus() -> dict[str, str]:
    """Return ``{filename: markdown}`` for the 15 committed recipes."""
    return {
        name: (CORPUS_DIR / name).read_text(encoding="utf-8")
        for name in GOLDEN_CORPUS_FILES
    }


def cleanup_neo4j(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:CanonicalTerm OR n:DomainPack OR n:Entity
                   OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )


def cleanup_postgres(conn: psycopg.Connection) -> None:
    with conn.transaction():
        rows = conn.execute(
            "SELECT id FROM users WHERE username LIKE %s", (f"{PREFIX}%",)
        ).fetchall()
        ids = [row[0] for row in rows]
        if ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (ids, [str(i) for i in ids]),
            )
            conn.execute(
                "DELETE FROM refresh_tokens WHERE user_id = ANY(%s)", (ids,)
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE %s", (f"{PREFIX}%",))


@pytest.fixture()
def client() -> Neo4jClient:
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    cleanup_neo4j(neo4j_client)
    try:
        yield neo4j_client
    finally:
        cleanup_neo4j(neo4j_client)
        neo4j_client.close()


@pytest.fixture()
def pg_conn():
    conn = psycopg.connect(TEST_DSN, autocommit=True)
    cleanup_postgres(conn)
    try:
        yield conn
    finally:
        cleanup_postgres(conn)
        conn.close()


@pytest.fixture()
def principal_viewer() -> Principal:
    return Principal(f"{PREFIX}u_viewer", ("viewer",), (), "default", f"{PREFIX}j_viewer")


@pytest.fixture()
def principal_team_a() -> Principal:
    return Principal(
        f"{PREFIX}u_team_a", ("viewer",), (f"{PREFIX}team_a",), "default", f"{PREFIX}j_team_a"
    )


@pytest.fixture()
def principal_team_b() -> Principal:
    return Principal(
        f"{PREFIX}u_team_b", ("viewer",), (f"{PREFIX}team_b",), "default", f"{PREFIX}j_team_b"
    )


@pytest.fixture()
def principal_admin() -> Principal:
    return Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")


def create_document(
    client: Neo4jClient,
    doc_id: str,
    *,
    title: str,
    source_title: str | None = None,
    source_lang: str = "it",
    verification_level: str = "L1",
    translation_state: str = "translated",
    is_public: bool = False,
    teams: list[str] | None = None,
    roles: list[str] | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Create/refresh a :Document node for RAG tests."""
    with client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.document_id = $document_id,
                d.title = $title,
                d.source_title = $source_title,
                d.lang = 'en',
                d.source_lang = $source_lang,
                d.source_language = $source_lang,
                d.canonical_hash = $hash,
                d.verification_level = $verification_level,
                d.translation_state = $translation_state,
                d.is_public = $is_public,
                d.roles = $roles,
                d.teams = $teams
            REMOVE d.embedding
            """,
            id=doc_id,
            document_id=doc_id,
            title=title,
            source_title=source_title,
            source_lang=source_lang,
            hash=f"hash-{doc_id}",
            verification_level=verification_level,
            translation_state=translation_state,
            is_public=is_public,
            roles=roles or [],
            teams=teams or [],
        )
        if embedding is not None:
            session.run(
                "MATCH (d:Document {id: $id}) SET d.embedding = $embedding",
                id=doc_id,
                embedding=embedding,
            )


def create_canonical_term(
    client: Neo4jClient,
    term_id: str,
    *,
    namespace: str,
    label_en: str,
    label_it: str | None = None,
    is_public: bool = False,
    teams: list[str] | None = None,
    roles: list[str] | None = None,
) -> None:
    """Create/refresh a :CanonicalTerm node for RAG tests."""
    with client.session() as session:
        session.run(
            """
            MERGE (t:CanonicalTerm {id: $id})
            SET t.namespace = $namespace,
                t.term_id = $term_id,
                t.label_en = $label_en,
                t.label_it = $label_it,
                t.is_public = $is_public,
                t.roles = $roles,
                t.teams = $teams
            """,
            id=term_id,
            namespace=namespace,
            term_id=term_id,
            label_en=label_en,
            label_it=label_it or label_en,
            is_public=is_public,
            roles=roles or [],
            teams=teams or [],
        )


def link_entity_to_document(
    client: Neo4jClient,
    entity_id: str,
    doc_id: str,
    *,
    label: str,
    entity_type: str,
    term_id: str | None = None,
) -> None:
    """Create an :Entity, link it to a :Document and optionally a term."""
    with client.session() as session:
        session.run(
            """
            MERGE (e:Entity {id: $entity_id})
            SET e.label = $label,
                e.type = $type,
                e.is_public = false,
                e.roles = [],
                e.teams = []
            WITH e
            MATCH (d:Document {id: $doc_id})
            MERGE (e)-[:PART_OF_DOC]->(d)
            """,
            entity_id=entity_id,
            label=label,
            type=entity_type,
            doc_id=doc_id,
        )
        if term_id is not None:
            session.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (t:CanonicalTerm {id: $term_id})
                MERGE (e)-[:NORMALIZED_TO]->(t)
                """,
                entity_id=entity_id,
                term_id=term_id,
            )
