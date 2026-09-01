-- ============================================================================
-- km_engine — PostgreSQL 16 — 005_resume.sql
-- Passo 16 (hardening batch reale): resume idempotente per componente.
-- Aggiunge `section` a canon_adjudication_log per dedup (document_id, section)
-- e indice per il check di resume in run_e2e_batch.
-- Idempotente: ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
-- ============================================================================

BEGIN;

ALTER TABLE canon_adjudication_log ADD COLUMN IF NOT EXISTS section TEXT;

CREATE INDEX IF NOT EXISTS idx_canon_adjudication_log_doc_section
    ON canon_adjudication_log (document_id, section);

COMMIT;
