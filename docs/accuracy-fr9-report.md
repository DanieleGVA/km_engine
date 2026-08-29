# Report Accuratezza FR9 — PDF francese reale, test in inglese

**Data:** 2026-08-29
**Scopo:** saggiare l'accuratezza del flusso multilingue (FR9) con contenuto reale:
PDF pubblico in francese, verifica in inglese.
**Corpus:** Dichiarazione universale dei diritti dell'uomo — PDF ufficiali UN
(`tests/fixtures/udhr/udhr_fr.pdf`, `udhr_en.pdf`), testo estratto con `pdftotext`
(FR 1973 parole, EN 1779 parole).

---

## Test 1 — Rilevamento lingua (euristica `normalize_language`)

| Input | Risultato atteso | Ottenuto |
|---|---|---|
| Testo FR completo | fr / pending | fr / pending ✅ |
| Testo EN completo | en / native | en / native ✅ |
| 19 chunk FR (3 paragrafi) | fr | **19/19 (100%)** ✅ |
| 20 chunk EN (3 paragrafi) | en | **20/20 (100%)** ✅ |

**Accuratezza rilevamento lingua: 100%** su testo reale (euristica deterministica
a parole funzionali + accenti; nessun servizio esterno).

## Test 2 — Flusso FR9 end-to-end (ingestione → grafo → API)

1. **Ingestione** del PDF francese via `IngestPipeline` (job document, prefix `udhr_`):
   - `:Source` → `language=fr`, `type=file` ✅
   - `:Fact` → `translation_state=pending`, `source_language=fr`, `language=en` (canonica) ✅
   - `:Entity` → `translation_state=pending` ✅
2. **API** (`GET /api/v1/entities/{id}/facts`, utente admin, `Accept-Language`):

| Utente | Prima del fix | Dopo il fix (f0a24e2) |
|---|---|---|
| `en` | nessun flag ❌ (contenuto FR spacciato per EN) | `untranslated=True` ✅ (EN non pronta) |
| `fr` | `untranslated=True` ❌ (contenuto nativo flaggato) | nessun flag ✅ (servito nativamente) |
| `de` | `untranslated=True` | `untranslated=True` ✅ (traduzione DE non disponibile) |

**Bug di accuratezza trovato e corretto:** la logica dei flag era invertita
rispetto a FR9.1/FR9.3. Fix in `app/query/engine.py` + test aggiornati
(`tests/api`, `tests/query`) — commit `f0a24e2`.

## Test 3 — Accuratezza traduzione FR→EN (LLM reale vs riferimento ufficiale)

Metodo: child agent `deepseek-v4-pro:cloud` traduce gli articoli 1–5 (testo
francese ufficiale UN); confronto con la traduzione inglese ufficiale UN
(riferimento) — similarità SequenceMatcher, precision/recall token di contenuto,
F1 bigrammi.

| Art. | Similarità | Token P | Token R | Bigram F1 |
|---|---|---|---|---|
| 1 | 0.988 | 1.000 | 1.000 | 1.000 |
| 2 | 0.988 | 1.000 | 1.000 | 1.000 |
| 3 | 1.000 | 1.000 | 1.000 | 1.000 |
| 4 | 0.991 | 1.000 | 1.000 | 1.000 |
| 5 | 0.989 | 1.000 | 1.000 | 1.000 |
| **Media** | **0.991** | **1.000** | **1.000** | **1.000** |

Le uniche differenze sono a capo (layout PDF); il contenuto è identico.
**Caveat:** la DUDU è un testo celebre, probabilmente noto al modello — questo
misura l'accuratezza su testo noto. Per un test più severo servirebbe un
documento francese meno diffuso (iterazione 1: corpus dedicato + metriche BLEU/COMET).

## Conclusioni

1. **Rilevamento lingua: 100%** su testo reale.
2. **Flusso FR9: corretto dopo il fix** — il flag `untranslated` ora riflette
   FR9.1 (EN canonica non pronta → avviso all'utente EN) e FR9.3 (lingua sorgente
   servita nativamente).
3. **Traduzione LLM: ~100% di accuratezza** sul corpus di prova (vs riferimento
   ufficiale), con caveat "testo noto".
4. **Limite noto:** la traduzione vera all'ingestione (FR9.2) è ancora lo stub
   `[EN] ...` — l'adattatore `LLMSemanticService` (KM_LLM_API_KEY) è lo scheletro
   da completare in iterazione 1; il test 3 dimostra che il modello usato dalla
   squadra traduce il francese con accuratezza ~perfetta su questo corpus.

## Artefatti

- `tests/fixtures/udhr/udhr_fr.pdf`, `udhr_en.pdf` (PDF ufficiali UN)
- `tests/fixtures/udhr/udhr_fr.txt`, `udhr_en.txt` (testo estratto)
- `tests/fixtures/udhr/articles_1_5.json` (articoli 1–5 FR + riferimento EN)
- `tests/fixtures/udhr/translation_llm.json` (traduzione LLM)
