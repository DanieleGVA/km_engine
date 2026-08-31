"""T12 — E2E flusso completo Iterazione A (gate GA6).

Flusso unico sullo stack dev reale (Neo4j + Postgres):

1. bootstrap Domain Pack (``scripts/load_domain_pack.load_pack``, idempotente)
2. carica il corpus ricette (15 file) con id ``ia6_``-prefissati
3. traduzione P2-safe (FakeLLMClient) -> ``verify_l1`` -> ``canonicalize``
   (con coda proposte glossario) -> ``write_canon_log`` -> ``extract_document``
4. query visibility-aware via ``app/query/domain.py`` (viewer default-deny,
   admin bypass) su :Document e :CanonicalTerm
5. ``recompose_document`` -> byte-identico al canonical.md generato
6. pulizia post-run e verifica 0 residui ``ia6_`` (Neo4j + Postgres:
   canon_log, adjudications, glossary_proposals, audit_log)
"""
from __future__ import annotations

import re

import psycopg
import pytest

from app.auth import Principal
from app.domain import (
    canonicalize,
    load_domain_pack,
    translate_document,
    verify_l1,
    write_canon_log,
)
from app.domain.extract import extract_document
from app.domain.recompose import recompose_document
from app.query.domain import (
    get_document,
    list_canonical_terms,
    list_documents,
)
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.conftest import PACK_DIR, read_corpus
from tests.domain.fake_llm import build_fake_llm

IA6_PREFIX = "ia6_"
TEST_DSN = "postgresql://km:km_dev_password@localhost:5432/km_engine"

_ID_LINE_RE = re.compile(r"(?m)^id: .+$")


def _prefix_corpus_ids(corpus: dict[str, str]) -> dict[str, str]:
    """Riscrive l'id frontmatter del corpus con il prefisso test ``ia6_``.

    Il contenuto (titolo, ingredienti, procedimento) resta identico; l'id
    prefissato propaga a translated/canonical e rende pulibili per pattern
    ``ia6_%`` anche canon_log e glossary_proposals (context = document_id).
    """
    prefixed: dict[str, str] = {}
    for name, md in corpus.items():
        prefixed[name] = _ID_LINE_RE.sub(
            lambda match: f"id: {IA6_PREFIX}{match.group(0).split(':', 1)[1].strip()}",
            md,
            count=1,
        )
    return prefixed


def _cleanup_neo4j_ia6(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=IA6_PREFIX,
        )


def _cleanup_postgres_ia6(conn: psycopg.Connection) -> None:
    with conn.transaction():
        proposal_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM glossary_proposals "
                "WHERE term LIKE %s OR context LIKE %s",
                (f"{IA6_PREFIX}%", f"{IA6_PREFIX}%"),
            ).fetchall()
        ]
        adjudication_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM adjudications WHERE document_id LIKE %s",
                (f"{IA6_PREFIX}%",),
            ).fetchall()
        ]
        if proposal_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE entity_type = 'GlossaryProposal' "
                "AND entity_id = ANY(%s)",
                ([str(i) for i in proposal_ids],),
            )
        if adjudication_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE entity_type = 'Adjudication' "
                "AND entity_id = ANY(%s)",
                ([str(i) for i in adjudication_ids],),
            )
        conn.execute(
            "DELETE FROM canon_log WHERE document_id LIKE %s", (f"{IA6_PREFIX}%",)
        )
        conn.execute(
            "DELETE FROM adjudications WHERE document_id LIKE %s",
            (f"{IA6_PREFIX}%",),
        )
        conn.execute(
            "DELETE FROM glossary_proposals WHERE term LIKE %s OR context LIKE %s",
            (f"{IA6_PREFIX}%", f"{IA6_PREFIX}%"),
        )


def _ia6_residue_neo4j(client: Neo4jClient) -> int:
    with client.session() as session:
        record = session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            RETURN count(n) AS c
            """,
            prefix=IA6_PREFIX,
        ).single()
        return int(record["c"])


def _ia6_residue_postgres(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM canon_log WHERE document_id LIKE %s",
            (f"{IA6_PREFIX}%",),
        )
        canon_log = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM adjudications WHERE document_id LIKE %s",
            (f"{IA6_PREFIX}%",),
        )
        adjudications = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM glossary_proposals "
            "WHERE term LIKE %s OR context LIKE %s",
            (f"{IA6_PREFIX}%", f"{IA6_PREFIX}%"),
        )
        proposals = cur.fetchone()[0]
    return {
        "canon_log": canon_log,
        "adjudications": adjudications,
        "glossary_proposals": proposals,
    }


@pytest.fixture()
def ia6_pg_conn():
    conn = psycopg.connect(TEST_DSN, autocommit=True)
    _cleanup_postgres_ia6(conn)
    try:
        yield conn
    finally:
        _cleanup_postgres_ia6(conn)
        conn.close()


@pytest.fixture()
def ia6_client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    _cleanup_neo4j_ia6(client)
    try:
        yield client
    finally:
        _cleanup_neo4j_ia6(client)
        client.close()


async def test_iteration_a_e2e_flow(ia6_client: Neo4jClient, ia6_pg_conn) -> None:
    """T12 — flusso completo bootstrap->load->translate->verify->canonicalize->
    extract->query->recompose, con pulizia e 0 residui ``ia6_``."""
    pack = load_domain_pack(PACK_DIR)

    # 1. Bootstrap pack (idempotente, nodi condivisi non ``ia6_``).
    bootstrap = load_pack(ia6_client, PACK_DIR)
    assert bootstrap["pack_id"].startswith("ricette:1.0.")
    assert bootstrap["terms"] > 0

    # 2. Carica corpus con id test-prefissati.
    corpus = _prefix_corpus_ids(read_corpus())
    llm = build_fake_llm(pack, corpus)

    canonical_by_doc_id: dict[str, str] = {}
    for name, source_md in corpus.items():
        # 3. Traduzione P2-safe + verifica L1 + canonicalizzazione.
        translated = await translate_document(pack, source_md, llm)
        l1 = verify_l1(source_md, translated.translated_md, pack=pack)
        assert l1.passed, f"L1 failed for {name}: {l1.issues}"

        canonical = canonicalize(
            pack, translated.translated_md, conn=ia6_pg_conn
        )
        write_canon_log(ia6_pg_conn, canonical.log_entries)

        doc_id = canonical.document_id
        assert doc_id.startswith(IA6_PREFIX), doc_id
        extract_document(
            ia6_client, ia6_pg_conn, doc_id, canonical.canonical_md, pack
        )
        canonical_by_doc_id[doc_id] = canonical.canonical_md

    assert len(canonical_by_doc_id) == len(read_corpus())

    # 4. Query visibility-aware su Document e CanonicalTerm.
    viewer = Principal(
        f"{IA6_PREFIX}u_viewer", ("viewer",), (), "default", f"{IA6_PREFIX}j_viewer"
    )
    admin = Principal(
        f"{IA6_PREFIX}u_admin", ("admin",), (), "default", f"{IA6_PREFIX}j_admin"
    )

    viewer_docs = list_documents(ia6_client, viewer)
    admin_docs = list_documents(ia6_client, admin)
    assert all(not doc["id"].startswith(IA6_PREFIX) for doc in viewer_docs)
    assert {doc["id"] for doc in admin_docs} >= set(canonical_by_doc_id)

    sample_doc_id = next(iter(canonical_by_doc_id))
    assert get_document(ia6_client, viewer, sample_doc_id) is None
    admin_doc = get_document(ia6_client, admin, sample_doc_id)
    assert admin_doc is not None
    assert admin_doc["id"] == sample_doc_id
    assert admin_doc["document_id"] == sample_doc_id
    assert admin_doc["lang"] == "en"
    assert admin_doc["source_lang"] == "it"

    viewer_terms = {term["id"] for term in list_canonical_terms(ia6_client, viewer)}
    admin_terms = {term["id"] for term in list_canonical_terms(ia6_client, admin)}
    assert "ingredienti:ING-TOMATO" not in viewer_terms
    assert "ingredienti:ING-TOMATO" in admin_terms

    # 5. Round-trip: recompose == canonical.md per tutte le 15 ricette.
    for doc_id, canonical_md in canonical_by_doc_id.items():
        assert recompose_document(ia6_client, doc_id) == canonical_md, doc_id

    # 6. Pulizia e verifica 0 residui ``ia6_``.
    _cleanup_neo4j_ia6(ia6_client)
    _cleanup_postgres_ia6(ia6_pg_conn)

    assert _ia6_residue_neo4j(ia6_client) == 0
    residue = _ia6_residue_postgres(ia6_pg_conn)
    assert residue == {
        "canon_log": 0,
        "adjudications": 0,
        "glossary_proposals": 0,
    }, residue
