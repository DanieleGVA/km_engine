"""Deterministic golden set for the code domain (Iteration D, WP-D3).

Queries are natural-language templates around the real symbol names extracted
from the corpus. The expected answer is the module (``document_id``) that owns
the symbol. The deterministic embedding tokenizes on word boundaries and
underscores, so the templates keep the exact symbol tokens (camelCase class
names stay intact, snake_case function names are split by the embedder).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_domain.mapping import ModuleInfo, collect_modules, graphify_extract_corpus


def _function_name(label: str) -> str:
    """``extract_document()`` -> ``extract_document``; ``.embed()`` -> ``embed``."""
    name = label.strip()
    if name.endswith("()"):
        name = name[:-2]
    return name.lstrip(".")


def _module_name(label: str) -> str:
    """``extract.py`` -> ``extract``."""
    return label[:-3] if label.endswith(".py") else label


def build_golden_set(
    modules: list[ModuleInfo],
    *,
    min_queries: int = 50,
) -> list[dict[str, Any]]:
    """Build a deterministic golden set of >= ``min_queries`` queries."""
    pairs: list[dict[str, Any]] = []
    seen_functions: set[str] = set()
    seen_classes: set[str] = set()

    for module in modules:
        module_name = _module_name(module.label)
        pairs.append(
            {
                "query": f"module {module_name}",
                "document_id": module.source_file,
                "kind": "module_name",
            }
        )
        pairs.append(
            {
                "query": f"file {module_name}",
                "document_id": module.source_file,
                "kind": "module_file",
            }
        )

        for fn in module.functions:
            name = _function_name(fn.label)
            if not name or name in seen_functions:
                continue
            seen_functions.add(name)
            pairs.append(
                {
                    "query": f"function {name} in {module_name}",
                    "document_id": module.source_file,
                    "kind": "function_name",
                }
            )
            pairs.append(
                {
                    "query": f"where is {name} defined in {module_name}",
                    "document_id": module.source_file,
                    "kind": "function_where",
                }
            )

        for cls in module.classes:
            name = cls.label
            if not name or name in seen_classes:
                continue
            seen_classes.add(name)
            pairs.append(
                {
                    "query": f"class {name} in {module_name}",
                    "document_id": module.source_file,
                    "kind": "class_name",
                }
            )
            pairs.append(
                {
                    "query": f"which module contains {name} in {module_name}",
                    "document_id": module.source_file,
                    "kind": "class_where",
                }
            )

    # Deterministic order: document_id, then query.
    pairs.sort(key=lambda p: (p["document_id"], p["query"]))
    if len(pairs) < min_queries:
        raise ValueError(
            f"golden set has {len(pairs)} queries, need at least {min_queries}"
        )
    return pairs


def build_golden_set_from_corpus(
    corpus_dir: str | Path,
    *,
    cache_root: str | Path | None = None,
    min_queries: int = 50,
) -> list[dict[str, Any]]:
    """Convenience wrapper: extract the corpus and build the golden set."""
    graph = graphify_extract_corpus(corpus_dir, cache_root=cache_root)
    return build_golden_set(collect_modules(graph), min_queries=min_queries)


def write_golden_set(path: str | Path, pairs: list[dict[str, Any]]) -> Path:
    """Write the golden set JSON (versioned artifact)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "generated_by": "code_domain/golden.py",
        "corpus": "tests/fixtures/corpus_code",
        "pairs": pairs,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_golden_set(path: str | Path) -> list[dict[str, Any]]:
    """Load a golden set JSON and return its pairs."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(raw["pairs"])
