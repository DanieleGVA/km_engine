-- ============================================================================
-- km_engine — Neo4j 5.x — 005_canon_component.cypher
-- Passo 11 PROGRAMMA-UNICO: decomposizione del canone in componenti.
-- Idempotente: CREATE CONSTRAINT/INDEX IF NOT EXISTS.
-- ============================================================================

// CanonComponent: un componente citabile di una ricetta (proteina, salsa,
// contorno, bagna, crust...), ancorato al documento d'origine.
CREATE CONSTRAINT canon_component_id_unique IF NOT EXISTS
  FOR (c:CanonComponent) REQUIRE c.id IS UNIQUE;

CREATE INDEX canon_component_source_idx IF NOT EXISTS
  FOR (c:CanonComponent) ON (c.source_document);

CREATE INDEX canon_component_label_idx IF NOT EXISTS
  FOR (c:CanonComponent) ON (c.label);
