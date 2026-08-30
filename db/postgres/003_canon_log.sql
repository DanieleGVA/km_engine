-- ============================================================================
-- km_engine — PostgreSQL 16 — 003_canon_log.sql
-- Iterazione A, WP-A5: canon-log (tracciamento deterministico translated -> canonical).
-- Idempotente: CREATE TABLE IF NOT EXISTS (ri-esecuzione sicura).
-- Riferimenti: spec-iterazione-A-domain-layer.md §5/Appendice A · roadmap sez. 1 (T9).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- CANON LOG — una riga per ogni differenza translated -> canonical
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS canon_log (
    id          BIGSERIAL  PRIMARY KEY,
    document_id TEXT       NOT NULL,                    -- id del documento (es. RIC-001)
    field       TEXT       NOT NULL,                    -- campo modificato (es. ingredients[0].unit)
    before_text TEXT       NOT NULL,                    -- valore prima ('' = inserimento)
    after_text  TEXT       NOT NULL,                    -- valore dopo  ('' = rimozione)
    rule_id     TEXT       NOT NULL,                    -- regola/termine che spiega la modifica
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canon_log_document ON canon_log (document_id);
CREATE INDEX IF NOT EXISTS idx_canon_log_rule     ON canon_log (rule_id);

COMMIT;

-- ============================================================================
-- VERIFICA (dopo l'applicazione):
--   \dt
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'canon_log' ORDER BY ordinal_position;
-- ============================================================================
