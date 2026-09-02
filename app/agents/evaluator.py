"""Evaluator (WP-C4): measure the draft pack on the PILOT corpus.

The evaluator runs the full deterministic pipeline with the draft pack on the
15-recipe pilot and compares it against the manual pack (the reference):

- round-trip % (``extract_document`` + ``recompose_document`` byte-identical);
- glossary coverage (% ingredient mentions resolved);
- precision/recall of normalization against the golden pilot A (the manual
  pack's per-mention decisions).

It writes a gate report JSON under ``docs/domain-briefs/`` and returns an
:class:`AgentReport` whose ``metrics`` carry the gate values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.agents.models import AgentReport
from app.domain import (
    LLMClient,
    canonicalize,
    extract_document,
    load_domain_pack,
    recompose_document,
    translate_document,
)
from app.domain.coverage import build_lookup, lookup_key, measure_documents
from app.domain.pack import DomainPackBundle
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

DEFAULT_DOC_PREFIX = "ice_"
DEFAULT_REPORT_DIR = Path("docs/domain-briefs")

GATE_ROUNDTRIP = 1.0
GATE_RELATIVE_COVERAGE = 0.90


@dataclass(frozen=True)
class PackEvaluation:
    """Esito della pipeline su un pack: decisioni per menzione + copertura."""

    decisions: list[MentionDecision]
    resolved_terms: int
    total_terms: int
    coverage: float
    lines_resolved: int
    lines_total: int
    roundtrip_ok: int
    roundtrip_total: int


@dataclass(frozen=True)
class MentionDecision:
    """One per-mention normalization decision."""

    doc: str
    index: int
    canonical_item: str
    resolved: bool
    term_id: str | None


async def _evaluate_pack(
    pack: DomainPackBundle,
    pack_dir: Path,
    corpus: dict[str, str],
    client: Neo4jClient | None,
    doc_prefix: str,
    llm: LLMClient | None,
) -> PackEvaluation:
    """Run the pipeline for one pack and return decisions + metrics.

    La copertura non e' piu' contata qui con una copia locale del lookup: i
    canonical.md prodotti vengono misurati da ``measure_documents``, la stessa
    funzione usata da ``scripts/measure_coverage.py`` (WP-F0).
    """
    llm = llm or build_fake_llm(pack, corpus)
    term_map = build_lookup(pack)
    decisions: list[MentionDecision] = []
    canonical_docs: dict[str, str] = {}
    total = 0
    resolved = 0
    roundtrip_ok = 0
    roundtrip_total = 0

    if client is not None:
        load_pack(client, pack_dir)

    for name in sorted(corpus):
        source_md = corpus[name]
        translated = await translate_document(pack, source_md, llm)
        canonical = canonicalize(pack, translated.translated_md)
        canonical_docs[name] = canonical.canonical_md

        for index, ingredient in enumerate(canonical.parsed.ingredients):
            key = lookup_key(ingredient.item)
            hit = term_map.get(key)
            is_resolved = hit is not None
            total += 1
            if is_resolved:
                resolved += 1
            decisions.append(
                MentionDecision(
                    doc=name,
                    index=index,
                    canonical_item=ingredient.item,
                    resolved=is_resolved,
                    term_id=hit[1] if hit is not None else None,
                )
            )

        if client is not None:
            doc_id = f"{doc_prefix}{canonical.document_id}"
            extract_document(client, None, doc_id, canonical.canonical_md, pack)
            recomposed = recompose_document(client, doc_id)
            roundtrip_total += 1
            if recomposed == canonical.canonical_md:
                roundtrip_ok += 1

    report = measure_documents(pack, canonical_docs, stage="translated")
    return PackEvaluation(
        decisions=decisions,
        resolved_terms=resolved,
        total_terms=total,
        coverage=report.coverage,
        lines_resolved=report.lines_resolved,
        lines_total=report.lines_total,
        roundtrip_ok=roundtrip_ok,
        roundtrip_total=roundtrip_total,
    )


def _normalization_metrics(
    draft: list[MentionDecision], golden: list[MentionDecision]
) -> dict[str, float]:
    """Precision/recall of draft normalization vs the golden decisions."""
    draft_by_key = {(d.doc, d.index): d for d in draft}
    golden_by_key = {(d.doc, d.index): d for d in golden}

    draft_resolved = {
        key for key, d in draft_by_key.items() if d.resolved
    }
    golden_resolved = {
        key for key, d in golden_by_key.items() if d.resolved
    }
    matches = {
        key
        for key in draft_resolved & golden_resolved
        if draft_by_key[key].canonical_item.casefold()
        == golden_by_key[key].canonical_item.casefold()
    }

    precision = len(matches) / len(draft_resolved) if draft_resolved else 1.0
    recall = len(matches) / len(golden_resolved) if golden_resolved else 1.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "matches": len(matches),
        "draft_resolved": len(draft_resolved),
        "golden_resolved": len(golden_resolved),
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


async def evaluate_draft(
    draft_dir: str | Path,
    manual_dir: str | Path,
    corpus: dict[str, str],
    *,
    client: Neo4jClient | None = None,
    doc_prefix: str = DEFAULT_DOC_PREFIX,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    write_artifacts: bool = True,
    llm: LLMClient | None = None,
) -> AgentReport:
    """Evaluate the draft pack against the manual pack on ``corpus``.

    ``llm`` is the optional translation hook; when ``None`` the deterministic
    glossary-based ``FakeLLMClient`` is used (never the network in tests).
    """
    draft_dir = Path(draft_dir)
    manual_dir = Path(manual_dir)
    report_dir = Path(report_dir)

    draft_pack = load_domain_pack(draft_dir)
    manual_pack = load_domain_pack(manual_dir)

    draft_eval = await _evaluate_pack(
        draft_pack, draft_dir, corpus, client, doc_prefix, llm
    )
    manual_eval = await _evaluate_pack(
        manual_pack, manual_dir, corpus, None, doc_prefix, llm
    )
    draft_decisions = draft_eval.decisions
    golden_decisions = manual_eval.decisions
    draft_rt_ok = draft_eval.roundtrip_ok
    draft_rt_total = draft_eval.roundtrip_total

    draft_coverage = draft_eval.coverage
    manual_coverage = manual_eval.coverage
    # Chiavi storiche: la vecchia copertura per-menzione, conservata con
    # suffisso ``_terms`` per confronto con i report precedenti a F0.
    draft_coverage_terms = (
        draft_eval.resolved_terms / draft_eval.total_terms
        if draft_eval.total_terms else 0.0
    )
    manual_coverage_terms = (
        manual_eval.resolved_terms / manual_eval.total_terms
        if manual_eval.total_terms else 0.0
    )
    relative_coverage = (
        draft_coverage / manual_coverage if manual_coverage else 0.0
    )
    roundtrip_ratio = draft_rt_ok / draft_rt_total if draft_rt_total else 0.0
    normalization = _normalization_metrics(draft_decisions, golden_decisions)

    gate_roundtrip = roundtrip_ratio >= GATE_ROUNDTRIP
    gate_coverage = relative_coverage >= GATE_RELATIVE_COVERAGE
    gate = gate_roundtrip and gate_coverage

    metrics = {
        "roundtrip_ok": draft_rt_ok,
        "roundtrip_total": draft_rt_total,
        "roundtrip_ratio": round(roundtrip_ratio, 4),
        "draft_resolved": draft_eval.lines_resolved,
        "draft_total": draft_eval.lines_total,
        "draft_coverage": round(draft_coverage, 4),
        "manual_resolved": manual_eval.lines_resolved,
        "manual_total": manual_eval.lines_total,
        "manual_coverage": round(manual_coverage, 4),
        "draft_resolved_terms": draft_eval.resolved_terms,
        "draft_total_terms": draft_eval.total_terms,
        "draft_coverage_terms": round(draft_coverage_terms, 4),
        "manual_resolved_terms": manual_eval.resolved_terms,
        "manual_total_terms": manual_eval.total_terms,
        "manual_coverage_terms": round(manual_coverage_terms, 4),
        "relative_coverage": round(relative_coverage, 4),
        "normalization": normalization,
        "gate_roundtrip": gate_roundtrip,
        "gate_coverage": gate_coverage,
        "gate": gate,
    }

    artifacts: list[str] = []
    if write_artifacts:
        golden_path = _write_json(
            report_dir / "ricette-golden-pilot-a.json",
            {
                "version": "1.0",
                "generated_by": "app/agents/evaluator.py",
                "corpus_size": len(corpus),
                "total_mentions": manual_eval.total_terms,
                "resolved_mentions": manual_eval.resolved_terms,
                "coverage": round(manual_coverage, 4),
                "decisions": [
                    {
                        "doc": d.doc,
                        "index": d.index,
                        "canonical_item": d.canonical_item,
                        "resolved": d.resolved,
                        "term_id": d.term_id,
                    }
                    for d in golden_decisions
                ],
            },
        )
        gate_path = _write_json(
            report_dir / "ricette-gate-report.json",
            {
                "version": "1.0",
                "generated_by": "app/agents/evaluator.py",
                "draft_dir": str(draft_dir),
                "manual_dir": str(manual_dir),
                "metrics": metrics,
            },
        )
        artifacts.extend([str(golden_path), str(gate_path)])

    status = "ok" if gate else "failed"
    summary = (
        f"evaluator: round-trip {draft_rt_ok}/{draft_rt_total} "
        f"({roundtrip_ratio:.1%}), draft coverage {draft_coverage:.1%} "
        f"vs manual {manual_coverage:.1%} (relative {relative_coverage:.1%}), "
        f"precision {normalization['precision']:.1%}, "
        f"recall {normalization['recall']:.1%} -> gate {'PASS' if gate else 'FAIL'}"
    )
    return AgentReport(
        agent="evaluator",
        status=status,
        summary=summary,
        metrics=metrics,
        artifacts=artifacts,
        errors=[] if gate else ["gate failed"],
    )
