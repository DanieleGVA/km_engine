"""WP-C6 Documenter tests (prefix ic5_).

Decision records are generated from canon-log + approved adjudications; the
pack changelog diffs two pack versions and is non-empty and coherent.
"""
from __future__ import annotations

import json
import shutil

import yaml

from app.agents import (
    generate_decision_records,
    generate_pack_changelog,
    write_decision_records,
    write_pack_changelog,
)
from app.domain import (
    CanonLogEntry,
    create_adjudication,
    decide_adjudication,
    load_domain_pack,
    write_canon_log,
)
from tests.agents.conftest import IC5_PREFIX, PACK_DIR


def test_ic5_decision_records_from_adjudication(ic5_pg_conn, ic5_user) -> None:
    document_id = f"{IC5_PREFIX}DOC-1"
    adjudication = create_adjudication(
        ic5_pg_conn,
        document_id,
        "ingredients",
        "semantic divergence in ingredients",
        suggestion="normalize to ING-X",
    )
    decide_adjudication(
        ic5_pg_conn, adjudication["id"], "approved", str(ic5_user["id"])
    )
    write_canon_log(
        ic5_pg_conn,
        [
            CanonLogEntry(
                document_id,
                "ingredients[0].item",
                "peeled sweet almonds",
                "sweet almonds",
                "ING-SWEET-ALMONDS",
            )
        ],
    )

    pack = load_domain_pack(PACK_DIR)
    records = generate_decision_records(ic5_pg_conn, pack)
    assert len(records) >= 1

    record = records[0]
    assert record.document_id == document_id
    assert record.field == "ingredients[0].item"
    assert record.before_text == "peeled sweet almonds"
    assert record.after_text == "sweet almonds"
    assert record.rule_id == "ING-SWEET-ALMONDS"
    assert record.resolved_by == str(ic5_user["id"])
    assert record.resolved_at is not None
    assert record.reason == "semantic divergence in ingredients"


def test_ic5_decision_records_empty_without_adjudications(ic5_pg_conn) -> None:
    pack = load_domain_pack(PACK_DIR)
    assert generate_decision_records(ic5_pg_conn, pack) == []


def test_ic5_write_decision_records(ic5_pg_conn, ic5_user, tmp_path) -> None:
    document_id = f"{IC5_PREFIX}DOC-2"
    adjudication = create_adjudication(
        ic5_pg_conn, document_id, "steps", "step divergence"
    )
    decide_adjudication(
        ic5_pg_conn, adjudication["id"], "approved", str(ic5_user["id"])
    )
    write_canon_log(
        ic5_pg_conn,
        [CanonLogEntry(document_id, "steps[0]", "a", "b", "STRUCT-METHOD")],
    )

    pack = load_domain_pack(PACK_DIR)
    records = generate_decision_records(ic5_pg_conn, pack)
    path = write_decision_records(records, tmp_path / "decision-records.json")
    assert path.is_file()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"] == len(records)
    assert payload["records"][0]["rule_id"] == "STRUCT-METHOD"


def test_ic5_pack_changelog_non_empty_and_coherent(tmp_path) -> None:
    before = load_domain_pack(PACK_DIR)
    work = tmp_path / "work"
    shutil.copytree(PACK_DIR, work)

    glossary_path = work / "glossari" / "ingredienti.yaml"
    raw = yaml.safe_load(glossary_path.read_text(encoding="utf-8"))
    raw["entries"][0]["aliases"].append("spaghetto")
    glossary_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )
    after = load_domain_pack(work)

    changelog = generate_pack_changelog(before, after)
    assert changelog.strip()
    assert before.pack.version in changelog
    assert after.pack.version in changelog
    assert "CHANGED" in changelog
    assert "spaghetto" in changelog


def test_ic5_write_pack_changelog(tmp_path) -> None:
    before = load_domain_pack(PACK_DIR)
    work = tmp_path / "work"
    shutil.copytree(PACK_DIR, work)
    after = load_domain_pack(work)

    changelog = generate_pack_changelog(before, after)
    path = write_pack_changelog(changelog, tmp_path / "pack-changelog.md")
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == changelog
