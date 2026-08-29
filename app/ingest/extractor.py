"""Code extractor: reuse graphify's AST extraction and dedup (FR1.1/FR1.6)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.ingest.models import ExtractionResult

# Priority languages from requirements Q2: Python, JS/TS, Go, Java, C/C++.
PRIORITY_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".go",
    ".java",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
})


class CodeExtractor(ABC):
    """Extract nodes/edges from a list of code files."""

    @abstractmethod
    def extract(self, paths: list[Path], root: Path) -> ExtractionResult:
        """Return deduplicated nodes/edges for ``paths``."""


class GraphifyCodeExtractor(CodeExtractor):
    """Reuse ``graphify.extract.extract`` + ``graphify.dedup.deduplicate_entities``.

    Graphify is imported lazily so the rest of the ingestion layer can be
    imported (and unit-tested) even when the optional graphify workspace
    dependency is not installed.
    """

    def __init__(self, *, cache_root: str | Path | None = None) -> None:
        self.cache_root = Path(cache_root) if cache_root is not None else None

    def extract(self, paths: list[Path], root: Path) -> ExtractionResult:
        if not paths:
            return ExtractionResult()
        from graphify.dedup import deduplicate_entities
        from graphify.extract import extract as graphify_extract

        raw = graphify_extract(
            [Path(p) for p in paths],
            cache_root=self.cache_root,
            root=root,
            parallel=False,
        )
        nodes = list(raw.get("nodes", []))
        edges = list(raw.get("edges", []))
        nodes, edges = deduplicate_entities(
            nodes, edges, communities={}, root=root
        )
        return ExtractionResult(nodes=nodes, edges=edges)
