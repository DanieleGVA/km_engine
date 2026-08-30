# ADR-004 — Domain Knowledge Layer (Iterazione A)

**Status:** Accepted (2026-08-30)
**Contesto:** il prototipo km_engine (MVP, ADR-001/002/003) gestisce fatti estratti
ma senza conoscenza di dominio strutturata. La roadmap (iterazioni A–E) introduce un
layer di dominio con IR markdown a due stadi, sotto-grafo canonico, verifica e
round-trip.

## Decisione

1. **Domain Pack come contratto di dominio** (`domain-packs/<dominio>/`): pack.yaml
   + template.md + glossari + units.yaml + regole, validati da schema pydantic.
   Il pack è la sorgente di verità dell'ontologia applicativa (P4/P5).
2. **IR a due stadi**: `translated.md` (traduzione LLM EN, P2-safe con segnaposto
   numerici e re-iniezione verificata) → `canonical.md` (canonicalizzazione
   DETERMINISTICA, mai LLM: unità da units.yaml con Decimal esatto e rule_id,
   termini longest-first dai glossari, irrisolti → coda proposte, mai inventati).
3. **canon-log**: ogni differenza translated↔canonical ha una riga di log
   (campo, prima, dopo, rule_id); invariante bidirezionale verificato da
   `verify_canon_log` (apply(log) ricostruisce byte-identico).
4. **Verifica a 3 livelli**: L1 strutturale/numerica (deterministica), L2 sezioni
   (overlap token deterministico; LLM opzionale), L3 coda adjudication umana con
   audit (approve/reject → frontmatter verification_level).
5. **Grafo**: nodi `:Document`/`:CanonicalTerm`/`:DomainPack` + relazioni
   PART_OF_PACK/NORMALIZED_TO/PART_OF_DOC; visibilità default-deny ereditata
   (principal_visibility_context); **indice vettoriale 384d cosine su
   Document.embedding** (supportato da Neo4j 5.26 Community — verificato).
6. **Round-trip come invariante di qualità**: `recompose(extract(canonical.md))
   == canonical.md` byte-identico; il ricompositore è inversa esatta
   dell'estrattore (Entity type='step' per l'ordine; Document.document_id per le
   chiavi namespaced).
7. **Riuso**: pipeline/storage/auth/query/conflict/invalidation esistenti estese
   in modo additivo; nessuna modifica ai contratti pubblici MVP.

## Alternative considerate

- LLM per la canonicalizzazione: rifiutato (non deterministico, viola P2/P3).
- Termini normalizzati direttamente nel md con id: rifiutato (P3/P5; i md
  canonici contengono label, gli id vivono nel grafo).
- Canon-log nel grafo (`:CanonicalLogEntry`) invece di Postgres: rifiutato per
  semplicità di auditing e query (tabella canon_log in 003_canon_log.sql).
- Indice vettoriale in Community: dubitato, verificato empiricamente e supportato.

## Conseguenze

- Il round-trip garantisce che il grafo restituisce esattamente la conoscenza
  canonica; ogni regressione è catturata da T11 sul corpus intero.
- La copertura glossario va misurata a ogni gate (target ≥85% prima di scalare;
  pilota A = 94.6%).
- I termini irrisolti finiscono in `glossary_proposals` (pending): la crescita
  della coda è il segnale per estendere i glossari (iterazione C, Curator).
- Embedding di :Document da generare nel WP-B1 (l'indice esiste, il valore no).

## Gate coperti

GA1 pack · GA2 traduzione P2-safe · GA3 verifica L1/L2/L3 · GA4 schema+visibilità ·
GA5 canonicalizzazione+canon-log · GA6 round-trip E2E. Tutti verdi.
