"""Chunked, incremental, resumable ingestion pipeline (WP4)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg

from app.conflict.detection import post_ingest_hook
from app.ingest.config import IngestSettings
from app.ingest.errors import InvalidJobStateError, JobNotFoundError
from app.ingest.extractor import (
    PRIORITY_CODE_EXTENSIONS,
    CodeExtractor,
    GraphifyCodeExtractor,
)
from app.ingest.graph_writer import GraphWriter
from app.ingest.hash_cache import HashCache
from app.ingest.jobs import JobManager
from app.ingest.language import normalize_language
from app.ingest.mapping import (
    make_entity_id,
    make_source_id,
    map_extraction,
    namespace_for,
)
from app.ingest.models import (
    CandidateFact,
    EntityRecord,
    FactRecord,
    FileInfo,
    IngestJob,
)
from app.ingest.semantic import SemanticService, StubSemanticService
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".text",
})
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
})
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".mypy_cache",
})
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("_", value).strip("_") or "value"


def hash_file(path: Path) -> str:
    """SHA-256 of the real file content."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_files(root: Path, job_type: str) -> list[Path]:
    """Return supported files under ``root`` for a job type, sorted."""
    if job_type == "code":
        extensions = PRIORITY_CODE_EXTENSIONS
    elif job_type == "document":
        extensions = DOCUMENT_EXTENSIONS
    elif job_type == "image":
        extensions = IMAGE_EXTENSIONS
    else:
        raise ValueError(f"unsupported job type {job_type!r}")

    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in extensions:
            files.append(path)
    return sorted(files)


class IngestPipeline:
    """Orchestrates job-based ingestion into Neo4j.

    The pipeline is deliberately synchronous and single-writer: chunked
    processing keeps memory bounded and the persisted job state makes a run
    resumable after interruption.
    """

    def __init__(
        self,
        *,
        repo: GraphRepository,
        client: Neo4jClient,
        conn: psycopg.Connection,
        settings: IngestSettings | None = None,
        semantic_service: SemanticService | None = None,
        code_extractor: CodeExtractor | None = None,
        hash_cache: HashCache | None = None,
        jobs: JobManager | None = None,
        enable_conflict_detection: bool = True,
    ) -> None:
        self.repo = repo
        self.client = client
        self.conn = conn
        self.settings = settings or IngestSettings()
        self.semantic = semantic_service or StubSemanticService()
        self.extractor = code_extractor or GraphifyCodeExtractor(
            cache_root=self.settings.cache_dir / "graphify_ast"
        )
        self.hash_cache = hash_cache or HashCache(
            self.settings.cache_dir / "hash_cache.json"
        )
        self.jobs = jobs or JobManager(
            conn, self.settings.cache_dir / "jobs"
        )
        self.writer = GraphWriter(repo, client)
        self.enable_conflict_detection = enable_conflict_detection

    def _run_conflict_hook(self, entity_ids: list[str]) -> None:
        """Run WP6 conflict detection for the Entities touched by a chunk."""
        if not self.enable_conflict_detection or not entity_ids:
            return
        post_ingest_hook(self.repo, self.conn, entity_ids)

    # ------------------------------------------------------------------ public
    def run(
        self,
        source_uri: str,
        root: str | Path,
        *,
        job_type: str = "code",
        job_id: int | None = None,
        resume: bool = False,
        chunk_size: int | None = None,
        stop_after_chunks: int | None = None,
    ) -> IngestJob:
        """Run (or resume) an ingestion job.

        ``stop_after_chunks`` is a test hook: when set, the run pauses cleanly
        after that many chunks so tests can exercise resume without killing the
        process.
        """
        root = Path(root).resolve()
        chunk_size = chunk_size or self.settings.chunk_size
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")

        if resume:
            if job_id is None:
                raise ValueError("resume=True requires job_id")
            job = self.jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(f"Ingest job {job_id!r} not found")
            state = self.jobs.load_state(job_id)
            if state is None:
                raise InvalidJobStateError(
                    f"Ingest job {job_id!r} has no resume state"
                )
            files = [root / rel for rel in state.get("files", [])]
            next_index = int(state.get("next_index", 0))
        else:
            if job_id is None:
                job = self.jobs.create(source_uri, job_type)
            else:
                job = self.jobs.get(job_id)
                if job is None:
                    raise JobNotFoundError(f"Ingest job {job_id!r} not found")
            files = scan_files(root, job_type)
            state = {
                "source_uri": source_uri,
                "job_type": job_type,
                "root": str(root),
                "files": [p.relative_to(root).as_posix() for p in files],
                "next_index": 0,
                "processed": [],
            }
            self.jobs.save_state(job.id, state)
            next_index = 0

        self.jobs.start(job.id)
        total = len(files)
        if total == 0:
            self.jobs.complete(job.id)
            return self.jobs.get(job.id)  # type: ignore[return-value]

        namespace = namespace_for(source_uri)
        try:
            chunk_index = 0
            while next_index < total:
                chunk_files = files[next_index : next_index + chunk_size]
                self._process_chunk(chunk_files, root, job_type, namespace)
                next_index += len(chunk_files)
                state["next_index"] = next_index
                state["processed"] = [
                    p.relative_to(root).as_posix() for p in files[:next_index]
                ]
                self.jobs.save_state(job.id, state)
                progress = int(next_index / total * 100)
                self.jobs.set_progress(job.id, progress)
                chunk_index += 1
                if stop_after_chunks is not None and chunk_index >= stop_after_chunks:
                    self.jobs.pause(job.id)
                    return self.jobs.get(job.id)  # type: ignore[return-value]
            self.jobs.complete(job.id)
            return self.jobs.get(job.id)  # type: ignore[return-value]
        except Exception as exc:
            self.jobs.fail(job.id, f"{type(exc).__name__}: {exc}")
            raise

    # ------------------------------------------------------------------ chunks
    def _process_chunk(
        self,
        files: list[Path],
        root: Path,
        job_type: str,
        namespace: str,
    ) -> None:
        if job_type == "code":
            self._process_code_chunk(files, root, namespace)
        elif job_type == "document":
            self._process_document_chunk(files, root, namespace)
        elif job_type == "image":
            self._process_image_chunk(files, root, namespace)
        else:
            raise ValueError(f"unsupported job type {job_type!r}")

    def _cache_key(self, namespace: str, rel: str) -> str:
        return f"{namespace}:{rel}"

    def _process_code_chunk(
        self, files: list[Path], root: Path, namespace: str
    ) -> None:
        changed: list[Path] = []
        file_info: dict[str, FileInfo] = {}
        for path in files:
            rel = path.relative_to(root).as_posix()
            content_hash = hash_file(path)
            key = self._cache_key(namespace, rel)
            if self.hash_cache.get(key) == content_hash:
                continue
            source_id = make_source_id(str(path))
            file_info[rel] = FileInfo(
                path=path, rel=rel, content_hash=content_hash, source_id=source_id
            )
            self.writer.upsert_source(
                source_id=source_id,
                uri=str(path),
                content_hash=content_hash,
                type="file",
                language="en",
            )
            changed.append(path)

        if changed:
            result = self.extractor.extract(changed, root)
            entities, facts, relations = map_extraction(
                result, namespace=namespace, root=root, file_info=file_info
            )
            for entity in entities:
                self.writer.upsert_entity(entity)
            for fact in facts:
                self.writer.upsert_fact(fact)
            for relation in relations:
                self.writer.upsert_relation(relation)
            self._run_conflict_hook([entity.entity_id for entity in entities])

        for info in file_info.values():
            self.hash_cache.set(
                self._cache_key(namespace, info.rel), info.content_hash
            )
        self.hash_cache.flush()

    def _process_document_chunk(
        self, files: list[Path], root: Path, namespace: str
    ) -> None:
        for path in files:
            rel = path.relative_to(root).as_posix()
            content_hash = hash_file(path)
            key = self._cache_key(namespace, rel)
            if self.hash_cache.get(key) == content_hash:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            info = normalize_language(text)
            source_id = make_source_id(str(path))
            self.writer.upsert_source(
                source_id=source_id,
                uri=str(path),
                content_hash=content_hash,
                type="file",
                language=info.detected,
            )
            candidates = self.semantic.analyze_text(
                text, source_uri=str(path), source_location="L1"
            )
            self._write_semantic_candidates(
                candidates,
                root=root,
                rel=rel,
                namespace=namespace,
                source_id=source_id,
                entity_type="document",
                default_translation_state=info.translation_state,
                default_source_language=info.source_language,
            )
            self.hash_cache.set(key, content_hash)
        self.hash_cache.flush()

    def _process_image_chunk(
        self, files: list[Path], root: Path, namespace: str
    ) -> None:
        for path in files:
            rel = path.relative_to(root).as_posix()
            content_hash = hash_file(path)
            key = self._cache_key(namespace, rel)
            if self.hash_cache.get(key) == content_hash:
                continue
            image_bytes = path.read_bytes()
            source_id = make_source_id(str(path))
            self.writer.upsert_source(
                source_id=source_id,
                uri=str(path),
                content_hash=content_hash,
                type="file",
                language="en",
            )
            candidates = self.semantic.analyze_image(
                image_bytes, source_uri=str(path), source_location="L1"
            )
            self._write_semantic_candidates(
                candidates,
                root=root,
                rel=rel,
                namespace=namespace,
                source_id=source_id,
                entity_type="image",
                default_translation_state="native",
                default_source_language=None,
            )
            self.hash_cache.set(key, content_hash)
        self.hash_cache.flush()

    def _write_semantic_candidates(
        self,
        candidates: list[CandidateFact],
        *,
        root: Path,
        rel: str,
        namespace: str,
        source_id: str,
        entity_type: str,
        default_translation_state: str,
        default_source_language: str | None,
    ) -> None:
        """Group candidate facts by entity label and write them to Neo4j."""
        grouped: dict[str, list[CandidateFact]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.entity_label, []).append(candidate)

        written_entity_ids: list[str] = []
        for entity_label, facts in grouped.items():
            entity_id = make_entity_id(namespace, f"doc:{rel}:{entity_label}")
            entity = EntityRecord(
                entity_id=entity_id,
                label=entity_label,
                type=entity_type,
                source_file=rel,
                source_location="L1",
                confidence="INFERRED",
                language="en",
                translation_state=default_translation_state,
                source_language=default_source_language,
            )
            self.writer.upsert_entity(entity)
            written_entity_ids.append(entity_id)
            for candidate in facts:
                fact_id = f"{entity_id}__{_slug(candidate.property)}"
                self.writer.upsert_fact(
                    FactRecord(
                        fact_id=fact_id,
                        entity_id=entity_id,
                        property=candidate.property,
                        value=candidate.value,
                        source_id=source_id,
                        confidence=candidate.confidence,
                        language=candidate.language,
                        translation_state=candidate.translation_state,
                        source_language=candidate.source_language,
                    )
                )
        self._run_conflict_hook(written_entity_ids)
