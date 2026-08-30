"""Curator loop (WP-C5): mine -> propose -> human gate -> incremental apply.

The Curator is a periodic improvement job. It mines improvement signals from
the graph and the Postgres queues, turns them into **proposals** (never applied
directly), and applies only proposals whose status is ``approved`` in the
backing Postgres row. Applying an approved proposal re-canonicalizes only the
documents touched by that proposal and re-extracts them with bitemporal
versioning (no DELETE, changed Facts get a new ``VERSION_OF`` version).

Human gate (P5) is enforced mechanically in three places:

1. ``propose_extension`` never writes anything (pure data).
2. ``apply_approved`` refuses ``pending``/``rejected`` proposals and refuses a
   backing Postgres row that is not ``approved``.
3. ``apply_approved`` refuses to write the production pack
   (``domain-packs/ricette``); approved extensions go to a working copy only.
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from app.agents.models import (
    ApplyResult,
    CuratorIssue,
    DomainBrief,
    Proposal,
)
from app.conflict import list_conflicts
from app.domain import (
    canonicalize,
    generate_canon_log,
    list_adjudications,
    list_glossary_proposals,
    load_domain_pack,
    parse_translated_md,
    write_canon_log,
)
from app.domain.pack import DomainPackBundle
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

MANUAL_PACK_DIR = Path("domain-packs/ricette")

_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TIME_RE = re.compile(
    r"-?\d+(?:\.\d+)?\s*(?:minuti|min\b|ore|h\b|secondi|sec\b|s\b)",
    flags=re.IGNORECASE,
)
_TEMPERATURE_RE = re.compile(
    r"-?\d+(?:\.\d+)?\s*(?:°C|°c|gradi|degrees)\b",
    flags=re.IGNORECASE,
)


class CuratorGateError(RuntimeError):
    """Raised when a proposal tries to bypass the human gate (P5)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(text: str) -> str:
    text = text.strip().lower().replace("\u2019", "'")
    return _SLUG_RE.sub("-", text).strip("-")


def _word_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term.casefold())}\b", flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------

def _ambiguous_fact_issues(client: Neo4jClient) -> list[CuratorIssue]:
    with client.session() as session:
        rows = session.run(
            """
            MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL AND f.confidence = 'AMBIGUOUS'
            OPTIONAL MATCH (e)-[:PART_OF_DOC]->(d:Document)
            RETURN f.logical_id AS fact_id, f.value AS value,
                   f.source_id AS source_id, e.id AS entity_id,
                   d.id AS document_id
            ORDER BY f.logical_id
            """
        ).data()
    issues: list[CuratorIssue] = []
    for row in rows:
        issues.append(
            CuratorIssue(
                issue_id=f"amb-{_slugify(str(row['fact_id']))}",
                kind="ambiguous_fact",
                term=str(row["value"]),
                document_id=row.get("document_id"),
                source_id=row.get("source_id"),
                note=f"AMBIGUOUS fact {row['fact_id']} on entity {row['entity_id']}",
            )
        )
    return issues


def _untranslated_issues(client: Neo4jClient) -> list[CuratorIssue]:
    with client.session() as session:
        rows = session.run(
            """
            MATCH (d:Document)
            WHERE NOT coalesce(d.translation_state, 'native') IN ['native', 'translated']
            RETURN d.id AS document_id, d.title AS title,
                   d.translation_state AS state
            ORDER BY d.id
            """
        ).data()
    issues: list[CuratorIssue] = []
    for row in rows:
        issues.append(
            CuratorIssue(
                issue_id=f"untr-{_slugify(str(row['document_id']))}",
                kind="untranslated",
                term=str(row.get("title") or row["document_id"]),
                document_id=row.get("document_id"),
                note=f"untranslated flag: translation_state={row.get('state')!r}",
            )
        )
    return issues


def _conflict_issues(conn: psycopg.Connection) -> list[CuratorIssue]:
    issues: list[CuratorIssue] = []
    for conflict in list_conflicts(conn, status="pending"):
        term = conflict.get("value_a") or conflict.get("value_b") or ""
        issues.append(
            CuratorIssue(
                issue_id=f"conf-{conflict['id']}",
                kind="pending_conflict",
                term=str(term),
                document_id=None,
                source_id=conflict.get("source_a") or conflict.get("source_b"),
                candidates=[
                    str(conflict.get("value_a") or ""),
                    str(conflict.get("value_b") or ""),
                ],
                note=f"pending conflict on {conflict.get('entity_id')}.{conflict.get('property')}",
            )
        )
    return issues


def _glossary_proposal_issues(conn: psycopg.Connection) -> list[CuratorIssue]:
    issues: list[CuratorIssue] = []
    for proposal in list_glossary_proposals(conn, status="pending"):
        issues.append(
            CuratorIssue(
                issue_id=f"gloss-{proposal['id']}",
                kind="glossary_proposal",
                term=str(proposal["term"]),
                context=proposal.get("context"),
                note="pending glossary proposal",
            )
        )
    return issues


def _brief_ambiguity_issues(brief: DomainBrief) -> list[CuratorIssue]:
    issues: list[CuratorIssue] = []
    for index, ambiguity in enumerate(brief.ambiguities, start=1):
        issues.append(
            CuratorIssue(
                issue_id=f"brief-{index}-{_slugify(ambiguity.term)}",
                kind="brief_ambiguity",
                term=ambiguity.term,
                candidates=ambiguity.candidates,
                note=ambiguity.note or "modifier ambiguity from the domain brief",
            )
        )
    return issues


def detect_modifier_terms(
    pack: DomainPackBundle, terms: list[str]
) -> list[tuple[str, str, str]]:
    """Return ``(term, base_alias, entry_id)`` for terms with a modifier.

    A term is a modifier ambiguity when it contains a whole glossary alias plus
    extra words (e.g. ``mandorle dolci sbucciate`` contains ``mandorle dolci``).
    Exact aliases are not flagged. Longest alias wins deterministically.
    """
    pairs: list[tuple[str, str, str]] = []
    for entry in pack.glossary_entries():
        for alias in (entry.labels_en, entry.labels_it, *entry.aliases):
            alias = alias.strip().casefold()
            if alias:
                pairs.append((alias, entry.id, entry.labels_en))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)

    results: list[tuple[str, str, str]] = []
    for term in terms:
        key = term.strip().casefold()
        for alias, entry_id, _label_en in pairs:
            if alias == key:
                break
            if _word_pattern(alias).search(key):
                results.append((term, alias, entry_id))
                break
    return results


def mine_issues(
    client: Neo4jClient,
    conn: psycopg.Connection,
    pack: DomainPackBundle,
    *,
    brief: DomainBrief | None = None,
) -> list[CuratorIssue]:
    """Mine every Curator improvement signal.

    Sources: AMBIGUOUS Facts (Neo4j), pending conflicts (Postgres), untranslated
    Document flags (Neo4j), the pending glossary-proposal queue (Postgres) and,
    when ``brief`` is supplied, the modifier ambiguities reported by the Domain
    Analyst (WP-C1).
    """
    issues: list[CuratorIssue] = []
    issues.extend(_ambiguous_fact_issues(client))
    issues.extend(_untranslated_issues(client))
    issues.extend(_conflict_issues(conn))
    issues.extend(_glossary_proposal_issues(conn))
    if brief is not None:
        issues.extend(_brief_ambiguity_issues(brief))
    return issues


# ---------------------------------------------------------------------------
# Proposal generation (pure, never writes)
# ---------------------------------------------------------------------------

def _glossary_namespace(pack: DomainPackBundle, entry_id: str) -> str | None:
    for namespace in ("tecnica", "ingredienti", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            if entry.id == entry_id:
                return namespace
    return None


def _find_base_entry(pack: DomainPackBundle, term: str) -> Any | None:
    """Return the glossary entry whose alias is a whole-word sub-term of ``term``."""
    pairs: list[tuple[str, Any]] = []
    for entry in pack.glossary_entries():
        for alias in (entry.labels_en, entry.labels_it, *entry.aliases):
            alias = alias.strip().casefold()
            if alias:
                pairs.append((alias, entry))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    key = term.strip().casefold()
    for alias, entry in pairs:
        if alias == key:
            return entry
        if _word_pattern(alias).search(key):
            return entry
    return None


def _new_entry_id(pack: DomainPackBundle, term: str, namespace: str) -> str:
    prefix = {"tecnica": "TEC", "ingredienti": "ING", "stati": "STA"}[namespace]
    base = _slugify(term).upper() or "TERM"
    entry_id = f"{prefix}-{base}"
    existing = {entry.id for entry in pack.glossary_entries()}
    suffix = 2
    while entry_id in existing:
        entry_id = f"{prefix}-{base}-{suffix}"
        suffix += 1
    return entry_id


def _p7_uri(term: str) -> str:
    """Deterministic external-ontology URI (P7: standards before proprietary)."""
    slug = _slugify(term).replace("-", "_")
    return f"http://dbpedia.org/resource/{slug}"


def _default_definition(term: str) -> str:
    return f"Curator proposal for {term.strip()}."


def propose_extension(issue: CuratorIssue, pack: DomainPackBundle) -> Proposal:
    """Generate a glossary extension proposal for one mined issue.

    The proposal is pure data: it is never written to the pack or the graph by
    this function. ``apply_approved`` is the only writer and it requires an
    explicit ``approved`` status.
    """
    term = issue.term.strip()
    base = _find_base_entry(pack, term)
    if base is not None:
        namespace = _glossary_namespace(pack, base.id) or "ingredienti"
        aliases = [term] if term.casefold() not in {
            alias.casefold() for alias in base.aliases
        } else []
        return Proposal(
            proposal_id=f"prop-{_slugify(issue.issue_id)}",
            issue_id=issue.issue_id,
            kind="add_alias",
            term=term,
            target_glossary=namespace,
            entry_id=base.id,
            labels_en=base.labels_en,
            labels_it=base.labels_it,
            aliases=aliases,
            definition=base.definition or _default_definition(term),
            ontology_uri=base.ontology_uri or _p7_uri(term),
            status="pending",
            affected_documents=[issue.document_id] if issue.document_id else [],
            note=issue.note,
        )

    namespace = "ingredienti"
    entry_id = _new_entry_id(pack, term, namespace)
    return Proposal(
        proposal_id=f"prop-{_slugify(issue.issue_id)}",
        issue_id=issue.issue_id,
        kind="add_entry",
        term=term,
        target_glossary=namespace,
        entry_id=entry_id,
        labels_en=term,
        labels_it=term,
        aliases=[term],
        definition=_default_definition(term),
        ontology_uri=_p7_uri(term),
        status="pending",
        affected_documents=[issue.document_id] if issue.document_id else [],
        note=issue.note,
    )


# ---------------------------------------------------------------------------
# Human gate + apply
# ---------------------------------------------------------------------------

def _db_row_status(
    conn: psycopg.Connection, source_type: str, source_id: int
) -> str | None:
    if source_type == "glossary_proposal":
        for row in list_glossary_proposals(conn):
            if row["id"] == source_id:
                return str(row["status"])
        return None
    if source_type == "adjudication":
        for row in list_adjudications(conn):
            if row["id"] == source_id:
                return str(row["status"])
        return None
    raise CuratorGateError(f"unknown proposal source_type {source_type!r}")


def _check_gate(
    conn: psycopg.Connection, pack: DomainPackBundle, proposal: Proposal
) -> None:
    if proposal.status != "approved":
        raise CuratorGateError(
            f"proposal {proposal.proposal_id!r} is {proposal.status!r}; "
            "only approved proposals can be applied (P5 human gate)"
        )
    if proposal.source_type is not None:
        if proposal.source_proposal_id is None:
            raise CuratorGateError(
                f"proposal {proposal.proposal_id!r} declares source_type "
                f"{proposal.source_type!r} but no source_proposal_id"
            )
        status = _db_row_status(
            conn, proposal.source_type, proposal.source_proposal_id
        )
        if status != "approved":
            raise CuratorGateError(
                f"proposal {proposal.proposal_id!r} backing row "
                f"{proposal.source_type}:{proposal.source_proposal_id} is "
                f"{status!r}, not approved (P5 human gate)"
            )
    if pack.root.resolve() == MANUAL_PACK_DIR.resolve():
        raise CuratorGateError(
            "refusing to write the production pack; approved extensions go to "
            "a working copy only (P5 human gate)"
        )


def _write_extension(pack_root: Path, proposal: Proposal) -> Path:
    """Write an approved glossary extension into the working pack copy."""
    glossary_path = pack_root / "glossari" / f"{proposal.target_glossary}.yaml"
    raw = yaml.safe_load(glossary_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        entries = raw["entries"]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise CuratorGateError(
            f"{glossary_path}: expected a mapping with 'entries' or a list"
        )

    if proposal.kind == "add_alias":
        entry = next(
            (item for item in entries if item.get("id") == proposal.entry_id), None
        )
        if entry is None:
            raise CuratorGateError(
                f"glossary entry {proposal.entry_id!r} not found in {glossary_path}"
            )
        aliases = list(entry.get("aliases") or [])
        for alias in proposal.aliases:
            if alias not in aliases:
                aliases.append(alias)
        entry["aliases"] = aliases
        if proposal.definition:
            entry["definition"] = proposal.definition
        if proposal.ontology_uri:
            entry["ontology_uri"] = proposal.ontology_uri
    elif proposal.kind == "add_entry":
        entries.append(
            {
                "id": proposal.entry_id,
                "labels_en": proposal.labels_en,
                "labels_it": proposal.labels_it,
                "aliases": proposal.aliases,
                "definition": proposal.definition,
                "ontology_uri": proposal.ontology_uri,
            }
        )
    else:  # pragma: no cover - guarded by the pydantic schema
        raise CuratorGateError(f"unknown proposal kind {proposal.kind!r}")

    if isinstance(raw, dict):
        raw["entries"] = entries
    else:
        raw = entries

    glossary_path.write_text(
        yaml.safe_dump(
            raw,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        ),
        encoding="utf-8",
    )
    return glossary_path


# ---------------------------------------------------------------------------
# Incremental re-canonicalization + versioned re-extraction
# ---------------------------------------------------------------------------

def _document_property(client: Neo4jClient, doc_id: str, key: str) -> Any | None:
    with client.session() as session:
        record = session.run(
            "MATCH (d:Document {id: $doc_id}) RETURN d[$key] AS value",
            doc_id=doc_id,
            key=key,
        ).single()
        return record["value"] if record else None


def _bump_canonical_version(md: str, version: int) -> str:
    lines = md.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("canonical_version:"):
            lines[index] = f"canonical_version: {version}"
            return "\n".join(lines) + "\n"
    end = next(
        (index for index, line in enumerate(lines) if line.strip() == "---" and index > 0),
        None,
    )
    if end is None:
        raise CuratorGateError("cannot bump canonical_version: malformed frontmatter")
    lines.insert(end, f"canonical_version: {version}")
    return "\n".join(lines) + "\n"


def _glossary_index(pack: DomainPackBundle) -> dict[str, tuple[str, Any]]:
    by_label_en: dict[str, tuple[str, Any]] = {}
    for namespace in ("tecnica", "ingredienti", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            key = entry.labels_en.strip().casefold()
            if key:
                by_label_en.setdefault(key, (namespace, entry))
    return by_label_en


def _term_id(namespace: str, entry_id: str) -> str:
    return f"{namespace}:{entry_id}"


def _find_step_terms(step: str, pack: DomainPackBundle) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    text = step.casefold()
    for namespace in ("tecnica", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            label = entry.labels_en.strip()
            if label and _word_pattern(label).search(text):
                found.append((namespace, entry))
    return found


def _extract_step_facts(step: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for match in _TIME_RE.finditer(step):
        facts.append(("time", match.group(0)))
    for match in _TEMPERATURE_RE.finditer(step):
        facts.append(("temperature", match.group(0)))
    return facts


def _build_rows(
    doc_id: str, canonical_md: str, pack: DomainPackBundle
) -> dict[str, Any]:
    """Build the same deterministic rows as ``app.domain.extract`` (read-only)."""
    parsed = parse_translated_md(canonical_md, known_units=pack.known_units())
    frontmatter = parsed.frontmatter
    by_label_en = _glossary_index(pack)

    ingredient_rows: list[dict[str, Any]] = []
    for index, ingredient in enumerate(parsed.ingredients):
        resolved = by_label_en.get(ingredient.item.strip().casefold())
        ingredient_rows.append(
            {
                "entity_id": f"{doc_id}:ing:{index}",
                "label": ingredient.item,
                "position": index,
                "qty": ingredient.qty,
                "unit": ingredient.unit,
                "term_id": _term_id(resolved[0], resolved[1].id)
                if resolved is not None
                else None,
            }
        )

    step_rows: list[dict[str, Any]] = []
    term_rows: list[dict[str, Any]] = []
    step_fact_rows: list[dict[str, Any]] = []
    for index, step in enumerate(parsed.steps):
        step_entity_id = f"{doc_id}:step:{index}"
        step_rows.append(
            {"entity_id": step_entity_id, "label": step, "position": index}
        )
        for namespace, entry in _find_step_terms(step, pack):
            term_rows.append(
                {
                    "entity_id": f"{doc_id}:{namespace}:{entry.id}:{index}",
                    "label": entry.labels_en,
                    "type": "technique" if namespace == "tecnica" else "state",
                    "position": index,
                    "term_id": _term_id(namespace, entry.id),
                }
            )
        for occurrence, (property_name, value) in enumerate(
            _extract_step_facts(step)
        ):
            step_fact_rows.append(
                {
                    "fact_id": f"{step_entity_id}:{property_name}:{occurrence}",
                    "entity_id": step_entity_id,
                    "property": property_name,
                    "value": value,
                }
            )

    return {
        "frontmatter": frontmatter,
        "ingredient_rows": ingredient_rows,
        "step_rows": step_rows,
        "term_rows": term_rows,
        "step_fact_rows": step_fact_rows,
    }


def _upsert_entity(
    client: Neo4jClient,
    entity_id: str,
    label: str,
    type_: str,
    source_uri: str,
    position: int,
) -> None:
    with client.session() as session:
        session.run(
            """
            MERGE (e:Entity {id: $entity_id})
            SET e.label = $label,
                e.type = $type,
                e.position = $position,
                e.source_file = $source_uri,
                e.confidence = 'EXTRACTED',
                e.is_public = false,
                e.roles = [],
                e.teams = []
            """,
            entity_id=entity_id,
            label=label,
            type=type_,
            position=position,
            source_uri=source_uri,
        )


def _link_part_of_doc(client: Neo4jClient, entity_id: str, doc_id: str) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (e:Entity {id: $entity_id})
            MATCH (d:Document {id: $doc_id})
            MERGE (e)-[:PART_OF_DOC]->(d)
            """,
            entity_id=entity_id,
            doc_id=doc_id,
        )


def _link_normalized_to(client: Neo4jClient, entity_id: str, term_id: str) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (e:Entity {id: $entity_id})
            MATCH (t:CanonicalTerm {id: $term_id})
            MERGE (e)-[:NORMALIZED_TO]->(t)
            """,
            entity_id=entity_id,
            term_id=term_id,
        )


def _link_derived_from(client: Neo4jClient, fact_id: str, source_id: str) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (f:Fact {logical_id: $fact_id})
            WHERE f.valid_to IS NULL
            MATCH (s:Source {id: $source_id})
            MERGE (f)-[:DERIVED_FROM]->(s)
            """,
            fact_id=fact_id,
            source_id=source_id,
        )


def _upsert_fact(
    repo: GraphRepository,
    client: Neo4jClient,
    fact_id: str,
    entity_id: str,
    property: str,
    value: str,
    source_id: str,
    user_id: str,
) -> int:
    """Create or version one Fact; return the number of new versions created."""
    current = repo.get_fact(fact_id)
    if current is None:
        repo.create_fact(
            fact_id=fact_id,
            entity_id=entity_id,
            property=property,
            value=value,
            source_id=source_id,
            confidence="EXTRACTED",
        )
        _link_derived_from(client, fact_id, source_id)
        return 0
    if current.get("value") != value:
        repo.update_fact(fact_id, value=value, author_id=user_id)
        _link_derived_from(client, fact_id, source_id)
        return 1
    return 0


def _invalidate_removed_facts(
    repo: GraphRepository,
    client: Neo4jClient,
    doc_id: str,
    expected_fact_ids: set[str],
    user_id: str,
) -> int:
    with client.session() as session:
        rows = session.run(
            """
            MATCH (d:Document {id: $doc_id})<-[:PART_OF_DOC]-(e:Entity)-[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL
            RETURN f.logical_id AS logical_id
            """,
            doc_id=doc_id,
        ).data()
    invalidated = 0
    for row in rows:
        logical_id = str(row["logical_id"])
        if logical_id not in expected_fact_ids:
            repo.invalidate_fact(logical_id, author_id=user_id)
            invalidated += 1
    return invalidated


def _re_extract_document(
    client: Neo4jClient,
    conn: psycopg.Connection,
    doc_id: str,
    translated_md: str,
    canonical_md: str,
    pack: DomainPackBundle,
    user_id: str,
) -> dict[str, int]:
    """Re-extract one changed document with bitemporal versioning (no DELETE)."""
    repo = GraphRepository(client)
    rows = _build_rows(doc_id, canonical_md, pack)
    frontmatter = rows["frontmatter"]
    digest = _sha256(canonical_md)
    now = _now()
    canonical_version = int(frontmatter.get("canonical_version", 1))
    source_id = f"{doc_id}:source:v{canonical_version}"
    source_uri = f"canonical://{doc_id}.md"
    document_id = str(frontmatter.get("id", doc_id))
    title = str(frontmatter.get("title", ""))
    source_lang = str(frontmatter.get("source_lang", pack.language))
    servings = int(frontmatter["servings"])
    time_min = int(frontmatter["time_min"])
    difficulty = str(frontmatter["difficulty"])
    verification_level = str(frontmatter.get("verification_level", "L1"))

    with client.session() as session:
        session.run(
            """
            MATCH (d:Document {id: $doc_id})
            SET d.document_id = $document_id,
                d.title = $title,
                d.source_lang = $source_lang,
                d.source_language = $source_lang,
                d.canonical_hash = $hash,
                d.verification_level = $verification_level,
                d.canonical_version = $canonical_version,
                d.servings = $servings,
                d.time_min = $time_min,
                d.difficulty = $difficulty
            """,
            doc_id=doc_id,
            document_id=document_id,
            title=title,
            source_lang=source_lang,
            hash=digest,
            verification_level=verification_level,
            canonical_version=canonical_version,
            servings=servings,
            time_min=time_min,
            difficulty=difficulty,
        )
        session.run(
            """
            MERGE (s:Source {id: $source_id})
            SET s.uri = $uri,
                s.type = 'file',
                s.hash = $hash,
                s.language = 'en',
                s.ingested_at = $now
            """,
            source_id=source_id,
            uri=source_uri,
            hash=digest,
            now=now,
        )

    fact_versions = 0
    expected_fact_ids: set[str] = set()

    for row in rows["ingredient_rows"]:
        _upsert_entity(
            client, row["entity_id"], row["label"], "ingredient", source_uri, row["position"]
        )
        _link_part_of_doc(client, row["entity_id"], doc_id)
        if row["term_id"] is not None:
            _link_normalized_to(client, row["entity_id"], row["term_id"])
        qty_fact_id = f"{row['entity_id']}:qty"
        expected_fact_ids.add(qty_fact_id)
        fact_versions += _upsert_fact(
            repo, client, qty_fact_id, row["entity_id"], "qty", row["qty"],
            source_id, user_id,
        )
        if row["unit"] is not None:
            unit_fact_id = f"{row['entity_id']}:unit"
            expected_fact_ids.add(unit_fact_id)
            fact_versions += _upsert_fact(
                repo, client, unit_fact_id, row["entity_id"], "unit", row["unit"],
                source_id, user_id,
            )

    for row in rows["step_rows"]:
        _upsert_entity(
            client, row["entity_id"], row["label"], "step", source_uri, row["position"]
        )
        _link_part_of_doc(client, row["entity_id"], doc_id)

    for row in rows["term_rows"]:
        _upsert_entity(
            client, row["entity_id"], row["label"], row["type"], source_uri, row["position"]
        )
        _link_part_of_doc(client, row["entity_id"], doc_id)
        _link_normalized_to(client, row["entity_id"], row["term_id"])

    for row in rows["step_fact_rows"]:
        expected_fact_ids.add(row["fact_id"])
        fact_versions += _upsert_fact(
            repo, client, row["fact_id"], row["entity_id"], row["property"],
            row["value"], source_id, user_id,
        )

    facts_invalidated = _invalidate_removed_facts(
        repo, client, doc_id, expected_fact_ids, user_id
    )

    # Persist the complete translated->canonical diff for traceability (C6).
    entries = generate_canon_log(pack, translated_md, canonical_md)
    if entries:
        write_canon_log(conn, entries)

    return {
        "fact_versions": fact_versions,
        "facts_invalidated": facts_invalidated,
    }


def apply_approved(
    conn: psycopg.Connection,
    client: Neo4jClient,
    pack: DomainPackBundle,
    proposal: Proposal,
    *,
    user_id: str = "curator",
) -> ApplyResult:
    """Apply an approved proposal and re-canonicalize only touched documents.

    The human gate is checked first: ``pending``/``rejected`` proposals, a
    backing Postgres row that is not ``approved``, or a production-pack target
    all raise :class:`CuratorGateError`. Approved extensions are written to the
    working pack copy, the pack is reloaded and bootstrapped into Neo4j, and
    only the documents in ``proposal.translated_documents`` are re-canonicalized
    and re-extracted (hash cache: unchanged documents are skipped).
    """
    _check_gate(conn, pack, proposal)
    _write_extension(pack.root, proposal)

    updated_pack = load_domain_pack(pack.root)
    from scripts.load_domain_pack import load_pack

    load_pack(client, pack.root)

    changed: list[str] = []
    unchanged: list[str] = []
    fact_versions = 0
    facts_invalidated = 0

    for doc_id in sorted(proposal.translated_documents):
        translated_md = proposal.translated_documents[doc_id]
        canonical = canonicalize(updated_pack, translated_md)
        current_version = _document_property(client, doc_id, "canonical_version") or 1
        new_md = _bump_canonical_version(canonical.canonical_md, int(current_version) + 1)
        new_hash = _sha256(new_md)
        current_hash = _document_property(client, doc_id, "canonical_hash")
        if current_hash == new_hash:
            unchanged.append(doc_id)
            continue
        counts = _re_extract_document(
            client, conn, doc_id, translated_md, new_md, updated_pack, user_id
        )
        changed.append(doc_id)
        fact_versions += counts["fact_versions"]
        facts_invalidated += counts["facts_invalidated"]

    return ApplyResult(
        proposal_id=proposal.proposal_id,
        applied=True,
        changed_documents=changed,
        unchanged_documents=unchanged,
        fact_versions_created=fact_versions,
        facts_invalidated=facts_invalidated,
        note=(
            f"applied {proposal.kind} {proposal.entry_id} to "
            f"{proposal.target_glossary}; changed={len(changed)} "
            f"unchanged={len(unchanged)}"
        ),
    )
