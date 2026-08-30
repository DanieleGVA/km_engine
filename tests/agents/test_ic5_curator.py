"""WP-C5 Curator loop tests (prefix ic5_).

Unit: mining, modifier detection, proposal schema, human gate negatives.
Integration: full mine -> propose -> approve -> apply cycle with injected
modifier ambiguities; only touched documents are re-canonicalized and the
bitemporal history is preserved.
E2E: ambiguity reduction >= 80% after N=3 cycles with simulated adjudication.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agents import (
    CuratorGateError,
    apply_approved,
    detect_modifier_terms,
    mine_issues,
    propose_extension,
)
from app.agents.models import (
    Ambiguity,
    CuratorIssue,
    DomainBrief,
    Proposal,
)
from app.conflict import scan_conflicts
from app.domain import (
    canonicalize,
    create_glossary_proposal,
    decide_glossary_proposal,
    extract_document,
    list_glossary_proposals,
    load_domain_pack,
    translate_document,
)
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from scripts.load_domain_pack import load_pack
from tests.agents.conftest import (
    IC5_PREFIX,
    PACK_DIR,
    injected_modifier_corpus,
)
from tests.domain.fake_llm import build_fake_llm


def _working_pack(tmp_path: Path):
    """Copy the manual pack into a working dir (never the production pack)."""
    dst = tmp_path / "work"
    shutil.copytree(PACK_DIR, dst)
    return load_domain_pack(dst)


def _doc_id(name: str) -> str:
    return f"{IC5_PREFIX}{name.replace('.md', '').replace('-', '_')}"


def _brief_with_ambiguities() -> DomainBrief:
    return DomainBrief(
        domain="ricette",
        language="it",
        canonical_language="en",
        version="1.0.0",
        corpus_size=1,
        ambiguities=[
            Ambiguity(
                term="mandorle dolci sbucciate",
                candidates=["mandorle dolci"],
                note="modifier 'sbucciate' is not an alias",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Unit: mining
# ---------------------------------------------------------------------------

def test_ic5_mine_issues_finds_ambiguous_fact(ic5_client, ic5_pg_conn) -> None:
    repo = GraphRepository(ic5_client)
    repo.create_entity(entity_id=f"{IC5_PREFIX}amb_entity", label="x")
    repo.create_fact(
        fact_id=f"{IC5_PREFIX}amb_fact",
        entity_id=f"{IC5_PREFIX}amb_entity",
        property="state",
        value="ambiguous value",
        confidence="AMBIGUOUS",
    )
    pack = load_domain_pack(PACK_DIR)
    issues = mine_issues(ic5_client, ic5_pg_conn, pack)
    assert any(
        issue.kind == "ambiguous_fact" and issue.term == "ambiguous value"
        for issue in issues
    )


def test_ic5_mine_issues_finds_untranslated(ic5_client, ic5_pg_conn) -> None:
    with ic5_client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.title = 'Untranslated doc', d.translation_state = 'pending'
            """,
            id=f"{IC5_PREFIX}untr_doc",
        )
    pack = load_domain_pack(PACK_DIR)
    issues = mine_issues(ic5_client, ic5_pg_conn, pack)
    assert any(
        issue.kind == "untranslated" and issue.document_id == f"{IC5_PREFIX}untr_doc"
        for issue in issues
    )


def test_ic5_mine_issues_finds_pending_conflict(ic5_client, ic5_pg_conn) -> None:
    repo = GraphRepository(ic5_client)
    repo.create_entity(entity_id=f"{IC5_PREFIX}conf_entity", label="x")
    repo.create_fact(
        fact_id=f"{IC5_PREFIX}conf_a",
        entity_id=f"{IC5_PREFIX}conf_entity",
        property="state",
        value="a",
        source_id=f"{IC5_PREFIX}src_a",
    )
    repo.create_fact(
        fact_id=f"{IC5_PREFIX}conf_b",
        entity_id=f"{IC5_PREFIX}conf_entity",
        property="state",
        value="b",
        source_id=f"{IC5_PREFIX}src_b",
    )
    scan_conflicts(repo, ic5_pg_conn)
    pack = load_domain_pack(PACK_DIR)
    issues = mine_issues(ic5_client, ic5_pg_conn, pack)
    assert any(issue.kind == "pending_conflict" for issue in issues)


def test_ic5_mine_issues_finds_glossary_proposal(ic5_client, ic5_pg_conn) -> None:
    create_glossary_proposal(ic5_pg_conn, f"{IC5_PREFIX}unresolved term")
    pack = load_domain_pack(PACK_DIR)
    issues = mine_issues(ic5_client, ic5_pg_conn, pack)
    assert any(
        issue.kind == "glossary_proposal"
        and issue.term == f"{IC5_PREFIX}unresolved term"
        for issue in issues
    )


def test_ic5_mine_issues_finds_brief_ambiguities(ic5_client, ic5_pg_conn) -> None:
    pack = load_domain_pack(PACK_DIR)
    issues = mine_issues(
        ic5_client, ic5_pg_conn, pack, brief=_brief_with_ambiguities()
    )
    assert any(
        issue.kind == "brief_ambiguity"
        and issue.term == "mandorle dolci sbucciate"
        for issue in issues
    )


def test_ic5_detect_modifier_terms() -> None:
    pack = load_domain_pack(PACK_DIR)
    hits = detect_modifier_terms(
        pack,
        [
            "sweet almonds peeled",
            "sweet almonds",
            "peeled tomatoes diced",
            "garlic chopped",
        ],
    )
    by_term = {term: (alias, entry_id) for term, alias, entry_id in hits}
    assert "sweet almonds" not in by_term
    assert by_term["sweet almonds peeled"][0] == "sweet almonds"
    assert by_term["peeled tomatoes diced"][0] == "peeled tomatoes"
    assert by_term["garlic chopped"][0] == "garlic"


# ---------------------------------------------------------------------------
# Unit: proposal generation
# ---------------------------------------------------------------------------

def test_ic5_propose_extension_add_alias_for_modifier() -> None:
    pack = load_domain_pack(PACK_DIR)
    issue = CuratorIssue(
        issue_id="gloss-1",
        kind="glossary_proposal",
        term="sweet almonds peeled",
    )
    proposal = propose_extension(issue, pack)
    assert isinstance(proposal, Proposal)
    assert proposal.kind == "add_alias"
    assert proposal.entry_id == "ING-SWEET-ALMONDS"
    assert proposal.labels_en == "sweet almonds"
    assert "sweet almonds peeled" in proposal.aliases
    assert proposal.ontology_uri
    assert proposal.status == "pending"


def test_ic5_propose_extension_add_entry_for_unknown() -> None:
    pack = load_domain_pack(PACK_DIR)
    issue = CuratorIssue(
        issue_id="gloss-2",
        kind="glossary_proposal",
        term="totally unknown ingredient",
    )
    proposal = propose_extension(issue, pack)
    assert proposal.kind == "add_entry"
    assert proposal.entry_id.startswith("ING-")
    assert proposal.labels_en == "totally unknown ingredient"
    assert proposal.ontology_uri.startswith("http://dbpedia.org/resource/")


def test_ic5_proposal_schema_is_strict() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Proposal(
            proposal_id="p",
            issue_id="i",
            kind="bad-kind",
            term="t",
            target_glossary="ingredienti",
            entry_id="ING-X",
            labels_en="t",
            labels_it="t",
        )


# ---------------------------------------------------------------------------
# Unit: human gate (negative tests)
# ---------------------------------------------------------------------------

def test_ic5_gate_rejects_pending_proposal(
    ic5_client, ic5_pg_conn, tmp_path
) -> None:
    pack = _working_pack(tmp_path)
    proposal = Proposal(
        proposal_id="prop-pending",
        issue_id="i",
        kind="add_alias",
        term="sweet almonds peeled",
        target_glossary="ingredienti",
        entry_id="ING-SWEET-ALMONDS",
        labels_en="sweet almonds",
        labels_it="mandorle dolci",
        aliases=["sweet almonds peeled"],
        status="pending",
    )
    with pytest.raises(CuratorGateError):
        apply_approved(ic5_pg_conn, ic5_client, pack, proposal)


def test_ic5_gate_rejects_rejected_proposal(
    ic5_client, ic5_pg_conn, tmp_path
) -> None:
    pack = _working_pack(tmp_path)
    proposal = Proposal(
        proposal_id="prop-rejected",
        issue_id="i",
        kind="add_alias",
        term="sweet almonds peeled",
        target_glossary="ingredienti",
        entry_id="ING-SWEET-ALMONDS",
        labels_en="sweet almonds",
        labels_it="mandorle dolci",
        aliases=["sweet almonds peeled"],
        status="rejected",
    )
    with pytest.raises(CuratorGateError):
        apply_approved(ic5_pg_conn, ic5_client, pack, proposal)


def test_ic5_gate_rejects_backing_row_not_approved(
    ic5_client, ic5_pg_conn, tmp_path
) -> None:
    pack = _working_pack(tmp_path)
    row = create_glossary_proposal(ic5_pg_conn, f"{IC5_PREFIX}term")
    proposal = Proposal(
        proposal_id="prop-db-pending",
        issue_id="i",
        kind="add_alias",
        term=f"{IC5_PREFIX}term",
        target_glossary="ingredienti",
        entry_id="ING-SWEET-ALMONDS",
        labels_en="sweet almonds",
        labels_it="mandorle dolci",
        aliases=[f"{IC5_PREFIX}term"],
        status="approved",
        source_type="glossary_proposal",
        source_proposal_id=row["id"],
    )
    with pytest.raises(CuratorGateError):
        apply_approved(ic5_pg_conn, ic5_client, pack, proposal)


def test_ic5_gate_rejects_production_pack(ic5_client, ic5_pg_conn) -> None:
    pack = load_domain_pack(PACK_DIR)
    proposal = Proposal(
        proposal_id="prop-manual",
        issue_id="i",
        kind="add_alias",
        term="sweet almonds peeled",
        target_glossary="ingredienti",
        entry_id="ING-SWEET-ALMONDS",
        labels_en="sweet almonds",
        labels_it="mandorle dolci",
        aliases=["sweet almonds peeled"],
        status="approved",
    )
    with pytest.raises(CuratorGateError):
        apply_approved(ic5_pg_conn, ic5_client, pack, proposal)


# ---------------------------------------------------------------------------
# Integration: full Curator cycle with injected ambiguities
# ---------------------------------------------------------------------------

async def _initial_extract(client, conn, pack, corpus):
    """Translate + canonicalize + extract the corpus; return translated md."""
    llm = build_fake_llm(pack, corpus)
    translated: dict[str, str] = {}
    for name in sorted(corpus):
        doc_id = _doc_id(name)
        result = await translate_document(pack, corpus[name], llm)
        translated[doc_id] = result.translated_md
        canonical = canonicalize(pack, result.translated_md, conn=conn)
        extract_document(client, None, doc_id, canonical.canonical_md, pack)
    return translated


def _affected_docs(proposal: Proposal, translated: dict[str, str]) -> dict[str, str]:
    term = proposal.term.casefold()
    return {
        doc_id: md
        for doc_id, md in translated.items()
        if term in md.casefold()
    }


def _simulate_approval(conn, proposal: Proposal, translated, user) -> None:
    rows = list_glossary_proposals(conn, status="pending")
    row = next((r for r in rows if r["term"] == proposal.term), None)
    if row is None:
        row = create_glossary_proposal(conn, proposal.term, context=f"{IC5_PREFIX}sim")
    decide_glossary_proposal(conn, row["id"], "approved", str(user["id"]))
    proposal.status = "approved"
    proposal.source_type = "glossary_proposal"
    proposal.source_proposal_id = row["id"]
    proposal.translated_documents = _affected_docs(proposal, translated)


def _document_hash(client: Neo4jClient, doc_id: str) -> str | None:
    with client.session() as session:
        record = session.run(
            "MATCH (d:Document {id: $id}) RETURN d.canonical_hash AS h",
            id=doc_id,
        ).single()
        return record["h"] if record else None


async def test_ic5_curator_cycle_incremental_and_bitemporal(
    ic5_client, ic5_pg_conn, ic5_user, tmp_path
) -> None:
    """mine -> propose -> approve -> apply; only touched docs are re-extracted."""
    pack = _working_pack(tmp_path)
    load_pack(ic5_client, pack.root)
    corpus = injected_modifier_corpus()
    translated = await _initial_extract(ic5_client, ic5_pg_conn, pack, corpus)

    hashes_before = {
        doc_id: _document_hash(ic5_client, doc_id) for doc_id in translated
    }

    issues = mine_issues(ic5_client, ic5_pg_conn, pack)
    glossary_issues = [i for i in issues if i.kind == "glossary_proposal"]
    assert len(glossary_issues) >= 5

    proposals = [propose_extension(issue, pack) for issue in glossary_issues]
    for proposal in proposals:
        _simulate_approval(ic5_pg_conn, proposal, translated, ic5_user)
        result = apply_approved(
            ic5_pg_conn, ic5_client, pack, proposal, user_id=str(ic5_user["id"])
        )
        assert result.applied is True
        assert result.changed_documents

    # Only the five modifier documents changed; the two clean docs did not.
    changed = {
        doc_id
        for proposal in proposals
        for doc_id in proposal.translated_documents
    }
    for doc_id in translated:
        if doc_id in changed:
            assert _document_hash(ic5_client, doc_id) != hashes_before[doc_id]
        else:
            assert _document_hash(ic5_client, doc_id) == hashes_before[doc_id]

    # Bitemporal history: old Source (v1) and new Source (v2) both exist.
    for doc_id in changed:
        with ic5_client.session() as session:
            v1 = session.run(
                "MATCH (s:Source {id: $id}) RETURN s", id=f"{doc_id}:source"
            ).single()
            v2 = session.run(
                "MATCH (s:Source {id: $id}) RETURN s", id=f"{doc_id}:source:v2"
            ).single()
        assert v1 is not None
        assert v2 is not None

    # No DELETE: the original qty/unit Facts are still present.
    with ic5_client.session() as session:
        count = session.run(
            """
            MATCH (f:Fact)
            WHERE f.id STARTS WITH $prefix AND f.property IN ['qty', 'unit']
            RETURN count(f) AS count
            """,
            prefix=IC5_PREFIX,
        ).single()["count"]
    assert count >= 10


def test_ic5_re_extract_creates_version_of_on_fact_change(
    ic5_client, ic5_pg_conn, ic5_user, tmp_path
) -> None:
    """White-box: a changed Fact value produces a VERSION_OF chain, no DELETE."""
    from app.agents.curator import _re_extract_document

    pack = _working_pack(tmp_path)
    load_pack(ic5_client, pack.root)
    doc_id = f"{IC5_PREFIX}ver_doc"
    md_v1 = """---
title: Versioned
id: ic5_VER_1
lang: en
source_lang: it
servings: 1
time_min: 5
difficulty: easy
verification_level: L1
canonical_version: 1
---
## Ingredients
- 100 g sweet almonds
## Method
1. Toast.
"""
    md_v2 = """---
title: Versioned
id: ic5_VER_1
lang: en
source_lang: it
servings: 1
time_min: 5
difficulty: easy
verification_level: L1
canonical_version: 2
---
## Ingredients
- 200 g sweet almonds
## Method
1. Toast.
"""
    extract_document(ic5_client, None, doc_id, md_v1, pack)
    _re_extract_document(
        ic5_client, ic5_pg_conn, doc_id, md_v1, md_v2, pack, str(ic5_user["id"])
    )

    repo = GraphRepository(ic5_client)
    history = repo.get_fact_history(f"{doc_id}:ing:0:qty")
    assert len(history) == 2
    assert history[0]["value"] == "100"
    assert history[0]["status"] == "obsolete"
    assert history[1]["value"] == "200"
    assert history[1]["status"] == "valid"

    with ic5_client.session() as session:
        record = session.run(
            """
            MATCH (old:Fact)-[r:VERSION_OF]->(new:Fact)
            WHERE old.logical_id = $id AND new.logical_id = $id
            RETURN count(r) AS count
            """,
            id=f"{doc_id}:ing:0:qty",
        ).single()
    assert record is not None and record["count"] == 1


# ---------------------------------------------------------------------------
# E2E: ambiguity reduction >= 80% after N=3 cycles
# ---------------------------------------------------------------------------

def _count_unresolved(pack, translated: dict[str, str]) -> int:
    unresolved: set[str] = set()
    for md in translated.values():
        canonical = canonicalize(pack, md)
        unresolved.update(canonical.unresolved_terms)
    return len(unresolved)


async def test_ic5_e2e_curator_reduces_ambiguities(
    ic5_client, ic5_pg_conn, ic5_user, tmp_path
) -> None:
    pack = _working_pack(tmp_path)
    load_pack(ic5_client, pack.root)
    corpus = injected_modifier_corpus()
    translated = await _initial_extract(ic5_client, ic5_pg_conn, pack, corpus)

    initial = _count_unresolved(pack, translated)
    assert initial >= 5

    for _cycle in range(3):
        issues = mine_issues(ic5_client, ic5_pg_conn, pack)
        proposals = [
            propose_extension(issue, pack)
            for issue in issues
            if issue.kind == "glossary_proposal"
        ]
        for proposal in proposals:
            _simulate_approval(ic5_pg_conn, proposal, translated, ic5_user)
            apply_approved(
                ic5_pg_conn, ic5_client, pack, proposal, user_id=str(ic5_user["id"])
            )
        pack = load_domain_pack(pack.root)

    final = _count_unresolved(pack, translated)
    reduction = (initial - final) / initial
    assert reduction >= 0.80
