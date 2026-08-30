"""RAG retrieval layer (Iterazione B, WP-B1/B2/B4).

Public API:
- :func:`app.rag.rag.populate_embeddings` — fill ``Document.embedding``.
- :func:`app.rag.rag.rag_query` — hybrid vector + graph retrieval.
- :func:`app.rag.rag.glossary_query` — structured glossary paths.
- :func:`app.rag.rag.localize_document` — FR9.3 localisation for Documents.
"""
from app.rag.rag import (
    LANG_BOOST,
    VERIFICATION_BOOST,
    RagHit,
    build_embedding_from_graph,
    glossary_query,
    localize_document,
    populate_embeddings,
    rag_query,
)

__all__ = [
    "LANG_BOOST",
    "VERIFICATION_BOOST",
    "RagHit",
    "build_embedding_from_graph",
    "glossary_query",
    "localize_document",
    "populate_embeddings",
    "rag_query",
]
