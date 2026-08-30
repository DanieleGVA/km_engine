"""Codegen (WP-C3): prove the draft pack works with the existing engine.

The codegen agent does **not** generate new core code. It verifies that the
draft Domain Pack produced by the Designer is directly usable by the existing
``app/domain`` engine (translate -> verify -> canonicalize -> extract ->
recompose) with zero changes to the core. It runs the Iteration-A suite-type
checks on a deterministic sample:

- P2 number invariants (``verify_l1``);
- complete canon-log (``verify_canon_log``);
- byte-identical round-trip (``extract_document`` + ``recompose_document``).

The result is an :class:`AgentReport` that the pipeline can gate on.
"""
from __future__ import annotations

from pathlib import Path

from app.agents.models import AgentReport
from app.domain import (
    LLMClient,
    canonicalize,
    extract_document,
    load_domain_pack,
    recompose_document,
    translate_document,
    verify_canon_log,
    verify_l1,
)
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

DEFAULT_SAMPLE_SIZE = 3
DEFAULT_DOC_PREFIX = "icc_"


async def run_conformance_suite(
    draft_dir: str | Path,
    corpus: dict[str, str],
    *,
    client: Neo4jClient | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    doc_prefix: str = DEFAULT_DOC_PREFIX,
    llm: LLMClient | None = None,
) -> AgentReport:
    """Run the Iteration-A suite-type checks on the draft pack.

    ``client`` enables the Neo4j round-trip leg (extract + recompose). When
    ``None`` the report covers the deterministic legs only (P2 + canon-log).
    ``llm`` is the optional translation hook; when ``None`` the deterministic
    glossary-based ``FakeLLMClient`` is used (never the network in tests).
    """
    draft_dir = Path(draft_dir)
    pack = load_domain_pack(draft_dir)
    llm = llm or build_fake_llm(pack, corpus)

    sample_names = sorted(corpus)[:sample_size]
    errors: list[str] = []
    p2_ok = 0
    canon_log_ok = 0
    roundtrip_ok = 0
    roundtrip_total = 0

    if client is not None:
        load_pack(client, draft_dir)

    for name in sample_names:
        source_md = corpus[name]
        try:
            translated = await translate_document(pack, source_md, llm)
            l1 = verify_l1(source_md, translated.translated_md, pack=pack)
            if l1.passed:
                p2_ok += 1
            else:
                errors.append(f"{name}: L1/P2 failed: {[i.message for i in l1.issues]}")

            canonical = canonicalize(pack, translated.translated_md)
            if verify_canon_log(
                pack,
                translated.translated_md,
                canonical.canonical_md,
                canonical.log_entries,
            ):
                canon_log_ok += 1
            else:  # pragma: no cover - verify_canon_log raises on failure
                errors.append(f"{name}: canon-log verification failed")

            if client is not None:
                doc_id = f"{doc_prefix}{canonical.document_id}"
                extract_document(client, None, doc_id, canonical.canonical_md, pack)
                recomposed = recompose_document(client, doc_id)
                roundtrip_total += 1
                if recomposed == canonical.canonical_md:
                    roundtrip_ok += 1
                else:
                    errors.append(f"{name}: round-trip mismatch")
        except Exception as exc:  # noqa: BLE001 - collected into the report
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    status = "ok" if not errors else "failed"
    metrics = {
        "sample_size": len(sample_names),
        "p2_ok": p2_ok,
        "canon_log_ok": canon_log_ok,
        "roundtrip_ok": roundtrip_ok,
        "roundtrip_total": roundtrip_total,
        "roundtrip_ratio": (roundtrip_ok / roundtrip_total) if roundtrip_total else None,
    }
    summary = (
        f"codegen conformance on {len(sample_names)} sample recipes: "
        f"P2 {p2_ok}/{len(sample_names)}, canon-log {canon_log_ok}/{len(sample_names)}"
        + (f", round-trip {roundtrip_ok}/{roundtrip_total}" if client is not None else "")
    )
    return AgentReport(
        agent="codegen",
        status=status,
        summary=summary,
        metrics=metrics,
        artifacts=[str(draft_dir)],
        errors=errors,
    )
