# Squadra Agentica — Riscrittura Graphify per Knowledge Enterprise

**Missione:** riscrivere Graphify per soddisfare i requisiti enterprise (R1–R7) su un knowledge base di ~10GB, ~100 utenti, con profilazione, resilienza, tracciabilità, conflict check e fact invalidation.

**Modelli disponibili (Ollama Cloud):**

| Modello | Parametri | Context | Profilo |
|---|---|---|---|
| `glm-5.3:cloud` | 753B MoE | 1M | Flagship coding, ragionamento complesso |
| `deepseek-v4-pro:cloud` | 1.65T | 1M | Lavoro tecnico profondo, analisi |
| `qwen3.5:cloud` | 397B | 256K | Backend/API, vision |
| `deepseek-v4-flash:cloud` | — | 1M | Veloce/economico, lavoro ripetitivo |

---

## 1. Struttura della squadra

| # | Ruolo | Modello | Requisiti | Responsabilità |
|---|---|---|---|---|
| 1 | **Architetto Software / Tech Lead** | `glm-5.3:cloud` | Tutti (coordinamento) | Architettura target, ADR, contratti di interfaccia, piano di migrazione |
| 2 | **Ingegnere Storage & Dati** | `deepseek-v4-pro:cloud` | R1, R4, R5 | Storage su DB grafo, sharding, modello dati bitemporale, migrazione da graph.json |
| 3 | **Ingegnere Backend & API** | `qwen3.5:cloud` | R2 | Server multi-processo stateless, rate limiting, pooling, evoluzione tool MCP |
| 4 | **Ingegnere Sicurezza & Identità** | `glm-5.3:cloud` | R3 | OIDC/SSO, RBAC/ACL su nodi, tenant isolation, audit log |
| 5 | **Ingegnere Query Engine & Grafo** | `deepseek-v4-pro:cloud` | R6, R7 | Filtro sottografo per visibilità, conflict detection, truth-maintenance, propagazione invalidazione |
| 6 | **DevOps / SRE** | `deepseek-v4-flash:cloud` | R4 | Replica, failover, backup/restore, CI/CD, load testing |
| 7 | **QA / Test Engineer** | `deepseek-v4-flash:cloud` | Trasversale | Strategia test, test migrazione/concorrenza/sicurezza, benchmark |

---

## 2. Modello di coordinamento

```
Fase 0: Architetto → architecture target + ADR + contratti interfaccia
              │
              ▼
Fase 1: Storage layer (Ing. Storage)  ║  Server stateless (Ing. Backend)   ← parallelo
              │                              │
              ▼                              ▼
Fase 2: Auth/RBAC (Ing. Sicurezza)     ║  Query engine (Ing. Grafo)         ← parallelo
              │                              │
              ▼                              ▼
Fase 3: Conflict + invalidation (Ing. Grafo)  ║  HA (SRE)                  ← parallelo
              │
              ▼
Fase 4: QA completo + benchmark + hardening (QA + SRE)
```

**Regole:**
- L'Architetto definisce i **contratti di interfaccia** (ADR) prima che gli ingegneri inizino — nessun ingegnere implementa senza contratto.
- Gli ingegneri lavorano **in parallelo** su moduli isolati (storage, server, auth, query).
- **Review incrociate:** l'Ing. Sicurezza reviewa il lavoro di tutti (ogni modulo tocca identità/permessi).
- Il QA valida ogni fase con test automatici prima del passaggio alla fase successiva.
- Ogni ingegnere produce **ADR + codice + test** per il proprio modulo.

---

## 3. Deliverable per ruolo

### 1. Architetto Software / Tech Lead — `glm-5.3:cloud`
- Architecture target (diagramma componenti: storage, server, auth, query, ingest)
- ADR-001: migrazione da `graph.json` a DB grafo (Neo4j/FalkorDB)
- ADR-002: modello dati bitemporale (`valid_from`/`valid_to`)
- ADR-003: modello di identità e autorizzazione (RBAC/ACL/tenant)
- Contratti di interfaccia tra moduli (schemi API)
- Piano di migrazione incrementale (graphify attuale → target, senza downtime)

### 2. Ingegnere Storage & Dati — `deepseek-v4-pro:cloud`
- Storage layer primario su Neo4j/FalkorDB (inversione del flusso export→storage)
- Schema grafo: nodi/archi con attributi di visibilità e validità temporale
- Sharding/partizionamento per dominio
- Migrazione dati da `graph.json` esistente
- Rimozione del cap 512 MiB come limite architetturale

### 3. Ingegnere Backend & API — `qwen3.5:cloud`
- Riscrittura `serve.py`: server stateless multi-istanza
- Rate limiting e pooling connessioni
- Evoluzione tool MCP (query con filtro visibilità)
- Compatibilità con i client esistenti (Claude Code, Cursor, ecc.)

### 4. Ingegnere Sicurezza & Identità — `glm-5.3:cloud`
- Autenticazione per utente (OIDC/SSO) al posto del singolo API key
- RBAC/ACL a livello di nodo/arco (`visibility: [role, team, tenant]`)
- Tenant isolation
- Audit log persistente (chi, cosa, quando)
- Middleware auth + filtro sottografo nel query engine

### 5. Ingegnere Query Engine & Grafo — `deepseek-v4-pro:cloud`
- Filtro del sottografo in base alla visibilità dell'utente
- Rilevamento conflitti tra fatti (stesso attributo, valori diversi)
- Workflow di risoluzione conflitti (approve/reject)
- Truth-maintenance: invalidazione automatica su cambio sorgente
- Propagazione dell'invalidazione ai fatti dipendenti
- Versioning temporale: query "com'era la conoscenza al tempo T"

### 6. DevOps / SRE — `deepseek-v4-flash:cloud`
- Deploy HA: replica primaria + read replicas
- Failover automatico
- Backup/restore (RPO < 5min, RTO < 15min)
- CI/CD pipeline per la riscrittura
- Harness di load testing (100 utenti concorrenti)

### 7. QA / Test Engineer — `deepseek-v4-flash:cloud`
- Strategia di test per la migrazione (parità graph.json → DB)
- Test di concorrenza (100 utenti)
- Test di sicurezza (RBAC, tenant isolation, injection)
- Test di resilienza (corruzione, failover, recovery)
- Benchmark di scala (10GB, latenza p95)

---

## 4. Definition of Done (per requisito)

| Requisito | Criterio di accettazione |
|---|---|
| R1 — Scala 10GB | Grafo 10GB indicizzato in DB; query p95 < 2s |
| R2 — 100 utenti | 100 utenti concorrenti; p95 < 3s; rate limiting attivo |
| R3 — Profilazione | RBAC per ruolo/team/tenant; audit per utente; nessun leak tra tenant |
| R4 — Resilienza | RPO < 5min; RTO < 15min; replica attiva; recovery automatico |
| R5 — Tracciabilità | Versioning temporale; audit trail completo; provenance per fatto |
| R6 — Conflict check | Rilevamento conflitti automatico; workflow approve/reject |
| R7 — Fact invalidation | Invalidazione automatica su cambio sorgente; propagazione ai dipendenti |

---

## 5. Stato attuale dell'ambiente

- ✅ `glm-5.3:cloud`, `deepseek-v4-pro:cloud`, `qwen3.5:cloud`, `deepseek-v4-flash:cloud` registrati in `~/.prime/agent/models.json`
- ⚠️ **La sessione corrente vede solo `deepseek-v4-flash:cloud`** — serve il riavvio della sessione/daemon per attivare il pool completo
- 📄 Gap analysis di riferimento: `graphify-gap-analysis.md`

---

*Documento generato il 2026-08-29. La squadra è pronta per lo spawn dopo il riavvio della sessione.*
