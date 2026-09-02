"""WP-C4 evaluator tests: metrics on the PILOT + golden pilot A artifact."""
from __future__ import annotations

import json

from app.agents import design_pack, evaluate_draft
from app.agents.models import DomainBrief
from tests.agents.conftest import IC_PREFIX, PACK_DIR, pilot_corpus


async def test_ic_evaluator_metrics(tmp_path, pilot_brief: DomainBrief, ic_client) -> None:
    """The draft reaches round-trip 15/15 and >=90% of the manual coverage."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    report = await evaluate_draft(
        result.staging_dir,
        PACK_DIR,
        pilot_corpus(),
        client=ic_client,
        doc_prefix=IC_PREFIX,
        write_artifacts=False,
    )
    assert report.status == "ok", report.errors
    metrics = report.metrics
    assert metrics["roundtrip_ok"] == 15
    assert metrics["roundtrip_total"] == 15
    assert metrics["roundtrip_ratio"] == 1.0
    assert metrics["relative_coverage"] >= 0.90
    # WP-F6: il gate e' sulla copertura ASSOLUTA, non piu' sul rapporto col
    # pack manuale (che restava alto anche se entrambi risolvevano poco).
    assert metrics["draft_coverage"] >= metrics["gate_corpus_coverage_threshold"]
    assert metrics["gate_coverage"] is True
    assert metrics["gate"] is True

    normalization = metrics["normalization"]
    assert 0.0 <= normalization["precision"] <= 1.0
    assert 0.0 <= normalization["recall"] <= 1.0
    assert normalization["recall"] >= 0.9


async def test_ic_evaluator_writes_golden_pilot_a(
    tmp_path, pilot_brief: DomainBrief, ic_client
) -> None:
    """The golden pilot A and gate report are written under docs/domain-briefs/."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    report = await evaluate_draft(
        result.staging_dir,
        PACK_DIR,
        pilot_corpus(),
        client=ic_client,
        doc_prefix=IC_PREFIX,
        report_dir=tmp_path / "briefs",
        write_artifacts=True,
    )
    golden_path = tmp_path / "briefs" / "ricette-golden-pilot-a.json"
    gate_path = tmp_path / "briefs" / "ricette-gate-report.json"
    assert golden_path.is_file()
    assert gate_path.is_file()

    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert golden["total_mentions"] == report.metrics["manual_total"]
    assert golden["resolved_mentions"] == report.metrics["manual_resolved"]

    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["metrics"]["gate"] is True
