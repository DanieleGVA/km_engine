"""WP-C3 codegen tests: draft pack passes the Iteration-A suite-type checks."""
from __future__ import annotations

from app.agents import design_pack, run_conformance_suite
from app.agents.models import AgentReport, DomainBrief
from tests.agents.conftest import IC_PREFIX, pilot_corpus


async def test_ic_codegen_conformance_suite(tmp_path, pilot_brief: DomainBrief, ic_client) -> None:
    """P2 invariants, complete canon-log and round-trip all pass on the draft."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    report = await run_conformance_suite(
        result.staging_dir,
        pilot_corpus(),
        client=ic_client,
        sample_size=3,
        doc_prefix=IC_PREFIX,
    )
    assert report.status == "ok", report.errors
    assert report.metrics["p2_ok"] == 3
    assert report.metrics["canon_log_ok"] == 3
    assert report.metrics["roundtrip_ok"] == 3
    assert report.metrics["roundtrip_total"] == 3
    assert report.metrics["roundtrip_ratio"] == 1.0


async def test_ic_codegen_deterministic(tmp_path, pilot_brief: DomainBrief, ic_client) -> None:
    """Two codegen runs on the same brief produce equivalent reports."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    first = await run_conformance_suite(
        result.staging_dir, pilot_corpus(), client=ic_client, sample_size=3, doc_prefix=IC_PREFIX
    )
    second = await run_conformance_suite(
        result.staging_dir, pilot_corpus(), client=ic_client, sample_size=3, doc_prefix=IC_PREFIX
    )
    assert first.metrics == second.metrics
    assert first.status == second.status == "ok"


async def test_ic_codegen_report_is_agent_report(tmp_path, pilot_brief: DomainBrief) -> None:
    """The codegen output is a valid AgentReport even without Neo4j."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    report = await run_conformance_suite(result.staging_dir, pilot_corpus(), client=None)
    assert isinstance(report, AgentReport)
    assert report.agent == "codegen"
    assert report.metrics["p2_ok"] == 3
    assert report.metrics["roundtrip_total"] == 0
