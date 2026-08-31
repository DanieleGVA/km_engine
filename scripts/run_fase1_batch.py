"""Run reale Fase 1 PROGRAMMA-UNICO: batch Pareto -> giudice di canone.

Per ogni card: converti (passo 0) -> standardizza (canonicalize + dosi) ->
decomponi in componenti (passo 11) -> retrieval candidati per componente
(RAG) -> route_k3 (passo 14) con l'LLM giudice -> verdetti approvati in
canon_adjudication_log (passo 16). Report con metriche e costi.

Uso (dentro il container prod, rete interna):
    uv run python scripts/run_fase1_batch.py --pdf /deliverables/.../Pareto_Recipe_Cards_v001.pdf --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time

import psycopg

from app.auth import Principal
from app.domain import load_domain_pack, parse_translated_md
from app.domain.components import decompose_document
from app.domain.doses import standardize_doses
from app.domain.e2e import run_e2e_batch
from app.domain.llm import HttpLLMClient
from app.rag.rag import build_embedding_from_graph, rag_query
from app.storage.client import Neo4jClient
from scripts.msc_to_md import card_to_md, extract_cards

PACK_DIR = pathlib.Path(__file__).resolve().parents[1] / "domain-packs" / "ricette"


def _retrieve_candidates(client, embedding, admin, component_lines, limit=3):
    """Retrieval candidati di canone per componente (RAG).

    La query usa i TERMINI CANONICI (non i nomi industriali grezzi): i nomi
    canonici matchano il vocabolario del canone, i nomi CalcMenu no.
    """
    query = " ".join(component_lines)
    try:
        hits = rag_query(client, admin, query, lang="en", limit=limit, embedding=embedding)
        return [
            {"document_id": h.document_id, "title": h.title,
             "lines": component_lines[:3]}  # prompt piu' corto
            for h in hits
        ]
    except Exception:
        return []


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, type=pathlib.Path)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    pack = load_domain_pack(str(PACK_DIR))
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    dsn = args.dsn or "postgresql://km:km_prod_password@postgres:5432/km_engine"
    conn = psycopg.connect(dsn, autocommit=True)
    admin = Principal("fase1_admin", ("admin",), (), "default", "fase1_jwt")
    embedding = build_embedding_from_graph(client, pack)

    cards = extract_cards(args.pdf)[: args.limit]
    batch = []
    t0 = time.time()
    for card in cards:
        if card.servings is None or card.errors:
            continue
        md = card_to_md(card)
        try:
            canonical = parse_translated_md(
                md, known_units=pack.known_units(),
                optional_when_native=tuple(pack.frontmatter_optional_when_native),
                countable_units=pack.countable_units(),
            )
            doses = standardize_doses(md, pack, servings_target=10)
        except Exception:
            continue
        components = decompose_document(doses.canonical_md, card.code, pack)
        parsed_doses = parse_translated_md(
            doses.canonical_md, known_units=pack.known_units(),
            optional_when_native=tuple(pack.frontmatter_optional_when_native),
            countable_units=pack.countable_units(),
        )
        card_candidates = []
        msc_map = pack.msc_mapping()
        for comp in components:
            # termini canonici: code-first (msc_mapping) > item normalizzato
            comp_terms = []
            for p in comp.ingredient_positions:
                ing = parsed_doses.ingredients[p]
                term = msc_map.get(ing.code or "", ing.item)
                comp_terms.append(term)
            comp_lines = [f"- {t}" for t in comp_terms]
            cands = _retrieve_candidates(client, embedding, admin, comp_lines)
            card_candidates.extend(cands)
        batch.append({
            "id": card.code,
            "canonical_md": doses.canonical_md,
            "candidates": card_candidates,
        })

    llm = HttpLLMClient()
    result = await run_e2e_batch(llm, batch, pack, conn)
    elapsed = time.time() - t0
    result.report["cards"] = len(batch)
    result.report["elapsed_s"] = round(elapsed, 1)
    result.report["llm_calls"] = result.processed * 3  # k=3 per componente
    print(json.dumps(result.report, ensure_ascii=False, indent=1))
    conn.close()
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
