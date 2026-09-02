-- ============================================================================
-- 006 — candidati sulle proposte di glossario (WP-F4)
--
-- Additiva e retro-compatibile: la colonna e' NULL sulle righe esistenti.
-- Quando la risoluzione a livelli non trova nulla (GLOSS-UNRESOLVED) mette
-- qui i termini di glossario piu' vicini con il loro punteggio, cosi' chi
-- lavora la coda (WP-F5) vede subito se si tratta di un alias mancante o di
-- una voce nuova.
--
-- Forma: [{"key": "brodo di pesce", "score": 0.43}, ...]
-- ============================================================================
BEGIN;

ALTER TABLE glossary_proposals
    ADD COLUMN IF NOT EXISTS candidates JSONB;

COMMIT;

-- ============================================================================
-- VERIFICA (dopo l'applicazione):
--   SELECT column_name, data_type FROM information_schema.columns
--    WHERE table_name = 'glossary_proposals' AND column_name = 'candidates';
-- ============================================================================
