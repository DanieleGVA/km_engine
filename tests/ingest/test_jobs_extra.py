"""Test mirati aggiuntivi per JobManager (FR1.5) — copertura WP8.

Coprono i rami non esercitati dalla suite WP4: created_by UUID, list con
filtro status, errori di validazione, stato resume corrotto/assente e
lifecycle completo/missing.
"""

from __future__ import annotations

import pytest

from app.ingest.errors import InvalidJobStateError, JobNotFoundError
from app.ingest.jobs import JobManager


def test_create_with_created_by_uuid(jobs: JobManager, conn) -> None:
    from app.auth.users import create_user

    user = create_user(
        conn,
        "wp4_job_owner",
        "wp4_job_owner@example.test",
        "wp4-job-owner-password-123",
        roles=("ingestor",),
    )
    job = jobs.create("wp4_job_created_by", "code", created_by=user["id"])
    assert job.created_by == str(user["id"])
    conn.execute("DELETE FROM users WHERE id = %s", (user["id"],))


def test_create_invalid_job_type_rejected(jobs: JobManager) -> None:
    with pytest.raises(ValueError):
        jobs.create("wp4_job_bad_type", "video")


def test_list_jobs_all_and_by_status(jobs: JobManager) -> None:
    a = jobs.create("wp4_job_list_a", "code")
    b = jobs.create("wp4_job_list_b", "document")
    jobs.start(b.id)
    jobs.complete(b.id)

    all_jobs = jobs.list_jobs()
    ids = {j.id for j in all_jobs}
    assert a.id in ids and b.id in ids

    # Il filtro status è relativo ai job creati qui (il DB dev può contenere
    # job residui di altri flussi, es. demo/sibling): non assumiamo una tabella vuota.
    completed = jobs.list_jobs(status="completed")
    completed_ids = {j.id for j in completed}
    assert b.id in completed_ids
    assert a.id not in completed_ids
    pending = jobs.list_jobs(status="pending")
    pending_ids = {j.id for j in pending}
    assert a.id in pending_ids
    assert b.id not in pending_ids


def test_list_jobs_invalid_status_rejected(jobs: JobManager) -> None:
    with pytest.raises(ValueError):
        jobs.list_jobs(status="exploded")


def test_set_status_with_error(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_error", "code")
    failed = jobs.set_status(job.id, "failed", error="kaboom")
    assert failed.status == "failed"
    assert failed.error == "kaboom"


def test_complete_and_fail_missing_job_raise(jobs: JobManager) -> None:
    with pytest.raises(JobNotFoundError):
        jobs.complete(999999)
    with pytest.raises(JobNotFoundError):
        jobs.fail(999999, "boom")


def test_start_missing_job_raises(jobs: JobManager) -> None:
    with pytest.raises(JobNotFoundError):
        jobs.start(999999)


def test_save_state_missing_job_raises(jobs: JobManager) -> None:
    with pytest.raises(JobNotFoundError):
        jobs.save_state(999999, {"files": []})


def test_load_state_missing_returns_none(jobs: JobManager) -> None:
    assert jobs.load_state(999999) is None


def test_load_state_corrupted_raises(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_corrupt_state", "code")
    path = jobs._state_path(job.id)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidJobStateError):
        jobs.load_state(job.id)


def test_load_state_non_object_raises(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_list_state", "code")
    path = jobs._state_path(job.id)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(InvalidJobStateError):
        jobs.load_state(job.id)


def test_delete_state_removes_sidecar(jobs: JobManager) -> None:
    job = jobs.create("wp4_job_delete_state", "code")
    jobs.save_state(job.id, {"files": ["a.py"]})
    assert jobs.load_state(job.id) is not None
    jobs.delete_state(job.id)
    assert jobs.load_state(job.id) is None
    # delete di uno stato inesistente non deve sollevare
    jobs.delete_state(job.id)
