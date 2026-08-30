"""Retrieval dal libro al grafo (richiesta committente).

Prende ricette REALI dal libro "La cucina italiana. Il grande ricettario"
(Gualtiero Marchesi) — testo grezzo salvato in tests/fixtures/book_recipes/
marchesi_raw.json — e verifica che il RAG le ritrovi nel grafo:

- 3 ricette presenti nel corpus (RIC-101 asparagi, RIC-102 fregola, RIC-103
  amaretti): query naturali costruite dal testo grezzo (titolo IT, titolo EN,
  ingredienti) -> il documento canonico ESATTO deve essere in top-5 con hash
  match (canonical_md byte-identico).
- 1 ricetta NON nel corpus (bavettine sul pesce): il sistema deve rispondere
  senza errori con il match piu' vicino (comportamento documentato).

Prefisso dati: ibk_ (pulizia dedicata, indice vettoriale ricreato).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from app.auth import Principal
from app.domain import canonicalize, parse_source_md, translate_document, verify_l1
from app.domain.extract import extract_document
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

PREFIX = "ibk_"
REPO = pathlib.Path(__file__).resolve().parents[2]
BOOK_RAW = REPO / "tests" / "fixtures" / "book_recipes" / "marchesi_raw.json"

# (chiave fixture, document_id atteso nel corpus, [query naturali dal libro])
BOOK_CASES = [
    (
        "asparagi-al-burro",
        "RIC-101",
        [
            "asparagi al burro",
            "ricetta asparagi al burro",
            "recipe with asparagus and butter",
            "asparagus with butter and grana",
        ],
    ),
    (
        "fregola-con-le-vongole",
        "RIC-102",
        [
            "fregola con le vongole",
            "ricetta fregola con le vongole",
            "recipe with clams and fregola",
            "fregola pasta with clams and tomato sauce",
        ],
    ),
    (
        "amaretti",
        "RIC-103",
        [
            "amaretti",
            "ricetta amaretti",
            "amaretti with almonds and sugar",
            "almond cookies with egg whites",
        ],
    ),
]

# Ricetta NON nel corpus: il sistema deve rispondere senza errori.
NOT_IN_CORPUS = "bavettine-sul-pesce"


def _recreate_vector_index(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run("DROP INDEX document_embedding_vector IF EXISTS")
        session.run(
            """
            CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS
            FOR (d:Document) ON (d.embedding)
            OPTIONS {indexConfig: {
                `vector.dimensions`: 384,
                `vector.similarity_function`: 'cosine'
            }}
            """
        )


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


def _read_all_corpus():
    from tests.rag.conftest import CORPUS_DIR
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(CORPUS_DIR.glob("ric-*.md"))
    }


async def test_ibk_book_recipes_found_in_graph(client, pack, pack_dir) -> None:
    """Le ricette del libro presenti nel corpus vengono ritrovate dal RAG."""
    _recreate_vector_index(client)
    load_pack(client, pack_dir)
    corpus = _read_all_corpus()
    llm = build_fake_llm(pack, corpus)

    canonical_by_doc_id: dict[str, str] = {}
    try:
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
        assert populated == len(corpus)

        raw = json.loads(BOOK_RAW.read_text(encoding="utf-8"))
        admin = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")

        found = 0
        for key, expected_doc, queries in BOOK_CASES:
            assert key in raw, f"fixture libro mancante: {key}"
            for query in queries:
                hits = rag_query(client, admin, query, lang="it", limit=5, embedding=embedding)
                top_ids = [h.document_id for h in hits]
                matched = next((h for h in hits if h.document_id == expected_doc), None)
                assert expected_doc in top_ids, (
                    f"[{key}] query {query!r}: atteso {expected_doc}, top5={top_ids}"
                )
                expected_md = canonical_by_doc_id[expected_doc]
                expected_hash = hashlib.sha256(expected_md.encode("utf-8")).hexdigest()
                assert matched.canonical_hash == expected_hash, f"[{key}] hash mismatch per {query!r}"
                assert matched.canonical_md == expected_md, f"[{key}] md non identico per {query!r}"
                found += 1
        print(f"\n[ibk] ricette libro trovate: {found}/{sum(len(q) for _, _, q in BOOK_CASES)} query")

        # Ricetta NON nel corpus: risposta senza errori, match piu' vicino.
        assert NOT_IN_CORPUS in raw
        hits = rag_query(client, admin, "bavettine sul pesce", lang="it", limit=5, embedding=embedding)
        assert hits, "attesa almeno una risposta per ricetta fuori corpus"
        print(f"[ibk] fuori corpus -> top1: {hits[0].document_id} ({hits[0].title})")
    finally:
        _cleanup(client)
