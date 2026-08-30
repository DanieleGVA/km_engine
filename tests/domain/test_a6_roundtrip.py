"""WP-A6 — T11 round-trip + T7-bis idempotenza estrattore.

T11: per tutte le 15 ricette del corpus genera il canonical.md
(``translate_document`` -> ``verify_l1`` -> ``canonicalize``), lo estrae nel
grafo e lo ricompone con ``recompose_document``; il confronto è byte-identico.

T7-bis: doppio ``extract_document`` sullo stesso ``doc_id``+``canonical_hash``
produce un solo :Document e zero duplicati di :Entity/:Fact/relazioni.

Prefisso test: ``ia6_``. Pulizia Neo4j: solo nodi ``ia6_`` (il Domain Pack
reale ``ricette:1.0.0`` e i suoi :CanonicalTerm sono bootstrap condiviso e
idempotente, non residuo di test).

Nota edge ``farina 00``: ``numbers.mask_numbers`` maschera ``00`` come numero
mentre ``numbers.extract_numbers`` lo esclude (all-zero). Il round-trip NON è
bloccato (il termine resta ``wheat flour 00`` irrisolto e viene riprodotto
byte-identico), ma la risoluzione glossario degrada. Fix da fare in
``app/domain/numbers.py`` (allineare ``mask_numbers`` a ``_find_numbers``),
segnalato al parent, non applicato qui.
"""
from __future__ import annotations

import pytest

from app.domain import canonicalize, translate_document, verify_l1
from app.domain.extract import extract_document
from app.domain.recompose import recompose_document
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.conftest import PACK_DIR, read_corpus
from tests.domain.fake_llm import build_fake_llm

IA6_PREFIX = "ia6_"


def _cleanup_ia6(client: Neo4jClient) -> None:
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


@pytest.fixture()
def ia6_client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    _cleanup_ia6(client)
    try:
        yield client
    finally:
        _cleanup_ia6(client)
        client.close()


def _count(client: Neo4jClient, label: str) -> int:
    with client.session() as session:
        record = session.run(
            f"MATCH (n:{label}) WHERE n.id STARTS WITH $prefix RETURN count(n) AS c",
            prefix=IA6_PREFIX,
        ).single()
        return int(record["c"])


def _duplicate_ids(client: Neo4jClient, label: str) -> int:
    with client.session() as session:
        record = session.run(
            f"""
            MATCH (n:{label})
            WHERE n.id STARTS WITH $prefix
            WITH n.id AS id, count(*) AS c
            WHERE c > 1
            RETURN count(*) AS duplicates
            """,
            prefix=IA6_PREFIX,
        ).single()
        return int(record["duplicates"])


async def test_ia6_t11_roundtrip_all_recipes(ia6_client: Neo4jClient, pack) -> None:
    """T11 — ``recompose(extract(canonical.md)) == canonical.md`` su 15/15.

    Report per ricetta (stampato e riportato nel docstring del test):
    nome file, id canonico, byte del canonical.md, termini irrisolti.
    """
    load_pack(ia6_client, PACK_DIR)
    corpus = read_corpus()
    llm = build_fake_llm(pack, corpus)

    report: list[tuple[str, str, int, list[str]]] = []
    for name, source_md in corpus.items():
        translated = await translate_document(pack, source_md, llm)
        l1 = verify_l1(source_md, translated.translated_md, pack=pack)
        assert l1.passed, f"L1 failed for {name}: {l1.issues}"

        canonical = canonicalize(pack, translated.translated_md)
        doc_id = f"{IA6_PREFIX}{canonical.document_id}"
        extract_document(ia6_client, None, doc_id, canonical.canonical_md, pack)
        recomposed = recompose_document(ia6_client, doc_id)

        assert recomposed == canonical.canonical_md, (
            f"round-trip mismatch for {name}"
        )
        report.append(
            (
                name,
                canonical.document_id,
                len(canonical.canonical_md.encode("utf-8")),
                canonical.unresolved_terms,
            )
        )

    print("\nT11 round-trip report (15/15):")
    for name, doc_id, size, unresolved in report:
        print(
            f"  OK {name} id={doc_id} bytes={size} "
            f"unresolved={unresolved or '-'}"
        )


async def test_ia6_t7bis_double_extract_idempotent(
    ia6_client: Neo4jClient, pack
) -> None:
    """T7-bis — doppio extract: 1 Document, zero duplicati Entity/Fact/relazioni."""
    load_pack(ia6_client, PACK_DIR)
    corpus = read_corpus()
    llm = build_fake_llm(pack, corpus)

    name = "ric-001-pomodoro.md"
    translated = await translate_document(pack, corpus[name], llm)
    canonical = canonicalize(pack, translated.translated_md)
    doc_id = f"{IA6_PREFIX}{canonical.document_id}"

    first = extract_document(ia6_client, None, doc_id, canonical.canonical_md, pack)
    second = extract_document(ia6_client, None, doc_id, canonical.canonical_md, pack)

    assert first.canonical_hash == second.canonical_hash
    assert _count(ia6_client, "Document") == 1
    assert _count(ia6_client, "Entity") == first.entities
    assert _count(ia6_client, "Fact") == first.facts
    assert _count(ia6_client, "Source") == 1

    for label in ("Document", "Entity", "Fact", "Source"):
        assert _duplicate_ids(ia6_client, label) == 0, label

    with ia6_client.session() as session:
        part_of_pack = session.run(
            """
            MATCH (d:Document {id: $doc_id})-[r:PART_OF_PACK]->()
            RETURN count(r) AS c
            """,
            doc_id=doc_id,
        ).single()
        part_of_doc = session.run(
            """
            MATCH (e:Entity)-[r:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN count(r) AS c
            """,
            doc_id=doc_id,
        ).single()
        has_fact = session.run(
            """
            MATCH (e:Entity)-[r:HAS_FACT]->(f:Fact)
            WHERE e.id STARTS WITH $prefix
            RETURN count(r) AS c
            """,
            prefix=IA6_PREFIX,
        ).single()
        derived_from = session.run(
            """
            MATCH (f:Fact)-[r:DERIVED_FROM]->(s:Source)
            WHERE f.id STARTS WITH $prefix
            RETURN count(r) AS c
            """,
            prefix=IA6_PREFIX,
        ).single()

    assert int(part_of_pack["c"]) == 1
    assert int(part_of_doc["c"]) == first.entities
    assert int(has_fact["c"]) == first.facts
    assert int(derived_from["c"]) == first.facts


async def test_ia6_t7bis_normalized_to_only_resolved(
    ia6_client: Neo4jClient, pack
) -> None:
    """NORMALIZED_TO solo per termini risolti; mai per gli irrisolti."""
    load_pack(ia6_client, PACK_DIR)
    corpus = read_corpus()
    llm = build_fake_llm(pack, corpus)

    name = "ric-103-amaretti.md"
    translated = await translate_document(pack, corpus[name], llm)
    canonical = canonicalize(pack, translated.translated_md)
    doc_id = f"{IA6_PREFIX}{canonical.document_id}"
    extract_document(ia6_client, None, doc_id, canonical.canonical_md, pack)

    with ia6_client.session() as session:
        resolved = session.run(
            """
            MATCH (e:Entity {type: 'ingredient', label: 'sugar'})
              -[:PART_OF_DOC]->(d:Document {id: $doc_id})
            MATCH (e)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN t.id AS term_id
            """,
            doc_id=doc_id,
        ).single()
        unresolved = list(
            session.run(
                """
                MATCH (e:Entity)-[:PART_OF_DOC]->(d:Document {id: $doc_id})
                WHERE e.type = 'ingredient'
                  AND e.label IN ['sweet almonds sbucciate', 'bitter almonds sbucciate']
                OPTIONAL MATCH (e)-[r:NORMALIZED_TO]->(:CanonicalTerm)
                RETURN e.label AS label, count(r) AS links
                ORDER BY e.label
                """,
                doc_id=doc_id,
            )
        )

    assert resolved is not None
    assert resolved["term_id"] == "ingredienti:ING-SUGAR"
    for record in unresolved:
        assert int(record["links"]) == 0, record["label"]
