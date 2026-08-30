// ============================================================================
// km_engine — Neo4j full-text extension (Iterazione E, WP-E2/GE2)
// Baseline: db/neo4j/001 (Entity.label, Fact.value) + 002 (Document.title,
//            CanonicalTerm.label_en). Questo file aggiunge gli indici full-text
//            mancanti per eliminare del tutto i CONTAINS dal search engine.
// Idempotente: CREATE FULLTEXT INDEX ... IF NOT EXISTS (ri-esecuzione sicura).
// ============================================================================

// Entity.type: il search engine cercava anche il tipo entità con CONTAINS.
CREATE FULLTEXT INDEX entity_type_fulltext IF NOT EXISTS
  FOR (e:Entity) ON EACH [e.type];

// Fact.property: il search engine cercava anche la proprietà del fatto con
// CONTAINS. L'indice su Fact.value esiste già in 001 (fact_value_fulltext).
CREATE FULLTEXT INDEX fact_property_fulltext IF NOT EXISTS
  FOR (f:Fact) ON EACH [f.property];

// ----------------------------------------------------------------------------
// VERIFICA (dopo l'applicazione)
// ----------------------------------------------------------------------------
// SHOW INDEXES YIELD name, type, state WHERE name IN
//   ['entity_type_fulltext', 'fact_property_fulltext'];
// ============================================================================
