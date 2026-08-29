"""Test audit log append-only: azione, entita', old/new jsonb (ADR-002 D4)."""
from __future__ import annotations

from app.auth import record_audit


def read_last_audit(conn, entity_id: str) -> dict:
    row = conn.execute(
        """
        SELECT user_id, action, entity_id, entity_type, old_value, new_value
        FROM audit_log WHERE entity_id = %s ORDER BY id DESC LIMIT 1
        """,
        (entity_id,),
    ).fetchone()
    assert row is not None, "riga di audit attesa ma non trovata"
    return {
        "user_id": row[0],
        "action": row[1],
        "entity_id": row[2],
        "entity_type": row[3],
        "old_value": row[4],
        "new_value": row[5],
    }


class TestAuditRecord:
    def test_create_user_writes_audit_row(self, conn, make_user):
        user = make_user("ugo")  # create_user registra da solo l'audit CREATE
        row = read_last_audit(conn, str(user["id"]))
        assert row["action"] == "CREATE"
        assert row["entity_type"] == "User"
        assert row["user_id"] is None  # azione di sistema/test: actor non passato
        assert row["new_value"]["username"] == "test_ugo"
        assert row["old_value"] is None
        # mai la password nel registro
        import json
        assert "password" not in json.dumps(row["new_value"]).lower()

    def test_record_with_old_and_new_jsonb(self, conn, make_user):
        user = make_user("vera")
        with conn.transaction():
            record_audit(
                conn,
                user["id"],
                "UPDATE",
                "fact-42",
                "Fact",
                old_value={"value": "vecchio", "valid": True},
                new_value={"value": "nuovo", "valid": True},
            )
        row = read_last_audit(conn, "fact-42")
        assert row["user_id"] == user["id"]
        assert row["action"] == "UPDATE"
        assert row["entity_type"] == "Fact"
        assert row["old_value"] == {"value": "vecchio", "valid": True}
        assert row["new_value"] == {"value": "nuovo", "valid": True}

    def test_role_change_is_audited(self, conn, make_user):
        from app.auth import assign_role, revoke_role
        user = make_user("wanda")
        assign_role(conn, user["id"], "editor", actor_id=user["id"])
        row = read_last_audit(conn, str(user["id"]))
        assert row["action"] == "GRANT_ROLE"
        assert row["new_value"] == {"role": "editor"}
        assert row["user_id"] == user["id"]
        revoke_role(conn, user["id"], "editor", actor_id=user["id"])
        row = read_last_audit(conn, str(user["id"]))
        assert row["action"] == "REVOKE_ROLE"
        assert row["old_value"] == {"role": "editor"}

    def test_record_rolls_back_with_caller_transaction(self, conn, make_user):
        user = make_user("zoe")
        try:
            with conn.transaction():
                record_audit(conn, user["id"], "UPDATE", "fact-99", "Fact", new_value={"x": 1})
                raise RuntimeError("fallimento simulato dopo l'audit")
        except RuntimeError:
            pass
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE entity_id = 'fact-99'"
        ).fetchone()[0] == 0
