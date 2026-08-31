-- ============================================================================
-- km_engine — PostgreSQL 16 — 004_judge.sql
-- Passo 4 PROGRAMMA-UNICO: primitiva judge() + coda dizionario.
-- Idempotente: ALTER TABLE ... ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.
-- Riferimenti: PROGRAMMA-UNICO §3.0 (migrazione unica 004).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. ADJUDICATIONS — estensione per kind/verdict (translation | canon | dictionary)
-- ----------------------------------------------------------------------------

ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS kind          TEXT
    CHECK (kind IN ('translation', 'canon', 'dictionary'));
ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS verdict_json  JSONB;
ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS llm_model     TEXT;
ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS llm_confidence REAL;
ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS candidate_ids TEXT[];

CREATE INDEX IF NOT EXISTS idx_adjudications_kind ON adjudications (kind);

-- ----------------------------------------------------------------------------
-- 2. CANON ADJUDICATION LOG — speculare a canon_log (passo 16: reversibilita')
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS canon_adjudication_log (
    id            BIGSERIAL  PRIMARY KEY,
    document_id   TEXT       NOT NULL,
    kind          TEXT       NOT NULL CHECK (kind IN ('translation', 'canon', 'dictionary')),
    verdict_json  JSONB      NOT NULL,
    llm_model     TEXT,
    llm_confidence REAL,
    candidate_ids TEXT[],
    approved_by   UUID REFERENCES users (id) ON DELETE SET NULL,
    approved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canon_adjudication_log_document ON canon_adjudication_log (document_id);
CREATE INDEX IF NOT EXISTS idx_canon_adjudication_log_kind     ON canon_adjudication_log (kind);

COMMIT;

-- ============================================================================
-- VERIFICA (dopo l'applicazione):
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'adjudications' ORDER BY ordinal_position;
--   \d canon_adjudication_log
-- ============================================================================
