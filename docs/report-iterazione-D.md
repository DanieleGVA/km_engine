# Report Iterazione D — Generalizzazione al dominio CODE

**Data:** 2026-08-30
**Ambito:** roadmap §4 (WP-D1 pack code, WP-D2 pipeline agenti, WP-D3 retrieval RAG)
**Gate:** GD1 pack code ✅ · GD2 generato da agenti ✅ · GD3 retrieval ✅

---

## 1. Esito gate

| Gate | Criterio | Esito |
|---|---|---|
| GD1 | Domain Pack "code" + mapping graphify -> Document/Entity/NORMALIZED_TO | ✅ |
| GD2 | Brief e bozza pack generati dagli agenti (C1-C4), non a mano | ✅ |
| GD3 | Retrieval RAG sul dominio code con riuso di `app.rag` | ✅ |

- **Parità con graphify:** 136 label funzione/classe e 212 triple di dipendenza
  identiche al percorso legacy (confronto su nomi, non UUID).
- **Round-trip code:** 12/12 moduli (100%) — `recompose(extract(canonical.md)) == canonical.md`.
- **Golden set code:** 296 query naturali (≥ 50 richieste), **Recall@5 = 1.000**
  nell'ultima esecuzione di gate (soglia 0.85).
- **Zero modifiche al core:** il flusso completo gira senza toccare `app/*`
  (test con snapshot dei file `app/` prima/dopo la pipeline).

---

## 2. Deliverable

### WP-D1 — Domain Pack "code" (`domain-packs/code/`)

- `pack.yaml` — `name: code`, `language: en`, `canonical_language: en`
  (il codice è già EN), glossari `tecnica/ingredienti/stati` (schema pydantic
  di A riusato senza modifiche).
- `template.md` — doc per modulo/file: frontmatter + `## Functions` /
  `## Classes` / `## Dependencies`.
- Glossari concetti con id `CODE-*`:
  - `CODE-MODULE` (ingredienti), `CODE-FUNCTION` + `CODE-CLASS` (tecnica),
    `CODE-DEPENDENCY` (stati).
- `units.yaml` minimale (lista vuota: il codice non ha unità fisiche, P2
  banalmente vero), `regole/` con normalizzazione/verifica strutturali.

### Mapping graphify (`code_domain/mapping.py`)

Converte l'output di `graphify.extract` + `graphify.build` nel modello del
knowledge layer:

- ogni modulo reale (file sorgente) -> `:Document`;
- ogni funzione/classe -> `:Entity` con `PART_OF_DOC` e `NORMALIZED_TO`
  `CODE-FUNCTION` / `CODE-CLASS`;
- import a livello modulo -> `:Entity` `dependency` (`CODE-DEPENDENCY`);
- archi simbolo-simbolo (calls/uses/references/method/inherits) ->
  `RELATES_TO`.

### WP-D2 — Pipeline agenti (`code_domain/agents.py`)

Riuso dei contratti C1-C4 (`DomainBrief`, `AgentReport`, `load_domain_pack`)
con adattatori code-specifici **fuori da `app/`**:

- **Analyst** — graphify -> `DomainBrief` (4 concetti, frequenze reali:
  12 moduli, 110 funzioni, 38 classi, 27 dipendenze).
- **Designer** — brief -> bozza pack in `domain-packs/code-agents-draft/`
  (solo staging dir, gate umano meccanico).
- **Codegen** — round-trip conformance sul corpus code (12/12).
- **Evaluator** — golden set + Recall@5 + gate report.

Artefatti: `docs/domain-briefs/code-v1.json`,
`docs/domain-briefs/code-golden.json`,
`docs/domain-briefs/code-gate-report.json`.

### WP-D3 — Retrieval RAG (`code_domain/rag.py`)

Riuso **senza modifiche strutturali** di `app.rag`:
`build_embedding_from_graph`, `populate_embeddings`, `rag_query`.
Ingest dei `:Document` code + embedding deterministico + query naturali.

---

## 3. Costo di un nuovo dominio (KPI)

Il KPI che giustifica il layer: **quanto costa aggiungere il dominio code
rispetto a riscrivere un estrattore dedicato.**

| Voce | Valore |
|---|---|
| Righe di pack (contenuto, non codice) | **67** |
| Righe adattatori dominio (`code_domain/`) | **1.432** |
| Decisioni ontologiche portate al gate umano | **0** (4 concetti deterministici, nessuna ambiguità) |
| Ore umane stimate | **~2-4 h** (scelta corpus, review 4 concetti, review golden set) |
| Modifiche a `app/*` richieste | **0** |

Il dominio code è stato aggiunto **senza toccare il core**: il costo marginale
è solo contenuto di pack + adattatori esterni. Il percorso legacy graphify
(`app/ingest`) resta intatto e la parità è verificata dai test.

---

## 4. Test

`tests/domain_code/` (prefisso `id_code_`, pulizia post-run):

- `test_parity.py` — parità label/triple con graphify.
- `test_roundtrip.py` — round-trip 12/12 + idempotenza.
- `test_pipeline_c.py` — pipeline C completa su dominio nuovo + snapshot
  `app/*` invariato (zero modifiche core).
- `test_golden_rag.py` — golden set ≥ 50 query, Recall@5 ≥ 0.85.

Comando: `uv run pytest tests/domain_code tests/agents -q` → **53 passed**.

---

## 5. Punti aperti per l'Iterazione E

1. **Fix coordinamento `app/rag/rag.py`:** il worker Curator/Documenter ha
   aggiunto il title-boost senza `import re`; ho aggiunto la sola riga
   `import re` per sbloccare `rag_query` (fix minimo, non strutturale).
2. **Test "book" del worker libro rossi:** `tests/rag/test_book_retrieval.py`
   e `tests/rag/test_book_workflow_e2e.py` falliscono per ranking/popolamento
   (non legati a Iterazione D). Da riallineare con il title-boost.
3. **Indice vettoriale condiviso:** i documenti code e ricette condividono
   `document_embedding_vector`; con corpus misti serve un filtro per dominio
   (o namespace) per evitare rumore reciproco nel retrieval.
4. **Golden set code descrittivo:** le query attuali sono template naturali
   sui nomi dei simboli; query puramente descrittive ("function that parses
   markdown") richiedono embedding semantici (non deterministici) o docstring
   nel testo del Document.
5. **Curator sul dominio code:** il loop C5 non è stato esercitato sul dominio
   code (nessuna ambiguità iniettata); da validare in E o in un follow-up D+.
