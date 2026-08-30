"""T7 — Bootstrap pack idempotente (WP-A4).

Criterio: doppia esecuzione di ``load_domain_pack.load_pack`` produce gli stessi
nodi e nessun duplicato (MERGE stabile su id deterministici).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack, pack_id, term_id

TEST_PREFIX = "ia4_"
PACK_NAME = "ia4_ricette"
PACK_VERSION = "1.0.0"
GLOSSARIES = ["ia4_tecnica", "ia4_ingredienti", "ia4_stati"]


def _write_pack(tmp_path: Path) -> Path:
    """Scrive un Domain Pack minimale in una directory temporanea."""
    pack_dir = tmp_path / "ricette"
    glossari_dir = pack_dir / "glossari"
    glossari_dir.mkdir(parents=True)

    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": PACK_NAME,
                "language": "it",
                "canonical_language": "en",
                "version": PACK_VERSION,
                "glossaries": GLOSSARIES,
            }
        ),
        encoding="utf-8",
    )

    (glossari_dir / "ia4_tecnica.yaml").write_text(
        yaml.safe_dump(
            {
                "TECH-BLANCH": {
                    "label_en": "Blanching",
                    "label_it": "Sbollentare",
                    "is_public": True,
                },
                "TECH-BOIL": {
                    "label_en": "Boiling",
                    "label_it": "Bollire",
                    "is_public": True,
                },
            }
        ),
        encoding="utf-8",
    )

    (glossari_dir / "ia4_ingredienti.yaml").write_text(
        yaml.safe_dump(
            {
                "ING-TOMATO": {
                    "label_en": "Tomato",
                    "label_it": "Pomodoro",
                    "ontology_uri": "http://purl.obolibrary.org/obo/FOODON_00001118",
                    "is_public": True,
                },
                "ING-RICE": {
                    "label_en": "Rice",
                    "label_it": "Riso",
                    "is_public": True,
                },
            }
        ),
        encoding="utf-8",
    )

    (glossari_dir / "ia4_stati.yaml").write_text(
        yaml.safe_dump(
            {
                "STATE-AL-DENTE": {
                    "label_en": "Al dente",
                    "label_it": "Al dente",
                    "is_public": True,
                },
                "STATE-CREAMY": {
                    "label_en": "Creamy",
                    "label_it": "Cremoso",
                    "is_public": True,
                },
            }
        ),
        encoding="utf-8",
    )

    return pack_dir


def _count(client: Neo4jClient, label: str, prefix: str) -> int:
    with client.session() as session:
        record = session.run(
            f"MATCH (n:{label}) WHERE n.id STARTS WITH $prefix RETURN count(n) AS c",
            prefix=prefix,
        ).single()
        return int(record["c"])


def test_load_pack_idempotent(client: Neo4jClient, tmp_path: Path) -> None:
    """Doppio load -> stessi nodi, nessun duplicato."""
    pack_dir = _write_pack(tmp_path)

    first = load_pack(client, pack_dir)
    second = load_pack(client, pack_dir)

    assert first["pack_id"] == pack_id(PACK_NAME, PACK_VERSION)
    assert first["terms"] == 6
    assert second["pack_id"] == first["pack_id"]
    assert second["terms"] == first["terms"]

    # Un solo DomainPack e 6 CanonicalTerm, anche dopo la seconda esecuzione.
    assert _count(client, "DomainPack", TEST_PREFIX) == 1
    assert _count(client, "CanonicalTerm", TEST_PREFIX) == 6

    # Nessun duplicato per id: ogni id compare una sola volta.
    with client.session() as session:
        record = session.run(
            """
            MATCH (t:CanonicalTerm)
            WHERE t.id STARTS WITH $prefix
            WITH t.id AS id, count(*) AS c
            WHERE c > 1
            RETURN count(*) AS duplicates
            """,
            prefix=TEST_PREFIX,
        ).single()
        assert int(record["duplicates"]) == 0


def test_load_pack_properties(client: Neo4jClient, tmp_path: Path) -> None:
    """Il MERGE aggiorna le proprietà del pack e dei termini."""
    pack_dir = _write_pack(tmp_path)
    load_pack(client, pack_dir)

    with client.session() as session:
        pack_record = session.run(
            "MATCH (p:DomainPack {id: $id}) RETURN p",
            id=pack_id(PACK_NAME, PACK_VERSION),
        ).single()
        assert pack_record is not None
        assert pack_record["p"]["name"] == PACK_NAME
        assert pack_record["p"]["version"] == PACK_VERSION
        assert pack_record["p"]["language"] == "it"
        assert pack_record["p"]["canonical_language"] == "en"

        term_record = session.run(
            "MATCH (t:CanonicalTerm {id: $id}) RETURN t",
            id=term_id("ia4_ingredienti", "ING-TOMATO"),
        ).single()
        assert term_record is not None
        assert term_record["t"]["namespace"] == "ia4_ingredienti"
        assert term_record["t"]["term_id"] == "ING-TOMATO"
        assert term_record["t"]["label_en"] == "Tomato"
        assert term_record["t"]["label_it"] == "Pomodoro"
        assert term_record["t"]["is_public"] is True


def test_load_pack_supports_list_format(client: Neo4jClient, tmp_path: Path) -> None:
    """Il parser autonomo accetta anche la forma a lista di termini."""
    pack_dir = tmp_path / "list_pack"
    glossari_dir = pack_dir / "glossari"
    glossari_dir.mkdir(parents=True)

    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "ia4_list_pack",
                "version": "1.0.0",
                "glossaries": ["ia4_list_glossary"],
            }
        ),
        encoding="utf-8",
    )
    (glossari_dir / "ia4_list_glossary.yaml").write_text(
        yaml.safe_dump(
            [
                {"id": "TECH-BLANCH", "label_en": "Blanching", "is_public": True},
                {"id": "TECH-BOIL", "label_en": "Boiling", "is_public": True},
            ]
        ),
        encoding="utf-8",
    )

    result = load_pack(client, pack_dir)
    assert result["terms"] == 2

    with client.session() as session:
        record = session.run(
            "MATCH (t:CanonicalTerm {id: $id}) RETURN t",
            id=term_id("ia4_list_glossary", "TECH-BLANCH"),
        ).single()
        assert record is not None
        assert record["t"]["label_en"] == "Blanching"
