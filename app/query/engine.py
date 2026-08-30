"""Query engine visibility-aware per km_engine (WP5, Gate G5).

Tutte le query applicano il filtro visibilità prima di restituire dati.
Il filtro usa principal_visibility_context come ponte da auth a storage.

FR9 (Multilingua):
- Lingua canonica: inglese
- localize_response aggiunge untranslated=True se lang != "en" e translation_state == "pending"
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from neo4j import ManagedTransaction

from app.auth import Principal, principal_visibility_context
from app.storage.client import Neo4jClient
from app.storage.visibility import Visibility, effective_visibility, is_visible


def _read(client: Neo4jClient, fn):
    """Esegue una funzione di lettura in transazione."""
    with client.session() as session:
        return session.execute_read(fn)


def _visibility_filter(visibility: Visibility, ctx: dict) -> bool:
    """Applica il filtro visibilità con la politica default-deny."""
    return is_visible(visibility, **ctx)


def _jsonable(value: Any) -> Any:
    """Converte tipi temporali Neo4j in stringhe ISO (JSON-serializzabili)."""
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
    """Converte un nodo Neo4j in dict, gestendo valid_to assente."""
    data = dict(node)
    if "valid_to" not in data and "Fact" in str(node.labels):
        data["valid_to"] = None
    return _jsonable(data)


def query_entities(
    client: Neo4jClient,
    principal: Principal,
    label: str | None = None,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Query entità filtrate per visibilità.

    Args:
        client: Neo4j client
        principal: Utente autenticato con ruoli/teams
        label: Filtro opzionale per label (es. "Function", "Class")
        entity_type: Filtro opzionale per tipo (es. "code", "doc")

    Returns:
        Lista di entità visibili con proprietà complete
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict]:
        # Query base per entità
        if label and entity_type:
            query = """
            MATCH (e:Entity)
            WHERE e.label = $label AND e.type = $type
            RETURN e
            """
            params = {"label": label, "type": entity_type}
        elif label:
            query = """
            MATCH (e:Entity)
            WHERE e.label = $label
            RETURN e
            """
            params = {"label": label}
        elif entity_type:
            query = """
            MATCH (e:Entity)
            WHERE e.type = $type
            RETURN e
            """
            params = {"type": entity_type}
        else:
            query = """
            MATCH (e:Entity)
            RETURN e
            """
            params = {}

        result = tx.run(query, **params)
        entities = []
        for record in result:
            node = record["e"]
            props = _node_to_dict(node)

            # Filtro visibilità (bypass solo per admin)
            if is_admin:
                entities.append(props)
            else:
                visibility = Visibility(
                    is_public=props.get("is_public", False),
                    roles=tuple(props.get("roles", [])),
                    teams=tuple(props.get("teams", [])),
                )
                if _visibility_filter(visibility, ctx):
                    entities.append(props)

        return entities

    return _read(client, work)


def query_facts(
    client: Neo4jClient,
    principal: Principal,
    entity_id: str | None = None,
    at_time: datetime | None = None,
) -> list[dict[str, Any]]:
    """Query fatti filtrati per visibilità, con supporto temporale.

    Args:
        client: Neo4j client
        principal: Utente autenticato
        entity_id: ID entità opzionale (None = tutti i fatti visibili)
        at_time: Timestamp opzionale per query "al tempo T" (FR5.3)

    Returns:
        Lista di fatti visibili con proprietà complete
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict]:
        # Costruiamo query con filtro temporale se specificato
        if entity_id:
            if at_time:
                query = """
                MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
                WHERE f.valid_from <= $at_time AND (f.valid_to IS NULL OR f.valid_to > $at_time)
                RETURN f
                """
                params = {"entity_id": entity_id, "at_time": at_time}
            else:
                query = """
                MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
                WHERE f.valid_to IS NULL
                RETURN f
                """
                params = {"entity_id": entity_id}
        else:
            if at_time:
                query = """
                MATCH (f:Fact)
                WHERE f.valid_from <= $at_time AND (f.valid_to IS NULL OR f.valid_to > $at_time)
                RETURN f
                """
                params = {"at_time": at_time}
            else:
                query = """
                MATCH (f:Fact)
                WHERE f.valid_to IS NULL
                RETURN f
                """
                params = {}

        result = tx.run(query, **params)
        facts = []
        for record in result:
            node = record["f"]
            props = _node_to_dict(node)

            # Filtro visibilità
            if is_admin:
                facts.append(props)
            else:
                # Per i fatti, dobbiamo considerare anche la visibilità dell'entità padre
                entity_query = """
                MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
                WHERE f.id = $fact_id
                RETURN e
                """
                entity_record = tx.run(entity_query, fact_id=props["id"]).single()

                if entity_record:
                    entity_props = _node_to_dict(entity_record["e"])
                    visibility = effective_visibility(props, entity_props)
                    if _visibility_filter(visibility, ctx):
                        facts.append(props)
                else:
                    # Fatto senza entità (caso anomalo) - usiamo solo visibilità del fatto
                    visibility = Visibility(
                        is_public=props.get("is_public", False),
                        roles=tuple(props.get("roles", [])),
                        teams=tuple(props.get("teams", [])),
                    )
                    if _visibility_filter(visibility, ctx):
                        facts.append(props)

        return facts

    return _read(client, work)


def query_relations(
    client: Neo4jClient,
    principal: Principal,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Query relazioni RELATES_TO filtrate per visibilità.

    Args:
        client: Neo4j client
        principal: Utente autenticato
        entity_id: ID entità sorgente

    Returns:
        Lista di relazioni visibili con source_id, target_id, proprietà
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> list[dict]:
        query = """
        MATCH (e:Entity {id: $entity_id})-[r:RELATES_TO]->(target:Entity)
        RETURN r, target
        """
        result = tx.run(query, entity_id=entity_id)
        relations = []

        for record in result:
            rel = record["r"]
            target = record["target"]

            rel_props = dict(rel)
            rel_props["source_id"] = entity_id
            rel_props["target_id"] = target["id"]
            rel_props["valid_to"] = rel_props.get("valid_to", None)
            rel_props = _jsonable(rel_props)

            # Filtro visibilità: controlliamo sia l'entità sorgente che quella target
            if is_admin:
                relations.append(rel_props)
            else:
                # Controlliamo visibilità dell'entità target
                target_props = _node_to_dict(target)
                visibility = Visibility(
                    is_public=target_props.get("is_public", False),
                    roles=tuple(target_props.get("roles", [])),
                    teams=tuple(target_props.get("teams", [])),
                )
                if _visibility_filter(visibility, ctx):
                    relations.append(rel_props)

        return relations

    return _read(client, work)


# Full-text indexes used by search (WP-E2). The first four are created in
# db/neo4j/001 and 003; the last two in db/neo4j/002.
FULLTEXT_ENTITY_LABEL = "entity_label_fulltext"
FULLTEXT_ENTITY_TYPE = "entity_type_fulltext"
FULLTEXT_FACT_VALUE = "fact_value_fulltext"
FULLTEXT_FACT_PROPERTY = "fact_property_fulltext"
FULLTEXT_DOCUMENT_TITLE = "document_title_fulltext"
FULLTEXT_CANONICAL_TERM_LABEL = "canonical_term_label_en_fulltext"

_SEARCH_INDEXES = (
    FULLTEXT_ENTITY_LABEL,
    FULLTEXT_ENTITY_TYPE,
    FULLTEXT_FACT_VALUE,
    FULLTEXT_FACT_PROPERTY,
    FULLTEXT_DOCUMENT_TITLE,
    FULLTEXT_CANONICAL_TERM_LABEL,
)


def _lucene_escape(text: str) -> str:
    """Escape Lucene special characters so user text is treated literally."""
    for char in "\\+-&|!(){}[]^\"~*?:/":
        text = text.replace(char, "\\" + char)
    return text


def _fulltext_query(text: str) -> str:
    """Build a Lucene wildcard query preserving the old CONTAINS semantics.

    ``*text*`` matches substrings (case-insensitive, analyzer-normalized).
    Leading wildcards are supported by the Neo4j/Lucene full-text index.
    """
    return f"*{_lucene_escape(text)}*"


def _visibility_from_props(props: dict[str, Any]) -> Visibility:
    """Read concrete visibility from a node property dict (default-deny)."""
    return Visibility(
        is_public=bool(props.get("is_public", False)),
        roles=tuple(props.get("roles") or ()),
        teams=tuple(props.get("teams") or ()),
        tenant=props.get("tenant"),
    )


def _fulltext_records(
    tx: ManagedTransaction, index: str, query_text: str
) -> list[tuple[Any, float]]:
    """Run a full-text query and return ``(node, score)`` pairs."""
    result = tx.run(
        """
        CALL db.index.fulltext.queryNodes($index, $query_text)
        YIELD node, score
        RETURN node, score
        """,
        index=index,
        query_text=query_text,
    )
    return [(record["node"], float(record["score"])) for record in result]


def search(
    client: Neo4jClient,
    principal: Principal,
    text: str,
    label: str | None = None,
    *,
    await_indexes: bool = False,
) -> list[dict[str, Any]]:
    """Ricerca full-text su Entity, Fact, Document e CanonicalTerm (FR3.5).

    WP-E2: sostituisce i CONTAINS con gli indici full-text Neo4j
    (``entity_label_fulltext``, ``entity_type_fulltext``,
    ``fact_value_fulltext``, ``fact_property_fulltext``,
    ``document_title_fulltext``, ``canonical_term_label_en_fulltext``).
    La query Lucene ``*text*`` conserva la semantica substring del vecchio
    CONTAINS, ma in modo case-insensitive e indicizzato.

    Args:
        client: Neo4j client
        principal: Utente autenticato
        text: Testo da cercare
        label: Filtro opzionale per label dell'entità
        await_indexes: se True, attende gli indici full-text prima della query
            (utile nei test subito dopo una scrittura; in produzione gli indici
            sono eventually-consistent e non si paga l'attesa).

    Returns:
        Lista di entità, fatti, documenti e termini che corrispondono.
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]
    query_text = _fulltext_query(text)

    def work(tx: ManagedTransaction) -> list[dict]:
        if await_indexes:
            for index in _SEARCH_INDEXES:
                tx.run("CALL db.awaitIndex($index, 5)", index=index)

        results: list[dict[str, Any]] = []

        # --- Entity: label + type (full-text) ---
        seen_entities: set[str] = set()
        for index in (FULLTEXT_ENTITY_LABEL, FULLTEXT_ENTITY_TYPE):
            for node, score in _fulltext_records(tx, index, query_text):
                props = _node_to_dict(node)
                if label is not None and props.get("label") != label:
                    continue
                if props["id"] in seen_entities:
                    continue
                if is_admin or _visibility_filter(_visibility_from_props(props), ctx):
                    seen_entities.add(props["id"])
                    results.append({**props, "match_type": "entity", "score": score})

        # --- Fact: value + property (full-text) ---
        seen_facts: set[str] = set()
        for index in (FULLTEXT_FACT_VALUE, FULLTEXT_FACT_PROPERTY):
            for node, score in _fulltext_records(tx, index, query_text):
                props = _node_to_dict(node)
                if props["id"] in seen_facts:
                    continue
                if is_admin:
                    seen_facts.add(props["id"])
                    results.append({**props, "match_type": "fact", "score": score})
                    continue
                entity_record = tx.run(
                    """
                    MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
                    WHERE f.id = $fact_id
                    RETURN e
                    """,
                    fact_id=props["id"],
                ).single()
                if entity_record is None:
                    continue
                entity_props = _node_to_dict(entity_record["e"])
                visibility = effective_visibility(props, entity_props)
                if _visibility_filter(visibility, ctx):
                    seen_facts.add(props["id"])
                    results.append({**props, "match_type": "fact", "score": score})

        # --- Document: title (full-text, WP-E2) ---
        for node, score in _fulltext_records(tx, FULLTEXT_DOCUMENT_TITLE, query_text):
            props = _node_to_dict(node)
            if is_admin or _visibility_filter(_visibility_from_props(props), ctx):
                results.append({**props, "match_type": "document", "score": score})

        # --- CanonicalTerm: label_en (full-text, WP-E2) ---
        for node, score in _fulltext_records(
            tx, FULLTEXT_CANONICAL_TERM_LABEL, query_text
        ):
            props = _node_to_dict(node)
            if is_admin or _visibility_filter(_visibility_from_props(props), ctx):
                results.append({**props, "match_type": "term", "score": score})

        return results

    return _read(client, work)


def get_entity_with_history(
    client: Neo4jClient,
    principal: Principal,
    entity_id: str,
) -> dict[str, Any] | None:
    """Ottieni entità con storico versioni (catena VERSION_OF).

    Args:
        client: Neo4j client
        principal: Utente autenticato
        entity_id: ID entità

    Returns:
        Dict con entity (versione corrente) e history (lista versioni precedenti)
        o None se l'entità non esiste o non è visibile
    """
    ctx = principal_visibility_context(principal)
    is_admin = ctx["is_admin"]

    def work(tx: ManagedTransaction) -> dict | None:
        # Ottieni entità corrente
        entity_record = tx.run(
            "MATCH (e:Entity {id: $entity_id}) RETURN e",
            entity_id=entity_id,
        ).single()

        if not entity_record:
            return None

        entity_props = _node_to_dict(entity_record["e"])

        # Filtro visibilità entità
        if not is_admin:
            visibility = Visibility(
                is_public=entity_props.get("is_public", False),
                roles=tuple(entity_props.get("roles", [])),
                teams=tuple(entity_props.get("teams", [])),
            )
            if not _visibility_filter(visibility, ctx):
                return None

        # Ottieni fatti correnti
        facts_query = """
        MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
        WHERE f.valid_to IS NULL
        RETURN f
        """
        facts_result = tx.run(facts_query, entity_id=entity_id)
        current_facts = []

        for record in facts_result:
            fact_props = _node_to_dict(record["f"])

            if is_admin or _visibility_filter(
                effective_visibility(fact_props, entity_props),
                ctx,
            ):
                current_facts.append(fact_props)

        # Semplificazione: otteniamo tutti i fatti versionati per questa entità
        version_query = """
        MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
        OPTIONAL MATCH (f)-[:VERSION_OF]->(prev:Fact)
        RETURN f, prev
        """
        version_result = tx.run(version_query, entity_id=entity_id)

        history = []
        for record in version_result:
            current_fact = _node_to_dict(record["f"])
            prev_fact = record.get("prev")

            if prev_fact:
                prev_props = _node_to_dict(prev_fact)
                # Filtra storico per visibilità
                if is_admin or _visibility_filter(
                    effective_visibility(prev_props, entity_props),
                    ctx,
                ):
                    history.append({
                        "current": current_fact,
                        "previous": prev_props,
                    })

        return {
            "entity": entity_props,
            "facts": current_facts,
            "history": history,
        }

    return _read(client, work)


def localize_response(
    response_data: dict[str, Any] | list,
    lang: str,
) -> dict[str, Any] | list:
    """Localizza la risposta per FR9 (multilingua).

    Per il prototipo (iterazione 1):
    - lang == "en": nessun flag se translation_state="native"; flag
      untranslated=True se "pending" (rappresentazione EN non ancora pronta)
    - lang == source_language: contenuto servito nativamente, nessun flag
    - altri lang con translation_state="pending": flag untranslated=True
    - La traduzione vera è WP4/LLM

    Args:
        response_data: Dati da localizzare (dict o lista)
        lang: Codice lingua (es. "fr", "de", "es", "it")

    Returns:
        Dati con annotazione untranslated se applicabile
    """
    def process_item(item: dict) -> dict:
        if not isinstance(item, dict):
            return item

        result = dict(item)

        # FR9.1: la rappresentazione canonica e' inglese. Se non e' ancora
        # pronta (translation_state=pending), l'utente inglese va avvisato.
        lang_l = (lang or "").lower()
        source_lang = (item.get("source_language") or "").lower()
        state = item.get("translation_state", "native")

        if lang_l in ("en", "eng", "english"):
            if state == "pending":
                result["untranslated"] = True
            return result

        # FR9.3: contenuto disponibile nativamente nella lingua dell'utente
        if lang_l == source_lang:
            return result

        # Traduzione verso la lingua richiesta non disponibile
        if state == "pending":
            result["untranslated"] = True

        return result

    if isinstance(response_data, list):
        return [process_item(item) for item in response_data]
    elif isinstance(response_data, dict):
        return process_item(response_data)

    return response_data
