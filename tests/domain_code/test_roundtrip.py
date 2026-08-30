"""GD1 round-trip: recompose(extract(canonical.md)) == canonical.md on code.

The code-domain extractor writes the canonical markdown into the graph and the
code-domain recomposer reconstructs it byte-for-byte for every module in the
corpus.
"""
from __future__ import annotations

from app.storage.client import Neo4jClient
from code_domain.extract import extract_code_document
from code_domain.mapping import doc_id_for_module, render_canonical_md
from code_domain.recompose import recompose_code_document
from scripts.load_domain_pack import load_pack


def test_code_roundtrip_all_modules(client: Neo4jClient, graph, pack, modules) -> None:
    load_pack(client, "domain-packs/code")

    assert len(modules) >= 10
    for module in modules:
        canonical_md = render_canonical_md(module)
        doc_id = doc_id_for_module(module.source_file, prefix="id_code_")
        extract_code_document(client, doc_id, canonical_md, pack)
        recomposed = recompose_code_document(client, doc_id)
        assert recomposed == canonical_md, module.source_file


def test_code_roundtrip_is_idempotent(client: Neo4jClient, graph, pack, modules) -> None:
    load_pack(client, "domain-packs/code")

    module = modules[0]
    canonical_md = render_canonical_md(module)
    doc_id = doc_id_for_module(module.source_file, prefix="id_code_")
    extract_code_document(client, doc_id, canonical_md, pack)
    extract_code_document(client, doc_id, canonical_md, pack)
    assert recompose_code_document(client, doc_id) == canonical_md
