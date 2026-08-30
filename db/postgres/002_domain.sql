-- ============================================================================
-- km_engine — PostgreSQL 16 — 002_domain.sql
-- Iterazione A, WP-A3: code L3 (adjudication) e proposte glossario (WP-A5).
-- Idempotente: CREATE TABLE IF NOT EXISTS (ri-esecuzione sicura).
-- Riferimenti: spec-iterazione-A-domain-layer.md §3/§5 · roadmap sez. 1.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. ADJUDICATION — coda L3 (verifica semantica divergente)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS adjudications (
    id          BIGSERIAL  PRIMARY KEY,
    document_id TEXT       NOT NULL,                    -- id del documento (es. RIC-001)
    section     TEXT       NOT NULL,                    -- title | ingredients | steps
    reason      TEXT       NOT NULL,                    -- motivo esplicito della divergenza
    suggestion  TEXT,                                   -- suggerimento per l'admin
    status      TEXT       NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'approved', 'rejected')),
    resolved_by UUID REFERENCES users (id) ON DELETE SET NULL,   -- solo admin (Q11)
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_adjudications_status   ON adjudications (status);
CREATE INDEX IF NOT EXISTS idx_adjudications_document ON adjudications (document_id);

-- ----------------------------------------------------------------------------
-- 2. GLOSSARY PROPOSALS — coda termini irrisolti (WP-A5, T10)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS glossary_proposals (
    id          BIGSERIAL  PRIMARY KEY,
    term        TEXT       NOT NULL,                    -- termine irrisolto (mai inventato nel md)
    context     TEXT,                                   -- contesto/documento di provenienza
    status      TEXT       NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'approved', 'rejected')),
    resolved_by UUID REFERENCES users (id) ON DELETE SET NULL,
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_glossary_proposals_status ON glossary_proposals (status);
CREATE INDEX IF NOT EXISTS idx_glossary_proposals_term   ON glossary_proposals (term);

COMMIT;

-- ============================================================================
-- VERIFICA (dopo l'applicazione):
--   \dt
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name IN ('adjudications', 'glossary_proposals') ORDER BY table_name, ordinal_position;
-- ============================================================================
