"""Shared fixtures for Iteration C agent tests (WP-C1..C4).

Neo4j cleanup is scoped to the ``ic_`` prefix used by the agent pipeline; the
Domain Pack bootstrap nodes (``ricette:1.0.0`` and its :CanonicalTerm nodes)
are shared, idempotent bootstrap data, exactly like the Iteration-A tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from app.agents import analyze_corpus, translate_corpus
from app.domain import load_domain_pack
from app.storage.client import Neo4jClient
from tests.domain.fake_llm import build_fake_llm

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "domain-packs" / "ricette"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_ricette"
STAGING_DIR = REPO_ROOT / "domain-packs" / "ricette-agents-draft"
BRIEF_DIR = REPO_ROOT / "docs" / "domain-briefs"

IC_PREFIX = "ic_"


def read_corpus() -> dict[str, str]:
    """Return ``{filename: markdown}`` for the full 70-recipe corpus."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("ric-*.md"))
    }


def pilot_corpus() -> dict[str, str]:
    """Return the 15-recipe validated pilot (ric-0xx + ric-1xx)."""
    return {
        name: text
        for name, text in read_corpus().items()
        if name.startswith(("ric-0", "ric-1"))
    }


def _manual_term_ids() -> set[str]:
    """Return the committed pack's CanonicalTerm ids (``namespace:term_id``)."""
    pack = load_domain_pack(PACK_DIR)
    return {
        f"{namespace}:{entry.id}"
        for namespace in ("tecnica", "ingredienti", "stati")
        for entry in getattr(pack.glossaries, namespace).entries
    }


def cleanup_ic(client: Neo4jClient) -> None:
    """Delete agent-pipeline data and restore the committed pack bootstrap.

    The draft pack shares the ``ricette`` pack id and a few technique/state
    term ids with the manual pack, so ``load_pack(draft)`` can overwrite those
    shared :CanonicalTerm nodes. Cleanup therefore (1) removes the ``ic_``
    graph data, (2) removes draft-only :CanonicalTerm nodes, and (3) re-loads
    the committed pack to restore the shared bootstrap terms.
    """
    from scripts.load_domain_pack import load_pack

    manual_ids = _manual_term_ids()
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=IC_PREFIX,
        )
        session.run(
            """
            MATCH (t:CanonicalTerm)
            WHERE t.namespace IN ['tecnica', 'ingredienti', 'stati']
              AND NOT t.id IN $manual_ids
            DETACH DELETE t
            """,
            manual_ids=list(manual_ids),
        )
    load_pack(client, PACK_DIR)


@pytest.fixture()
def ic_client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    cleanup_ic(client)
    try:
        yield client
    finally:
        cleanup_ic(client)
        client.close()


@pytest_asyncio.fixture
async def pilot_brief():
    """Build the deterministic DomainBrief from the 15-recipe pilot."""
    pack = load_domain_pack(PACK_DIR)
    corpus = pilot_corpus()
    llm = build_fake_llm(pack, corpus)
    translated = await translate_corpus(pack, corpus, llm)
    return analyze_corpus(corpus, translated)
