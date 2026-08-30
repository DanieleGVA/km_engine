"""E2E golden set retrieval (WP-B1, gate GB1).

Ingests the 15 committed recipes, populates deterministic embeddings, runs the
120-query golden set and checks:

- Recall@5 >= 0.9
- the returned recipe is *exactly* the canonical one (canonical_hash match and
  recomposed canonical.md byte-identical to the generated canonical.md)
"""
from __future__ import annotations

import hashlib
import json

from app.auth import Principal
from app.domain import canonicalize, translate_document, verify_l1
from app.domain.extract import extract_document
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm
from tests.rag.conftest import (
    GOLDEN_PATH,
    PREFIX,
    cleanup_neo4j,
    read_golden_corpus,
)


def _residue_count(client: Neo4jClient) -> int:
    with client.session() as session:
        record = session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            RETURN count(n) AS c
            """,
            prefix=PREFIX,
        ).single()
        return int(record["c"])


async def test_ib_golden_e2e_recall_and_hash_match(
    client: Neo4jClient, pack, pack_dir
) -> None:
    """GB1 — golden set Recall@5 >= 0.9 with exact canonical hash match."""
    load_pack(client, pack_dir)
    corpus = read_golden_corpus()
    llm = build_fake_llm(pack, corpus)

    canonical_by_doc_id: dict[str, str] = {}
    for name, source_md in sorted(corpus.items()):
        translated = await translate_document(pack, source_md, llm)
        l1 = verify_l1(source_md, translated.translated_md, pack=pack)
        assert l1.passed, f"L1 failed for {name}: {l1.issues}"

        canonical = canonicalize(pack, translated.translated_md)
        doc_id = f"{PREFIX}{canonical.document_id}"
        extract_document(client, None, doc_id, canonical.canonical_md, pack)
        canonical_by_doc_id[canonical.document_id] = canonical.canonical_md

        # Additive embedding extension point: extract_document does not store
        # the source title, so the RAG layer reads this optional property when
        # present (see app/rag/rag.py populate_embeddings).
        from app.domain.verify import parse_source_md

        source = parse_source_md(source_md, known_units=pack.known_units())
        with client.session() as session:
            session.run(
                "MATCH (d:Document {id: $id}) SET d.source_title = $title",
                id=doc_id,
                title=source.title,
            )

    assert len(canonical_by_doc_id) == 15

    embedding = build_embedding_from_graph(client, pack)
    populated = populate_embeddings(client, embedding)
    assert populated == 15

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    pairs = golden["pairs"]
    assert len(pairs) >= 100

    admin = Principal(
        f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin"
    )

    recall_hits = 0
    hash_matches = 0
    for pair in pairs:
        query = pair["query"]
        expected_doc_id = pair["document_id"]
        lang = pair.get("lang")

        hits = rag_query(
            client,
            admin,
            query,
            lang=lang,
            limit=5,
            embedding=embedding,
        )
        top_ids = [hit.document_id for hit in hits]
        if expected_doc_id in top_ids:
            recall_hits += 1

        matched = next(
            (hit for hit in hits if hit.document_id == expected_doc_id), None
        )
        if matched is not None:
            expected_md = canonical_by_doc_id[expected_doc_id]
            expected_hash = hashlib.sha256(expected_md.encode("utf-8")).hexdigest()
            assert matched.canonical_hash == expected_hash, pair
            assert matched.canonical_md == expected_md, pair
            hash_matches += 1

    recall = recall_hits / len(pairs)
    print(f"\nGB1 golden set: Recall@5 = {recall_hits}/{len(pairs)} = {recall:.3f}")
    assert recall >= 0.9, f"Recall@5 {recall:.3f} < 0.9"
    assert hash_matches == len(pairs)

    # Post-run cleanup and zero-residue verification.
    cleanup_neo4j(client)
    assert _residue_count(client) == 0
