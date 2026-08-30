#!/usr/bin/env python
"""Rollback di versione Domain Pack (WP-E4, GE4).

Procedura per riportare i documenti alla versione precedente del pack
(``vN -> vN-1``) preservando lo storico bitemporale dei fatti:

1. snapshot dei fatti correnti del documento;
2. ri-canonicalizzazione + ri-estrazione con il pack vecchio;
3. versionamento dei fatti cambiati/scomparsi (``app.ops.rollback``).

Uso (un documento):
    uv run python scripts/rollback_pack.py \\
        --old-pack-dir domain-packs/ricette \\
        --translated-md path/to/translated.md \\
        --doc-id RIC-001

Uso (corpus):
    uv run python scripts/rollback_pack.py \\
        --old-pack-dir domain-packs/ricette \\
        --corpus-dir path/to/translated_corpus

Il grafo usa ``doc_id`` come chiave del nodo :Document. I file tradotti devono
avere frontmatter ``id`` coerente con ``doc_id`` (o si passa ``--doc-id``).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.domain import load_domain_pack
from app.domain.canonical import canonicalize
from app.domain.extract import extract_document
from app.ops.rollback import apply_rollback_versions, snapshot_document_facts
from app.storage.client import Neo4jClient


def _doc_id_from_md(md: str, fallback: str) -> str:
    from app.domain.verify import parse_translated_md

    try:
        parsed = parse_translated_md(md)
        return str(parsed.frontmatter.get("id", fallback))
    except Exception:  # noqa: BLE001 - fallback esplicito
        return fallback


def rollback_one(
    client: Neo4jClient,
    old_pack,
    doc_id: str,
    translated_md: str,
) -> dict:
    """Rollback di un singolo documento al pack vecchio."""
    snapshot = snapshot_document_facts(client, doc_id)
    canonical_md = canonicalize(old_pack, translated_md).canonical_md
    extract_document(client, None, doc_id, canonical_md, old_pack)
    changes = apply_rollback_versions(client, doc_id, snapshot)
    return {"doc_id": doc_id, "changes": changes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-pack-dir", required=True, help="Directory del pack vN-1")
    parser.add_argument("--translated-md", help="Singolo translated.md da rollbackare")
    parser.add_argument("--corpus-dir", help="Directory di translated.md (corpus)")
    parser.add_argument("--doc-id", help="Override del doc_id (con --translated-md)")
    args = parser.parse_args()

    if bool(args.translated_md) == bool(args.corpus_dir):
        parser.error("Specificare esattamente uno tra --translated-md e --corpus-dir.")

    old_pack = load_domain_pack(Path(args.old_pack_dir))
    client = Neo4jClient.from_env()
    client.verify_connectivity()

    try:
        if args.translated_md:
            md = Path(args.translated_md).read_text(encoding="utf-8")
            doc_id = args.doc_id or _doc_id_from_md(md, Path(args.translated_md).stem)
            result = rollback_one(client, old_pack, doc_id, md)
            print(result)
            return 0

        corpus = sorted(Path(args.corpus_dir).glob("*.md"))
        for path in corpus:
            md = path.read_text(encoding="utf-8")
            doc_id = _doc_id_from_md(md, path.stem)
            result = rollback_one(client, old_pack, doc_id, md)
            print(result)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
