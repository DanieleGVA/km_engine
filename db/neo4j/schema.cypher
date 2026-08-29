// ============================================================================
// km_engine — Neo4j schema — Cypher 5.x (testato per Neo4j 5.26, container km-neo4j)
// Baseline: work-plan §2.1 · requirements FR2/FR3/FR5/FR7/FR9 · ADR-001
// Idempotente: tutte le istruzioni usano IF NOT EXISTS (ri-esecuzione sicura).
// NOTA: constraint di esistenza proprietà e node-key sono Enterprise-only;
//       qui si usano solo uniqueness constraint e indici (Community-safe).
//       La presenza delle proprietà obbligatorie è garantita dallo storage
//       layer Python (WP2), che è l'unico writer del grafo (ADR-001 D1).
// ============================================================================

// ----------------------------------------------------------------------------
// 1. MODELLO DATI (riferimento — vedere ADR-001 D2/D3/D4/D5)
// ----------------------------------------------------------------------------
// :Entity  { id, label, type, source_file, source_location, confidence,
//            is_public, roles, teams }                      // visibilità FR2.4
// :Fact     { id, property, value,
//             valid_from, valid_to,                         // tempo di sistema
//             source_valid_from, source_valid_to,           // validità sorgente
//             source_id, author_id, confidence, status }    // provenance FR5.1
// :Source   { id, uri, type, hash, language, ingested_at }  // FR9.4 lingua orig.
// :Version  { id, created_at, author_id, change_type }      // audit nel grafo
//
// Relazioni:
//   (:Entity)-[:HAS_FACT]->(:Fact)
//   (:Entity)-[:RELATES_TO { relation, confidence, status,
//                            valid_from, valid_to,
//                            source_valid_from, source_valid_to,
//                            source_id }]->(:Entity)
//   (:Fact)-[:DERIVED_FROM]->(:Source)      // provenance: fatto -> sorgente
//   (:Fact)-[:VERSION_OF]->(:Fact)          // catena versioni (bitemporale)
//   (:Version)-[:VERSIONS]->(:Entity | :Fact)
//
// Convenzioni bitemporali (ADR-001 D3):
//   valid_to IS NULL            = intervallo di sistema aperto (versione corrente)
//   invalidazione               = scrittura valid_to = now() + status = 'obsolete'
//   update                      = nuovo nodo Fact + (old)-[:VERSION_OF]->(new)
//   DELETE applicativo          = MAI (FR2.3); le versioni non si cancellano
//   status ∈ {'valid','obsolete','under_review'}  (FR7.4)
//   confidence ∈ {'EXTRACTED','INFERRED','AMBIGUOUS'} (FR2.1)

// ----------------------------------------------------------------------------
// 2. VINCOLI DI UNICITÀ (id come chiave applicativa di ogni label)
// ----------------------------------------------------------------------------
CREATE CONSTRAINT entity_id_unique  IF NOT EXISTS FOR (e:Entity)  REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT fact_id_unique    IF NOT EXISTS FOR (f:Fact)    REQUIRE f.id IS UNIQUE;
CREATE CONSTRAINT source_id_unique  IF NOT EXISTS FOR (s:Source)  REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT version_id_unique IF NOT EXISTS FOR (v:Version) REQUIRE v.id IS UNIQUE;

// ----------------------------------------------------------------------------
// 3. INDICI DI SUPPORTO ALLE QUERY
// ----------------------------------------------------------------------------
// Entity: filtri per tipo e per provenance (fatto -> sorgente -> file -> riga, FR5.4)
CREATE INDEX entity_type_idx        IF NOT EXISTS FOR (e:Entity) ON (e.type);
CREATE INDEX entity_source_file_idx IF NOT EXISTS FOR (e:Entity) ON (e.source_file);
CREATE INDEX entity_confidence_idx  IF NOT EXISTS FOR (e:Entity) ON (e.confidence);

// Fact: query temporali (FR3.4) e stato invalidazione (FR7.4)
CREATE INDEX fact_valid_from_idx  IF NOT EXISTS FOR (f:Fact) ON (f.valid_from);
CREATE INDEX fact_status_idx      IF NOT EXISTS FOR (f:Fact) ON (f.status);
CREATE INDEX fact_property_idx    IF NOT EXISTS FOR (f:Fact) ON (f.property);
CREATE INDEX fact_source_id_idx   IF NOT EXISTS FOR (f:Fact) ON (f.source_id);
CREATE INDEX fact_confidence_idx  IF NOT EXISTS FOR (f:Fact) ON (f.confidence);

// Source: ingestione incrementale (hash, FR1.4) e lookup per URI
CREATE INDEX source_uri_idx       IF NOT EXISTS FOR (s:Source) ON (s.uri);
CREATE INDEX source_hash_idx      IF NOT EXISTS FOR (s:Source) ON (s.hash);
CREATE INDEX source_language_idx  IF NOT EXISTS FOR (s:Source) ON (s.language);

// Version: consultazione/confronto storico (FR5.3)
CREATE INDEX version_created_at_idx IF NOT EXISTS FOR (v:Version) ON (v.created_at);
CREATE INDEX version_author_idx     IF NOT EXISTS FOR (v:Version) ON (v.author_id);

// ----------------------------------------------------------------------------
// 4. INDICI SU PROPRIETÀ DI RELAZIONE
// ----------------------------------------------------------------------------
// RELATES_TO bitemporale: query temporali sugli archi (FR3.4)
CREATE INDEX relates_valid_from_idx IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.valid_from);
CREATE INDEX relates_status_idx     IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.status);
CREATE INDEX relates_relation_idx   IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.relation);

// VERSION_OF: navigazione della catena di versioni (FR5.3)
CREATE INDEX version_of_valid_from_idx IF NOT EXISTS FOR ()-[r:VERSION_OF]-() ON (r.valid_from);

// ----------------------------------------------------------------------------
// 5. INDICI FULL-TEXT (FR3.1 ricerca, Q5 full-text di base)
// ----------------------------------------------------------------------------
// Nota (ADR-001 D7): coprono label di Entity e valori di Fact. Il full-text
// integrale dei documenti sorgente è un punto aperto (dove persistere i chunk).
CREATE FULLTEXT INDEX entity_label_fulltext IF NOT EXISTS
  FOR (e:Entity) ON EACH [e.label];
CREATE FULLTEXT INDEX fact_value_fulltext IF NOT EXISTS
  FOR (f:Fact) ON EACH [f.value];

// ----------------------------------------------------------------------------
// 6. VERIFICA (eseguire dopo l'applicazione)
// ----------------------------------------------------------------------------
// SHOW CONSTRAINTS;
// SHOW INDEXES;
