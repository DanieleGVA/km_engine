"""Parity test: km_engine code extraction vs graphify on the same corpus.

The comparison is at extraction level (labels and relation triples), not UUIDs.
Graphify is a workspace dependency, so the reference run is executed live in
the same environment. The markdown file in the corpus is intentionally ignored
here: code parity covers the Python files, while the document pass is covered
by the FR9 pipeline test.
"""

from __future__ import annotations

from pathlib import Path

from app.ingest.config import IngestSettings
from app.ingest.pipeline import IngestPipeline
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

CORPUS = Path(__file__).parent.parent / "fixtures" / "wp4_corpus"


def _graphify_code_reference(tmp_path: Path) -> tuple[set[str], set[tuple[str, str, str]]]:
    from graphify.build import build as graphify_build
    from graphify.extract import extract as graphify_extract

    corpus = CORPUS.resolve()
    files = sorted(p for p in corpus.rglob("*") if p.is_file())
    raw = graphify_extract(
        files, cache_root=tmp_path / "graphify_cache", root=corpus, parallel=False
    )
    graph = graphify_build([raw], dedup=True, root=corpus)

    labels: set[str] = set()
    edges: set[tuple[str, str, str]] = set()
    for nid, data in graph.nodes(data=True):
        if data.get("file_type") == "code":
            labels.add(str(data.get("label")))
    for u, v, data in graph.edges(data=True):
        if graph.nodes[u].get("file_type") != "code":
            continue
        if graph.nodes[v].get("file_type") != "code":
            continue
        src = data.get("_src")
        tgt = data.get("_tgt")
        if src is None or tgt is None:
            continue
        edges.add(
            (
                str(graph.nodes[src].get("label")),
                str(data.get("relation")),
                str(graph.nodes[tgt].get("label")),
            )
        )
    return labels, edges


def _km_code_actual(
    client: Neo4jClient,
) -> tuple[set[str], set[tuple[str, str, str]]]:
    labels: set[str] = set()
    edges: set[tuple[str, str, str]] = set()
    with client.session() as session:
        for record in session.run(
            """
            MATCH (e:Entity)
            WHERE e.id STARTS WITH 'wp4_' AND e.type = 'code'
            RETURN e.label AS label
            """
        ):
            labels.add(str(record["label"]))
        for record in session.run(
            """
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.id STARTS WITH 'wp4_'
              AND b.id STARTS WITH 'wp4_'
              AND a.type = 'code'
              AND b.type = 'code'
            RETURN a.label AS src, r.relation AS rel, b.label AS tgt
            """
        ):
            edges.add(
                (str(record["src"]), str(record["rel"]), str(record["tgt"]))
            )
    return labels, edges


def test_code_extraction_parity_with_graphify(
    repo: GraphRepository, client: Neo4jClient, conn, tmp_path: Path
) -> None:
    expected_labels, expected_edges = _graphify_code_reference(tmp_path)

    settings = IngestSettings(
        chunk_size=10,
        cache_dir=tmp_path / "km_cache",
    )
    pipeline = IngestPipeline(
        repo=repo,
        client=client,
        conn=conn,
        settings=settings,
    )
    job = pipeline.run("wp4_parity", CORPUS, job_type="code", chunk_size=10)
    assert job.status == "completed"

    actual_labels, actual_edges = _km_code_actual(client)

    assert actual_labels == expected_labels
    assert actual_edges == expected_edges
