"""Job manager for ``ingest_jobs`` (FR1.5).

The Postgres row stores status/progress/error/timestamps. Resume position is
persisted in a JSON sidecar under ``state_dir`` because the baseline schema
(``db/postgres/001_init.sql``) has no state/position column and WP4 must not
modify the schema.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from app.ingest.errors import InvalidJobStateError, JobNotFoundError
from app.ingest.models import IngestJob

VALID_JOB_TYPES = {"code", "document", "image"}
VALID_JOB_STATUSES = {"pending", "running", "paused", "completed", "failed"}

_STATE_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


class JobManager:
    """CRUD and state persistence for ingestion jobs."""

    def __init__(self, conn: psycopg.Connection, state_dir: str | Path) -> None:
        self.conn = conn
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row_to_job(row: Any) -> IngestJob:
        return IngestJob(
            id=row[0],
            source_uri=row[1],
            type=row[2],
            status=row[3],
            progress=row[4],
            error=row[5],
            created_by=str(row[6]) if row[6] is not None else None,
            started_at=row[7],
            finished_at=row[8],
            created_at=row[9],
            updated_at=row[10],
        )

    def _state_path(self, job_id: int) -> Path:
        return self.state_dir / f"job_{job_id}.json"

    def _require_job(self, job_id: int) -> IngestJob:
        job = self.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Ingest job {job_id!r} not found")
        return job

    # ------------------------------------------------------------------ CRUD
    def create(
        self,
        source_uri: str,
        job_type: str,
        *,
        created_by: UUID | str | None = None,
    ) -> IngestJob:
        """Create a pending job."""
        if job_type not in VALID_JOB_TYPES:
            raise ValueError(f"invalid job type {job_type!r}; expected one of {sorted(VALID_JOB_TYPES)}")
        with self.conn.transaction():
            row = self.conn.execute(
                """
                INSERT INTO ingest_jobs (source_uri, type, created_by)
                VALUES (%s, %s, %s)
                RETURNING id, source_uri, type, status, progress, error,
                          created_by, started_at, finished_at, created_at, updated_at
                """,
                (source_uri, job_type, _as_uuid(created_by)),
            ).fetchone()
        return self._row_to_job(row)

    def get(self, job_id: int) -> IngestJob | None:
        """Return a job by id, or None."""
        row = self.conn.execute(
            """
            SELECT id, source_uri, type, status, progress, error,
                   created_by, started_at, finished_at, created_at, updated_at
            FROM ingest_jobs WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, *, status: str | None = None) -> list[IngestJob]:
        """List jobs, optionally filtered by status."""
        if status is not None and status not in VALID_JOB_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        if status is None:
            rows = self.conn.execute(
                """
                SELECT id, source_uri, type, status, progress, error,
                       created_by, started_at, finished_at, created_at, updated_at
                FROM ingest_jobs ORDER BY id
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id, source_uri, type, status, progress, error,
                       created_by, started_at, finished_at, created_at, updated_at
                FROM ingest_jobs WHERE status = %s ORDER BY id
                """,
                (status,),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def _update(self, job_id: int, *, fields: dict[str, Any]) -> IngestJob:
        """Update a job row with the given fields and ``updated_at=now()``."""
        allowed = {
            "status", "progress", "error", "started_at", "finished_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in VALID_JOB_STATUSES:
            raise ValueError(f"invalid status {fields['status']!r}")
        if "progress" in fields:
            progress = int(fields["progress"])
            if not 0 <= progress <= 100:
                raise ValueError("progress must be between 0 and 100")
            fields["progress"] = progress
        assignments = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [job_id]
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE ingest_jobs
                SET {assignments}, updated_at = now()
                WHERE id = %s
                RETURNING id, source_uri, type, status, progress, error,
                          created_by, started_at, finished_at, created_at, updated_at
                """,
                values,
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Ingest job {job_id!r} not found")
        return self._row_to_job(row)

    def set_status(self, job_id: int, status: str, *, error: str | None = None) -> IngestJob:
        """Set status (and optionally error)."""
        fields: dict[str, Any] = {"status": status}
        if error is not None:
            fields["error"] = error
        return self._update(job_id, fields=fields)

    def set_progress(self, job_id: int, progress: int) -> IngestJob:
        """Set progress (0-100)."""
        return self._update(job_id, fields={"progress": progress})

    def start(self, job_id: int) -> IngestJob:
        """Mark running, preserving the original ``started_at`` on resume."""
        self._require_job(job_id)
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE ingest_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE id = %s
                RETURNING id, source_uri, type, status, progress, error,
                          created_by, started_at, finished_at, created_at, updated_at
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def complete(self, job_id: int) -> IngestJob:
        """Mark completed with progress 100."""
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE ingest_jobs
                SET status = 'completed', progress = 100,
                    finished_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING id, source_uri, type, status, progress, error,
                          created_by, started_at, finished_at, created_at, updated_at
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Ingest job {job_id!r} not found")
        return self._row_to_job(row)

    def fail(self, job_id: int, error: str) -> IngestJob:
        """Mark failed with an error message."""
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE ingest_jobs
                SET status = 'failed', error = %s,
                    finished_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING id, source_uri, type, status, progress, error,
                          created_by, started_at, finished_at, created_at, updated_at
                """,
                (error, job_id),
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Ingest job {job_id!r} not found")
        return self._row_to_job(row)

    def pause(self, job_id: int) -> IngestJob:
        """Mark paused (used when a chunked run stops cleanly)."""
        return self.set_status(job_id, "paused")

    # ------------------------------------------------------------------ state
    def save_state(self, job_id: int, state: dict[str, Any]) -> None:
        """Persist resume state atomically."""
        self._require_job(job_id)
        payload = {"version": _STATE_VERSION, "job_id": job_id, **state}
        path = self._state_path(job_id)
        fd, tmp = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(self.state_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_state(self, job_id: int) -> dict[str, Any] | None:
        """Return the persisted resume state, or None."""
        path = self._state_path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise InvalidJobStateError(
                f"cannot read resume state for job {job_id}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise InvalidJobStateError(f"resume state for job {job_id} is not an object")
        return data

    def delete_state(self, job_id: int) -> None:
        """Delete the resume state sidecar."""
        try:
            self._state_path(job_id).unlink()
        except FileNotFoundError:
            pass
