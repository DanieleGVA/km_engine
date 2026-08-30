# Report Iterazione A — Domain Knowledge Layer (ricette)

**Data:** 2026-08-30 · **Stato:** COMPLETATA (gate GA1–GA6 verdi)
**Riferimenti:** `roadmap-iterazioni-e-test.md`, `spec-iterazione-A-domain-layer.md`, `ADR-004`

## Deliverable

| WP | Consegna | Gate |
|---|---|---|
| A1 | Domain Pack ricette: pack.yaml, template.md, units.yaml (16 regole), glossari (55 ING + 6 TEC + 5 STA), regole verifica | GA1 ✅ |
| A2 | Stadio 1 traduzione P2-safe: LLMClient (Http reale + Fake deterministico), segnaposto numerici, invariante multiset | GA2 ✅ |
| A3 | Verifica L1/L2/L3: parser template, invarianti numerici, overlap sezioni, coda adjudication + audit | GA3 ✅ |
| A4 | Schema Neo4j domain: Document/CanonicalTerm/DomainPack, fulltext, **indice vettoriale 384d**, visibilità default-deny, bootstrap idempotente | GA4 ✅ |
| A5 | Stadio 2 canonicalizzazione deterministica + canon-log verificabile + coda proposte | GA5 ✅ |
| A6 | Estrattore md→grafo + ricompositore + round-trip + E2E | GA6 ✅ |

## Metriche

| Metrica | Valore | Criterio roadmap | Esito |
|---|---|---|---|
| Round-trip `recompose(extract(md)) == md` | **15/15 ricette byte-identiche** (T11) | 100% corpus | ✅ |
| E2E flusso completo (T12) | PASS (bootstrap→load→translate→verify→canonicalize→extract→query→recompose) | 1 test stabile | ✅ |
| Copertura glossario (mention ingredienti) | **94.6%** (88/93) | ≥85% prima di scalare | ✅ |
| Termini irrisolti (coda proposte) | 5: sweet/bitter almonds sbucciate, butter fuso, chicken intero (1.2 kg), zucchini medie | tracciati, mai inventati | ✅ |
| Invariante canon-log (T9) | 100% diff spiegati (verifica bidirezionale) | 100% | ✅ |
| Conversioni unità (T2) | 16/16 regole, Decimal esatto, arrotondamento half-up, rule_id presente | tutte + limiti | ✅ |
| Invarianti P2 (T3) | pass su tutto il corpus (incl. corruzioni sintetiche intercettate) | nessun numero alterato | ✅ |
| Verifica L1 (T4) | corruzioni intercettate (numero alterato, ingrediente rimosso, step aggiunto) | ogni corruzione | ✅ |
| Verifica L2 (T5) | divergenze localizzate per sezione + escalation L3 | localizzazione | ✅ |
| Coda L3 + adjudication (T6) | workflow approve/reject + audit + frontmatter | completo | ✅ |
| Bootstrap idempotente (T7) | doppio load → zero duplicati | stabile | ✅ |
| Visibilità Document/CanonicalTerm (T8) | default-deny, team, public, admin, fulltext filtrato | P4 | ✅ |
| Test suite | **292 passed + 1 skip** (MVP 207 + iterazione A 85) | nessuna regressione | ✅ |
| Copertura / lint | ruff pulito | standard repo | ✅ |

## Note

- **"farina 00"**: incoerenza mask/extract numeri trovata da A6 e **fissata in
  numbers.py** (all-zero non mascherato → ora "farina 00" risolve a
  ING-WHEAT-FLOUR). Nessuna regressione.
- FoodOn URI nei glossari: seed da validare con l'esperto di dominio (P5) prima
  della scala B3.
- Embedding dei :Document non ancora popolati → WP-B1 (indice vettoriale già ONLINE).

## Uscita verso Iterazione B

1. WP-B1 retrieval ibrido: embedding + vettoriale + fulltext + espansione grafo +
   filtro visibilità post-retrieval; endpoint POST /api/v1/rag/query.
2. WP-B2 query strutturate dal glossario (CanonicalTerm ← NORMALIZED_TO ← Entity
   → PART_OF_DOC → Document).
3. WP-B3 scala corpus (~1650 ricette): soglia copertura glossario monitorata.
4. WP-B4 localizzazione risposte estesa ai Document.
5. WP-B5 performance (cache, tuning, NFR1 su stack prod-like).
