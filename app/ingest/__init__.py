"""WP4 ingestion pipeline: job-based, chunked, incremental, FR9-aware.

Public API:
- :class:`IngestPipeline` — orchestration.
- :class:`JobManager` — ``ingest_jobs`` CRUD + resume state.
- :class:`GraphifyCodeExtractor` — reuse of graphify extract/dedup.
- :class:`StubSemanticService` / :class:`LLMSemanticService` — semantic pass.
- :func:`normalize_language` — FR9 canonical-language detection.
"""

from __future__ import annotations

from app.ingest.config import IngestSettings, get_ingest_settings
from app.ingest.errors import (
    IngestError,
    InvalidJobStateError,
    JobNotFoundError,
    UnsupportedFileTypeError,
)
from app.ingest.extractor import (
    PRIORITY_CODE_EXTENSIONS,
    CodeExtractor,
    GraphifyCodeExtractor,
)
from app.ingest.hash_cache import HashCache
from app.ingest.jobs import JobManager
from app.ingest.language import LanguageInfo, normalize_language
from app.ingest.models import (
    CandidateFact,
    EntityRecord,
    ExtractionResult,
    FactRecord,
    FileInfo,
    IngestJob,
    RelationRecord,
)
from app.ingest.pipeline import IngestPipeline
from app.ingest.semantic import (
    LLMSemanticService,
    SemanticService,
    StubSemanticService,
)

__all__ = [
    "PRIORITY_CODE_EXTENSIONS",
    "CandidateFact",
    "CodeExtractor",
    "EntityRecord",
    "ExtractionResult",
    "FactRecord",
    "FileInfo",
    "GraphifyCodeExtractor",
    "HashCache",
    "IngestError",
    "IngestJob",
    "IngestPipeline",
    "IngestSettings",
    "InvalidJobStateError",
    "JobManager",
    "JobNotFoundError",
    "LLMSemanticService",
    "LanguageInfo",
    "RelationRecord",
    "SemanticService",
    "StubSemanticService",
    "UnsupportedFileTypeError",
    "get_ingest_settings",
    "normalize_language",
]
