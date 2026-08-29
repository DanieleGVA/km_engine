"""Tests for the ingest_jobs JobManager (FR1.5)."""

from __future__ import annotations

import pytest

from app.ingest.errors import JobNotFoundError
from app.ingest.jobs import JobManager


def test_create_and_get_job(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_create", "code")
    assert job.id > 0
    assert job.source_uri == "wp4_job_create"
    assert job.type == "code"
    assert job.status == "pending"
    assert job.progress == 0
    assert job.error is None

    fetched = jobs.get(job.id)
    assert fetched is not None
    assert fetched.id == job.id
    assert fetched.status == "pending"


def test_get_missing_job_returns_none(jobs: JobManager) -> None:
    assert jobs.get(999999) is None


def test_start_complete_lifecycle(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_lifecycle", "document")
    started = jobs.start(job.id)
    assert started.status == "running"
    assert started.started_at is not None

    jobs.set_progress(job.id, 42)
    assert jobs.get(job.id).progress == 42

    completed = jobs.complete(job.id)
    assert completed.status == "completed"
    assert completed.progress == 100
    assert completed.finished_at is not None


def test_fail_stores_error(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_fail", "image")
    jobs.start(job.id)
    failed = jobs.fail(job.id, "boom")
    assert failed.status == "failed"
    assert failed.error == "boom"
    assert failed.finished_at is not None


def test_pause_and_resume_state_roundtrip(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_state", "code")
    jobs.start(job.id)
    jobs.pause(job.id)
    assert jobs.get(job.id).status == "paused"

    state = {
        "source_uri": "wp4_job_state",
        "job_type": "code",
        "files": ["a.py", "b.py"],
        "next_index": 1,
        "processed": ["a.py"],
    }
    jobs.save_state(job.id, state)
    loaded = jobs.load_state(job.id)
    assert loaded is not None
    assert loaded["next_index"] == 1
    assert loaded["files"] == ["a.py", "b.py"]
    assert loaded["job_id"] == job.id


def test_update_missing_job_raises(jobs: JobManager) -> None:
    with pytest.raises(JobNotFoundError):
        jobs.set_status(999999, "running")


def test_invalid_status_rejected(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_invalid_status", "code")
    with pytest.raises(ValueError):
        jobs.set_status(job.id, "exploded")


def test_invalid_progress_rejected(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_invalid_progress", "code")
    with pytest.raises(ValueError):
        jobs.set_progress(job.id, 101)
