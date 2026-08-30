"""Iteration C agent pipeline (WP-C1..C4): Domain Pack generator.

Public API:
- :class:`DomainBrief` / :class:`AgentReport` — shared pydantic contracts.
- :func:`analyze_corpus` / :func:`translate_corpus` — Domain Analyst (WP-C1).
- :func:`design_pack` — Ontology Designer (WP-C2, staging-dir only).
- :func:`run_conformance_suite` — Codegen (WP-C3).
- :func:`evaluate_draft` — Evaluator (WP-C4).
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
from app.agents.designer import (
    STAGING_DIR,
    DesignError,
    DesignResult,
    design_pack,
    slugify,
)
from app.agents.evaluator import evaluate_draft
from app.agents.models import (
    AgentReport,
    Ambiguity,
    CandidateEntity,
    DomainBrief,
    OntologyCandidate,
    UnitObservation,
    Vocabulary,
)

__all__ = [
    "STAGING_DIR",
    "AgentReport",
    "Ambiguity",
    "CandidateEntity",
    "DesignError",
    "DesignResult",
    "DomainBrief",
    "OntologyCandidate",
    "UnitObservation",
    "Vocabulary",
    "analyze_corpus",
    "clean_item",
    "default_brief_path",
    "design_pack",
    "evaluate_draft",
    "load_brief",
    "run_conformance_suite",
    "slugify",
    "translate_corpus",
    "write_brief",
]
