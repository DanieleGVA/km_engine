// ============================================================================
// km_engine — Neo4j domain schema (Iterazione A, WP-A4)
// Baseline: spec-iterazione-A-domain-layer.md §4 · architecture.md §4.1
// Idempotente: tutte le istruzioni usano IF NOT EXISTS (ri-esecuzione sicura).
// NOTA: constraint di esistenza proprietà e node-key sono Enterprise-only;
//       qui si usano solo uniqueness constraint e indici (Community-safe).
//       La completezza delle proprietà è responsabilità dello storage layer
//       Python (ADR-001 D1), che resta l'unico writer del grafo.
// ============================================================================

// ----------------------------------------------------------------------------
// 1. MODELLO DATI (riferimento — spec A §4)
// ----------------------------------------------------------------------------
// :Document      { id, title, lang, source_lang, canonical_hash,
//                  verification_level, translation_state, source_language,
//                  embedding, is_public, roles, teams }
//                  // embedding: vettore 384-dim per retrieval (WP-B1);
//                  // is_public/roles/teams: visibilità P4 (default-deny)
// :CanonicalTerm { id, namespace, term_id, label_en, label_it, definition,
//                  ontology_uri, is_public, roles, teams }
// :DomainPack    { id, name, version, language, canonical_language }
//
// Relazioni:
//   (:Document)-[:PART_OF_PACK]->(:DomainPack)
//   (:Document)-[:NORMALIZED_TO]->(:CanonicalTerm)   // entità normalizzata
//   (:Entity)-[:NORMALIZED_TO]->(:CanonicalTerm)
//   (:Entity)-[:PART_OF_DOC]->(:Document)
//
// Visibilità P4: :Document e :CanonicalTerm ereditano default-deny
// (is_public/roles/teams); il filtro è applicato SEMPRE nel query engine
// tramite principal_visibility_context (app/auth/__init__.py).

// ----------------------------------------------------------------------------
// 2. VINCOLI DI UNICITÀ (id come chiave applicativa di ogni label)
// ----------------------------------------------------------------------------
CREATE CONSTRAINT document_id_unique      IF NOT EXISTS FOR (d:Document)      REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT canonical_term_id_unique IF NOT EXISTS FOR (t:CanonicalTerm) REQUIRE t.id IS UNIQUE;
CREATE CONSTRAINT domain_pack_id_unique    IF NOT EXISTS FOR (p:DomainPack)    REQUIRE p.id IS UNIQUE;

// ----------------------------------------------------------------------------
// 3. INDICI DI SUPPORTO ALLE QUERY
// ----------------------------------------------------------------------------
// Document: filtri per lingua, stato traduzione e livello di verifica
CREATE INDEX document_lang_idx                IF NOT EXISTS FOR (d:Document) ON (d.lang);
CREATE INDEX document_source_lang_idx         IF NOT EXISTS FOR (d:Document) ON (d.source_lang);
CREATE INDEX document_verification_level_idx  IF NOT EXISTS FOR (d:Document) ON (d.verification_level);
CREATE INDEX document_translation_state_idx   IF NOT EXISTS FOR (d:Document) ON (d.translation_state);

// CanonicalTerm: lookup per namespace e term_id (glossario, WP-B2)
CREATE INDEX canonical_term_namespace_idx IF NOT EXISTS FOR (t:CanonicalTerm) ON (t.namespace);
CREATE INDEX canonical_term_term_id_idx   IF NOT EXISTS FOR (t:CanonicalTerm) ON (t.term_id);

// DomainPack: lookup per nome
CREATE INDEX domain_pack_name_idx IF NOT EXISTS FOR (p:DomainPack) ON (p.name);

// ----------------------------------------------------------------------------
// 4. INDICI FULL-TEXT (FR3.5, ricerca su titolo documento e label EN termine)
// ----------------------------------------------------------------------------
CREATE FULLTEXT INDEX document_title_fulltext IF NOT EXISTS
  FOR (d:Document) ON EACH [d.title];
CREATE FULLTEXT INDEX canonical_term_label_en_fulltext IF NOT EXISTS
  FOR (t:CanonicalTerm) ON EACH [t.label_en];

// ----------------------------------------------------------------------------
// 5. INDICE VETTORIALE (retrieval ibrido, WP-B1)
// ----------------------------------------------------------------------------
// VERIFICA EMPIRICA (2026-08-30): Neo4j 5.26.30 Community SUPPORTA gli indici
// vettoriali. L'indice qui sotto è stato applicato sul container km-neo4j e
// risulta ONLINE in SHOW INDEXES; db.index.vector.queryNodes funziona con
// vettori a 384 dimensioni (test attivo in tests/domain/test_ia4_vector_index.py).
// Nessun fallback necessario: il criterio è "comportamento documentato e stabile".
CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS
  FOR (d:Document) ON (d.embedding)
  OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'COSINE'}};

// ----------------------------------------------------------------------------
// 6. VERIFICA (eseguire dopo l'applicazione)
// ----------------------------------------------------------------------------
// SHOW CONSTRAINTS;
// SHOW INDEXES;
