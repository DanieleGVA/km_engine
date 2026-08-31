"""Passo 11 PROGRAMMA-UNICO: decomposizione del canone in componenti.

Ogni :CanonComponent cita il documento d'origine e raggruppa gli ingredienti
con lo stesso ruolo/componente. La ricomposizione dei componenti di una
ricetta restituisce esattamente i suoi ingredienti (nessun orfano, nessuna
aggiunta).

- card MSC: il suffisso ``{component: ...}`` (passo 0/1) e' la fonte primaria
- libri: raggruppamento per classe del dizionario (proteina, amido, verdura,
  salsa, ...) con mappa ruolo -> componente
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain import parse_translated_md
from app.domain.pack import DomainPackBundle
from app.storage.client import Neo4jClient

# Mappa classe -> componente (per i libri senza suffisso {component}).
CLASS_COMPONENT: dict[str, str] = {
    "proteina": "main protein",
    "amido": "starch",
    "verdura": "vegetable",
    "frutta": "fruit",
    "latticino": "dairy",
    "grasso": "fat",
    "condimento": "sauce",
    "spezia": "spice",
    "erba": "herb",
    "liquido": "liquid",
    "dolcificante": "sweetener",
    "legume": "legume",
    "cereale": "cereal",
    "uovo": "egg",
    "fungo": "mushroom",
    "frutta_secca": "nuts",
    "bevanda": "beverage",
    "altro": "other",
}

DEFAULT_COMPONENT = "main"


@dataclass
class ComponentGroup:
    """Un componente di una ricetta."""

    label: str
    source_document: str
    ingredient_positions: list[int] = field(default_factory=list)


def decompose_document(
    canonical_md: str,
    doc_id: str,
    pack: DomainPackBundle,
) -> list[ComponentGroup]:
    """Raggruppa gli ingredienti di un documento in componenti.

    Priorita': suffisso ``{component}`` (card MSC) > classe del dizionario
    (libri) > componente di default.
    """
    parsed = parse_translated_md(
        canonical_md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    class_by_item = {
        e.labels_en.casefold(): e.class_ for e in pack.glossary_entries()
        if e.class_ is not None
    }
    groups: dict[str, ComponentGroup] = {}
    for pos, ing in enumerate(parsed.ingredients):
        if ing.component:
            label = ing.component
        else:
            cls = class_by_item.get(ing.item.casefold())
            label = CLASS_COMPONENT.get(cls or "", DEFAULT_COMPONENT)
        group = groups.setdefault(
            label, ComponentGroup(label=label, source_document=doc_id)
        )
        group.ingredient_positions.append(pos)
    return list(groups.values())


def write_components(
    client: Neo4jClient, doc_id: str, groups: list[ComponentGroup]
) -> int:
    """Scrive i :CanonComponent nel grafo (MERGE idempotente)."""
    n = 0
    with client.session() as session:
        for g in groups:
            cid = f"{doc_id}:comp:{g.label}"
            session.run(
                """
                MERGE (c:CanonComponent {id: $id})
                SET c.label = $label,
                    c.source_document = $source
                WITH c
                MATCH (d:Document {id: $doc_id})
                MERGE (c)-[:PART_OF_DOC]->(d)
                """,
                id=cid, label=g.label, source=g.source_document, doc_id=doc_id,
            )
            for pos in g.ingredient_positions:
                session.run(
                    """
                    MATCH (e:Entity {id: $entity_id})
                    MATCH (c:CanonComponent {id: $comp_id})
                    MERGE (e)-[:PART_OF_COMPONENT]->(c)
                    """,
                    entity_id=f"{doc_id}:ing:{pos}",
                    comp_id=cid,
                )
            n += 1
    return n


def verify_recomposition(
    client: Neo4jClient, doc_id: str, expected_positions: list[int]
) -> list[str]:
    """Verifica: la ricomposizione dei componenti restituisce esattamente
    gli ingredienti del documento (nessun orfano, nessuna aggiunta)."""
    problems: list[str] = []
    with client.session() as session:
        # ingredienti del documento
        doc_ings = {
            r["id"]
            for r in session.run(
                "MATCH (e:Entity {type: 'ingredient'})-[:PART_OF_DOC]->"
                "(d:Document {id: $doc_id}) RETURN e.id AS id",
                doc_id=doc_id,
            ).data()
        }
        # ingredienti nei componenti
        comp_ings = {
            r["id"]
            for r in session.run(
                "MATCH (e:Entity)-[:PART_OF_COMPONENT]->(:CanonComponent)"
                "-[:PART_OF_DOC]->(d:Document {id: $doc_id}) RETURN e.id AS id",
                doc_id=doc_id,
            ).data()
        }
        orphans = doc_ings - comp_ings
        additions = comp_ings - doc_ings
        if orphans:
            problems.append(f"orfani: {sorted(orphans)[:5]}")
        if additions:
            problems.append(f"aggiunte: {sorted(additions)[:5]}")
    return problems
