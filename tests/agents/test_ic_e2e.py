"""WP-C1..C4 E2E: regenerate the ricette pack from the PILOT corpus.

The full deterministic pipeline (analyst -> designer -> codegen -> evaluator)
must produce a draft pack that reaches the Iteration-A gates on the 15-recipe
pilot: round-trip 15/15 (100%) and glossary coverage >= 90% of the manual pack.
The comparison report is written to ``docs/pack-rigenerato-vs-manuale.md``.
"""
from __future__ import annotations

from pathlib import Path

from app.agents import (
    analyze_corpus,
    design_pack,
    evaluate_draft,
    run_conformance_suite,
    translate_corpus,
    write_brief,
)
from app.domain import load_domain_pack
from tests.agents.conftest import (
    BRIEF_DIR,
    IC_PREFIX,
    PACK_DIR,
    REPO_ROOT,
    STAGING_DIR,
    pilot_corpus,
)
from tests.domain.fake_llm import build_fake_llm


def _write_comparison_report(
    path: Path,
    brief,
    design,
    codegen_report,
    evaluator_report,
) -> None:
    metrics = evaluator_report.metrics
    normalization = metrics["normalization"]
    lines = [
        "# Pack rigenerato vs pack manuale (Iterazione C, WP-C1..C4)",
        "",
        "Pipeline deterministica eseguita sul PILOT (15 ricette validate).",
        "",
        "## Esito gate",
        "",
        (
            f"- Round-trip: **{metrics['roundtrip_ok']}/{metrics['roundtrip_total']}** "
            f"({metrics['roundtrip_ratio']:.1%}) — gate: "
            f"{'PASS' if metrics['gate_roundtrip'] else 'FAIL'}"
        ),
        (
            f"- Copertura glossario draft: **{metrics['draft_coverage']:.1%}** "
            f"({metrics['draft_resolved']}/{metrics['draft_total']} mention risolte)"
        ),
        (
            f"- Copertura glossario manuale: **{metrics['manual_coverage']:.1%}** "
            f"({metrics['manual_resolved']}/{metrics['manual_total']})"
        ),
        (
            f"- Copertura relativa (draft/manuale): **{metrics['relative_coverage']:.1%}** "
            f"— soglia >= 90%: {'PASS' if metrics['gate_coverage'] else 'FAIL'}"
        ),
        f"- Gate complessivo: **{'PASS' if metrics['gate'] else 'FAIL'}**",
        "",
        "## Normalizzazione vs golden pilot A",
        "",
        f"- Precision: **{normalization['precision']:.1%}**",
        f"- Recall: **{normalization['recall']:.1%}**",
        (
            f"- Match: {normalization['matches']} "
            f"(draft risolte {normalization['draft_resolved']}, "
            f"golden risolte {normalization['golden_resolved']})"
        ),
        "",
        "## Codegen (suite-tipo A sul draft)",
        "",
        (
            f"- P2 invarianti: {codegen_report.metrics['p2_ok']}/"
            f"{codegen_report.metrics['sample_size']}"
        ),
        (
            f"- Canon-log completo: {codegen_report.metrics['canon_log_ok']}/"
            f"{codegen_report.metrics['sample_size']}"
        ),
        (
            f"- Round-trip campione: {codegen_report.metrics['roundtrip_ok']}/"
            f"{codegen_report.metrics['roundtrip_total']}"
        ),
        "",
        "## Struttura del brief",
        "",
        f"- Entità candidate: {len(brief.entities)}",
        "- Vocabolari: " + ", ".join(
            f"{v.name} ({len(v.entries)})" for v in brief.vocabularies
        ),
        f"- Unità rilevate: {len(brief.units)}",
        f"- Ambiguità: {len(brief.ambiguities)}",
        f"- Ontologie candidate: {len(brief.ontologies)}",
        "",
        "## Draft generato",
        "",
        f"- Staging dir: `{design.staging_dir}`",
        f"- Entry glossario: {design.glossary_entries}",
        f"- Regole unità: {design.unit_rules}",
        "",
        "## Artefatti",
        "",
        "- Brief: `docs/domain-briefs/ricette-v1.json`",
        "- Golden pilot A: `docs/domain-briefs/ricette-golden-pilot-a.json`",
        "- Gate report: `docs/domain-briefs/ricette-gate-report.json`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


async def test_ic_e2e_regenerate_ricette_pack(ic_client) -> None:
    """E2E: analyst+designer+codegen+evaluator on the PILOT -> gate PASS."""
    pack = load_domain_pack(PACK_DIR)
    corpus = pilot_corpus()
    llm = build_fake_llm(pack, corpus)

    # WP-C1 — Domain Analyst.
    translated = await translate_corpus(pack, corpus, llm)
    brief = analyze_corpus(corpus, translated)
    brief_path = write_brief(brief, BRIEF_DIR / "ricette-v1.json")
    assert brief_path.is_file()

    # WP-C2 — Ontology Designer (staging dir only).
    design = design_pack(brief, staging_dir=STAGING_DIR, overwrite=True)
    assert design.staging_dir == STAGING_DIR.resolve()

    # WP-C3 — Codegen conformance.
    codegen_report = await run_conformance_suite(
        design.staging_dir,
        corpus,
        client=ic_client,
        sample_size=3,
        doc_prefix=IC_PREFIX,
    )
    assert codegen_report.status == "ok", codegen_report.errors

    # WP-C4 — Evaluator on the PILOT.
    evaluator_report = await evaluate_draft(
        design.staging_dir,
        PACK_DIR,
        corpus,
        client=ic_client,
        doc_prefix=IC_PREFIX,
        report_dir=BRIEF_DIR,
        write_artifacts=True,
    )

    metrics = evaluator_report.metrics
    assert metrics["roundtrip_ok"] == 15
    assert metrics["roundtrip_total"] == 15
    assert metrics["roundtrip_ratio"] == 1.0
    assert metrics["relative_coverage"] >= 0.90
    assert metrics["gate"] is True, evaluator_report.errors

    report_path = REPO_ROOT / "docs" / "pack-rigenerato-vs-manuale.md"
    _write_comparison_report(
        report_path, brief, design, codegen_report, evaluator_report
    )
    assert report_path.is_file()
