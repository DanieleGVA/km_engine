#!/usr/bin/env python
"""Benchmark scalato Iterazione E (WP-E3, GE3).

Esegue un benchmark in 6 fasi su un corpus sintetico di documenti canonici
(prefisso ``ie_bench_``) e PULISCE tutti i dati alla fine. Non usa la pipeline
di dominio completa (``extract_document``) ma un bulk-writer diretto sul grafo:
misura il throughput dello storage layer Neo4j, che e' il lower-bound del path
di ingestione. L'estrapolazione a 10GB e' nel report (docs/benchmark-report.md).

Uso:
    uv run python scripts/benchmark_e3.py --docs 1000 --concurrency 20 --queries 200
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import time
from typing import Any

from app.auth import Principal
from app.query.engine import search
from app.storage.client import Neo4jClient

PREFIX = "ie_bench_"
VIEWER = Principal(f"{PREFIX}u_viewer", ("viewer",), (), "default", f"{PREFIX}j_viewer")


def make_doc(i: int) -> dict[str, Any]:
    title = f"Bench Recipe {i}"
    md = f"""---
title: {title}
id: {PREFIX}{i}
lang: en
source_lang: it
servings: 4
time_min: 30
difficulty: easy
verification_level: L1
canonical_version: 1
---
## Ingredients
- 200 g flour
- 100 ml water
- 1 tablespoon olive oil
- 2 cloves garlic
- 1 pinch salt
## Method
1. Mix the flour and water.
2. Add the olive oil and garlic.
3. Cook for 10 minutes at 180°C.
"""
    return {
        "id": f"{PREFIX}{i}",
        "title": title,
        "md": md,
        "hash": hashlib.sha256(md.encode()).hexdigest(),
    }


def bulk_ingest(client: Neo4jClient, docs: list[dict[str, Any]]) -> None:
    """Bulk-writer diretto: Document + Source + Entity/Fact minimi per doc."""

    def work(tx: Any) -> None:
        for d in docs:
            doc_id = d["id"]
            uri = f"canonical://{doc_id}.md"
            tx.run(
                """
                MERGE (d:Document {id: $id})
                SET d.document_id = $id, d.title = $title, d.lang = 'en',
                    d.source_lang = 'it', d.canonical_hash = $hash,
                    d.verification_level = 'L1', d.translation_state = 'translated',
                    d.source_language = 'it', d.servings = 4, d.time_min = 30,
                    d.difficulty = 'easy', d.canonical_version = 1,
                    d.is_public = true, d.roles = [], d.teams = []
                """,
                id=doc_id,
                title=d["title"],
                hash=d["hash"],
            )
            tx.run(
                """
                MERGE (s:Source {id: $id})
                SET s.uri = $uri, s.type = 'file', s.hash = $hash,
                    s.language = 'en', s.ingested_at = datetime()
                """,
                id=f"{doc_id}:source",
                uri=uri,
                hash=d["hash"],
            )
            for j, ing in enumerate(["flour", "water", "olive oil", "garlic", "salt"]):
                eid = f"{doc_id}:ing:{j}"
                tx.run(
                    """
                    MERGE (e:Entity {id: $eid})
                    SET e.label = $label, e.type = 'ingredient', e.position = $pos,
                        e.source_file = $uri, e.confidence = 'EXTRACTED',
                        e.is_public = true, e.roles = [], e.teams = []
                    WITH e
                    MATCH (d:Document {id: $doc_id})
                    MERGE (e)-[:PART_OF_DOC]->(d)
                    """,
                    eid=eid,
                    label=ing,
                    pos=j,
                    uri=uri,
                    doc_id=doc_id,
                )
                tx.run(
                    """
                    MATCH (e:Entity {id: $eid})
                    MATCH (s:Source {id: $sid})
                    MERGE (f:Fact {id: $fid})
                    SET f.logical_id = $fid, f.property = 'qty', f.value = $value,
                        f.valid_from = datetime(), f.status = 'valid',
                        f.confidence = 'EXTRACTED', f.source_id = $sid
                    MERGE (e)-[:HAS_FACT]->(f)
                    MERGE (f)-[:DERIVED_FROM]->(s)
                    """,
                    eid=eid,
                    sid=f"{doc_id}:source",
                    fid=f"{eid}:qty",
                    value="200",
                )
            for j, step in enumerate(
                [
                    "Mix the flour and water.",
                    "Add the olive oil and garlic.",
                    "Cook for 10 minutes at 180°C.",
                ]
            ):
                eid = f"{doc_id}:step:{j}"
                tx.run(
                    """
                    MERGE (e:Entity {id: $eid})
                    SET e.label = $label, e.type = 'step', e.position = $pos,
                        e.source_file = $uri, e.confidence = 'EXTRACTED',
                        e.is_public = true, e.roles = [], e.teams = []
                    WITH e
                    MATCH (d:Document {id: $doc_id})
                    MERGE (e)-[:PART_OF_DOC]->(d)
                    """,
                    eid=eid,
                    label=step,
                    pos=j,
                    uri=uri,
                    doc_id=doc_id,
                )

    with client.session() as session:
        session.execute_write(work)


def await_indexes(client: Neo4jClient) -> None:
    indexes = (
        "entity_label_fulltext",
        "entity_type_fulltext",
        "fact_value_fulltext",
        "fact_property_fulltext",
        "document_title_fulltext",
        "canonical_term_label_en_fulltext",
    )
    with client.session() as session:
        for index in indexes:
            session.run("CALL db.awaitIndex($index, 30)", index=index)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def run_search_once(client: Neo4jClient, q: str) -> float:
    start = time.perf_counter()
    search(client, VIEWER, q)
    return (time.perf_counter() - start) * 1000.0


def cleanup(client: Neo4jClient) -> int:
    with client.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n RETURN count(n) AS c",
            prefix=PREFIX,
        ).single()
        return int(result["c"]) if result else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()

    client = Neo4jClient.from_env()
    client.verify_connectivity()
    cleanup(client)

    print(f"# Benchmark E3 scalato — docs={args.docs} concurrency={args.concurrency} queries={args.queries}")
    print()

    # Fase 1: generazione corpus sintetico
    t0 = time.perf_counter()
    docs = [make_doc(i) for i in range(args.docs)]
    corpus_bytes = sum(len(d["md"].encode()) for d in docs)
    gen_s = time.perf_counter() - t0
    print(f"## Fase 1 — generazione corpus: {args.docs} doc, {corpus_bytes/1e6:.2f} MB, {gen_s:.3f}s")
    print()

    # Fase 2: ingest throughput
    t0 = time.perf_counter()
    bulk_ingest(client, docs)
    ingest_s = time.perf_counter() - t0
    docs_s = args.docs / ingest_s
    mb_s = (corpus_bytes / 1e6) / ingest_s
    print(f"## Fase 2 — ingest bulk: {ingest_s:.3f}s, {docs_s:.1f} doc/s, {mb_s:.3f} MB/s")
    print()

    # Fase 3: retrieval single-user (search full-text)
    await_indexes(client)
    queries = [f"Bench Recipe {i % args.docs}" for i in range(args.queries)]
    latencies = [run_search_once(client, q) for q in queries]
    print(
        "## Fase 3 — search single-user: "
        f"p50={percentile(latencies, .50):.1f}ms p95={percentile(latencies, .95):.1f}ms "
        f"p99={percentile(latencies, .99):.1f}ms"
    )
    print()

    # Fase 4: query concorrenti (search)
    per_worker = max(1, args.queries // args.concurrency)
    worker_queries = [queries[i % len(queries)] for i in range(args.concurrency * per_worker)]

    def worker(q: str) -> float:
        return run_search_once(client, q)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        conc_latencies = list(pool.map(worker, worker_queries))
    conc_s = time.perf_counter() - t0
    print(
        "## Fase 4 — search concorrente: "
        f"{args.concurrency} worker, {len(conc_latencies)} query, wall={conc_s:.3f}s, "
        f"p50={percentile(conc_latencies, .50):.1f}ms p95={percentile(conc_latencies, .95):.1f}ms "
        f"p99={percentile(conc_latencies, .99):.1f}ms"
    )
    print()

    # Fase 5: retrieval vettoriale (rag_query) su un campione
    try:
        from app.rag.rag import (
            build_embedding_from_graph,
            populate_embeddings,
            rag_query,
        )

        embedding = build_embedding_from_graph(client)
        populated = populate_embeddings(client, embedding)
        rag_latencies: list[float] = []
        for q in queries[: min(args.queries, 100)]:
            start = time.perf_counter()
            rag_query(client, VIEWER, q, limit=5, embedding=embedding)
            rag_latencies.append((time.perf_counter() - start) * 1000.0)
        print(
            "## Fase 5 — rag_query (vettoriale): "
            f"populated={populated}, p50={percentile(rag_latencies, .50):.1f}ms "
            f"p95={percentile(rag_latencies, .95):.1f}ms p99={percentile(rag_latencies, .99):.1f}ms"
        )
    except Exception as exc:  # noqa: BLE001 - la fase vettoriale e' best-effort
        print(f"## Fase 5 — rag_query: saltata ({exc})")
    print()

    # Fase 6: pulizia e verifica
    deleted = cleanup(client)
    remaining = 0
    with client.session() as session:
        rec = session.run(
            "MATCH (n) WHERE n.id STARTS WITH $prefix RETURN count(n) AS c", prefix=PREFIX
        ).single()
        remaining = int(rec["c"]) if rec else 0
    print(f"## Fase 6 — cleanup: {deleted} nodi rimossi, residui={remaining}")
    client.close()
    return 0 if remaining == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
