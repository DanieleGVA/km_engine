"""WP-D1: map graphify code extraction to the knowledge layer model.

The code domain reuses the vendored graphify code-intelligence (extract/build)
and maps its output onto the same graph model used by the recipe domain:

- every real module (source file) -> :Document
- every function/class -> :Entity with ``PART_OF_DOC`` and ``NORMALIZED_TO``
  the ``CODE-FUNCTION`` / ``CODE-CLASS`` :CanonicalTerm
- module-level imports -> :Entity of type ``dependency`` (``CODE-DEPENDENCY``)
- symbol-level edges (calls/uses/references/method/inherits) -> ``RELATES_TO``

The canonical markdown rendered here is the code-domain IR; it is written to
the graph by :func:`code_domain.extract.extract_code_document` and can be
reconstructed byte-for-byte by :func:`code_domain.recompose.recompose_code_document`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain.pack import DomainPackBundle
from app.storage.client import Neo4jClient

from code_domain.extract import extract_code_document

CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".go", ".java", ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
})

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")
_LOC_RE = re.compile(r"^L(\d+)$")


def _line_number(source_location: str | None) -> int:
    """Sort key for graphify ``source_location`` values (``L123``)."""
    if not source_location:
        return 0
    match = _LOC_RE.match(str(source_location))
    return int(match.group(1)) if match else 0


def doc_id_for_module(source_file: str, prefix: str = "id_code_") -> str:
    """Deterministic graph key for a module Document."""
    slug = _SLUG_RE.sub("_", source_file).strip("_") or "module"
    return f"{prefix}{slug}"


def function_entity_id(doc_id: str, position: int) -> str:
    return f"{doc_id}:fn:{position}"


def class_entity_id(doc_id: str, position: int) -> str:
    return f"{doc_id}:cls:{position}"


def dependency_entity_id(doc_id: str, position: int) -> str:
    return f"{doc_id}:dep:{position}"


@dataclass(frozen=True)
class SymbolInfo:
    """One function/class symbol extracted by graphify."""

    node_id: str
    label: str
    source_file: str
    source_location: str
    kind: str  # 'function' | 'class'


@dataclass
class ModuleInfo:
    """One real module (source file) and its symbols/dependencies."""

    node_id: str
    label: str
    source_file: str
    functions: list[SymbolInfo] = field(default_factory=list)
    classes: list[SymbolInfo] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)


def graphify_extract_corpus(
    corpus_dir: str | Path,
    *,
    cache_root: str | Path | None = None,
) -> Any:
    """Run graphify extract+build on a code corpus and return the graph."""
    from graphify.build import build as graphify_build
    from graphify.extract import extract as graphify_extract

    corpus = Path(corpus_dir).resolve()
    files = sorted(
        p for p in corpus.rglob("*")
        if p.is_file() and p.suffix.lower() in CODE_EXTENSIONS
    )
    raw = graphify_extract(
        files,
        cache_root=Path(cache_root) if cache_root is not None else None,
        root=corpus,
        parallel=False,
    )
    return graphify_build([raw], dedup=True, root=corpus)


def _is_module_node(data: dict[str, Any]) -> bool:
    return (
        data.get("file_type") == "code"
        and bool(data.get("source_file"))
        and str(data.get("label", "")).endswith(".py")
    )


def _is_function_node(data: dict[str, Any]) -> bool:
    return (
        data.get("file_type") == "code"
        and bool(data.get("_callable"))
        and not data.get("_callable_class")
    )


def _is_class_node(data: dict[str, Any]) -> bool:
    return (
        data.get("file_type") == "code"
        and bool(data.get("_callable_class"))
    )


def collect_modules(graph: Any) -> list[ModuleInfo]:
    """Group graphify code nodes into modules (source files)."""
    module_nodes: dict[str, ModuleInfo] = {}
    for node_id, data in graph.nodes(data=True):
        if _is_module_node(data):
            module_nodes[node_id] = ModuleInfo(
                node_id=node_id,
                label=str(data.get("label")),
                source_file=str(data.get("source_file")),
            )

    for node_id, data in graph.nodes(data=True):
        source_file = str(data.get("source_file") or "")
        if not source_file:
            continue
        module = next(
            (m for m in module_nodes.values() if m.source_file == source_file),
            None,
        )
        if module is None:
            continue
        if _is_function_node(data):
            module.functions.append(
                SymbolInfo(
                    node_id=node_id,
                    label=str(data.get("label") or node_id),
                    source_file=source_file,
                    source_location=str(data.get("source_location") or ""),
                    kind="function",
                )
            )
        elif _is_class_node(data):
            module.classes.append(
                SymbolInfo(
                    node_id=node_id,
                    label=str(data.get("label") or node_id),
                    source_file=source_file,
                    source_location=str(data.get("source_location") or ""),
                    kind="class",
                )
            )

    module_by_id = {m.node_id: m for m in module_nodes.values()}
    for u, v, data in graph.edges(data=True):
        src = data.get("_src")
        tgt = data.get("_tgt")
        if src is None or tgt is None:
            continue
        relation = str(data.get("relation") or "")
        if relation not in {"imports_from", "re_exports"}:
            continue
        source_module = module_by_id.get(src)
        target_module = module_by_id.get(tgt)
        if source_module is None or target_module is None:
            continue
        if target_module.label not in source_module.dependencies:
            source_module.dependencies.append(target_module.label)

    modules = list(module_nodes.values())
    for module in modules:
        module.functions.sort(key=lambda s: (_line_number(s.source_location), s.label))
        module.classes.sort(key=lambda s: (_line_number(s.source_location), s.label))
        module.dependencies.sort()
    modules.sort(key=lambda m: m.source_file)
    return modules


def render_canonical_md(module: ModuleInfo) -> str:
    """Render the code-domain canonical markdown for one module."""
    lines = [
        "---",
        f"title: {module.label}",
        f"id: {module.source_file}",
        "lang: en",
        "source_lang: en",
        "verification_level: L1",
        "canonical_version: 1",
        "---",
        "## Functions",
    ]
    lines.extend(f"- {fn.label}" for fn in module.functions)
    lines.append("## Classes")
    lines.extend(f"- {cls.label}" for cls in module.classes)
    lines.append("## Dependencies")
    lines.extend(f"- {dep}" for dep in module.dependencies)
    return "\n".join(lines) + "\n"


@dataclass
class MappingResult:
    """Counts produced by one :func:`map_graphify_to_graph` run."""

    modules: int
    functions: int
    classes: int
    dependencies: int
    relations: int
    documents: list[str] = field(default_factory=list)


def map_graphify_to_graph(
    client: Neo4jClient,
    graph: Any,
    pack: DomainPackBundle,
    *,
    doc_prefix: str = "id_code_",
) -> MappingResult:
    """Write the code corpus into the knowledge layer graph (idempotent)."""
    modules = collect_modules(graph)
    entity_id_map: dict[str, str] = {}
    documents: list[str] = []

    for module in modules:
        doc_id = doc_id_for_module(module.source_file, prefix=doc_prefix)
        canonical_md = render_canonical_md(module)
        extract_code_document(client, doc_id, canonical_md, pack)
        documents.append(doc_id)
        for position, fn in enumerate(module.functions):
            entity_id_map[fn.node_id] = function_entity_id(doc_id, position)
        for position, cls in enumerate(module.classes):
            entity_id_map[cls.node_id] = class_entity_id(doc_id, position)

    relations = _write_relations(client, graph, entity_id_map)

    return MappingResult(
        modules=len(modules),
        functions=sum(len(m.functions) for m in modules),
        classes=sum(len(m.classes) for m in modules),
        dependencies=sum(len(m.dependencies) for m in modules),
        relations=relations,
        documents=documents,
    )


def _write_relations(
    client: Neo4jClient,
    graph: Any,
    entity_id_map: dict[str, str],
) -> int:
    """Write symbol-level graphify edges as ``RELATES_TO`` arcs."""
    rows: list[tuple[str, str, str]] = []
    for u, v, data in graph.edges(data=True):
        src = data.get("_src")
        tgt = data.get("_tgt")
        if src is None or tgt is None:
            continue
        if src not in entity_id_map or tgt not in entity_id_map:
            continue
        relation = str(data.get("relation") or "RELATES_TO")
        rows.append((entity_id_map[src], entity_id_map[tgt], relation))

    if not rows:
        return 0

    def work(tx: Any) -> None:
        for source_id, target_id, relation in rows:
            tx.run(
                """
                MATCH (a:Entity {id: $source_id})
                MATCH (b:Entity {id: $target_id})
                MERGE (a)-[r:RELATES_TO {relation: $relation}]->(b)
                """,
                source_id=source_id,
                target_id=target_id,
                relation=relation,
            )

    with client.session() as session:
        session.execute_write(work)
    return len(rows)


def reference_labels_and_triples(graph: Any) -> tuple[set[str], set[tuple[str, str, str]]]:
    """Reference function/class labels and dependency triples from graphify.

    This is the parity oracle: the code pack mapping must reproduce exactly
    these names/relations (UUIDs are deliberately excluded).
    """
    labels: set[str] = set()
    for node_id, data in graph.nodes(data=True):
        if _is_function_node(data) or _is_class_node(data):
            labels.add(str(data.get("label") or node_id))

    triples: set[tuple[str, str, str]] = set()
    for u, v, data in graph.edges(data=True):
        src = data.get("_src")
        tgt = data.get("_tgt")
        if src is None or tgt is None:
            continue
        src_data = graph.nodes.get(src)
        tgt_data = graph.nodes.get(tgt)
        if src_data is None or tgt_data is None:
            continue
        if not (
            (_is_function_node(src_data) or _is_class_node(src_data))
            and (_is_function_node(tgt_data) or _is_class_node(tgt_data))
        ):
            continue
        triples.add(
            (
                str(src_data.get("label") or src),
                str(data.get("relation") or "RELATES_TO"),
                str(tgt_data.get("label") or tgt),
            )
        )
    return labels, triples
