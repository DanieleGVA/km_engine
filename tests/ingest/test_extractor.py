"""Tests for code extraction mapping and graphify reuse."""

from __future__ import annotations

from pathlib import Path

from app.ingest.extractor import GraphifyCodeExtractor
from app.ingest.mapping import make_entity_id, make_source_id, map_extraction
from app.ingest.models import ExtractionResult, FileInfo


def test_mapping_creates_entities_facts_and_relations(tmp_path: Path) -> None:
    root = tmp_path
    (root / "a.py").write_text("def a(): pass\n")
    (root / "b.py").write_text("def b(): pass\n")
    file_info = {
        "a.py": FileInfo(
            path=root / "a.py",
            rel="a.py",
            content_hash="hash-a",
            source_id=make_source_id(str(root / "a.py")),
        ),
        "b.py": FileInfo(
            path=root / "b.py",
            rel="b.py",
            content_hash="hash-b",
            source_id=make_source_id(str(root / "b.py")),
        ),
    }
    result = ExtractionResult(
        nodes=[
            {"id": "a", "label": "a.py", "file_type": "code", "source_file": "a.py", "source_location": "L1"},
            {"id": "a_a", "label": "a()", "file_type": "code", "source_file": "a.py", "source_location": "L1"},
            {"id": "b", "label": "b.py", "file_type": "code", "source_file": "b.py", "source_location": "L1"},
            {"id": "b_b", "label": "b()", "file_type": "code", "source_file": "b.py", "source_location": "L1"},
        ],
        edges=[
            {"source": "a", "target": "a_a", "relation": "contains", "confidence": "EXTRACTED", "source_file": "a.py"},
            {"source": "a_a", "target": "b_b", "relation": "calls", "confidence": "EXTRACTED", "source_file": "a.py"},
            {"source": "a", "target": "external", "relation": "imports", "confidence": "EXTRACTED", "source_file": "a.py"},
        ],
    )
    entities, facts, relations = map_extraction(
        result, namespace="ns", root=root, file_info=file_info
    )
    assert len(entities) == 4
    assert len(facts) == 4
    # dangling external edge is dropped
    assert len(relations) == 2
    assert {r.relation for r in relations} == {"contains", "calls"}


def test_entity_and_source_ids_are_deterministic() -> None:
    assert make_entity_id("ns", "a_b") == make_entity_id("ns", "a_b")
    assert make_entity_id("ns", "a_b") != make_entity_id("ns", "a_c")
    assert make_source_id("/tmp/x.py") == make_source_id("/tmp/x.py")


def test_graphify_extractor_python(tmp_path: Path) -> None:
    root = tmp_path
    (root / "sample.py").write_text(
        "import os\n\nclass Greeter:\n    def greet(self, name):\n        return f'hi {name}'\n"
    )
    extractor = GraphifyCodeExtractor(cache_root=tmp_path / "gcache")
    result = extractor.extract([root / "sample.py"], root)
    labels = {n.get("label") for n in result.nodes}
    assert "Greeter" in labels
    relations = {e.get("relation") for e in result.edges}
    assert "contains" in relations
