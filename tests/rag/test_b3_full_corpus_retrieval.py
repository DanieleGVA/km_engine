"""B3-scale golden retrieval (gate GB3): corpus completo a 70 ricette.

Come test_ib_golden_e2e ma sull'intero corpus (70 ricette, incluse le 55
estratte automaticamente da Marchesi). Verifica:
- Recall@5 >= 0.9 sul golden set full (560 coppie deterministiche);
- hash match e canonical_md byte-identico per le coppie recuperate.
Prefisso dati: b3f_ (pulizia dedicata, non tocca ib_).
"""
from __future__ import annotations

import hashlib
import json

from app.auth import Principal
from app.domain import canonicalize, parse_source_md, translate_document, verify_l1
from app.domain.extract import extract_document
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

PREFIX = "b3f_"
FULL_GOLDEN = __import__("pathlib").Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rag_golden_full.json"


def _read_all_corpus(pack):
    from tests.rag.conftest import CORPUS_DIR
    corpus = {}
    for p in sorted(CORPUS_DIR.glob("ric-*.md")):
        corpus[p.name] = p.read_text(encoding="utf-8")
    return corpus


def _cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )


def _residue(client: Neo4jClient) -> int:
    with client.session() as session:
        rec = session.run(
            "MATCH (n) WHERE (n:Document OR n:Entity OR n:Fact OR n:Source) AND n.id STARTS WITH $prefix RETURN count(n) AS c",
            prefix=PREFIX,
        ).single()
        return int(rec["c"])


async def test_b3f_full_corpus_recall_and_hash(client, pack, pack_dir) -> None:
    load_pack(client, pack_dir)
    corpus = _read_all_corpus(pack)
    assert len(corpus) >= 60, f"corpus atteso >= 60, trovato {len(corpus)}"
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
        source = parse_source_md(source_md, known_units=pack.known_units())
        with client.session() as session:
            session.run(
                "MATCH (d:Document {id: $id}) SET d.source_title = $title",
                id=doc_id,
                title=source.title,
            )

    embedding = build_embedding_from_graph(client, pack)
    populated = populate_embeddings(client, embedding)
    assert populated == len(corpus), f"embedding attesi {len(corpus)}, popolati {populated}"

    golden = json.loads(FULL_GOLDEN.read_text(encoding="utf-8"))
    pairs = golden["pairs"]
    assert len(pairs) >= 500

    admin = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")

    recall_hits = hash_matches = evaluated = 0
    for pair in pairs:
        expected = pair["document_id"]
        hits = rag_query(client, admin, pair["query"], lang=pair.get("lang"), limit=5, embedding=embedding)
        top_ids = [h.document_id for h in hits]
        evaluated += 1
        if expected in top_ids:
            recall_hits += 1
        matched = next((h for h in hits if h.document_id == expected), None)
        if matched is not None:
            expected_md = canonical_by_doc_id[expected]
            expected_hash = hashlib.sha256(expected_md.encode("utf-8")).hexdigest()
            if matched.canonical_hash == expected_hash and matched.canonical_md == expected_md:
                hash_matches += 1

    recall = recall_hits / evaluated
    print(f"\nGB3 full corpus: Recall@5 = {recall_hits}/{evaluated} = {recall:.3f} | hash match = {hash_matches}/{recall_hits}")
    assert recall >= 0.9, f"Recall@5 {recall:.3f} < 0.9 su corpus completo"
    assert hash_matches == recall_hits, "ogni hit recuperato deve essere ESATTAMENTE il canonico"

    _cleanup(client)
    assert _residue(client) == 0, "residui b3f_ presenti dopo la pulizia"
