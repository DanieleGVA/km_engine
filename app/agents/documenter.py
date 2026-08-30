"""Documenter (WP-C6): decision records + pack changelog.

- :func:`generate_decision_records` joins the Postgres ``canon_log`` with the
  approved ``adjudications`` and emits one :class:`DecisionRecord` per
  adjudicated mapping (who, when, why, rule_id).
- :func:`generate_pack_changelog` diffs two Domain Packs (draft vs manual, or
  vN vs vN-1) and returns a deterministic markdown changelog.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.agents.models import DecisionRecord
from app.domain import list_adjudications, load_domain_pack
from app.domain.pack import DomainPackBundle

DEFAULT_DECISION_RECORDS_PATH = Path("docs/domain-briefs/decision-records.json")
DEFAULT_CHANGELOG_PATH = Path("docs/domain-briefs/pack-changelog.md")

_GLOSSARY_NAMES = ("tecnica", "ingredienti", "stati")


def _load_pack(pack: DomainPackBundle | str | Path) -> DomainPackBundle:
    if isinstance(pack, DomainPackBundle):
        return pack
    return load_domain_pack(Path(pack))


def _section_matches(section: str, field: str) -> bool:
    section = section.strip().lower()
    field = field.strip().lower()
    if section == "title":
        return field == "frontmatter.title" or field.startswith("frontmatter.title")
    if section in {"ingredients", "steps"}:
        return field.startswith(section)
    return False


def _canon_log_rows(conn: psycopg.Connection, document_id: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, document_id, field, before_text, after_text, rule_id, created_at
            FROM canon_log
            WHERE document_id = %s
            ORDER BY id
            """,
            (document_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def generate_decision_records(
    conn: psycopg.Connection, pack: DomainPackBundle
) -> list[DecisionRecord]:
    """Build one decision record per adjudicated canon-log mapping.

    ``pack`` is accepted for API symmetry with the other agents and is currently
    unused: the decision records are derived from the Postgres audit queues.
    """
    del pack  # reserved for future pack-aware rule resolution
    records: list[DecisionRecord] = []
    for adjudication in list_adjudications(conn, status="approved"):
        adjudication_id = int(adjudication["id"])
        document_id = str(adjudication["document_id"])
        section = str(adjudication["section"])
        reason = str(adjudication.get("reason") or "")
        resolved_by = adjudication.get("resolved_by")
        resolved_at = adjudication.get("resolved_at")

        matches = [
            row
            for row in _canon_log_rows(conn, document_id)
            if _section_matches(section, str(row["field"]))
        ]
        if not matches:
            records.append(
                DecisionRecord(
                    record_id=f"dr-{adjudication_id}-0",
                    document_id=document_id,
                    field=section,
                    before_text="",
                    after_text="",
                    rule_id="ADJUDICATION",
                    resolved_by=resolved_by,
                    resolved_at=resolved_at,
                    reason=reason,
                    created_at=str(adjudication.get("created_at") or ""),
                )
            )
            continue

        for row in matches:
            records.append(
                DecisionRecord(
                    record_id=f"dr-{adjudication_id}-{row['id']}",
                    document_id=document_id,
                    field=str(row["field"]),
                    before_text=str(row["before_text"]),
                    after_text=str(row["after_text"]),
                    rule_id=str(row["rule_id"]),
                    resolved_by=resolved_by,
                    resolved_at=resolved_at,
                    reason=reason,
                    created_at=(
                        row["created_at"].isoformat()
                        if row["created_at"] is not None
                        else ""
                    ),
                )
            )
    return records


def write_decision_records(
    records: list[DecisionRecord], path: str | Path = DEFAULT_DECISION_RECORDS_PATH
) -> Path:
    """Serialize decision records to JSON (versioned artifact in the repo)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "generated_by": "app/agents/documenter.py",
        "count": len(records),
        "records": [record.model_dump(mode="json") for record in records],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _glossary_map(pack: DomainPackBundle) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for namespace in _GLOSSARY_NAMES:
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            result[entry.id] = {
                "namespace": namespace,
                "labels_en": entry.labels_en,
                "labels_it": entry.labels_it,
                "aliases": list(entry.aliases),
                "definition": entry.definition,
                "ontology_uri": entry.ontology_uri,
            }
    return result


def _unit_map(pack: DomainPackBundle) -> dict[str, dict[str, Any]]:
    return {
        rule.rule_id: {
            "from_unit": rule.from_unit,
            "to_unit": rule.to_unit,
            "factor": rule.factor,
            "rounding": rule.rounding,
        }
        for rule in pack.units
    }


def _diff_entries(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    lines: list[str] = []
    for entry_id in sorted(set(after) - set(before)):
        entry = after[entry_id]
        lines.append(
            f"- ADDED {entry_id}: {entry['labels_en']} "
            f"(glossario {entry['namespace']})"
        )
    for entry_id in sorted(set(before) - set(after)):
        entry = before[entry_id]
        lines.append(
            f"- REMOVED {entry_id}: {entry['labels_en']} "
            f"(glossario {entry['namespace']})"
        )
    for entry_id in sorted(set(before) & set(after)):
        old = before[entry_id]
        new = after[entry_id]
        changes: list[str] = []
        for key in ("labels_en", "labels_it", "definition", "ontology_uri"):
            if old.get(key) != new.get(key):
                changes.append(f"{key}: {old.get(key)!r} -> {new.get(key)!r}")
        old_aliases = set(old.get("aliases") or [])
        new_aliases = set(new.get("aliases") or [])
        if old_aliases != new_aliases:
            added = sorted(new_aliases - old_aliases)
            removed = sorted(old_aliases - new_aliases)
            if added:
                changes.append(f"aliases +{added}")
            if removed:
                changes.append(f"aliases -{removed}")
        if changes:
            lines.append(f"- CHANGED {entry_id}: " + "; ".join(changes))
    return lines


def _diff_units(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    lines: list[str] = []
    for rule_id in sorted(set(after) - set(before)):
        rule = after[rule_id]
        lines.append(
            f"- ADDED {rule_id}: {rule['from_unit']} -> {rule['to_unit']} "
            f"(factor {rule['factor']})"
        )
    for rule_id in sorted(set(before) - set(after)):
        lines.append(f"- REMOVED {rule_id}")
    for rule_id in sorted(set(before) & set(after)):
        old = before[rule_id]
        new = after[rule_id]
        changes = [
            f"{key}: {old.get(key)!r} -> {new.get(key)!r}"
            for key in ("from_unit", "to_unit", "factor", "rounding")
            if old.get(key) != new.get(key)
        ]
        if changes:
            lines.append(f"- CHANGED {rule_id}: " + "; ".join(changes))
    return lines


def generate_pack_changelog(
    pack_a: DomainPackBundle | str | Path,
    pack_b: DomainPackBundle | str | Path,
) -> str:
    """Return a deterministic markdown changelog between two pack versions."""
    before = _load_pack(pack_a)
    after = _load_pack(pack_b)

    lines = [
        "# Pack changelog",
        "",
        f"- {before.pack.name}: {before.pack.version} -> {after.pack.version}",
        f"- language: {before.pack.language} -> {after.pack.language}",
        (
            f"- canonical_language: {before.pack.canonical_language} -> "
            f"{after.pack.canonical_language}"
        ),
        "",
    ]

    glossary_lines = _diff_entries(_glossary_map(before), _glossary_map(after))
    if glossary_lines:
        lines.append("## Glossario")
        lines.extend(glossary_lines)
        lines.append("")

    unit_lines = _diff_units(_unit_map(before), _unit_map(after))
    if unit_lines:
        lines.append("## Unità")
        lines.extend(unit_lines)
        lines.append("")

    if not glossary_lines and not unit_lines:
        lines.append("Nessuna differenza rilevata tra i due pack.")
        lines.append("")

    return "\n".join(lines)


def write_pack_changelog(
    changelog: str, path: str | Path = DEFAULT_CHANGELOG_PATH
) -> Path:
    """Write the pack changelog markdown artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(changelog, encoding="utf-8")
    return path
