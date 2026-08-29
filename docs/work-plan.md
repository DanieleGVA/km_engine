# Piano di Lavoro — Replatforming completo di Graphify → km_engine

**Obiettivo:** riscrivere Graphify come piattaforma enterprise di knowledge management (10GB, 100 utenti, profilazione, resilienza, tracciabilità, conflict check, fact invalidation).

**Decisioni prese (2026-08-29):**

| # | Decisione | Scelta |
|---|---|---|
| 1 | Storage | **Neo4j** (grafo primario, ACID) |
| 2 | Deploy | **Docker Compose** su singolo server |
| 3 | Identità | **JWT semplice** (utenti+ruoli nel nostro DB) |
| 4 | Contenuto | **Misto** (codice + docs + PDF + immagini + video/audio) |
| 5 | Compatibilità | **Rottura pulita** — nuove interfacce, migrazione una tantum |
| 6 | Stack | **Python** (riuso di extract/build/cluster/query engine) |
| 7 | Timeline | **Prototipo in ~2 settimane**, poi iterazioni |
| 8 | Scope MVP (2026-08-29) | **RTO e TLS fuori scope MVP** — nginx in plain HTTP, backup giornaliero RPO 24h senza target RTO; TLS e RTO formale in iterazione 1/2 |

---

## 1. Architettura target

```
┌──────────────────────────────────────────────────────────────┐
│  CLIENT LAYER                                                │
│  CLI (km) · REST API · MCP Server · Web UI (opzionale)       │
├──────────────────────────────────────────────────────────────┤
│  API GATEWAY (nginx) — TLS, rate limiting, routing           │
├──────────────────────────────────────────────────────────────┤
│  AUTH LAYER — JWT (access + refresh), RBAC, tenant scope      │
├──────────────────────────────────────────────────────────────┤
│  APPLICATION LAYER (Python, stateless, multi-istanza)         │
│  ┌─────────────┐ ┌──────────────┐ ┌───────────────────────┐  │
│  │ Query Engine│ │ Ingestor    │ │ Conflict & Invalidation│  │
│  │ (filtro     │ │ (pipeline   │ │ (detection, workflow,  │  │
│  │  visibilità)│ │  extract→   │ │  truth-maintenance)    │  │
│  │             │ │  build→     │ │                        │  │
│  │             │ │  cluster)   │ │                        │  │
│  └─────────────┘ └──────────────┘ └───────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│  STORAGE LAYER                                               │
│  ┌──────────────┐  ┌──────────────────────────────────────┐  │
│  │ Neo4j        │  │ PostgreSQL                           │  │
│  │ grafo: nodi, │  │ utenti, ruoli, permessi, audit log,   │  │
│  │ archi, fatti,│  │ job di ingestione, workflow conflitti, │  │
│  │ versioni,    │  │ metadati sorgenti                     │  │
│  │ visibilità   │  │                                      │  │
│  └──────────────┘  └──────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Principi:**
- **Stateless app layer** → scaling orizzontale con più istanze dietro nginx
- **Neo4j = fonte di verità del grafo** (nodi/archi/fatti/versioni)
- **Postgres = identità + audit + workflow** (dati relazionali per natura)
- **Riuso massimo del codice graphify esistente** (extract, build, cluster, query scoring) — riscrittura solo di storage, auth, API e layer enterprise

---

## 2. Modello dati

### 2.1 Neo4j — grafo della conoscenza

```
(:Entity {id, label, type, source_file, source_location, confidence})
(:Fact {id, property, value, valid_from, valid_to, source_id, confidence})
(:Source {id, uri, type, hash, ingested_at})
(:Version {id, created_at, author_id, change_type})   -- per audit nel grafo

(Entity)-[:HAS_FACT]->(Fact)
(Entity)-[:RELATES_TO {relation, confidence, valid_from, valid_to}]->(Entity)
(Fact)-[:DERIVED_FROM]->(Source)
(Fact)-[:VERSION_OF]->(Fact)          -- catena di versioni (bitemporale)
(Entity)-[:VISIBLE_TO {roles, teams, public}]->(...)   -- attributi di visibilità
```

**Bitemporalità (R5):** ogni fatto/arco ha `valid_from`/`valid_to` (tempo di sistema) + `source_valid_from`/`source_valid_to` (validità dichiarata dalla sorgente). Le versioni precedenti restano come nodi `VERSION_OF` — mai cancellate, solo invalidate.

**Visibilità (R3):** attributi `visibility` su Entity/Fact: `{public: bool, roles: [...], teams: [...]}`. Il query engine filtra per permessi effettivi dell'utente.

### 2.2 PostgreSQL — identità, audit, workflow

```
users(id, username, password_hash, email, created_at, active)
roles(id, name, description)
user_roles(user_id, role_id)
teams(id, name)
user_teams(user_id, team_id)
permissions(user_id, entity_pattern, access)     -- ACL granulare opzionale
audit_log(id, user_id, action, entity_id, entity_type, old_value, new_value, ts)
ingest_jobs(id, source_uri, type, status, progress, error, started_at, finished_at)
conflicts(id, entity_id, property, value_a, value_b, source_a, source_b, status, resolved_by, resolved_at)
```

---

## 3. Work Packages (WP) — mappati ai requisiti

| WP | Nome | Requisiti | Descrizione | Test (deliverable QA) |
|---|---|---|---|---|
| WP1 | Architettura & ADR | Tutti | Architecture target, ADR, contratti API, schema DB | Review ADR; test di conformità schema DB (migrazioni) |
| WP2 | Storage layer Neo4j | R1, R4, R5 | Driver, schema, CRUD fatti, versioning bitemporale, migrazione dati | Unit test CRUD; test versioning (valid_from/valid_to); test migrazione graph.json→Neo4j (parità nodi/archi) |
| WP3 | Auth JWT + RBAC | R3 | Registrazione/login, JWT access+refresh, ruoli, visibilità | Unit test JWT (scadenza, refresh, revoca); test RBAC (ruoli, permessi); test tenant isolation |
| WP4 | Pipeline di ingestione | R1, R4, FR9 | Job-based ingestion (codice AST + semantica LLM per docs/media), incremental, dedup, **traduzione semantica FR9** (EN come lingua canonica) | Test ingestione chunked; test resume/ripristino job; test dedup; test parità con extract/build graphify; **test traduzione semantica (FR9)** |
| WP5 | Query engine + API | R2, R5, FR9 | Filtro sottografo per visibilità, REST API (OpenAPI), rate limiting, **risposte nella lingua dell'utente (FR9)** | Test filtro visibilità (utente vede solo ciò che può); test API (contratti OpenAPI); test rate limiting; **test risposte multilingue (FR9)** |
| WP6 | Conflict check + invalidation | R6, R7 | Rilevamento conflitti, workflow risoluzione, truth-maintenance, propagazione | Test rilevamento conflitti; test workflow approve/reject; test invalidazione + propagazione ai dipendenti |
| WP7 | Deploy & resilienza | R2, R4 | Docker Compose, healthcheck, backup/restore, failover, load test | Test healthcheck; test backup/restore (RPO/RTO); test failover; load test 100 utenti |
| WP8 | QA & benchmark | Tutti | Coordinamento QA trasversale, benchmark 10GB/100 utenti | Suite E2E completa; benchmark report; gate di qualità per ogni fase |

---

## 4. Timeline — prototipo in 2 settimane

### Settimana 1 (giorni 1–5): fondamenta

| Giorno | Attività | WP | Responsabile | Gate di test (QA) |
|---|---|---|---|---|
| 1 | Architecture target + ADR-001/002/003 + schema DB | WP1 | Architetto | Review ADR; validazione schema DB |
| 2 | Setup repo km_engine, Docker Compose base (Neo4j+Postgres) | WP1, WP7 | Architetto + SRE | Smoke test: container up, connettività Neo4j+Postgres |
| 3 | Storage layer: driver Neo4j, CRUD fatti, versioning | WP2 | Ing. Storage | **Gate G1:** unit test CRUD + versioning passano |
| 4 | Auth: utenti, ruoli, JWT, middleware | WP3 | Ing. Sicurezza | **Gate G2:** unit test JWT + RBAC passano |
| 5 | Integrazione storage+auth | WP2, WP3 | Storage + Sicurezza | **Gate G3:** test integrazione storage+auth; test tenant isolation |

### Settimana 2 (giorni 6–14): funzionalità enterprise

| Giorno | Attività | WP | Responsabile | Gate di test (QA) |
|---|---|---|---|---|
| 6–7 | Pipeline ingestione: job-based, riuso extract/build/cluster | WP4 | Ing. Storage + Grafo | **Gate G4:** test ingestione chunked + resume + dedup; parità con graphify |
| 8–9 | Query engine: filtro visibilità, REST API, risposte multilingue (FR9) | WP5 | Ing. Backend + Grafo | **Gate G5:** test filtro visibilità; test contratti API (OpenAPI); test rate limiting; test risposte multilingue |
| 10–11 | Conflict detection + workflow risoluzione | WP6 | Ing. Grafo | **Gate G6:** test rilevamento conflitti + workflow approve/reject |
| 12 | Fact invalidation + propagazione (truth-maintenance) | WP6 | Ing. Grafo | **Gate G7:** test invalidazione + propagazione ai dipendenti |
| 13 | Deploy Docker Compose completo, backup, healthcheck | WP7 | SRE | **Gate G8:** test backup/restore (RPO/RTO); test failover; load test 100 utenti |
| 14 | QA end-to-end, benchmark, demo | WP8 | QA + tutti | **Gate G9 (finale):** suite E2E completa + benchmark report + demo |

**Regola:** un gate di test **blocca** il passaggio alla fase successiva. Se un gate fallisce, si itera sul WP prima di procedere (nessun accumulo di debito tecnico).

### Strategia di test (trasversale a tutte le fasi)

**Livelli di test:**

| Livello | Cosa si testa | Quando | Strumenti |
|---|---|---|---|
| Unit | Funzioni singole (CRUD, JWT, filtri, rilevamento) | In ogni WP, dal giorno 1 | pytest |
| Integration | Interazione moduli (storage↔auth, ingest↔query) | A ogni gate G1–G8 | pytest + Docker Compose |
| E2E | Flusso completo: login → ingest → query → conflict → invalidation | Gate G9 (giorno 14) | pytest + API client |
| Performance | Latenza query, throughput ingestione | Gate G5, G8 | k6 / locust |
| Security | RBAC, tenant isolation, injection, JWT | Gate G2, G3, G9 | pytest + OWASP ZAP (opz.) |
| Resilience | Backup/restore, failover, corruzione dati | Gate G8 | chaos testing (kill container) |

**Regole QA:**
1. **Test-first**: ogni WP consegna i test insieme al codice (nessun codice senza test).
2. **Gate di qualità**: G1–G9 bloccano il passaggio di fase (vedi timeline).
3. **QA in ogni fase**: il QA/Test Engineer reviewa e valida ogni WP, non solo a fine progetto.
4. **Parità di migrazione**: test automatico che confronta graph.json → Neo4j (stessi nodi/archi/fatti).
5. **Copertura minima**: ≥80% unit test sui moduli core (storage, auth, query, conflict).

### Iterazioni successive (dopo il prototipo)

- **Iterazione 1 (sett. 3–4):** Web UI, tenant isolation multi-tenant, audit trail completo
- **Iterazione 2 (sett. 5–6):** scaling multi-istanza, rate limiting avanzato, SSO/OIDC
- **Iterazione 3 (sett. 7–8):** benchmark 10GB reale, hardening, disaster recovery

---

## 5. Squadra e assegnazioni

| Ruolo | Modello | WP |
|---|---|---|
| Architetto Software / Tech Lead | `glm-5.3:cloud` | WP1, coordinamento |
| Ingegnere Storage & Dati | `deepseek-v4-pro:cloud` | WP2, WP4 |
| Ingegnere Backend & API | `qwen3.5:cloud` | WP5 |
| Ingegnere Sicurezza & Identità | `glm-5.3:cloud` | WP3 |
| Ingegnere Query Engine & Grafo | `deepseek-v4-pro:cloud` | WP4, WP5, WP6 |
| DevOps / SRE | `deepseek-v4-flash:cloud` | WP7 |
| QA / Test Engineer | `deepseek-v4-flash:cloud` | **Tutti i WP** (gate G1–G9) |

**Dipendenza critica:** WP1 (Architetto) deve completare ADR e contratti prima che gli altri inizino. Poi WP2/WP3 in parallelo (giorni 3–4), poi WP4/WP5/WP6 in parallelo (giorni 6–12).

---

## 6. Definition of Done

| Requisito | Criterio di accettazione (prototipo) | Test richiesti |
|---|---|---|
| R1 — Scala 10GB | Ingestione chunked funzionante; query su grafo Neo4j < 2s p95 (test con corpus di prova) | Test ingestione 10GB; benchmark latenza p95 |
| R2 — 100 utenti | API stateless; load test 100 utenti concorrenti senza errori | Load test (k6/locust); test rate limiting |
| R3 — Profilazione | Login JWT; ruoli; filtro visibilità nel query engine; test tenant isolation | Test RBAC; test tenant isolation; test JWT (scadenza/refresh/revoca) |
| R4 — Resilienza | Docker Compose con healthcheck; backup Neo4j+Postgres schedulati; recovery testato | Test backup/restore (RPO/RTO); test failover; chaos test |
| R5 — Tracciabilità | Versioning bitemporale dei fatti; audit log su Postgres; query "al tempo T" | Test versioning; test query temporale; test audit log |
| R6 — Conflict check | Rilevamento automatico conflitti; workflow approve/reject via API | Test rilevamento; test workflow risoluzione |
| R7 — Fact invalidation | Invalidazione su cambio sorgente; propagazione ai fatti dipendenti | Test invalidazione; test propagazione |

---

## 7. Rischi e mitigazioni

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Neo4j bitemporal non nativo | Alto | Pattern version-nodes + VERSION_OF; test dedicati |
| Riuso codice graphify (estrazione) fragile | Medio | Isolare extract/build/cluster come libreria; test di parità |
| Ingestione 10GB lenta | Alto | Chunking, parallelismo, job state su Postgres, resume |
| LLM per contenuto misto costoso | Medio | Cache semantica (riuso cache.py), batch, modello locale opzionale |
| JWT semplice insufficiente per enterprise | Medio | Design con estensione a OIDC in iterazione 2 |
| Migrazione dati da graph.json | Medio | Script di migrazione + test di parità (stessi nodi/archi) |

---

## 8. Deliverable del prototipo (giorno 14)

1. Repo `km_engine` con codice funzionante (app layer + storage + auth)
2. Docker Compose completo (nginx + app + Neo4j + Postgres)
3. CLI `km` (query, ingest, admin) + REST API (OpenAPI)
4. Script di migrazione da graph.json a Neo4j
5. Test suite (migrazione, concorrenza, sicurezza, resilienza)
6. Benchmark report (10GB corpus di prova, 100 utenti)
7. Documentazione (README, ADR, schema DB, API reference)
8. Demo end-to-end

---

*Piano generato il 2026-08-29. Riferimenti: `graphify-gap-analysis.md`, `graphify-squad.md` (stessa cartella).*
