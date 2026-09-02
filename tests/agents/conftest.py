"""Shared fixtures for Iteration C agent tests (WP-C1..C4).

Neo4j cleanup is scoped to the ``ic_`` prefix used by the agent pipeline; the
Domain Pack bootstrap nodes (``ricette:1.0.0`` and its :CanonicalTerm nodes)
are shared, idempotent bootstrap data, exactly like the Iteration-A tests.
"""
from __future__ import annotations

import os
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
    return analyze_corpus(
        corpus,
        translated,
        known_units=pack.known_units(),
        countable_units=pack.countable_units(),
    )


# ---------------------------------------------------------------------------
# WP-C5/C6 — Curator + Documenter fixtures (ic5_ prefix)
# ---------------------------------------------------------------------------

IC5_PREFIX = "ic5_"
IC5_TEST_PASSWORD = "ic5-test-password-123"

TEST_PG_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)


def cleanup_ic5_postgres(conn) -> None:
    """Delete only the rows created by Curator/Documenter tests (ic5_ prefix)."""
    with conn.transaction():
        conn.execute(
            "DELETE FROM canon_log WHERE document_id LIKE %s", (f"{IC5_PREFIX}%",)
        )
        conn.execute(
            "DELETE FROM glossary_proposals WHERE term LIKE %s OR context LIKE %s",
            (f"{IC5_PREFIX}%", f"{IC5_PREFIX}%"),
        )
        conn.execute(
            "DELETE FROM adjudications WHERE document_id LIKE %s", (f"{IC5_PREFIX}%",)
        )
        conn.execute(
            "DELETE FROM conflicts WHERE entity_id LIKE %s", (f"{IC5_PREFIX}%",)
        )
        user_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM users WHERE username LIKE %s", (f"{IC5_PREFIX}%",)
            ).fetchall()
        ]
        if user_ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (user_ids, [str(i) for i in user_ids]),
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (user_ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE %s", (f"{IC5_PREFIX}%",))


@pytest.fixture()
def ic5_pg_conn():
    import psycopg

    conn = psycopg.connect(TEST_PG_DSN, autocommit=True)
    cleanup_ic5_postgres(conn)
    try:
        yield conn
    finally:
        cleanup_ic5_postgres(conn)
        conn.close()


@pytest.fixture()
def ic5_user(ic5_pg_conn):
    from app.auth.users import create_user

    return create_user(
        ic5_pg_conn,
        f"{IC5_PREFIX}resolver",
        f"{IC5_PREFIX}resolver@example.test",
        IC5_TEST_PASSWORD,
        roles=("admin",),
    )


def injected_modifier_corpus() -> dict[str, str]:
    """A small corpus with five modifier ambiguities injected.

    Each ingredient contains a base glossary term plus a non-alias modifier
    (``sbucciate``, ``a cubetti``, ``aromatizzato``, ``tritato``, ``fresco``).
    """
    return {
        "ic5-ric-001.md": """---
title: Mandorle dolci sbucciate
id: ic5_RIC_001
lang: it
servings: 2
time_min: 10
difficulty: facile
---
## Ingredienti
- 100 g mandorle dolci sbucciate
- 1 pizzico sale
## Procedimento
1. Tostare le mandorle.
""",
        "ic5-ric-002.md": """---
title: Pomodori a cubetti
id: ic5_RIC_002
lang: it
servings: 2
time_min: 15
difficulty: facile
---
## Ingredienti
- 200 g pomodori pelati a cubetti
- 1 cucchiaio olio extravergine di oliva
## Procedimento
1. Rosolare i pomodori.
""",
        "ic5-ric-003.md": """---
title: Olio aromatizzato
id: ic5_RIC_003
lang: it
servings: 1
time_min: 5
difficulty: facile
---
## Ingredienti
- 50 ml olio extravergine di oliva aromatizzato
- 1 spicchio aglio
## Procedimento
1. Scaldare l'olio.
""",
        "ic5-ric-004.md": """---
title: Aglio tritato
id: ic5_RIC_004
lang: it
servings: 1
time_min: 5
difficulty: facile
---
## Ingredienti
- 1 spicchio aglio tritato
- 1 pizzico sale
## Procedimento
1. Soffriggere l'aglio.
""",
        "ic5-ric-005.md": """---
title: Basilico fresco
id: ic5_RIC_005
lang: it
servings: 1
time_min: 5
difficulty: facile
---
## Ingredienti
- 5 foglie basilico fresco
- 1 pizzico sale
## Procedimento
1. Aggiungere il basilico.
""",
        "ic5-ric-006.md": """---
title: Riso semplice
id: ic5_RIC_006
lang: it
servings: 2
time_min: 20
difficulty: facile
---
## Ingredienti
- 200 g riso carnaroli
- 1 pizzico sale
## Procedimento
1. Cuocere il riso.
""",
        "ic5-ric-007.md": """---
title: Spaghetti aglio e olio
id: ic5_RIC_007
lang: it
servings: 2
time_min: 15
difficulty: facile
---
## Ingredienti
- 200 g spaghetti
- 2 cucchiai olio extravergine di oliva
- 1 spicchio aglio
## Procedimento
1. Cuocere gli spaghetti.
""",
    }


def cleanup_ic5_neo4j(client: Neo4jClient) -> None:
    """Delete ic5_ graph data and restore the committed pack bootstrap."""
    from scripts.load_domain_pack import load_pack

    manual_ids = _manual_term_ids()
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=IC5_PREFIX,
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
def ic5_client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    cleanup_ic5_neo4j(client)
    try:
        yield client
    finally:
        cleanup_ic5_neo4j(client)
        client.close()
