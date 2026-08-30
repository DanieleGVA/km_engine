"""Code domain adapters for the km_engine knowledge layer (Iteration D).

This package lives OUTSIDE ``app/`` on purpose: adding the ``code`` domain must
not touch the core (``app/agents``, ``app/domain``, ``app/rag``, ``app/api``).
It reuses the core interfaces (``app.domain.pack.load_domain_pack``,
``app.agents.models``, ``app.rag``) and adds only domain content + adapters.

Modules:
- :mod:`code_domain.mapping` — graphify output -> Document/Entity/NORMALIZED_TO.
- :mod:`code_domain.extract` — code canonical.md -> Neo4j domain graph.
- :mod:`code_domain.recompose` — Neo4j domain graph -> code canonical.md.
- :mod:`code_domain.agents` — code-domain Analyst/Designer/Codegen/Evaluator.
- :mod:`code_domain.rag` — RAG orchestration reusing ``app.rag``.
- :mod:`code_domain.golden` — deterministic golden set for the code corpus.
"""
from __future__ import annotations

__all__ = [
    "mapping",
    "extract",
    "recompose",
    "agents",
    "rag",
    "golden",
]
