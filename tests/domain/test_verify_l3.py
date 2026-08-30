"""T6 — L3 adjudication queue: approve/reject + audit + frontmatter update."""
from __future__ import annotations

import pytest

from app.domain import (
    AdjudicationAlreadyResolvedError,
    AdjudicationNotFoundError,
    create_adjudication,
    create_glossary_proposal,
    decide_adjudication,
    decide_glossary_proposal,
    list_adjudications,
    list_glossary_proposals,
    update_document_verification_level,
)


def _audit_rows(pg_conn, entity_id: str) -> list[dict]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT action, entity_type, old_value, new_value FROM audit_log "
            "WHERE entity_type = 'Adjudication' AND entity_id = %s ORDER BY id",
            (entity_id,),
        )
        return [
            {"action": r[0], "entity_type": r[1], "old_value": r[2], "new_value": r[3]}
            for r in cur.fetchall()
        ]


def test_adjudication_approve_workflow(pg_conn, admin_user) -> None:
    created = create_adjudication(
        pg_conn,
        "ia_RIC-101",
        "steps",
        "L2 token overlap below threshold",
        suggestion="review the translated method",
        user_id=admin_user["id"],
    )
    assert created["status"] == "pending"
    assert created["document_id"] == "ia_RIC-101"

    pending = list_adjudications(pg_conn, status="pending")
    assert any(row["id"] == created["id"] for row in pending)

    decided = decide_adjudication(
        pg_conn, created["id"], "approved", admin_user["id"]
    )
    assert decided["status"] == "approved"
    assert decided["resolved_by"] == str(admin_user["id"])
    assert decided["resolved_at"] is not None

    rows = _audit_rows(pg_conn, str(created["id"]))
    assert [row["action"] for row in rows] == ["CREATE", "RESOLVE"]
    assert rows[1]["new_value"]["status"] == "approved"

    # The decision is reflected on the document frontmatter (verification_level).
    document_md = (
        "---\ntitle: Asparagus with butter\nid: RIC-101\nlang: en\n"
        "source_lang: it\nservings: 4\ntime_min: 15\ndifficulty: easy\n---\n"
        "## Ingredients\n- 1.5 kg asparagus\n## Method\n1. Cook.\n"
    )
    updated_md = update_document_verification_level(document_md, "L3")
    assert "verification_level: L3" in updated_md


def test_adjudication_reject_workflow(pg_conn, admin_user) -> None:
    created = create_adjudication(
        pg_conn, "ia_RIC-102", "ingredients", "ingredient count mismatch"
    )
    decided = decide_adjudication(
        pg_conn, created["id"], "rejected", admin_user["id"]
    )
    assert decided["status"] == "rejected"
    assert list_adjudications(pg_conn, status="pending") == []


def test_adjudication_already_resolved_raises(pg_conn, admin_user) -> None:
    created = create_adjudication(pg_conn, "ia_RIC-103", "title", "divergence")
    decide_adjudication(pg_conn, created["id"], "approved", admin_user["id"])
    with pytest.raises(AdjudicationAlreadyResolvedError):
        decide_adjudication(pg_conn, created["id"], "rejected", admin_user["id"])


def test_adjudication_missing_raises(pg_conn, admin_user) -> None:
    with pytest.raises(AdjudicationNotFoundError):
        decide_adjudication(pg_conn, 999999, "approved", admin_user["id"])


def test_update_document_verification_level() -> None:
    md = (
        "---\ntitle: Asparagus with butter\nid: RIC-101\nlang: en\n"
        "source_lang: it\nservings: 4\ntime_min: 15\ndifficulty: easy\n---\n"
        "## Ingredients\n- 1.5 kg asparagus\n## Method\n1. Cook.\n"
    )
    updated = update_document_verification_level(md, "L3")
    assert "verification_level: L3" in updated
    # Existing value is replaced, not duplicated.
    updated_again = update_document_verification_level(updated, "L2")
    assert updated_again.count("verification_level:") == 1
    assert "verification_level: L2" in updated_again


def test_glossary_proposal_workflow(pg_conn, admin_user) -> None:
    created = create_glossary_proposal(
        pg_conn,
        "ia_mandorle dolci sbucciate",
        context="ia_RIC-103",
        user_id=admin_user["id"],
    )
    assert created["status"] == "pending"
    decided = decide_glossary_proposal(
        pg_conn, created["id"], "approved", admin_user["id"]
    )
    assert decided["status"] == "approved"
    assert list_glossary_proposals(pg_conn, status="pending") == []
