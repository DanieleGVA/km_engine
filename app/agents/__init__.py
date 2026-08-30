"""Iteration C agent pipeline (WP-C1..C6): Domain Pack generator + Curator.

Public API:
- :class:`DomainBrief` / :class:`AgentReport` — shared pydantic contracts.
- :func:`analyze_corpus` / :func:`translate_corpus` — Domain Analyst (WP-C1).
- :func:`design_pack` — Ontology Designer (WP-C2, staging-dir only).
- :func:`run_conformance_suite` — Codegen (WP-C3).
- :func:`evaluate_draft` — Evaluator (WP-C4).
- :func:`mine_issues` / :func:`propose_extension` / :func:`apply_approved` —
  Curator loop (WP-C5).
- :func:`generate_decision_records` / :func:`generate_pack_changelog` —
  Documenter (WP-C6).
"""
from __future__ import annotations

from app.agents.analyst import (
    analyze_corpus,
    clean_item,
    default_brief_path,
    load_brief,
    translate_corpus,
    write_brief,
)
from app.agents.codegen import run_conformance_suite
from app.agents.curator import (
    CuratorGateError,
    apply_approved,
    detect_modifier_terms,
    mine_issues,
    propose_extension,
)
from app.agents.designer import (
    STAGING_DIR,
    DesignError,
    DesignResult,
    design_pack,
    slugify,
)
from app.agents.documenter import (
    generate_decision_records,
    generate_pack_changelog,
    write_decision_records,
    write_pack_changelog,
)
from app.agents.evaluator import evaluate_draft
from app.agents.models import (
    AgentReport,
    Ambiguity,
    ApplyResult,
    CandidateEntity,
    CuratorIssue,
    DecisionRecord,
    DomainBrief,
    OntologyCandidate,
    Proposal,
    UnitObservation,
    Vocabulary,
)

__all__ = [
    "STAGING_DIR",
    "AgentReport",
    "Ambiguity",
    "ApplyResult",
    "CandidateEntity",
    "CuratorGateError",
    "CuratorIssue",
    "DecisionRecord",
    "DesignError",
    "DesignResult",
    "DomainBrief",
    "OntologyCandidate",
    "Proposal",
    "UnitObservation",
    "Vocabulary",
    "analyze_corpus",
    "apply_approved",
    "clean_item",
    "default_brief_path",
    "design_pack",
    "detect_modifier_terms",
    "evaluate_draft",
    "generate_decision_records",
    "generate_pack_changelog",
    "load_brief",
    "mine_issues",
    "propose_extension",
    "run_conformance_suite",
    "slugify",
    "translate_corpus",
    "write_brief",
    "write_decision_records",
    "write_pack_changelog",
]
