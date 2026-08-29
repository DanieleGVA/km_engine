"""End-to-end tests for the chunked, incremental, resumable pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingest.config import IngestSettings
from app.ingest.extractor import CodeExtractor
from app.ingest.models import ExtractionResult
from app.ingest.pipeline import IngestPipeline
from app.ingest.semantic import StubSemanticService
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository


class RecordingExtractor(CodeExtractor):
    """Deterministic fake extractor: one entity per file, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def extract(self, paths: list[Path], root: Path) -> ExtractionResult:
        self.calls.append(sorted(p.name for p in paths))
        nodes = []
        for path in paths:
            rel = path.relative_to(root).as_posix()
            node_id = rel.replace("/", "_").replace(".", "_")
            nodes.append(
                {
                    "id": node_id,
                    "label": path.stem,
                    "file_type": "code",
                    "source_file": rel,
                    "source_location": "L1",
                }
            )
        return ExtractionResult(nodes=nodes, edges=[])


def _make_pipeline(
    repo: GraphRepository,
    client: Neo4jClient,
    conn,
    settings: IngestSettings,
    extractor: CodeExtractor | None = None,
) -> IngestPipeline:
    return IngestPipeline(
        repo=repo,
        client=client,
        conn=conn,
        settings=settings,
        code_extractor=extractor,
        semantic_service=StubSemanticService(),
    )


def _neo4j_labels(client: Neo4jClient) -> set[str]:
    with client.session() as session:
        rows = session.run(
            "MATCH (e:Entity) WHERE e.id STARTS WITH 'wp4_' RETURN e.label AS label"
        )
        return {r["label"] for r in rows}


def test_code_ingestion_writes_entities_facts_sources_and_relations(
    repo: GraphRepository, client: Neo4jClient, conn, settings: IngestSettings
) -> None:
    corpus = Path(__file__).parent.parent / "fixtures" / "wp4_corpus"
    pipeline = _make_pipeline(repo, client, conn, settings)
    job = pipeline.run("wp4_pipeline_code", corpus, job_type="code", chunk_size=2)

    assert job.status == "completed"
    assert job.progress == 100

    labels = _neo4j_labels(client)
    assert "Calculator" in labels
    assert "add" in labels or "add()" in labels
    assert "main()" in labels

    with client.session() as session:
        relations = session.run(
            """
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.id STARTS WITH 'wp4_' AND b.id STARTS WITH 'wp4_'
            RETURN a.label AS src, r.relation AS rel, b.label AS tgt
            """
        )
        rel_triples = {(r["src"], r["rel"], r["tgt"]) for r in relations}
        assert any(rel == "imports" for _, rel, _ in rel_triples)
        assert any(rel == "calls" for _, rel, _ in rel_triples)

        sources = session.run(
            "MATCH (s:Source) WHERE s.id STARTS WITH 'wp4_' RETURN s.uri AS uri, s.hash AS hash"
        )
        source_rows = list(sources)
        assert len(source_rows) >= 3
        for row in source_rows:
            path = Path(row["uri"])
            assert path.exists()
            assert row["hash"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_incremental_run_only_reprocesses_changed_files(
    repo: GraphRepository, client: Neo4jClient, conn, settings: IngestSettings, tmp_path: Path
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.py").write_text("def a(): pass\n")
    (root / "b.py").write_text("def b(): pass\n")
    (root / "c.py").write_text("def c(): pass\n")

    extractor = RecordingExtractor()
    pipeline = _make_pipeline(repo, client, conn, settings, extractor=extractor)

    first = pipeline.run("wp4_pipeline_incr", root, job_type="code", chunk_size=2)
    assert first.status == "completed"
    assert extractor.calls == [["a.py", "b.py"], ["c.py"]]

    # No changes: the extractor must not be called again.
    second = pipeline.run("wp4_pipeline_incr", root, job_type="code", chunk_size=2)
    assert second.status == "completed"
    assert extractor.calls == [["a.py", "b.py"], ["c.py"]]

    # Change one file: only that file is re-extracted.
    (root / "a.py").write_text("def a(): return 1\n")
    third = pipeline.run("wp4_pipeline_incr", root, job_type="code", chunk_size=2)
    assert third.status == "completed"
    assert extractor.calls == [["a.py", "b.py"], ["c.py"], ["a.py"]]


def test_resume_continues_from_persisted_position(
    repo: GraphRepository, client: Neo4jClient, conn, settings: IngestSettings, tmp_path: Path
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text(f"def {name[0]}(): pass\n")

    extractor = RecordingExtractor()
    pipeline = _make_pipeline(repo, client, conn, settings, extractor=extractor)

    paused = pipeline.run(
        "wp4_pipeline_resume",
        root,
        job_type="code",
        chunk_size=1,
        stop_after_chunks=1,
    )
    assert paused.status == "paused"
    assert extractor.calls == [["a.py"]]

    completed = pipeline.run(
        "wp4_pipeline_resume",
        root,
        job_type="code",
        chunk_size=1,
        job_id=paused.id,
        resume=True,
    )
    assert completed.status == "completed"
    assert extractor.calls == [["a.py"], ["b.py"], ["c.py"]]
    assert _neo4j_labels(client) == {"a", "b", "c"}


def test_fr9_non_english_document_enters_graph_with_translation_metadata(
    repo: GraphRepository, client: Neo4jClient, conn, settings: IngestSettings, tmp_path: Path
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "gestion.md"
    doc.write_text(
        "# Gestion\n\nLe système de gestion des connaissances est en cours de développement.\n",
        encoding="utf-8",
    )

    pipeline = _make_pipeline(repo, client, conn, settings)
    job = pipeline.run("wp4_pipeline_fr9", root, job_type="document", chunk_size=1)
    assert job.status == "completed"

    with client.session() as session:
        source = session.run(
            "MATCH (s:Source {uri: $uri}) RETURN s.language AS language",
            uri=str(doc),
        ).single()
        assert source is not None
        assert source["language"] == "fr"

        entity = session.run(
            """
            MATCH (e:Entity {source_file: 'gestion.md'})
            RETURN e.language AS language, e.translation_state AS state,
                   e.source_language AS source_language
            """
        ).single()
        assert entity is not None
        assert entity["language"] == "en"
        assert entity["state"] == "pending"
        assert entity["source_language"] == "fr"

        fact = session.run(
            """
            MATCH (e:Entity {source_file: 'gestion.md'})-[:HAS_FACT]->(f:Fact)
            WHERE f.property = 'summary'
            RETURN f.language AS language, f.translation_state AS state,
                   f.source_language AS source_language, f.value AS value
            """
        ).single()
        assert fact is not None
        assert fact["language"] == "en"
        assert fact["state"] == "pending"
        assert fact["source_language"] == "fr"
        assert fact["value"].startswith("[EN] ")
