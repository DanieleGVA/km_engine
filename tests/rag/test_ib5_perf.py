"""WP-B5 performance tests (gate GB5): TTL caches + micro-benchmark.

- Cache tests (standard suite): the second call to
  ``build_embedding_from_graph`` / ``recompose_document`` / ``rag_query`` must
  not re-run the graph work; ``extract_document`` must invalidate the caches.
- Micro-benchmark (``-m perf``): 200 ``rag_query`` calls on 70 documents,
  caches disabled (pre-optimisation baseline) vs enabled (WP-B5). Run with::

      uv run pytest tests/rag/test_ib5_perf.py -m perf -q

The standard suite excludes ``-m perf`` tests (see ``pyproject.toml``
``addopts``).
"""
from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

import pytest

from app.domain.embedding import DeterministicEmbedding
from app.domain.recompose import recompose_document
from app.rag.cache import invalidate_rag_caches, recompose_cache, set_cache_ttl
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from tests.rag.conftest import GOLDEN_PATH, create_document, link_entity_to_document

BENCH_PREFIX = "ib_bench_"
BENCH_DOCS = 70
BENCH_QUERIES = 200

MINIMAL_CANONICAL_MD = """---
title: New doc
id: ib_bench_new
lang: en
source_lang: it
servings: 2
time_min: 10
difficulty: easy
verification_level: L1
canonical_version: 1
---
## Ingredients
- 1 garlic
## Method
1. chop
"""


def _pct(values: list[float], p: float) -> float:
    return statistics.quantiles(sorted(values), n=100, method="inclusive")[int(p) - 1]


def _fmt(lat: list[float]) -> str:
    return (
        f"p50={_pct(lat, 50) * 1000:.1f}ms "
        f"p95={_pct(lat, 95) * 1000:.1f}ms "
        f"p99={_pct(lat, 99) * 1000:.1f}ms "
        f"mean={statistics.mean(lat) * 1000:.1f}ms"
    )


# ---------------------------------------------------------------------------
# Cache behaviour (standard suite)
# ---------------------------------------------------------------------------

def test_ib5_vocab_cache_second_call_no_recompute(
    client: Neo4jClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP-B5: build_embedding_from_graph is cached; the second call does not
    recompute the vocabulary; invalidation forces a fresh build."""
    calls = {"n": 0}
    original = DeterministicEmbedding.from_texts

    def spy(cls, texts):
        calls["n"] += 1
        return original(texts)

    monkeypatch.setattr(DeterministicEmbedding, "from_texts", classmethod(spy))

    first = build_embedding_from_graph(client)
    second = build_embedding_from_graph(client)
    assert calls["n"] == 1, "second call must not recompute the vocabulary"
    assert first is second, "cached call must return the same instance"

    invalidate_rag_caches()
    third = build_embedding_from_graph(client)
    assert calls["n"] == 2, "invalidation must force a fresh build"
    assert third is not first


def test_ib5_recompose_cache_second_call_no_recompute(
    client: Neo4jClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP-B5: recompose_document is cached per document; the second call opens
    no new Neo4j session."""
    doc_id = f"{BENCH_PREFIX}rec"
    create_document(client, doc_id, title="cached pasta", is_public=True)
    link_entity_to_document(
        client, f"{doc_id}:ent0", doc_id, label="garlic", entity_type="ingredient"
    )

    calls = {"n": 0}
    original_session = client.session

    def counting_session(**kwargs):
        calls["n"] += 1
        return original_session(**kwargs)

    monkeypatch.setattr(client, "session", counting_session)

    md1 = recompose_document(client, doc_id)
    first_calls = calls["n"]
    md2 = recompose_document(client, doc_id)
    assert calls["n"] == first_calls, "second call must not open a session"
    assert md1 == md2
    assert recompose_cache.get((client.config.uri, doc_id)) == md1


def test_ib5_rag_query_cache_reduces_graph_roundtrips(
    client: Neo4jClient, monkeypatch: pytest.MonkeyPatch, principal_admin
) -> None:
    """WP-B5: after warm-up, a repeated rag_query runs only the vector query
    (context + recompose are served from the TTL caches)."""
    embedding = build_embedding_from_graph(client)
    for i, title in enumerate(["tomato pasta", "garlic bread", "olive oil cake"]):
        create_document(client, f"{BENCH_PREFIX}q{i}", title=title, is_public=True)
    populate_embeddings(client, embedding)

    calls = {"n": 0}
    original_session = client.session

    def counting_session(**kwargs):
        calls["n"] += 1
        return original_session(**kwargs)

    monkeypatch.setattr(client, "session", counting_session)

    hits1 = rag_query(
        client, principal_admin, "tomato", lang="it", limit=3, embedding=embedding
    )
    first_calls = calls["n"]
    hits2 = rag_query(
        client, principal_admin, "tomato", lang="it", limit=3, embedding=embedding
    )
    second_calls = calls["n"] - first_calls

    assert first_calls >= 1
    assert second_calls == 1, (
        "repeated query must run only the vector query, "
        f"got {second_calls} sessions"
    )
    assert [h.document_id for h in hits1] == [h.document_id for h in hits2]


def test_ib5_extract_invalidates_rag_caches(
    client: Neo4jClient, pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP-B5: extract_document (ingest) invalidates vocab + recompose caches."""
    from app.domain.extract import extract_document

    embedding = build_embedding_from_graph(client, pack)
    vocab_key = (client.config.uri, f"{pack.pack.name}:{pack.pack.version}")
    from app.rag.cache import vocab_cache

    assert vocab_cache.get(vocab_key) is embedding

    doc_id = f"{BENCH_PREFIX}inv"
    create_document(client, doc_id, title="invalidation target", is_public=True)
    recompose_document(client, doc_id)
    assert recompose_cache.get((client.config.uri, doc_id)) is not None

    calls = {"n": 0}
    original = invalidate_rag_caches

    def spy():
        calls["n"] += 1
        original()

    monkeypatch.setattr("app.domain.extract.invalidate_rag_caches", spy)

    extract_document(client, None, f"{BENCH_PREFIX}new", MINIMAL_CANONICAL_MD, pack)

    assert calls["n"] == 1, "extract_document must invalidate the RAG caches"
    assert vocab_cache.get(vocab_key) is None
    assert recompose_cache.get((client.config.uri, doc_id)) is None


# ---------------------------------------------------------------------------
# Micro-benchmark (-m perf)
# ---------------------------------------------------------------------------

def _load_benchmark_corpus(client: Neo4jClient, pack) -> DeterministicEmbedding:
    """Create BENCH_DOCS documents (real corpus titles) + embeddings."""
    corpus_dir = (
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "corpus_ricette"
    )
    files = sorted(corpus_dir.glob("ric-*.md"))[:BENCH_DOCS]
    assert len(files) == BENCH_DOCS
    for i, path in enumerate(files):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
        title = match.group(1).strip() if match else path.stem
        doc_id = f"{BENCH_PREFIX}{i:03d}"
        create_document(client, doc_id, title=title, is_public=True)
        link_entity_to_document(
            client,
            f"{doc_id}:ent0",
            doc_id,
            label=title.split()[0].lower(),
            entity_type="ingredient",
        )
    embedding = build_embedding_from_graph(client, pack)
    populated = populate_embeddings(client, embedding)
    assert populated == BENCH_DOCS
    return embedding


@pytest.mark.perf
def test_ib5_micro_benchmark_200_rag_queries(
    client: Neo4jClient, pack, principal_admin
) -> None:
    """WP-B5: 200 rag queries on 70 documents — caches off vs on.

    Measures p50/p95/p99/mean latency for both phases and asserts the cached
    phase improves p95 (and stays under the 2s NFR1 signal on dev).
    """
    _load_benchmark_corpus(client, pack)
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    pairs = golden["pairs"]
    queries = [(pair["query"], pair.get("lang")) for pair in pairs]
    queries = (queries * 2)[:BENCH_QUERIES]
    assert len(queries) == BENCH_QUERIES

    def run_phase() -> list[float]:
        latencies: list[float] = []
        for query, lang in queries:
            t0 = time.perf_counter()
            # embedding=None mirrors the API path (get_embedding_service):
            # build_embedding_from_graph runs per query when uncached.
            rag_query(client, principal_admin, query, lang=lang, limit=5)
            latencies.append(time.perf_counter() - t0)
        return latencies

    set_cache_ttl(0)  # pre-optimisation baseline: no TTL caches
    before = run_phase()
    set_cache_ttl(300)  # WP-B5: TTL caches enabled
    after = run_phase()

    print(f"\n[ib5 perf] {BENCH_QUERIES} rag queries, {BENCH_DOCS} documents")
    print(f"  BEFORE (no cache): {_fmt(before)}")
    print(f"  AFTER  (TTL cache): {_fmt(after)}")

    assert _pct(after, 95) < _pct(before, 95), "p95 must improve with caches"
    assert _pct(after, 95) < 2.0, "NFR1 signal: p95 < 2s on dev single-instance"
