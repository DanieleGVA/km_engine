"""GD2: the full Iteration-C pipeline runs on the code domain with zero
modifications to ``app/*``.

The test snapshots every ``app/`` file before running the pipeline and asserts
the snapshot is unchanged afterwards. The pipeline itself is the code-domain
adaptation of C1..C4 (analyst -> designer -> codegen -> evaluator) plus the
code-domain extract/recompose and the reused ``app.rag`` retrieval.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.storage.client import Neo4jClient
from code_domain.agents import (
    analyze_code_corpus,
    design_code_pack,
    evaluate_code_draft,
    run_code_conformance,
    write_brief,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"


def _app_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(APP_DIR.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(REPO_ROOT))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


@pytest.mark.asyncio
async def test_code_pipeline_zero_core_modifications(
    client: Neo4jClient, tmp_path
) -> None:
    before = _app_snapshot()

    brief = analyze_code_corpus(
        "tests/fixtures/corpus_code", cache_root=tmp_path / "graphify_cache"
    )
    assert brief.domain == "code"
    assert brief.corpus_size >= 10
    assert brief.stats["functions"] >= 50
    assert brief.stats["classes"] >= 20

    brief_path = tmp_path / "code-v1.json"
    write_brief(brief, brief_path)
    assert brief_path.is_file()

    staging = tmp_path / "code-agents-draft"
    design = design_code_pack(brief, staging)
    assert design.glossary_entries == 4
    assert (staging / "pack.yaml").is_file()

    codegen = await run_code_conformance(
        staging,
        "tests/fixtures/corpus_code",
        client=client,
        doc_prefix="id_code_",
        cache_root=tmp_path / "graphify_cache_codegen",
    )
    assert codegen.status == "ok", codegen.errors
    assert codegen.metrics["roundtrip_ratio"] == 1.0

    evaluator = await evaluate_code_draft(
        staging,
        "tests/fixtures/corpus_code",
        client=client,
        doc_prefix="id_code_",
        report_dir=tmp_path / "reports",
        cache_root=tmp_path / "graphify_cache_eval",
    )
    assert evaluator.status == "ok", evaluator.errors
    assert evaluator.metrics["recall_at_5"] >= 0.85
    assert evaluator.metrics["golden_queries"] >= 50

    after = _app_snapshot()
    assert after == before, "app/* files must not be modified by the code pipeline"
