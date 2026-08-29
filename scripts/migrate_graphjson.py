"""CLI for the one-time graph.json -> Neo4j migration.

Usage:
    uv run python scripts/migrate_graphjson.py path/to/graph.json [--chunk-size 500] [--no-sources]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.storage.migrate import migrate_graphjson


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph_json", type=Path, help="graphify node-link JSON file")
    parser.add_argument("--chunk-size", type=int, default=500, help="rows per transaction")
    parser.add_argument(
        "--no-sources",
        action="store_true",
        help="do not register original source files as :Source nodes",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = migrate_graphjson(
        args.graph_json,
        chunk_size=args.chunk_size,
        register_sources=not args.no_sources,
    )
    print(
        f"migrated {report.nodes_read} nodes and {report.links_read} links -> "
        f"{report.entities_written} Entity, {report.relations_written} RELATES_TO, "
        f"{report.sources_written} Source"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
