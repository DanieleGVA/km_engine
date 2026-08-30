"""Hybrid RAG retrieval over the Neo4j domain graph (WP-B1/B2/B4).

Retrieval contract:

1. Embed the query with a deterministic 384-dimension service.
2. Vector search on ``Document.embedding`` (cosine index).
3. **Visibility filter before returning anything** (default-deny via
   ``principal_visibility_context``). Vector candidates that are not visible
   are discarded before ranking and before any content is returned.
4. Graph expansion: ``Entity -> NORMALIZED_TO -> CanonicalTerm`` (and the
   reverse direction), ``PART_OF_DOC`` and ``Source`` provenance.
5. Deterministic, explainable ranking (no LLM):

   ``score = cosine * (1 + boost_lang) * (1 + boost_verification)``

   ``boost_lang`` rewards the user language when it matches the document
   source language or when an English user asks for a canonical EN document.
   ``boost_verification`` rewards higher verification levels (L1 < L2 < L3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neo4j import ManagedTransaction

from app.auth import Principal, principal_visibility_context
from app.domain.embedding import DeterministicEmbedding, EmbeddingService
from app.domain.pack import DomainPackBundle, load_domain_pack
from app.domain.recompose import recompose_document
from app.rag.cache import context_cache, invalidate_rag_caches, vocab_cache
from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError
from app.storage.visibility import Visibility, is_visible

VECTOR_INDEX = "document_embedding_vector"
PACK_DIR = Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"

LANG_BOOST = 0.10
TITLE_BOOST = 0.15
VERIFICATION_BOOST = {"L1": 0.0, "L2": 0.05, "L3": 0.10}
# Candidate heuristic (WP-B5, documented): the vector index returns
# ``max(limit * _CANDIDATE_FACTOR, _CANDIDATE_FLOOR)`` candidates so the
# visibility filter (default-deny) cannot silently drop the only visible
# matches. The factor/floor are a deliberate trade-off: a larger candidate
# set costs vector-index work per query, a smaller one can hurt recall when
# many candidates are filtered out. The values are part of the MVP ranking
# contract (GB1) and are NOT tuned here: changing them would alter which
# documents are eligible for the top-k, i.e. the retrieval semantics.
_CANDIDATE_FACTOR = 4
_CANDIDATE_FLOOR = 50


@dataclass
class RagHit:
    """One ranked retrieval result."""

    document_id: str
    graph_id: str
    title: str
    score: float
    cosine: float
    match_reason: str
    canonical_md: str
    canonical_hash: str
    source_lang: str
    verification_level: str
    untranslated: bool
    terms: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    provenance: str | None
    source_ref: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation for the API."""
        return {
            "document_id": self.document_id,
            "title": self.title,
            "score": self.score,
            "match_reason": self.match_reason,
            "canonical_md": self.canonical_md,
            "untranslated": self.untranslated,
            "canonical_hash": self.canonical_hash,
            "source_lang": self.source_lang,
            "verification_level": self.verification_level,
            "terms": self.terms,
            "entities": self.entities,
            "provenance": self.provenance,
        }


# ---------------------------------------------------------------------------
# Small visibility/JSON helpers (mirrors app/query/domain.py)
# ---------------------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    try:
        from neo4j.time import Date, DateTime, Time
    except ImportError:  # pragma: no cover
        return value
    if isinstance(value, (DateTime, Date, Time)):
        return value.iso_format()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _node_to_dict(node: Any) -> dict[str, Any]:
    return _jsonable(dict(node))


def _can_see(props: dict[str, Any], ctx: dict[str, object]) -> bool:
    return is_visible(
        Visibility(
            is_public=bool(props.get("is_public", False)),
            roles=tuple(props.get("roles") or ()),
            teams=tuple(props.get("teams") or ()),
        ),
        **ctx,
    )


# ---------------------------------------------------------------------------
# Embedding service construction
# ---------------------------------------------------------------------------

def build_embedding_from_graph(
    client: Neo4jClient,
    pack: DomainPackBundle | None = None,
) -> DeterministicEmbedding:
    """Build a deterministic embedding vocabulary from pack + graph corpus.

    The vocabulary is internal (tokens only): it never returns document
    content and is not subject to the visibility filter. It reads every
    ``:Document`` title/entity/term label so query and document embeddings
    share the same IDF weights.

    WP-B5: the result is cached in-process with a TTL (``KM_RAG_CACHE_TTL``,
    default 300s) keyed by ``(neo4j uri, pack id)``. The cache is invalidated
    by :func:`populate_embeddings` and by ``extract_document`` (ingest), so a
    changed corpus is picked up immediately; the TTL bounds staleness for any
    other graph mutation.
    """
    # Cache lookup happens BEFORE the pack load: loading the pack YAML is
    # itself expensive (~tens of ms), so a cached vocabulary must not pay it.
    pack_id = f"{pack.pack.name}:{pack.pack.version}" if pack is not None else None
    key = (client.config.uri, pack_id)
    cached = vocab_cache.get(key)
    if cached is not None:
        return cached

    if pack is None:
        try:
            pack = load_domain_pack(PACK_DIR)
        except Exception:  # noqa: BLE001 - pack is a vocabulary hint, not a hard dependency
            # If the pack is invalid/in-progress, fall back to the graph
            # corpus alone so retrieval keeps working.
            pack = None

    texts: list[str] = []
    if pack is not None:
        for entry in pack.glossary_entries():
            parts = [entry.labels_en, entry.labels_it, *entry.aliases, entry.definition]
            texts.append(" ".join(part for part in parts if part))

    with client.session() as session:
        records = session.run(
            """
            MATCH (d:Document)
            OPTIONAL MATCH (d)<-[:PART_OF_DOC]-(e:Entity)
            OPTIONAL MATCH (e)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN d.title AS title,
                   d.source_title AS source_title,
                   collect(DISTINCT e.label) AS entity_labels,
                   collect(DISTINCT t.label_en) AS term_labels
            """
        )
        for record in records:
            parts = [record["title"] or ""]
            if record["source_title"]:
                parts.append(record["source_title"])
            parts.extend(record["entity_labels"] or [])
            parts.extend(record["term_labels"] or [])
            texts.append(" ".join(parts))

    embedding = DeterministicEmbedding.from_texts(texts)
    vocab_cache.set(key, embedding)
    return embedding


# ---------------------------------------------------------------------------
# Embedding population
# ---------------------------------------------------------------------------

def _document_text(
    props: dict[str, Any],
    entity_labels: list[str] | None,
    term_labels: list[str] | None,
) -> str:
    parts = [props.get("title") or ""]
    if props.get("source_title"):
        parts.append(props["source_title"])
    parts.extend(entity_labels or [])
    parts.extend(term_labels or [])
    return " ".join(parts)


def populate_embeddings(
    client: Neo4jClient,
    embedding: EmbeddingService,
    principal: Principal | None = None,
) -> int:
    """Fill ``Document.embedding`` for documents that lack it (idempotent).

    ``principal=None`` means maintenance/admin mode: every document is
    embedded. When a principal is supplied, only documents visible to that
    principal are embedded (the same default-deny policy used by reads).

    Returns the number of documents populated.

    WP-B5: invalidates the RAG caches (embedding vocabulary, canonical_md,
    document context) so a subsequent ``build_embedding_from_graph`` /
    ``rag_query`` sees the freshly embedded corpus.
    """
    ctx = principal_visibility_context(principal) if principal is not None else None
    is_admin = bool(ctx["is_admin"]) if ctx is not None else True

    rows: list[tuple[str, list[float]]] = []
    with client.session() as session:
        records = session.run(
            """
            MATCH (d:Document)
            WHERE d.embedding IS NULL
            OPTIONAL MATCH (d)<-[:PART_OF_DOC]-(e:Entity)
            OPTIONAL MATCH (e)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN d,
                   collect(DISTINCT e.label) AS entity_labels,
                   collect(DISTINCT t.label_en) AS term_labels
            """
        )
        for record in records:
            props = _node_to_dict(record["d"])
            if ctx is not None and not (is_admin or _can_see(props, ctx)):
                continue
            text = _document_text(
                props, record["entity_labels"], record["term_labels"]
            )
            rows.append((props["id"], embedding.embed(text)))

    if not rows:
        return 0

    def write(tx: ManagedTransaction) -> None:
        for doc_id, vector in rows:
            tx.run(
                """
                MATCH (d:Document {id: $doc_id})
                SET d.embedding = $embedding
                """,
                doc_id=doc_id,
                embedding=vector,
            )

    with client.session() as session:
        session.execute_write(write)
        # L'indice vettoriale e' eventually-consistent: attendiamo che le nuove
        # entrate siano indicizzate prima di restituire, altrimenti le query
        # subito successive vedono un set di candidati incompleto (recall
        # non deterministico). db.awaitIndex e' sincrono e con timeout.
        try:
            session.run("CALL db.awaitIndex($index, 30)", index=VECTOR_INDEX)
        except Exception:  # noqa: BLE001, S110 - indice assente/vecchia versione
            pass
    invalidate_rag_caches()
    return len(rows)


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------

def _lang_boost(props: dict[str, Any], lang: str | None) -> float:
    if not lang:
        return 0.0
    lang_l = lang.lower()
    source_lang = (
        props.get("source_lang") or props.get("source_language") or ""
    ).lower()
    if lang_l == source_lang:
        return LANG_BOOST
    if lang_l in ("en", "eng", "english") and props.get("lang") == "en":
        return LANG_BOOST
    return 0.0


def _verification_boost(props: dict[str, Any]) -> float:
    return VERIFICATION_BOOST.get(props.get("verification_level", "L1"), 0.0)


def _title_boost(props: dict[str, Any], query: str) -> float:
    """Hybrid full-text boost: overlap tra i token della query e il titolo
    (canonico EN o sorgente IT). Deterministico e spiegabile; rende il
    retrieval ibrido (vettoriale + titolo) come da roadmap WP-B1.
    """
    if not query:
        return 0.0
    q_tokens = set(re.findall(r"[a-z0-9]+", query.casefold()))
    if not q_tokens:
        return 0.0
    title = " ".join(
        str(props.get(k) or "") for k in ("title", "source_title")
    ).casefold()
    t_tokens = set(re.findall(r"[a-z0-9]+", title))
    if not t_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    if overlap == 0:
        return 0.0
    return TITLE_BOOST * (overlap / len(q_tokens))



def _document_context(client: Neo4jClient, doc_id: str) -> dict[str, Any]:
    """Graph expansion for one document (entities, terms, provenance).

    WP-B5: cached in-process with TTL keyed by ``(neo4j uri, doc_id)`` and
    invalidated on ingest (``extract_document``) / ``populate_embeddings``.
    """
    key = (client.config.uri, doc_id)
    cached = context_cache.get(key)
    if cached is not None:
        return cached
    with client.session() as session:
        entity_records = session.run(
            """
            MATCH (d:Document {id: $doc_id})<-[:PART_OF_DOC]-(e:Entity)
            OPTIONAL MATCH (e)-[:NORMALIZED_TO]->(t:CanonicalTerm)
            RETURN e.id AS entity_id, e.label AS label, e.type AS type,
                   t.id AS term_id, t.label_en AS term_label,
                   t.namespace AS namespace
            ORDER BY e.id, t.id
            """,
            doc_id=doc_id,
        )
        entities: dict[str, dict[str, Any]] = {}
        terms: dict[str, dict[str, Any]] = {}
        for record in entity_records:
            entity_id = record["entity_id"]
            entities[entity_id] = {
                "id": entity_id,
                "label": record["label"],
                "type": record["type"],
            }
            if record["term_id"]:
                terms[record["term_id"]] = {
                    "id": record["term_id"],
                    "label": record["term_label"],
                    "namespace": record["namespace"],
                }

        source_record = session.run(
            "MATCH (s:Source {id: $source_id}) RETURN s.uri AS uri",
            source_id=f"{doc_id}:source",
        ).single()

    context = {
        "entities": list(entities.values()),
        "terms": list(terms.values()),
        "provenance": source_record["uri"] if source_record else None,
    }
    context_cache.set(key, context)
    return context


def _match_reason(
    cosine: float,
    lang_boost: float,
    verification_boost: float,
    title_boost: float,
    final: float,
    context: dict[str, Any],
) -> str:
    term_labels = ", ".join(
        sorted({term["label"] for term in context["terms"] if term.get("label")})
    )
    return (
        f"cosine={cosine:.4f} boost_lang={lang_boost:.2f} "
        f"boost_verification={verification_boost:.2f} boost_title={title_boost:.2f} "
        f"final={final:.4f} terms={term_labels or '-'}"
    )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def rag_query(
    client: Neo4jClient,
    principal: Principal,
    query: str,
    lang: str | None = None,
    limit: int = 5,
    *,
    embedding: EmbeddingService | None = None,
) -> list[RagHit]:
    """Hybrid vector + graph retrieval with visibility-before-return.

    The vector index returns more candidates than ``limit`` so the visibility
    filter cannot silently drop the only visible matches. Candidates are then
    ranked deterministically and the top ``limit`` are expanded/recomposed.
    """
    limit = max(limit, 1)
    if embedding is None:
        embedding = build_embedding_from_graph(client)

    vector = embedding.embed(query)
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]
    candidate_k = max(limit * _CANDIDATE_FACTOR, _CANDIDATE_FLOOR)

    candidates: list[tuple[dict[str, Any], float, float, float, float]] = []
    with client.session() as session:
        records = session.run(
            """
            CALL db.index.vector.queryNodes($index, $k, $vector)
            YIELD node, score
            RETURN node, score
            """,
            index=VECTOR_INDEX,
            k=candidate_k,
            vector=vector,
        )
        for record in records:
            props = _node_to_dict(record["node"])
            if not (is_admin or _can_see(props, ctx)):
                continue
            cosine = float(record["score"])
            lang_boost = _lang_boost(props, lang)
            verification_boost = _verification_boost(props)
            title_boost = _title_boost(props, query)
            final = (
                cosine
                * (1.0 + lang_boost)
                * (1.0 + verification_boost)
                * (1.0 + title_boost)
            )
            candidates.append(
                (props, cosine, lang_boost, verification_boost, title_boost, final)
            )

    # Deterministic tie-break: higher score first, then document id ascending.
    candidates.sort(key=lambda item: (-item[5], item[0].get("id", "")))

    hits: list[RagHit] = []
    for props, cosine, lang_boost, verification_boost, title_boost, final in candidates[:limit]:
        doc_id = props["id"]
        # L'indice vettoriale puo' contenere entry stantie (nodi eliminati da
        # run/ingest precedenti, lag di indicizzazione): in quel caso il nodo
        # non esiste piu' e l'hit va saltato, non fatto fallire.
        try:
            context = _document_context(client, doc_id)
            canonical_md = recompose_document(client, doc_id)
        except NotFoundError:
            continue
        localized = localize_document(props, lang)
        hits.append(
            RagHit(
                document_id=props.get("document_id", doc_id),
                graph_id=doc_id,
                title=props.get("title", ""),
                score=round(final, 6),
                cosine=round(cosine, 6),
                match_reason=_match_reason(
                    cosine, lang_boost, verification_boost, title_boost, final, context
                ),
                canonical_md=canonical_md,
                canonical_hash=props.get("canonical_hash", ""),
                source_lang=props.get("source_lang") or props.get("source_language", ""),
                source_ref={
                    k: props.get(k)
                    for k in ("source_author", "source_book", "source_page", "source_position")
                    if props.get(k)
                } or None,
                verification_level=props.get("verification_level", "L1"),
                untranslated=bool(localized.get("untranslated", False)),
                terms=context["terms"],
                entities=context["entities"],
                provenance=context["provenance"],
            )
        )
    return hits


# ---------------------------------------------------------------------------
# Structured glossary queries (WP-B2)
# ---------------------------------------------------------------------------

def glossary_query(
    client: Neo4jClient,
    principal: Principal,
    term_id: str | None = None,
    technique: str | None = None,
    ingredient: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Query documents through ``CanonicalTerm <- NORMALIZED_TO <- Entity``.

    Exactly one selector must be supplied: ``term_id`` (full canonical id) or
    one of ``technique``/``ingredient``/``state`` (a term id or label inside
    that namespace). Results are grouped by document and filtered for
    visibility on both the term and the document.
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    if term_id is not None:
        where = "t.id = $term_id"
        params: dict[str, Any] = {"term_id": term_id}
    else:
        namespace: str | None = None
        value: str | None = None
        if technique is not None:
            namespace, value = "tecnica", technique
        elif ingredient is not None:
            namespace, value = "ingredienti", ingredient
        elif state is not None:
            namespace, value = "stati", state
        if namespace is None or value is None:
            return []
        where = (
            "t.namespace = $namespace AND ("
            "t.term_id = $value OR toLower(t.label_en) = toLower($value) "
            "OR toLower(t.label_it) = toLower($value))"
        )
        params = {"namespace": namespace, "value": value}

    grouped: dict[str, dict[str, Any]] = {}
    with client.session() as session:
        records = session.run(
            f"""
            MATCH (t:CanonicalTerm)<-[:NORMALIZED_TO]-(e:Entity)
                  -[:PART_OF_DOC]->(d:Document)
            WHERE {where}
            RETURN t, e, d
            ORDER BY d.id, e.id
            """,
            **params,
        )
        for record in records:
            term = _node_to_dict(record["t"])
            entity = _node_to_dict(record["e"])
            document = _node_to_dict(record["d"])
            if not (is_admin or _can_see(term, ctx)):
                continue
            if not (is_admin or _can_see(document, ctx)):
                continue
            key = document["id"]
            if key not in grouped:
                grouped[key] = {
                    "document": document,
                    "term": term,
                    "entities": [],
                }
            grouped[key]["entities"].append(
                {
                    "id": entity.get("id"),
                    "label": entity.get("label"),
                    "type": entity.get("type"),
                }
            )

    results: list[dict[str, Any]] = []
    for doc_id in sorted(grouped):
        item = grouped[doc_id]
        document = item["document"]
        term = item["term"]
        results.append(
            {
                "document_id": document.get("document_id", document.get("id")),
                "graph_id": document.get("id"),
                "title": document.get("title", ""),
                "canonical_hash": document.get("canonical_hash", ""),
                "verification_level": document.get("verification_level", "L1"),
                "source_lang": document.get("source_lang")
                or document.get("source_language", ""),
                "term": {
                    "id": term.get("id"),
                    "namespace": term.get("namespace"),
                    "term_id": term.get("term_id"),
                    "label_en": term.get("label_en"),
                    "label_it": term.get("label_it"),
                },
                "entities": item["entities"],
            }
        )
    return results


# ---------------------------------------------------------------------------
# Localisation (WP-B4)
# ---------------------------------------------------------------------------

def localize_document(doc: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """FR9.3 localisation extended to ``:Document`` nodes.

    - ``source_language == lang``: served natively, no flag.
    - ``lang == en`` and the canonical EN representation is ready
      (``translation_state`` in ``native``/``translated``): no flag.
    - otherwise: ``untranslated=True``.

    Reuses the same ``untranslated`` flag and the same
    ``source_language``/``translation_state`` fields as
    :func:`app.query.engine.localize_response`.
    """
    result = dict(doc)
    if not lang:
        return result
    lang_l = lang.lower()
    source_lang = (
        doc.get("source_language") or doc.get("source_lang") or ""
    ).lower()
    state = doc.get("translation_state", "native")

    if lang_l == source_lang:
        return result
    if lang_l in ("en", "eng", "english") and state in ("native", "translated"):
        return result
    result["untranslated"] = True
    return result
