"""graph.json -> Neo4j migration parity tests (ADR-001 D6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.storage.client import Neo4jClient
from app.storage.migrate import migrate_graphjson

PREFIX = "wp2test_mig_"


def _synthetic_graph() -> dict:
    nodes = [
        {
            "id": f"{PREFIX}n{i}",
            "label": f"node_{i}",
            "file_type": "code" if i % 2 == 0 else "doc",
            "source_file": f"{PREFIX}src_{i % 3}.py",
            "source_location": f"L{i + 1}",
            "community": i % 3,
        }
        for i in range(10)
    ]
    links = [
        {
            "source": f"{PREFIX}n{i}",
            "target": f"{PREFIX}n{(i + 1) % 10}",
            "relation": "uses" if i % 2 == 0 else "calls",
            "confidence": "EXTRACTED" if i % 3 else "INFERRED",
            "source_file": f"{PREFIX}src_{i % 3}.py",
            "source_location": f"L{i + 1}",
        }
        for i in range(10)
    ]
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": links,
    }


def _write_graph(tmp_path: Path, graph: dict) -> Path:
    path = tmp_path / "synthetic_graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def test_migration_parity(client: Neo4jClient, tmp_path: Path) -> None:
    graph = _synthetic_graph()
    path = _write_graph(tmp_path, graph)
    report = migrate_graphjson(
        path,
        client=client,
        chunk_size=3,
        migration_time=datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC),
    )
    assert report.nodes_read == 10
    assert report.links_read == 10
    assert report.entities_written == 10
    assert report.relations_written == 10
    assert report.sources_written == 3

    with client.session() as session:
        entity_ids = {
            record["id"]
            for record in session.run(
                "MATCH (e:Entity) WHERE e.id STARTS WITH $prefix RETURN e.id AS id",
                prefix=PREFIX,
            )
        }
        rel_triples = {
            (record["source"], record["target"], record["relation"])
            for record in session.run(
                """
                MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
                WHERE a.id STARTS WITH $prefix AND b.id STARTS WITH $prefix
                RETURN a.id AS source, b.id AS target, r.relation AS relation
                """,
                prefix=PREFIX,
            )
        }
        source_uris = {
            record["uri"]
            for record in session.run(
                "MATCH (s:Source) WHERE s.uri STARTS WITH $prefix RETURN s.uri AS uri",
                prefix=PREFIX,
            )
        }

    assert entity_ids == {node["id"] for node in graph["nodes"]}
    assert rel_triples == {
        (link["source"], link["target"], link["relation"]) for link in graph["links"]
    }
    assert source_uris == {node["source_file"] for node in graph["nodes"]}


def test_migration_maps_properties(client: Neo4jClient, tmp_path: Path) -> None:
    graph = _synthetic_graph()
    path = _write_graph(tmp_path, graph)
    migrate_graphjson(path, client=client, chunk_size=4)

    with client.session() as session:
        entity = session.run(
            "MATCH (e:Entity {id: $id}) RETURN e", id=f"{PREFIX}n0"
        ).single()["e"]
        rel = session.run(
            """
            MATCH (a:Entity {id: $source})-[r:RELATES_TO]->(b:Entity {id: $target})
            RETURN r
            """,
            source=f"{PREFIX}n0",
            target=f"{PREFIX}n1",
        ).single()["r"]

    assert entity["label"] == "node_0"
    assert entity["type"] == "code"
    assert entity["confidence"] == "EXTRACTED"
    assert entity["is_public"] is False
    assert entity["roles"] == []
    assert entity["teams"] == []
    assert rel["relation"] == "uses"
    assert rel["confidence"] == "INFERRED"
    assert rel["status"] == "valid"
    assert rel["valid_to"] is None


def test_migration_is_idempotent(client: Neo4jClient, tmp_path: Path) -> None:
    graph = _synthetic_graph()
    path = _write_graph(tmp_path, graph)
    first = migrate_graphjson(path, client=client, chunk_size=5)
    second = migrate_graphjson(path, client=client, chunk_size=5)

    assert first == second
    with client.session() as session:
        entity_count = session.run(
            "MATCH (e:Entity) WHERE e.id STARTS WITH $prefix RETURN count(e) AS c",
            prefix=PREFIX,
        ).single()["c"]
        rel_count = session.run(
            """
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.id STARTS WITH $prefix AND b.id STARTS WITH $prefix
            RETURN count(r) AS c
            """,
            prefix=PREFIX,
        ).single()["c"]
    assert entity_count == 10
    assert rel_count == 10
