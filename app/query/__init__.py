"""Query layer visibility-aware sopra app/storage (WP5, Gate G5).

Questo modulo fornisce query filtrate per visibilità su entità, fatti, relazioni,
ricerca full-text e storico versioni. Tutti i metodi accettano un Principal e
applicano il filtro visibilità prima di restituire i dati.

POLITICA DI VISIBILITÀ (default-deny):
- Se un oggetto ha UNA QUALSIASI restrizione esplicita (teams/roles) e NON è
  esplicitamente is_public=True, è trattato come NON pubblico.
- Una restrizione esplicita su una dimensione rende l'oggetto privato per tutte
  le altre dimensioni (documentato in app/query/README.md).
- Il bypass storage è solo admin (is_admin); gli Editor NON vedono altri tenant.
- Usa SEMPRE principal_visibility_context come unico ponte da auth a storage.

FR9 (Multilingua):
- La lingua interna del knowledge base è inglese (canonica).
- I contenuti hanno un opzionale campo translation_state (pending/translated).
- Se lang != "en" e translation_state == "pending", il response include
  untranslated=True per segnalare che la traduzione non è disponibile.
- La traduzione vera è WP4/LLM; qui solo annotazione.

Iterazione A (WP-A4): estensioni domain per :Document e :CanonicalTerm in
``app/query/domain.py``, con lo stesso filtro visibilità P4 (default-deny).
"""
from __future__ import annotations

from .domain import (
    get_document,
    get_document_by_entity,
    list_canonical_terms,
    list_documents,
    search_canonical_terms,
    search_documents,
)
from .engine import (
    get_entity_with_history,
    localize_response,
    query_entities,
    query_facts,
    query_relations,
    search,
)

__all__ = [
    "get_document",
    "get_document_by_entity",
    "get_entity_with_history",
    "list_canonical_terms",
    "list_documents",
    "localize_response",
    "query_entities",
    "query_facts",
    "query_relations",
    "search",
    "search_canonical_terms",
    "search_documents",
]
