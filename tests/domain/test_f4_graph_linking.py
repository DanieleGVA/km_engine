"""WP-F4 — il grafo riflette la risoluzione: NORMALIZED_TO, stati, preparazione.

Il gate GF4 chiede che, dopo l'ingest, il rapporto fra Entity ingrediente
collegate a un CanonicalTerm e Entity ingrediente totali sia >= 0,68. Qui si
misura su un campione del corpus reale (non sull'intero libro: l'ingest di
1462 ricette non e' un test unitario) e si verifica che il rapporto segua la
copertura misurata dallo strumento di WP-F0.
"""
from __future__ import annotations

import pytest

from app.domain import canonicalize, translate_document
from app.domain.coverage import measure_documents
from app.domain.extract import extract_document
from app.domain.recompose import recompose_document
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.conftest import PACK_DIR, REPO_ROOT
from tests.domain.fake_llm import build_fake_llm

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"
SAMPLE_SIZE = 60
IF4_PREFIX = "if4_"
GATE_LINK_RATIO = 0.68


def _cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=IF4_PREFIX,
        )


@pytest.fixture()
def if4_client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    _cleanup(client)
    try:
        yield client
    finally:
        _cleanup(client)
        client.close()


@pytest.fixture(scope="module")
def sample() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("*.md"))[:SAMPLE_SIZE]
    }


async def _ingest(pack, client, sample) -> dict[str, str]:
    load_pack(client, PACK_DIR)
    llm = build_fake_llm(pack, sample)
    canonical_docs: dict[str, str] = {}
    for name, source_md in sorted(sample.items()):
        translated = await translate_document(pack, source_md, llm)
        canonical = canonicalize(pack, translated.translated_md)
        doc_id = f"{IF4_PREFIX}{canonical.document_id}"
        extract_document(client, None, doc_id, canonical.canonical_md, pack)
        canonical_docs[doc_id] = canonical.canonical_md
    return canonical_docs


async def test_f4_normalized_to_ratio_meets_the_gate(pack, if4_client, sample) -> None:
    """GF4: >= 68% delle Entity ingrediente sono collegate a un CanonicalTerm."""
    canonical_docs = await _ingest(pack, if4_client, sample)

    with if4_client.session() as session:
        record = session.run(
            """
            MATCH (e:Entity {type: 'ingredient'})-[:PART_OF_DOC]->(d:Document)
            WHERE d.id STARTS WITH $prefix
            OPTIONAL MATCH (e)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN count(e) AS total, count(t) AS linked
            """,
            prefix=IF4_PREFIX,
        ).single()

    total, linked = record["total"], record["linked"]
    assert total > 0
    ratio = linked / total
    assert ratio >= GATE_LINK_RATIO, (
        f"NORMALIZED_TO {linked}/{total} = {ratio:.2%}, gate {GATE_LINK_RATIO:.0%}"
    )

    # Il grafo non puo' essere piu' collegato di quanto la misura dica: se lo
    # fosse, uno dei due starebbe usando una normalizzazione diversa.
    measured = measure_documents(pack, canonical_docs, stage="translated")
    assert ratio == pytest.approx(measured.coverage, abs=0.02)


async def test_f4_states_and_prep_reach_the_graph(pack, if4_client) -> None:
    """Stati e preparazione diventano Fact, e lo stato punta al suo termine."""
    md = (
        "---\n"
        "title: Test\nid: if4-STATE\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n"
        "verification_level: L1\ncanonical_version: 1\n---\n"
        "## Ingredients\n"
        "- 120 g sweet almonds [peeled]\n"
        "- 1 lemon (juice)\n"
        "## Method\n"
        "1. Cook.\n"
    )
    load_pack(if4_client, PACK_DIR)
    doc_id = f"{IF4_PREFIX}if4-STATE"
    extract_document(if4_client, None, doc_id, md, pack)

    with if4_client.session() as session:
        states = session.run(
            """
            MATCH (e:Entity {type: 'ingredient'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            MATCH (e)-[:HAS_FACT]->(f:Fact {property: 'state'})
            OPTIONAL MATCH (f)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN f.value AS value, t.id AS term_id
            """,
            doc_id=doc_id,
        ).data()
        preps = session.run(
            """
            MATCH (e:Entity {type: 'ingredient'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            MATCH (e)-[:HAS_FACT]->(f:Fact {property: 'prep'})
            RETURN f.value AS value
            """,
            doc_id=doc_id,
        ).data()

    assert [row["value"] for row in states] == ["peeled"]
    assert states[0]["term_id"] == "stati:STA-SBUCCIATO"
    assert [row["value"] for row in preps] == ["juice"]


async def test_f4_roundtrip_preserves_states_and_prep(pack, if4_client) -> None:
    """T11 con stati e preparazione: il markdown ricomposto e' byte-identico."""
    md = (
        "---\n"
        "title: Test\nid: if4-RT\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n"
        "verification_level: L1\ncanonical_version: 1\n---\n"
        "## Ingredients\n"
        "- 120 g sweet almonds [peeled]\n"
        "- 1 lemon (juice)\n"
        "- to taste salt\n"
        "- 2-3 egg\n"
        "## Method\n"
        "1. Cook.\n"
    )
    load_pack(if4_client, PACK_DIR)
    doc_id = f"{IF4_PREFIX}if4-RT"
    extract_document(if4_client, None, doc_id, md, pack)
    assert recompose_document(if4_client, doc_id) == md
