"""Ricerca di ricette nel knowledge base (branch validate-recipe).

La ricerca combina tre segnali:
a) impronta ingredienti standard con dosi (canonical item + qty scalata)
b) procedura standardizzata (overlap token)
c) nome della ricetta (RAG vettoriale)

Il punteggio combinato determina se la ricetta e' presente nel knowledge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.auth import Principal
from app.domain import parse_translated_md
from app.domain.pack import DomainPackBundle
from app.rag.rag import build_embedding_from_graph, rag_query
from app.storage.client import Neo4jClient


@dataclass
class RecipeMatch:
    """Esito della ricerca di una ricetta nel knowledge."""

    found: bool
    document_id: str | None = None
    title: str | None = None
    score: float = 0.0
    name_score: float = 0.0
    ingredient_score: float = 0.0
    procedure_score: float = 0.0
    matched_ingredients: list[str] = field(default_factory=list)
    missing_ingredients: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def ingredient_fingerprint(md: str, known_units: set[str] | None = None) -> list[tuple[str, float]]:
    """Impronta ingredienti: (item canonico, qty scalata) ordinata."""
    parsed = parse_translated_md(md, known_units=known_units or set())
    fp = []
    for ing in parsed.ingredients:
        if ing.qty is not None:
            fp.append((ing.item.casefold(), float(ing.qty)))
    return sorted(fp)


def _ingredient_overlap(query_fp: list[tuple[str, float]], doc_fp: list[tuple[str, float]]) -> tuple[float, list[str], list[str]]:
    """Overlap tra impronte: frazione di ingredienti della query presenti nel doc."""
    q_items = {item for item, _ in query_fp}
    d_items = {item for item, _ in doc_fp}
    matched = sorted(q_items & d_items)
    missing = sorted(q_items - d_items)
    score = len(matched) / len(q_items) if q_items else 0.0
    return score, matched, missing


def _procedure_overlap(query_steps: list[str], doc_steps: list[str]) -> float:
    if not query_steps or not doc_steps:
        return 0.0
    q = set()
    for s in query_steps:
        q |= _tokens(s)
    d = set()
    for s in doc_steps:
        d |= _tokens(s)
    if not q or not d:
        return 0.0
    return len(q & d) / len(q)


def _all_documents(client: Neo4jClient) -> list[dict]:
    """Tutti i :Document del grafo (per il confronto impronte)."""
    with client.session() as session:
        records = session.run(
            """
            MATCH (d:Document)
            RETURN d.id AS id, d.title AS title, d.canonical_hash AS hash
            ORDER BY d.id
            """
        ).data()
    return records


def search_recipe(
    client: Neo4jClient,
    pack: DomainPackBundle,
    principal: Principal,
    canonical_md: str,
    name: str,
    limit: int = 5,
) -> RecipeMatch:
    """Cerca una ricetta nel knowledge: impronta ingredienti + procedura + nome."""
    parsed = parse_translated_md(canonical_md, known_units=pack.known_units())
    query_fp = ingredient_fingerprint(canonical_md, pack.known_units())
    query_steps = list(parsed.steps)

    # 1) impronta ingredienti: confronto con tutti i documenti del grafo
    best_ing = 0.0
    best_doc = None
    best_matched: list[str] = []
    best_missing: list[str] = []
    for doc in _all_documents(client):
        doc_md = _recompose(client, doc["id"])
        if doc_md is None:
            continue
        doc_fp = ingredient_fingerprint(doc_md, pack.known_units())
        ing_score, matched, missing = _ingredient_overlap(query_fp, doc_fp)
        if ing_score > best_ing:
            best_ing = ing_score
            best_doc = doc
            best_matched, best_missing = matched, missing

    # 2) procedura: overlap con il miglior candidato
    proc_score = 0.0
    if best_doc is not None:
        doc_md = _recompose(client, best_doc["id"])
        if doc_md:
            doc_parsed = parse_translated_md(doc_md, known_units=pack.known_units())
            proc_score = _procedure_overlap(query_steps, list(doc_parsed.steps))

    # 3) nome: RAG vettoriale
    embedding = build_embedding_from_graph(client)
    hits = rag_query(client, principal, name, lang="en", limit=limit, embedding=embedding)
    name_score = 0.0
    name_doc = None
    if hits:
        name_score = hits[0].score
        name_doc = hits[0].document_id

    # combinazione: nome (0.4) + ingredienti (0.4) + procedura (0.2)
    score = 0.4 * name_score + 0.4 * best_ing + 0.2 * proc_score
    # decisione: la maggioranza degli ingredienti deve combaciare E
    # (nome o procedura) devono confermare
    found = best_ing >= 0.6 and (name_score >= 0.4 or proc_score >= 0.4)

    return RecipeMatch(
        found=found,
        document_id=best_doc["id"] if best_doc else name_doc,
        title=best_doc["title"] if best_doc else None,
        score=score,
        name_score=name_score,
        ingredient_score=best_ing,
        procedure_score=proc_score,
        matched_ingredients=best_matched,
        missing_ingredients=best_missing,
    )


def _recompose(client: Neo4jClient, doc_id: str) -> str | None:
    from app.domain.recompose import recompose_document
    try:
        return recompose_document(client, doc_id)
    except Exception:
        return None
