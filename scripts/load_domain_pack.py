#!/usr/bin/env python3
"""Bootstrap idempotente del Domain Pack nel grafo Neo4j (Iterazione A, WP-A4).

Legge ``domain-packs/<pack>/pack.yaml`` e i glossari YAML, poi esegue MERGE di
``:DomainPack`` e ``:CanonicalTerm``. Una doppia esecuzione produce gli stessi
nodi e nessun duplicato (MERGE su id deterministici).

Parser YAML autonomo e autosufficiente: NON dipende da ``app/domain`` (WP-A1
può non esistere ancora). Supporta tre forme di glossario:

1. lista di termini::

       - id: TECH-BLANCH
         label_en: Blanching
         label_it: Sbollentare
         definition: ...
         is_public: true

2. mappa term_id -> termine::

       TECH-BLANCH:
         label_en: Blanching
         label_it: Sbollentare

3. mappa namespace -> term_id -> termine::

       tecnica:
         TECH-BLANCH:
           label_en: Blanching

Uso::

    uv run python scripts/load_domain_pack.py [--pack-dir domain-packs/ricette]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from neo4j import ManagedTransaction

from app.storage.client import Neo4jClient

DEFAULT_PACK_DIR = "domain-packs/ricette"
DEFAULT_GLOSSARIES = ["tecnica", "ingredienti", "stati"]

# Chiavi che identificano un "termine" (vs un namespace annidato) in un mapping.
_TERM_KEYS = {
    "id",
    "term_id",
    "namespace",
    "label_en",
    "label_it",
    "label",
    "definition",
    "ontology_uri",
    "uri",
    "is_public",
    "roles",
    "teams",
    "aliases",
    "alias",
}


def load_yaml(path: Path) -> Any:
    """Carica un file YAML; file assente o vuoto -> {}."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def pack_id(name: str, version: str) -> str:
    """Id deterministico del DomainPack (name:version)."""
    return f"{name}:{version}"


def term_id(namespace: str, term_key: str) -> str:
    """Id deterministico del CanonicalTerm (namespace:term_id)."""
    return f"{namespace}:{term_key}"


def _looks_like_term(value: Any) -> bool:
    """True se il mapping sembra un termine (ha chiavi da termine)."""
    return isinstance(value, dict) and bool(set(value.keys()) & _TERM_KEYS)


def _normalize_term(
    namespace: str,
    key: str | None,
    item: Any,
) -> dict[str, Any] | None:
    """Normalizza un termine in un dict pronto per il MERGE."""
    if not isinstance(item, dict):
        return None

    term_key = item.get("id") or item.get("term_id") or key
    if term_key is None:
        return None
    term_key = str(term_key)

    ns = str(item.get("namespace") or namespace)
    label_en = item.get("label_en") or item.get("label") or term_key
    label_it = item.get("label_it") or label_en

    return {
        "id": term_id(ns, term_key),
        "namespace": ns,
        "term_id": term_key,
        "label_en": label_en,
        "label_it": label_it,
        "definition": item.get("definition"),
        "ontology_uri": item.get("ontology_uri") or item.get("uri"),
        "is_public": bool(item.get("is_public", False)),
        "roles": list(item.get("roles") or []),
        "teams": list(item.get("teams") or []),
    }


def parse_glossary(namespace: str, data: Any) -> list[dict[str, Any]]:
    """Normalizza i formati YAML supportati in una lista di termini."""
    terms: list[dict[str, Any]] = []

    if isinstance(data, list):
        for item in data:
            term = _normalize_term(namespace, None, item)
            if term:
                terms.append(term)
        return terms

    if not isinstance(data, dict):
        return terms

    for key, value in data.items():
        if isinstance(value, list):
            # namespace -> lista di termini
            for item in value:
                term = _normalize_term(key, None, item)
                if term:
                    terms.append(term)
        elif isinstance(value, dict):
            if _looks_like_term(value):
                # term_id -> termine
                term = _normalize_term(namespace, key, value)
                if term:
                    terms.append(term)
            else:
                # namespace -> term_id -> termine
                for sub_key, sub_value in value.items():
                    term = _normalize_term(key, sub_key, sub_value)
                    if term:
                        terms.append(term)

    return terms


def _write_pack(
    tx: ManagedTransaction,
    *,
    pid: str,
    name: str,
    version: str,
    language: str,
    canonical_language: str,
    terms: list[dict[str, Any]],
) -> None:
    """MERGE idempotente di DomainPack e CanonicalTerm."""
    pack_props = {
        "name": name,
        "version": version,
        "language": language,
        "canonical_language": canonical_language,
    }
    tx.run(
        "MERGE (p:DomainPack {id: $id}) SET p += $props",
        id=pid,
        props=pack_props,
    )

    for term in terms:
        term_props = {k: v for k, v in term.items() if k != "id" and v is not None}
        tx.run(
            "MERGE (t:CanonicalTerm {id: $id}) SET t += $props",
            id=term["id"],
            props=term_props,
        )


def load_pack(client: Neo4jClient, pack_dir: Path) -> dict[str, Any]:
    """Carica il pack in ``pack_dir`` nel grafo. Ritorna un riepilogo."""
    pack_meta = load_yaml(pack_dir / "pack.yaml")
    name = str(pack_meta.get("name") or pack_dir.name)
    version = str(pack_meta.get("version") or "1.0.0")
    language = str(pack_meta.get("language") or "it")
    canonical_language = str(pack_meta.get("canonical_language") or "en")
    glossaries = pack_meta.get("glossaries") or DEFAULT_GLOSSARIES

    terms: list[dict[str, Any]] = []
    for glossary_name in glossaries:
        glossary_name = str(glossary_name)
        glossary_path = pack_dir / "glossari" / f"{glossary_name}.yaml"
        if not glossary_path.exists():
            glossary_path = pack_dir / "glossari" / f"{glossary_name}.yml"
        if not glossary_path.exists():
            print(
                f"WARN: glossario {glossary_name!r} non trovato in "
                f"{pack_dir / 'glossari'}",
                file=sys.stderr,
            )
            continue
        data = load_yaml(glossary_path)
        parsed = parse_glossary(glossary_name, data)
        terms.extend(parsed)

    pid = pack_id(name, version)
    with client.session() as session:
        session.execute_write(
            _write_pack,
            pid=pid,
            name=name,
            version=version,
            language=language,
            canonical_language=canonical_language,
            terms=terms,
        )

    return {"pack_id": pid, "name": name, "version": version, "terms": len(terms)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack-dir",
        default=DEFAULT_PACK_DIR,
        help=f"Directory del Domain Pack (default: {DEFAULT_PACK_DIR})",
    )
    args = parser.parse_args(argv)

    pack_dir = Path(args.pack_dir)
    if not pack_dir.exists():
        print(f"ERROR: pack dir non trovato: {pack_dir}", file=sys.stderr)
        return 1

    client = Neo4jClient.from_env()
    try:
        client.verify_connectivity()
        result = load_pack(client, pack_dir)
        print(
            f"OK: pack {result['pack_id']} caricato con "
            f"{result['terms']} termini canonici"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
