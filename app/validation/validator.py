"""Validazione e ricerca ricette contro il knowledge base (branch validate-recipe).

Funzionalita':
- ``validate_recipe_md``: valida una ricetta in formato md (translated/canonical)
  senza toccare il grafo: parse, unita' riconosciute (MKS), scaling dosi a N persone,
  copertura ingredienti sul glossario, presenza procedura.
- ``validate_and_ingest``: valida, ingesta nel grafo (con riferimenti sorgente),
  popola l'embedding e verifica il retrieval RAG della ricetta normalizzata.
- ``search_recipes``: ricerca RAG di ricette per query naturale.

Il report (pydantic) espone ogni esito per il gate di qualita'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.auth import Principal
from app.domain import parse_translated_md
from app.domain.doses import MKS_FACTORS, MKS_NATIVE, standardize_doses
from app.domain.extract import extract_document
from app.domain.pack import DomainPackBundle
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient

# Unita' di conteggio/porzione ammesse (non convertibili in MKS: 1 uovo resta 1 uovo).
COUNT_UNITS = {
    "serving", "servings", "pcs", "piece", "pieces", "each", "unit", "units",
    "egg", "eggs", "clove", "pinch", "tablespoon", "teaspoon", "cup", "slice",
    "sprig", "leaf", "drop", "bunch", "sachet", "thread", "rib", "tuft",
    "walnut", "grain", "zest", "etto",
    # conteggi/porzioni formato industriale
    "pz", "ea", "t", "tsp", "tbsp", "ltr", "lts",
}

ALLOWED_UNITS = MKS_NATIVE | set(MKS_FACTORS) | COUNT_UNITS


@dataclass
class RecipeValidationReport:
    """Esito della validazione di una ricetta."""

    recipe_id: str
    title: str
    servings: int
    scale_factor: float
    n_ingredients: int
    n_steps: int
    units_ok: bool
    unknown_units: list[str] = field(default_factory=list)
    ingredients_resolved: int = 0
    coverage: float = 0.0
    rag_found: bool = False
    rag_top1: str | None = None
    rag_score: float | None = None
    source_ref: dict | None = None

    @property
    def passed(self) -> bool:
        """Gate: parse ok, unita' MKS/conteggio, procedura presente, RAG trovata."""
        return (
            self.n_ingredients > 0
            and self.n_steps > 0
            and self.units_ok
            and self.rag_found
        )


def _unknown_units(md: str) -> list[str]:
    """Unita' non riconosciute (non MKS e non conteggio)."""
    bad: set[str] = set()
    for line in md.splitlines():
        m = re.match(r"^- (\S+) (\S+) (.+)$", line)
        if m:
            u = m.group(2).lower()
            if u not in ALLOWED_UNITS:
                bad.add(u)
    return sorted(bad)


def _coverage(md: str, pack: DomainPackBundle) -> tuple[int, float]:
    """Frazione di ingredienti risolti dal glossario (labels_en/aliases)."""
    labels = {e.labels_en.casefold() for e in pack.glossary_entries()}
    for e in pack.glossary_entries():
        labels.update(a.casefold() for a in e.aliases)
    total = resolved = 0
    for line in md.splitlines():
        m = re.match(r"^- (\S+) (\S+) (.+)$", line)
        if m:
            total += 1
            item = m.group(3).strip().casefold()
            if any(item == l or item.startswith(l + " ") or item.endswith(" " + l) for l in labels):
                resolved += 1
    return resolved, (resolved / total if total else 0.0)


def validate_recipe_md(
    md: str,
    pack: DomainPackBundle,
    servings_target: int = 10,
) -> RecipeValidationReport:
    """Valida una ricetta md (formato translated/canonical) senza toccare il grafo."""
    parsed = parse_translated_md(
        md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native)
    )
    doses = standardize_doses(md, pack, servings_target=servings_target)
    resolved, coverage = _coverage(doses.canonical_md, pack)
    return RecipeValidationReport(
        recipe_id=str(parsed.frontmatter.get("id", "?")),
        title=str(parsed.frontmatter.get("title", "")),
        servings=doses.servings,
        scale_factor=doses.scale_factor,
        n_ingredients=len(parsed.ingredients),
        n_steps=len(parsed.steps),
        units_ok=not _unknown_units(doses.canonical_md),
        unknown_units=_unknown_units(doses.canonical_md),
        ingredients_resolved=resolved,
        coverage=coverage,
    )


def validate_and_ingest(
    client: Neo4jClient,
    pack: DomainPackBundle,
    md: str,
    source_ref: dict | None = None,
    principal: Principal | None = None,
    servings_target: int = 10,
    prefix: str = "val_",
) -> RecipeValidationReport:
    """Valida, ingesta nel grafo (con riferimenti), popola il vettore e verifica il RAG."""
    report = validate_recipe_md(md, pack, servings_target=servings_target)
    parsed = parse_translated_md(
        md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native)
    )
    doc_id = f"{prefix}{report.recipe_id}"
    extract_document(client, None, doc_id, standardize_doses(md, pack, servings_target).canonical_md, pack, source_ref=source_ref)
    with client.session() as session:
        session.run(
            "MATCH (d:Document {id: $id}) SET d.source_title = $title",
            id=doc_id,
            title=parsed.title,
        )
    embedding = build_embedding_from_graph(client, pack)
    populate_embeddings(client, embedding)
    admin = principal or Principal(f"{prefix}u_admin", ("admin",), (), "default", f"{prefix}j_admin")
    hits = rag_query(client, admin, report.title, lang="en", limit=5, embedding=embedding)
    report.rag_found = any(h.document_id == report.recipe_id for h in hits)
    report.rag_top1 = hits[0].document_id if hits else None
    report.rag_score = hits[0].score if hits else None
    report.source_ref = source_ref
    return report


def search_recipes(
    client: Neo4jClient,
    principal: Principal,
    query: str,
    limit: int = 5,
    lang: str | None = None,
) -> list[dict]:
    """Ricerca RAG di ricette per query naturale (con riferimenti)."""
    embedding = build_embedding_from_graph(client)
    hits = rag_query(client, principal, query, lang=lang, limit=limit, embedding=embedding)
    return [h.to_dict() for h in hits]
