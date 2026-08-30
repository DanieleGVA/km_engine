#!/usr/bin/env python3
"""Domain Pack: validation/dump (WP-A1) + idempotent graph bootstrap (WP-A4).

Two responsibilities share this script:
- ``main()`` (WP-A1): validate a pack with ``app.domain.load_domain_pack`` and
  print a JSON control dump. No graph access.
- ``load_pack`` / ``pack_id`` / ``term_id`` (WP-A4): idempotent Neo4j bootstrap
  of ``:DomainPack`` and ``:CanonicalTerm`` nodes (MERGE on deterministic ids).

Usage:
    uv run python scripts/load_domain_pack.py [--pack-dir domain-packs/ricette] [--dump out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from app.storage.client import Neo4jClient

_TERM_KEYS = {
    "id", "label_en", "labels_en", "label_it", "labels_it",
    "definition", "ontology_uri", "is_public", "roles", "teams",
}


def pack_id(name: str, version: str) -> str:
    """Deterministic :DomainPack id."""
    return f"{name}:{version}"


def term_id(namespace: str, term_id: str) -> str:
    """Deterministic :CanonicalTerm id."""
    return f"{namespace}:{term_id}"


def _looks_like_term(value: Any) -> bool:
    return isinstance(value, dict) and bool(_TERM_KEYS.intersection(value))


def _iter_terms(raw: Any, glossary_name: str) -> Iterator[tuple[str, str, dict]]:
    """Yield ``(namespace, term_id, term_dict)`` for the supported YAML shapes.

    Supported shapes:
    1. list of term dicts (each with ``id``)
    2. mapping ``term_id -> term dict``
    3. mapping ``namespace -> term_id -> term dict``
    4. mapping with an ``entries`` list (Domain Pack schema of WP-A1)
    """
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                yield glossary_name, str(item["id"]), item
        return

    if not isinstance(raw, dict):
        return

    if isinstance(raw.get("entries"), list):
        for item in raw["entries"]:
            if isinstance(item, dict) and item.get("id"):
                yield glossary_name, str(item["id"]), item
        return

    for key, value in raw.items():
        if _looks_like_term(value):
            yield glossary_name, str(key), value
        elif isinstance(value, dict):
            for term_key, term_value in value.items():
                if isinstance(term_value, dict):
                    yield str(key), str(term_key), term_value


def _term_value(term: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in term and term[key] is not None:
            return term[key]
    return default


def load_pack(client: Neo4jClient, pack_dir: str | Path) -> dict[str, Any]:
    """Idempotently load a Domain Pack into Neo4j (WP-A4).

    Returns ``{"pack_id": ..., "terms": N}``. Re-running is safe: every write
    is a MERGE on a deterministic id.
    """
    pack_dir = Path(pack_dir)
    pack_raw = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    if not isinstance(pack_raw, dict):
        raise TypeError(f"{pack_dir / 'pack.yaml'}: expected a YAML mapping")

    name = str(pack_raw["name"])
    version = str(pack_raw.get("version", "0.0.0"))
    language = str(pack_raw.get("language", "it"))
    canonical_language = str(pack_raw.get("canonical_language", "en"))
    glossary_names = pack_raw.get("glossaries", [])

    terms: list[tuple[str, str, dict]] = []
    for glossary_name in glossary_names:
        glossary_path = pack_dir / "glossari" / f"{glossary_name}.yaml"
        raw = yaml.safe_load(glossary_path.read_text(encoding="utf-8"))
        terms.extend(_iter_terms(raw, str(glossary_name)))

    pid = pack_id(name, version)
    with client.session() as session:
        session.run(
            """
            MERGE (p:DomainPack {id: $id})
            SET p.name = $name,
                p.version = $version,
                p.language = $language,
                p.canonical_language = $canonical_language
            """,
            id=pid,
            name=name,
            version=version,
            language=language,
            canonical_language=canonical_language,
        )
        for namespace, term_key, term in terms:
            session.run(
                """
                MERGE (t:CanonicalTerm {id: $id})
                SET t.namespace = $namespace,
                    t.term_id = $term_id,
                    t.label_en = $label_en,
                    t.label_it = $label_it,
                    t.definition = $definition,
                    t.ontology_uri = $ontology_uri,
                    t.is_public = $is_public,
                    t.roles = $roles,
                    t.teams = $teams
                """,
                id=term_id(namespace, term_key),
                namespace=namespace,
                term_id=term_key,
                label_en=_term_value(term, "label_en", "labels_en", default=term_key),
                label_it=_term_value(term, "label_it", "labels_it", default=term_key),
                definition=_term_value(term, "definition", default=""),
                ontology_uri=_term_value(term, "ontology_uri"),
                is_public=bool(_term_value(term, "is_public", default=False)),
                roles=_term_value(term, "roles", default=[]) or [],
                teams=_term_value(term, "teams", default=[]) or [],
            )

    return {"pack_id": pid, "terms": len(terms)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack-dir",
        default="domain-packs/ricette",
        help="Domain Pack directory (default: domain-packs/ricette)",
    )
    parser.add_argument(
        "--dump",
        default=None,
        help="Optional JSON path for the control dump (default: stdout)",
    )
    args = parser.parse_args()

    from app.domain import DomainPackValidationError, load_domain_pack

    try:
        bundle = load_domain_pack(Path(args.pack_dir))
    except DomainPackValidationError as exc:
        print(f"Domain Pack INVALID ({len(exc.errors)} errors):", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    payload = json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.dump:
        Path(args.dump).write_text(payload + "\n", encoding="utf-8")
        print(f"control dump written to {args.dump}")
    else:
        print(payload)

    print(
        f"Domain Pack OK: name={bundle.pack.name} version={bundle.pack.version} "
        f"language={bundle.pack.language}->{bundle.pack.canonical_language} "
        f"glossaries={len(bundle.glossary_entries())} entries "
        f"units={len(bundle.units)} rules",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
