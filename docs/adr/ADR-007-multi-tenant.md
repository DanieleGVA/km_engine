# ADR-007 — Multi-tenant: Document tenant-scoped, glossari condivisi

**Numero:** ADR-007
**Titolo:** Isolamento multi-tenant su Document/CanonicalTerm via claim `tenant`; glossari condivisi di default con termini privati opzionali
**Data:** 2026-08-30
**Stato:** Accepted (Iterazione E, WP-E5/GE5)

## Status

Accettato. Estende ADR-001 D4 (visibilità) e ADR-002 D3 (tenant scope) per il
knowledge layer dell'Iterazione A. Non modifica i contratti API esistenti.

## Context

L'Iterazione E introduce il multi-tenant (roadmap §5, WP-E5). Il prototipo MVP
aveva un tenant unico `default` (ADR-002 D3) e visibilità `{is_public, roles,
teams}` su Entity/Fact (ADR-001 D4). Con l'aggiunta di `:Document` e
`:CanonicalTerm` (ADR-004) serve decidere:

1. come isolare i documenti tra tenant;
2. se i glossari (CanonicalTerm) sono per-tenant o condivisi.

Vincoli: app layer stateless, claim `tenant` già presente nel JWT, filtro
visibilità già centralizzato in `app/storage/visibility.is_visible`.

## Decision

### D1 — `tenant` come dimensione di visibilità

- `Visibility` acquisisce la dimensione opzionale `tenant: str | None`.
- `principal_visibility_context` espone `tenant` dal claim del Principal.
- `is_visible` applica la regola: **se `visibility.tenant` è valorizzato, il
  tenant del principal deve coincidere** (dopo il bypass admin/editor).
  `tenant=None` = contenuto condiviso/non tenant-scoped (comportamento
  pre-E5 invariato).
- L'ereditarietà Fact→Entity include `tenant` (esplicito vince su ereditato),
  come già per `is_public`/`roles`/`teams`.

### D2 — Document tenant-scoped, glossari condivisi di default

- **Document**: `tenant` valorizzato al tenant proprietario. Un documento
  `is_public=true` è pubblico **solo dentro il proprio tenant**, non
  cross-tenant.
- **CanonicalTerm (glossari)**: condivisi di default (`tenant=None`). I
  glossari sono dati di riferimento dell'ontologia (P4/P5) e vengono
  riusati tra tenant per evitare duplicazione e deriva semantica.
- Un tenant può creare **termini privati** impostando `tenant` sul singolo
  `CanonicalTerm`; in tal caso il termine è visibile solo a quel tenant.

### D3 — Limite noto RAG

Il retrieval vettoriale (`app/rag/rag.py`) è fuori scope per questa modifica
(vincolo di iterazione: `app/rag/*` non modificato). Il filtro tenant è attivo
sulle letture documentali/glossario (`app/query/domain.py`) e sul search
full-text (`app/query/engine.py`). Il RAG va allineato al filtro tenant in un
work package dedicato prima di abilitare il multi-tenant in produzione.

## Alternatives considered

| Alternativa | Perché scartata |
|---|---|
| Glossari per-tenant | Duplica l'ontologia, aumenta il carico di curation e rompe il riuso cross-tenant; nessun requisito lo impone. |
| Tenant come colonna fisica su tutte le tabelle Postgres | Non necessario: il grafo è la fonte di verità dei contenuti; il tenant vive nel claim JWT e nelle proprietà dei nodi. |
| Isolamento solo a livello API | Fragile: ogni nuova query dovrebbe ricordarsi del filtro; centralizzarlo in `is_visible` è l'unico punto di enforcement. |

## Consequences

**Positive:**
- Un solo punto di enforcement (`is_visible`) per tenant/ruoli/team.
- Glossari condivisi = ontologia coerente e curation centralizzata.
- Nessuna rottura API: i client continuano a inviare lo stesso JWT.

**Negative / costi:**
- Il RAG non è ancora tenant-aware (limite noto, vedi D3).
- I documenti pubblici non sono cross-tenant: scelta deliberata, da
  comunicare agli stakeholder.

**Rischi e mitigazioni:**
- *Dimenticare `tenant` in una nuova query*: test dedicati in
  `tests/query/test_tenant_domain.py` + review del query layer.
- *RAG che espone documenti cross-tenant*: finché il multi-tenant non è
  attivo in produzione, il RAG resta usato solo nel tenant `default`; il
  work package di allineamento è tracciato come punto aperto.

---

**Punti aperti:**
1. Allineare `app/rag/rag.py` al filtro tenant (work package dedicato).
2. Migrazione dei tenant esistenti: policy di backfill del campo `tenant`
   sui Document già estratti.
3. Se servono glossari condivisi ma con override per-tenant (es. label
   localizzate), valutare un modello `CanonicalTerm` condiviso + attributi
   per-tenant.

*Correlati: ADR-001 (visibilità), ADR-002 (tenant claim), ADR-004 (domain
layer), `app/storage/visibility.py`, `app/query/domain.py`.*
