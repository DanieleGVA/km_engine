"""Shared fixtures for Iteration A domain tests.

Two workers share this directory:
- WP-A1/A2/A3 (this worker): ``pack``, ``pg_conn``, ``admin_user``, corpus
  helpers, clean ``ia_`` Postgres data.
- WP-A4 (sibling): ``client``, ``principal_no_team``, ``principal_admin``,
  ``create_document``, ``create_canonical_term``, clean ``ia4_`` Neo4j data.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from app.auth import Principal
from app.auth.users import create_user
from app.domain import load_domain_pack
from app.storage.client import Neo4jClient

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)

PREFIX = "ia_"
TEST_PASSWORD = "ia-test-password-123"

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "domain-packs" / "ricette"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_ricette"

NEO4J_PREFIX = "ia4_"


# ---------------------------------------------------------------------------
# WP-A1/A2/A3 — Postgres + pack fixtures
# ---------------------------------------------------------------------------

def cleanup_postgres(conn: psycopg.Connection) -> None:
    """Delete only the rows created by domain tests (``ia_`` prefixed)."""
    with conn.transaction():
        adjudication_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM adjudications WHERE document_id LIKE %s",
                (f"{PREFIX}%",),
            ).fetchall()
        ]
        proposal_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM glossary_proposals WHERE term LIKE %s",
                (f"{PREFIX}%",),
            ).fetchall()
        ]
        if adjudication_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE entity_type = 'Adjudication' "
                "AND entity_id = ANY(%s)",
                ([str(i) for i in adjudication_ids],),
            )
        if proposal_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE entity_type = 'GlossaryProposal' "
                "AND entity_id = ANY(%s)",
                ([str(i) for i in proposal_ids],),
            )
        conn.execute(
            "DELETE FROM adjudications WHERE document_id LIKE %s", (f"{PREFIX}%",)
        )
        conn.execute(
            "DELETE FROM glossary_proposals WHERE term LIKE %s", (f"{PREFIX}%",)
        )

        user_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM users WHERE username LIKE %s", (f"{PREFIX}%",)
            ).fetchall()
        ]
        if user_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (user_ids, [str(i) for i in user_ids]),
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (user_ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE %s", (f"{PREFIX}%",))


@pytest.fixture(scope="session")
def pack():
    return load_domain_pack(PACK_DIR)


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
def admin_user(pg_conn):
    """A real Postgres admin user used as resolver in L3 tests."""
    return create_user(
        pg_conn,
        f"{PREFIX}admin",
        f"{PREFIX}admin@example.test",
        TEST_PASSWORD,
        roles=("admin",),
    )


def read_corpus() -> dict[str, str]:
    """Return ``{filename: markdown}`` for the 15-recipe corpus."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("ric-*.md"))
    }


def real_recipe_names() -> list[str]:
    return [
        "ric-101-asparagi-burro.md",
        "ric-102-fregola-vongole.md",
        "ric-103-amaretti.md",
    ]


# ---------------------------------------------------------------------------
# WP-A4 — Neo4j fixtures (sibling worker)
# ---------------------------------------------------------------------------

def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only the nodes created by WP-A4 domain tests (``ia4_``)."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:CanonicalTerm OR n:DomainPack OR n:Entity)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=NEO4J_PREFIX,
        )


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
def principal_no_team() -> Principal:
    return Principal("ia4_u_no_team", ("viewer",), (), "default", "ia4_j_no_team")


@pytest.fixture()
def principal_admin() -> Principal:
    return Principal("ia4_u_admin", ("admin",), (), "default", "ia4_j_admin")


def create_document(
    client: Neo4jClient,
    doc_id: str,
    *,
    title: str,
    is_public: bool = False,
    teams: list[str] | None = None,
    roles: list[str] | None = None,
) -> None:
    """Create/refresh a :Document node for visibility tests."""
    with client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.title = $title,
                d.lang = 'en',
                d.source_lang = 'it',
                d.canonical_hash = $hash,
                d.verification_level = 'L1',
                d.translation_state = 'native',
                d.source_language = 'it',
                d.is_public = $is_public,
                d.roles = $roles,
                d.teams = $teams
            """,
            id=doc_id,
            title=title,
            hash=f"hash-{doc_id}",
            is_public=is_public,
            roles=roles or [],
            teams=teams or [],
        )


def create_canonical_term(
    client: Neo4jClient,
    term_id: str,
    *,
    label_en: str,
    is_public: bool = False,
    teams: list[str] | None = None,
    roles: list[str] | None = None,
) -> None:
    """Create/refresh a :CanonicalTerm node for visibility tests."""
    with client.session() as session:
        session.run(
            """
            MERGE (t:CanonicalTerm {id: $id})
            SET t.namespace = 'ia4_test',
                t.term_id = $term_id,
                t.label_en = $label_en,
                t.label_it = $label_en,
                t.is_public = $is_public,
                t.roles = $roles,
                t.teams = $teams
            """,
            id=term_id,
            term_id=term_id,
            label_en=label_en,
            is_public=is_public,
            roles=roles or [],
            teams=teams or [],
        )
