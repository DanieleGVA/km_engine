"""Code-domain agent pipeline (Iteration D, WP-D2).

Reuses the Iteration-C contracts (``app.agents.models.DomainBrief`` /
``AgentReport``) and the core pack loader, but keeps every code-domain
heuristic OUTSIDE ``app/``. The four agents mirror C1..C4:

- Analyst: graphify extraction -> structured :class:`DomainBrief`.
- Designer: brief -> draft Domain Pack in ``domain-packs/code-agents-draft``.
- Codegen: round-trip conformance on the code corpus with the draft pack.
- Evaluator: golden-set retrieval (Recall@5) + gate report.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agents.models import (
    AgentReport,
    Ambiguity,
    CandidateEntity,
    DomainBrief,
    OntologyCandidate,
    Vocabulary,
)
from app.auth import Principal
from app.domain import load_domain_pack
from app.rag.rag import (
    build_embedding_from_graph,
    populate_embeddings,
    rag_query,
)
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack

from code_domain.golden import build_golden_set
from code_domain.mapping import (
    collect_modules,
    doc_id_for_module,
    graphify_extract_corpus,
    map_graphify_to_graph,
    render_canonical_md,
)
from code_domain.recompose import recompose_code_document

DEFAULT_STAGING_DIR = Path("domain-packs/code-agents-draft")
DEFAULT_BRIEF_DIR = Path("docs/domain-briefs")
DEFAULT_REPORT_DIR = Path("docs/domain-briefs")
DEFAULT_DOC_PREFIX = "id_code_"

GATE_RECALL_AT_5 = 0.85

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_CODE_DEFINITIONS: dict[str, str] = {
    "module": "A source file that groups functions and classes.",
    "function": "A named callable unit of code.",
    "class": "A blueprint that groups data and behaviour.",
    "dependency": "A directed relation between code symbols.",
}

_CODE_ALIASES: dict[str, list[str]] = {
    "module": ["file", "source file", "package"],
    "function": ["def", "method", "callable", "routine"],
    "class": ["type", "object", "dataclass"],
    "dependency": ["import", "call", "relation", "depends on"],
}

_CODE_ONTOLOGIES: list[OntologyCandidate] = [
    OntologyCandidate(
        prefix="python",
        uri="https://docs.python.org/3/library/",
        note="Python language reference (P7: standard before proprietary)",
    ),
]


def slugify(text: str) -> str:
    text = text.strip().lower()
    return _SLUG_RE.sub("-", text).strip("-")


# ---------------------------------------------------------------------------
# Analyst (C1)
# ---------------------------------------------------------------------------

def analyze_code_corpus(
    corpus_dir: str | Path,
    *,
    domain: str = "code",
    language: str = "en",
    canonical_language: str = "en",
    version: str = "1.0.0",
    cache_root: str | Path | None = None,
) -> DomainBrief:
    """Build a :class:`DomainBrief` from the code corpus via graphify."""
    graph = graphify_extract_corpus(corpus_dir, cache_root=cache_root)
    modules = collect_modules(graph)

    n_modules = len(modules)
    n_functions = sum(len(m.functions) for m in modules)
    n_classes = sum(len(m.classes) for m in modules)
    n_dependencies = sum(len(m.dependencies) for m in modules)

    def entity(term: str, kind: str, frequency: int) -> CandidateEntity:
        return CandidateEntity(
            term=term,
            source_terms=_CODE_ALIASES[term],
            frequency=max(frequency, 1),
            kind=kind,
            contexts=[f"{term} observed in the code corpus"],
        )

    module_entity = entity("module", "ingredient", n_modules)
    function_entity = entity("function", "technique", n_functions)
    class_entity = entity("class", "technique", n_classes)
    dependency_entity = entity("dependency", "state", n_dependencies)

    entities = [module_entity, function_entity, class_entity, dependency_entity]
    vocabularies = [
        Vocabulary(name="ingredienti", entries=[module_entity]),
        Vocabulary(name="tecnica", entries=[function_entity, class_entity]),
        Vocabulary(name="stati", entries=[dependency_entity]),
    ]

    stats = {
        "source_modules": n_modules,
        "functions": n_functions,
        "classes": n_classes,
        "dependencies": n_dependencies,
        "symbols": n_functions + n_classes,
        "unit_types": 0,
        "ambiguities": 0,
    }

    return DomainBrief(
        domain=domain,
        language=language,
        canonical_language=canonical_language,
        version=version,
        corpus_size=n_modules,
        entities=entities,
        vocabularies=vocabularies,
        units=[],
        ambiguities=[],
        ontologies=_CODE_ONTOLOGIES,
        stats=stats,
    )


def write_brief(brief: DomainBrief, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(brief.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Designer (C2)
# ---------------------------------------------------------------------------

class DesignError(RuntimeError):
    """Raised when the designer refuses to write outside the staging dir."""


@dataclass
class DesignResult:
    staging_dir: Path
    files: list[Path] = field(default_factory=list)
    glossary_entries: int = 0
    unit_rules: int = 0


def _safe_write(root: Path, relpath: str, content: str) -> Path:
    root = root.resolve()
    target = (root / relpath).resolve()
    if not target.is_relative_to(root):
        raise DesignError(f"refusing to write outside staging dir {root}: {relpath!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _yaml_dump(data: object) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


_CODE_TEMPLATE_MD = """---
title: <module>
id: <module_path>
lang: en
source_lang: en
verification_level: L1
canonical_version: 1
---
## Functions
- <function>
## Classes
- <class>
## Dependencies
- <dependency>
"""


def _glossary_entry(entity: CandidateEntity) -> dict:
    term = entity.term.strip()
    entry_id = f"CODE-{slugify(term).upper()}"
    aliases = [a for a in entity.source_terms if a.strip() and a.strip() != term]
    return {
        "id": entry_id,
        "labels_en": term,
        "labels_it": term,
        "aliases": aliases,
        "definition": _CODE_DEFINITIONS.get(term, ""),
        "ontology_uri": None,
    }


def _build_glossary(name: str, entities: list[CandidateEntity]) -> dict:
    return {"name": name, "entries": [_glossary_entry(e) for e in entities]}


def _build_pack_yaml(brief: DomainBrief) -> dict:
    return {
        "name": brief.domain,
        "language": brief.language,
        "canonical_language": brief.canonical_language,
        "version": brief.version,
        "ontologies": [
            {"prefix": o.prefix, "uri": o.uri} for o in brief.ontologies
        ],
        "units_source": "units.yaml",
        "glossaries": ["tecnica", "ingredienti", "stati"],
        "paths": {
            "template": "template.md",
            "glossaries": "glossari",
            "units": "units.yaml",
            "rules": "regole",
        },
    }


def design_code_pack(
    brief: DomainBrief,
    staging_dir: str | Path = DEFAULT_STAGING_DIR,
) -> DesignResult:
    """Write the draft code Domain Pack from the brief (staging dir only)."""
    root = Path(staging_dir)
    files: list[Path] = []
    files.append(_safe_write(root, "pack.yaml", _yaml_dump(_build_pack_yaml(brief))))
    files.append(_safe_write(root, "template.md", _CODE_TEMPLATE_MD))
    files.append(_safe_write(root, "units.yaml", "# Code has no physical units.\n[]\n"))

    glossary_entries = 0
    for vocabulary in brief.vocabularies:
        name = vocabulary.name
        glossary = _build_glossary(name, vocabulary.entries)
        glossary_entries += len(glossary["entries"])
        files.append(
            _safe_write(root, f"glossari/{name}.yaml", _yaml_dump(glossary))
        )

    files.append(
        _safe_write(
            root,
            "regole/normalizzazione.yaml",
            _yaml_dump(
                {
                    "name": "normalizzazione",
                    "version": "1.0.0",
                    "order": ["structure"],
                    "identity": True,
                    "note": "Code is already canonical EN; normalization is structural only.",
                }
            ),
        )
    )
    files.append(
        _safe_write(
            root,
            "regole/verifica.yaml",
            _yaml_dump(
                {
                    "name": "verifica",
                    "version": "1.0.0",
                    "levels": ["L1"],
                    "note": "Code domain verification is structural round-trip only.",
                }
            ),
        )
    )

    return DesignResult(
        staging_dir=root,
        files=files,
        glossary_entries=glossary_entries,
        unit_rules=0,
    )


# ---------------------------------------------------------------------------
# Codegen (C3)
# ---------------------------------------------------------------------------

async def run_code_conformance(
    draft_dir: str | Path,
    corpus_dir: str | Path,
    *,
    client: Neo4jClient | None = None,
    doc_prefix: str = DEFAULT_DOC_PREFIX,
    cache_root: str | Path | None = None,
) -> AgentReport:
    """Run the code-domain round-trip conformance on the draft pack."""
    draft_dir = Path(draft_dir)
    pack = load_domain_pack(draft_dir)
    graph = graphify_extract_corpus(corpus_dir, cache_root=cache_root)
    modules = collect_modules(graph)

    if client is not None:
        load_pack(client, draft_dir)

    errors: list[str] = []
    roundtrip_ok = 0
    for module in modules:
        canonical_md = render_canonical_md(module)
        doc_id = doc_id_for_module(module.source_file, prefix=doc_prefix)
        try:
            from code_domain.extract import extract_code_document

            extract_code_document(client, doc_id, canonical_md, pack)
            recomposed = recompose_code_document(client, doc_id)
            if recomposed == canonical_md:
                roundtrip_ok += 1
            else:
                errors.append(f"{module.source_file}: round-trip mismatch")
        except Exception as exc:  # noqa: BLE001 - collected into the report
            errors.append(f"{module.source_file}: {type(exc).__name__}: {exc}")

    total = len(modules)
    status = "ok" if not errors and roundtrip_ok == total else "failed"
    metrics = {
        "modules": total,
        "roundtrip_ok": roundtrip_ok,
        "roundtrip_total": total,
        "roundtrip_ratio": round(roundtrip_ok / total, 4) if total else None,
    }
    return AgentReport(
        agent="codegen",
        status=status,
        summary=f"codegen conformance on {total} code modules: round-trip {roundtrip_ok}/{total}",
        metrics=metrics,
        artifacts=[str(draft_dir)],
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Evaluator (C4)
# ---------------------------------------------------------------------------

def _admin_principal(prefix: str) -> Principal:
    return Principal(f"{prefix}u_admin", ("admin",), (), "default", f"{prefix}j_admin")


def measure_recall_at_5(
    client: Neo4jClient,
    principal: Principal,
    pairs: list[dict[str, Any]],
    embedding: Any,
) -> dict[str, Any]:
    """Run the golden set and return Recall@5 + per-query hits."""
    hits_at_5 = 0
    results: list[dict[str, Any]] = []
    for pair in pairs:
        hits = rag_query(
            client,
            principal,
            pair["query"],
            lang="en",
            limit=5,
            embedding=embedding,
        )
        returned = [hit.document_id for hit in hits]
        expected = pair["document_id"]
        ok = expected in returned
        if ok:
            hits_at_5 += 1
        results.append(
            {
                "query": pair["query"],
                "expected": expected,
                "returned": returned,
                "hit": ok,
            }
        )
    total = len(pairs)
    recall = hits_at_5 / total if total else 0.0
    return {
        "recall_at_5": round(recall, 4),
        "hits_at_5": hits_at_5,
        "total": total,
        "results": results,
    }


async def evaluate_code_draft(
    draft_dir: str | Path,
    corpus_dir: str | Path,
    *,
    client: Neo4jClient | None = None,
    doc_prefix: str = DEFAULT_DOC_PREFIX,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    write_artifacts: bool = True,
    cache_root: str | Path | None = None,
) -> AgentReport:
    """Evaluate the draft pack: ingest + embeddings + golden-set Recall@5."""
    draft_dir = Path(draft_dir)
    report_dir = Path(report_dir)
    pack = load_domain_pack(draft_dir)
    graph = graphify_extract_corpus(corpus_dir, cache_root=cache_root)
    modules = collect_modules(graph)
    golden = build_golden_set(modules)

    if client is None:
        raise ValueError("evaluate_code_draft requires a Neo4j client")

    load_pack(client, draft_dir)
    mapping = map_graphify_to_graph(client, graph, pack, doc_prefix=doc_prefix)

    embedding = build_embedding_from_graph(client, pack)
    populated = populate_embeddings(client, embedding)

    principal = _admin_principal(doc_prefix)
    recall = measure_recall_at_5(client, principal, golden, embedding)

    gate = recall["recall_at_5"] >= GATE_RECALL_AT_5
    metrics = {
        "modules": mapping.modules,
        "functions": mapping.functions,
        "classes": mapping.classes,
        "dependencies": mapping.dependencies,
        "relations": mapping.relations,
        "embeddings_populated": populated,
        "golden_queries": recall["total"],
        "recall_at_5": recall["recall_at_5"],
        "hits_at_5": recall["hits_at_5"],
        "gate_recall_at_5": GATE_RECALL_AT_5,
        "gate": gate,
    }

    artifacts: list[str] = []
    if write_artifacts:
        golden_path = report_dir / "code-golden.json"
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "generated_by": "code_domain/agents.py",
                    "corpus": str(corpus_dir),
                    "pairs": golden,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.append(str(golden_path))

        report_path = report_dir / "code-gate-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "gate": gate,
                    "metrics": metrics,
                    "recall": recall,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.append(str(report_path))

    return AgentReport(
        agent="evaluator",
        status="ok" if gate else "failed",
        summary=(
            f"code evaluator: Recall@5 {recall['recall_at_5']:.4f} "
            f"({recall['hits_at_5']}/{recall['total']}) gate={gate}"
        ),
        metrics=metrics,
        artifacts=artifacts,
        errors=[] if gate else ["Recall@5 below gate threshold"],
    )
