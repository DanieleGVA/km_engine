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


def _candidate_lines(hit, max_lines=5):
    """Righe ingrediente del candidato dal suo canonical_md (per il prompt).

    Il giudice confronta le righe della card con le righe del candidato:
    servono le righe PROPRIE del candidato, mai quelle della query.
    """
    return [l for l in hit.canonical_md.splitlines() if l.startswith("- ")][:max_lines]


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
             "lines": _candidate_lines(h)}
            for h in hits
        ]
    except Exception:
        return []


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, type=pathlib.Path)
    ap.add_argument("--limit", type=int, default=None,
                    help="numero massimo di card (default: tutte)")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    pack = load_domain_pack(str(PACK_DIR))
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    dsn = args.dsn or "postgresql://km:km_prod_password@postgres:5432/km_engine"
    conn = psycopg.connect(dsn, autocommit=True)
    admin = Principal("fase1_admin", ("admin",), (), "default", "fase1_jwt")
    embedding = build_embedding_from_graph(client, pack)

    cards = extract_cards(args.pdf)
    if args.limit is not None:
        cards = cards[: args.limit]
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
        msc_map = pack.msc_mapping()
        card_components = []
        for comp in components:
            # righe REALI della card per questo componente (con dosi): il
            # giudice confronta queste con le righe dei candidati
            comp_lines = [
                parsed_doses.ingredients[p].raw for p in comp.ingredient_positions
            ]
            # termini canonici per il retrieval: code-first (msc_mapping) >
            # item normalizzato
            comp_terms = []
            for p in comp.ingredient_positions:
                ing = parsed_doses.ingredients[p]
                term = msc_map.get(ing.code or "", ing.item)
                comp_terms.append(term)
            cands = _retrieve_candidates(client, embedding, admin, comp_terms)
            card_components.append({
                "name": comp.label,
                "lines": comp_lines,
                "candidates": cands,
            })
        batch.append({
            "id": card.code,
            "canonical_md": doses.canonical_md,
            "components": card_components,
        })

    llm = HttpLLMClient()
    result = await run_e2e_batch(llm, batch, pack, conn)
    elapsed = time.time() - t0
    result.report["cards"] = len(batch)
    result.report["elapsed_s"] = round(elapsed, 1)
    result.report["llm_calls"] = result.components * 3  # k=3 per componente
    print(json.dumps(result.report, ensure_ascii=False, indent=1))
    conn.close()
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
