"""Passo 11 PROGRAMMA-UNICO: decomposizione del canone in componenti (una tantum).

Legge i documenti dal grafo, li decompone in :CanonComponent e verifica la
ricomposizione (nessun orfano, nessuna aggiunta).

Uso:
    uv run python scripts/decompose_canon.py [--limit N]
"""
from __future__ import annotations

import argparse
import json

from app.domain import load_domain_pack
from app.domain.components import (
    decompose_document,
    verify_recomposition,
    write_components,
)
from app.domain.errors import ParseError
from app.domain.recompose import recompose_document
from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError

PACK_DIR = "domain-packs/ricette"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    pack = load_domain_pack(PACK_DIR)
    client = Neo4jClient.from_env()
    client.verify_connectivity()

    with client.session() as session:
        ids = [r["id"] for r in session.run(
            "MATCH (d:Document) RETURN d.id AS id ORDER BY d.id").data()]
    if args.limit:
        ids = ids[: args.limit]

    n_components = 0
    n_problems = 0
    for doc_id in ids:
        try:
            md = recompose_document(client, doc_id)
        except (NotFoundError, ParseError):
            continue
        groups = decompose_document(md, doc_id, pack)
        n_components += write_components(client, doc_id, groups)
        expected = list(range(sum(len(g.ingredient_positions) for g in groups)))
        problems = verify_recomposition(client, doc_id, expected)
        if problems:
            n_problems += 1
            print(f"{doc_id}: {problems}")

    print(json.dumps({
        "documenti": len(ids),
        "componenti": n_components,
        "documenti_con_problemi": n_problems,
    }, ensure_ascii=False, indent=1))
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
