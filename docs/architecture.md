# Architettura km_engine — descrizione dettagliata dell'implementazione

**Versione:** 1.0 (MVP completato, 2026-08-29)
**Stato:** prototipo funzionante — 12 commit, 207 test verdi, gate G1–G9 superati
**Riferimenti:** `docs/requirements.md` (baseline), `docs/work-plan.md` (piano),
`docs/adr/ADR-001/002/003` (decisioni), `docs/comparison-graphify-km-engine.md` (confronto)

---

## 1. Panoramica

km_engine è la riscrittura enterprise di **Graphify** (code-intelligence open-source):
da memoria personale su file JSON (cap 512 MiB, single-user) a piattaforma di
knowledge management su **Neo4j + PostgreSQL** con profilazione (RBAC/teams),
tracciabilità bitemporale, conflict check, fact invalidation e supporto multilingue (FR9).

**Obiettivi del prototipo (2 settimane):** ~10GB di contenuti misti, ~100 utenti
concorrenti, resilienza, tracciabilità, conflitti, invalidazione, multilingue.

**Principi guida (ADR):**
- **Neo4j = fonte di verità del grafo** (nodi/archi/fatti/versioni); **Postgres = identità, audit, workflow** (dati relazionali per natura). Nessuna FK tra i due DB: riferimenti incrociati per id.
- **App layer stateless** → scaling orizzontale (multi-istanza dietro nginx).
- **Riuso massimo di graphify**: extract/build/dedup riusati come libreria (parità testata live).
- **Bitemporalità**: mai DELETE, solo invalidazione; versioni precedenti restano come nodi `VERSION_OF`.
- **Default-deny** sulla visibilità: un oggetto senza attributi non è visibile a nessuno (tranne admin).

---

## 2. Stack tecnologico

| Componente | Scelta | Note |
|---|---|---|
| Linguaggio | Python ≥3.11 | Riuso del codice graphify |
| Grafo | Neo4j 5.26 (Community) | ACID, indici RANGE/FULLTEXT, vincoli di unicità |
| Relazionale | PostgreSQL 16 | identità, audit, job, conflitti |
| API | FastAPI 0.141 + Uvicorn | OpenAPI generata, rate limiting in-memory |
| Auth | PyJWT + argon2-cffi | access 15' + refresh 14gg con rotazione |
| Driver | neo4j ≥5.20, psycopg 3 | |
| Deploy | Docker Compose v5 | dev (2 servizi) + prod-like (4 servizi) |
| Test | pytest + pytest-cov | 207 test, copertura 91% |
| Qualità | ruff | lint pulito |

---

## 3. Architettura a strati

```
┌──────────────────────────────────────────────────────────────┐
│  CLIENT                                                       │
│  REST API (OpenAPI) · scripts (migrazione, load test, backup)│
├──────────────────────────────────────────────────────────────┤
│  API GATEWAY (nginx, solo stack prod)                        │
│  proxy /auth + /api/v1 · rate limit 5r/s auth, 20r/s api     │
├──────────────────────────────────────────────────────────────┤
│  APPLICATION LAYER (Python, stateless, multi-istanza)        │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌───────────────┐  │
│  │ app/api  │→│ app/auth │ │ app/query │ │ app/conflict  │  │
│  │ FastAPI  │ │ JWT/RBAC │ │ visibilità│ │ detection+    │  │
│  │          │ │ audit    │ │ temporale │ │ workflow      │  │
│  └──────────┘ └──────────┘ └───────────┘ └───────────────┘  │
│  ┌──────────────┐ ┌──────────────────┐ ┌─────────────────┐  │
│  │ app/ingest   │ │ app/invalidation │ │ app/storage     │  │
│  │ pipeline     │ │ truth-maintenance│ │ Neo4jClient +   │  │
│  │ job-based    │ │ propagazione     │ │ GraphRepository │  │
│  └──────────────┘ └──────────────────┘ └─────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│  STORAGE LAYER                                               │
│  Neo4j: Entity/Fact/Source/Version, HAS_FACT, RELATES_TO,    │
│         DERIVED_FROM, VERSION_OF, VERSIONS                   │
│  Postgres: users, roles, teams, refresh_tokens, audit_log,    │
│            ingest_jobs, conflicts, permissions               │
└──────────────────────────────────────────────────────────────┘
```

**Regola di dipendenza:** `app/api` orchestra; `app/query`, `app/conflict`,
`app/invalidation`, `app/ingest` usano `app/storage` e `app/auth`; nessun modulo
di basso livello dipende dall'API.

---

## 4. Modello dati

### 4.1 Neo4j — grafo della conoscenza (`db/neo4j/schema.cypher`)

```
(:Entity {id, label, type, source_file, source_location, confidence,
          is_public, roles, teams, language, translation_state, source_language})
(:Fact   {id, property, value, valid_from, valid_to,
          source_valid_from, source_valid_to, status, confidence,
          source_id, is_public, roles, teams,
          language, translation_state, source_language})
(:Source {id, uri, type, hash, language, ingested_at,
          invalidated_at, invalidation_reason, invalidated_by})
(:Version{id, created_at, author_id, change_type})

(:Entity)-[:HAS_FACT]->(:Fact)
(:Entity)-[:RELATES_TO {relation, confidence, status, valid_from, valid_to,
                        source_id, source_file, source_location}]->(:Entity)
(:Fact)-[:DERIVED_FROM]->(:Source)          -- provenance: fatto → sorgente
(:Fact)-[:VERSION_OF]->(:Fact)              -- catena versioni (bitemporale)
(:Version)-[:VERSIONS]->(:Entity | :Fact)   -- audit nel grafo
```

- **Bitemporalità:** `valid_from/valid_to` (tempo di sistema) + `source_valid_from/to`
  (validità dichiarata dalla sorgente). `valid_to IS NULL` = versione corrente.
  Un update **non modifica** il nodo: crea un nuovo `:Fact`, chiude il vecchio
  (`valid_to=now`, `status=obsolete`) e collega `(old)-[:VERSION_OF]->(new)`.
  Nessun DELETE applicativo.
- **Visibilità:** proprietà piatte `is_public/roles/teams` su Entity e Fact
  (default-deny). Ereditarietà Fact→Entity per dimensione, "esplicito vince".
  Policy (deciso in WP5): una restrizione esplicita su una dimensione rende
  l'oggetto **non pubblico** per le altre, salvo `is_public=True` esplicito.
- **Confidence:** `EXTRACTED / INFERRED / AMBIGUOUS` su Entity, Fact, RELATES_TO.
- **Vincoli:** 4 unicità (id) + indici RANGE su proprietà di filtro + 2 FULLTEXT
  (Entity.label, Fact.value) + LOOKUP. Tutto Community-safe.

### 4.2 PostgreSQL — identità, audit, workflow (`db/postgres/001_init.sql`)

| Tabella | Scopo |
|---|---|
| `users` | username, password_hash (argon2id), email, active |
| `roles` | 4 ruoli seed: admin, editor, viewer, ingestor (CHECK) |
| `user_roles`, `user_teams` | many-to-many |
| `teams` | dimensione organizzativa per la profilazione |
| `permissions` | ACL granulare opzionale (vuota nel MVP) |
| `refresh_tokens` | hash SHA-256 dei refresh token, revoca, rotazione |
| `audit_log` | append-only: azione, entità, old/new jsonb, ts |
| `ingest_jobs` | stato/progress/errore dei job di ingestione (resume) |
| `conflicts` | conflitti pending→approved/rejected, suggestion, resolved_by/at |

Schema idempotente (`IF NOT EXISTS` + `ON CONFLICT DO NOTHING`), applicato e
ri-applicato senza errori sui container dev.

---

## 5. Moduli applicativi

### 5.1 `app/storage` — accesso al grafo (WP2, gate G1)
- `client.py` — `Neo4jClient` (config da `KM_NEO4J_URI/USER/PASSWORD`, context manager, verify_connectivity).
- `repository.py` — `GraphRepository`: `create_entity/get_entity/update_entity`,
  `create_fact/get_fact/get_fact_history/update_fact/invalidate_fact`,
  `create_relation/get_relations/get_facts_for_entity`. L'update di un fatto
  implementa il pattern version-nodes (nuova versione + chiusura + `VERSION_OF` + nodo `:Version`).
- `visibility.py` — `Visibility`, `effective_visibility`, `is_visible`, `apply_visibility`
  (default-deny, ereditarietà per dimensione).
- `migrate.py` + `scripts/migrate_graphjson.py` — migrazione una tantum
  `graph.json → Neo4j` (MERGE chunked, idempotente, parità testata).

### 5.2 `app/auth` — identità e accesso (WP3, gate G2)
- `hashing.py` — argon2id, verifica constant-time, politica ≥12 caratteri, fallback bcrypt documentato.
- `users.py` — CRUD utenti, assegnazione ruoli/teams, `resolve_identity` → (roles, teams).
- `tokens.py` — `login`, `refresh` (rotazione con `SELECT ... FOR UPDATE`),
  `logout/revoke_refresh`, `revoke_all_user_tokens`; access JWT 15' (claims
  sub/typ/roles/teams/tenant/jti), refresh 14gg con hash in Postgres.
  **Anti-reuse:** riuso di un refresh revocato → revoca a cascata di tutti i
  refresh attivi dell'utente (persistita prima di sollevare l'errore).
- `deps.py` — `Principal`, `auth_required`, `require_roles` (RBAC, unione permissiva).
- `audit.py` — `record_audit` append-only, nella transazione del chiamante.
- `bootstrap.py` — `bootstrap_admin` idempotente da `KM_ADMIN_USERNAME/PASSWORD`
  (crea al primo avvio, ripara ruolo/attivazione, non tocca la password).
- `routes.py` — router `/auth` (login/refresh/logout), OIDC-ready (interfaccia unica).

### 5.3 `app/ingest` — pipeline di ingestione (WP4, gate G4)
- `pipeline.py` — `IngestPipeline.run(source_uri, root, job_type, resume, ...)`:
  job-based su `ingest_jobs` (stato/progress/resume), chunked, incrementale
  (hash cache: solo file cambiati), hook post-ingest per conflict detection.
- `extractor.py` — `GraphifyCodeExtractor`: **riuso di `graphify.extract` +
  `graphify.dedup.deduplicate_entities`** (FR1.1/FR1.6), linguaggi prioritari
  Python/JS/TS/Go/Java/C/C++.
- `semantic.py` — `SemanticService` (interfaccia) + `StubSemanticService`
  (deterministico, per test) + `LLMSemanticService` (scheletro documentato:
  `KM_LLM_API_KEY/ENDPOINT/MODEL` — da completare in iterazione 1).
- `language.py` — `normalize_language`: euristica deterministica (parole
  funzionali + accenti) per en/fr/de/it/es; EN = lingua canonica (FR9.1).
- `graph_writer.py` — scrittura Entity/Fact/Source con metadati FR9
  (language, translation_state, source_language) e provenance
  (source_file/source_location).
- `jobs.py`, `hash_cache.py`, `mapping.py`, `models.py` — job manager, cache
  hash, id deterministici con prefisso configurabile, modelli dati.

### 5.4 `app/query` — query engine visibility-aware (WP5, gate G5)
- `engine.py` — `query_entities`, `query_facts` (con `at_time` per la query
  temporale FR5.3), `query_relations`, `search` (CONTAINS su label/type/value),
  `get_entity_with_history` (versione corrente + catena VERSION_OF),
  `localize_response` (FR9.3: flag `untranslated` con semantica corretta).
- **Filtro visibilità su TUTTI i punti di lettura** (entity, fatti, storico,
  relazioni, ricerca) tramite l'unico ponte `principal_visibility_context(Principal)`
  (in `app/auth/__init__.py`). Admin bypass; Editor senza vista cross-tenant.

### 5.5 `app/conflict` — conflict check (WP6, gate G6)
- `detection.py` — conflitto = due Fact correnti (valid_to IS NULL, status=valid)
  sulla stessa Entity+property, valori diversi, sorgenti diverse; dedup delle
  righe pending; suggerimento automatico (Q10): confidence
  (EXTRACTED>INFERRED>AMBIGUOUS) → sorgente più recente → "b".
- `workflow.py` — `approve` (invalida il fatto perdente nel grafo + status
  approved + audit RESOLVE), `reject` (status rejected, grafo invariato),
  `list_conflicts(status=...)`. Errori 404/409/422.

### 5.6 `app/invalidation` — truth-maintenance (WP7, gate G7)
- `maintenance.py` — `invalidate_source(source_id, reason, user, max_depth=3)`:
  fatti `DERIVED_FROM` la sorgente → `obsolete` (nuova versione, idempotente);
  **propagazione** ai dipendenti (fatti INFERRED derivati dal fatto padre o
  stessa Entity) → `under_review`; ricorsione con `max_depth` (default 3, max 10);
  audit `INVALIDATE_SOURCE` + metadati sul nodo Source.

### 5.7 `app/api` — REST API (WP5, gate G5)
- `app.py` — `create_app()`: FastAPI con router `/auth` (rate limiting 5r/s),
  endpoint `/api/v1/*` (auth_required), `/healthz` composito (bolt+psql),
  rate limiting token-bucket in-memory per IP (20r/s api), error handling
  401/403/404/422, `get_response_lang` (query `lang` o `Accept-Language`).
- Endpoint: vedi §7. OpenAPI salvata in `docs/openapi.json` (`scripts/generate_openapi.py`).

---

## 6. Flussi principali

### 6.1 Ingestione (job-based, FR1/FR9)
```
CLI/script → IngestPipeline.run()
  → JobManager: crea job in ingest_jobs (status/progress)
  → scan_files (code|document|image) → chunk
  → per file: hash_cache (solo cambiati) → estrazione
      code: GraphifyCodeExtractor (extract+dedup graphify) → Entity/Fact/RELATES_TO
      doc : normalize_language → SemanticService.analyze_text → CandidateFact
      img : SemanticService.analyze_image (stub)
  → GraphWriter: upsert Source (uri, hash, language) + Entity/Fact con
      provenance e metadati FR9 (translation_state, source_language)
  → hook post-ingest: conflict detection sulle entità toccate
  → stato salvato dopo ogni chunk (resume) → complete/fail
```

### 6.2 Autenticazione e autorizzazione (FR4)
```
POST /auth/login (argon2id verify) → access JWT 15' + refresh 14gg (hash in PG)
POST /auth/refresh → rotazione atomica (FOR UPDATE): revoca vecchio, emette nuovo
POST /auth/logout → revoca refresh
Ogni endpoint: auth_required → Principal (sub/roles/teams/tenant/jti)
  → require_roles per admin/editor
  → principal_visibility_context → filtro nel query engine (default-deny)
```

### 6.3 Query con visibilità (FR3.3/FR3.4)
```
GET /api/v1/entities/{id}/facts?at_time=T&lang=fr
  → auth_required → Principal
  → query_facts(client, principal, entity_id, at_time)   // filtro temporale
  → filtro visibilità (entity padre + fatto, effective_visibility)
  → localize_response(facts, lang)  // FR9.3: flag untranslated se serve
```

### 6.4 Conflict workflow (FR6)
```
Ingest hook / scan → detection → riga pending in conflicts (con suggestion)
GET  /api/v1/conflicts?status=pending
POST /api/v1/conflicts/{id}/approve {choice: a|b}
  → invalidate_fact(fatto perdente) in Neo4j → status=approved → audit RESOLVE
POST /api/v1/conflicts/{id}/reject → status=rejected (grafo invariato) → audit
```

### 6.5 Invalidation con propagazione (FR7)
```
POST /api/v1/sources/{source_id}/invalidate {reason, max_depth}
  → fatti DERIVED_FROM → obsolete (nuova versione VERSION_OF)
  → dipendenti INFERRED → under_review (ricorsione max_depth)
  → audit INVALIDATE_SOURCE + metadati sul Source
```

---

## 7. API REST (contratto OpenAPI in `docs/openapi.json`)

| Metodo | Path | Auth | Descrizione |
|---|---|---|---|
| POST | `/auth/login` | – | login → access+refresh |
| POST | `/auth/refresh` | – | rotazione refresh |
| POST | `/auth/logout` | sì | revoca refresh |
| GET | `/api/v1/healthz` | – | health composito Neo4j+Postgres |
| GET | `/api/v1/entities` | sì | lista entità filtrate (`label`, `type`, `lang`) |
| GET | `/api/v1/entities/{id}` | sì | dettaglio + storico versioni |
| GET | `/api/v1/entities/{id}/facts` | sì | fatti (`at_time`, `lang`) |
| GET | `/api/v1/entities/{id}/relations` | sì | relazioni RELATES_TO |
| GET | `/api/v1/search?q=` | sì | ricerca (CONTAINS) |
| GET | `/api/v1/conflicts` | sì | lista conflitti (`status`) |
| POST | `/api/v1/conflicts/{id}/approve` | admin/editor | risoluzione con scelta |
| POST | `/api/v1/conflicts/{id}/reject` | admin/editor | rigetto |
| POST | `/api/v1/sources/{id}/invalidate` | admin/editor | invalidazione sorgente |

---

## 8. FR9 — supporto multilingue

- **FR9.1** lingua canonica interna = inglese: ogni fatto ha `language=en`,
  `translation_state` (native|pending), `source_language` (lingua originale).
- **FR9.2** traduzione all'ingestione: interfaccia `SemanticService` pronta;
  stub deterministico per i test; adattatore LLM da completare (iterazione 1).
  Accuratezza verificata con test dedicato: PDF francese reale (DUDU UN) →
  traduzione LLM ~100% vs traduzione ufficiale (sim 0.991, token P/R 1.000).
- **FR9.3** risposte nella lingua dell'utente: `Accept-Language` (o `?lang=`)
  → `localize_response`: utente EN avvisato se EN non pronta; lingua sorgente
  servita nativamente; altre lingue flag `untranslated=True`.
- **FR9.4** tracciabilità: `source_language` + riferimento sorgente su ogni fatto.
- **FR9.5** set lingue: en, fr, de, it, es (euristica in `language.py`).

---

## 9. Deploy e operatività

### 9.1 Ambiente di sviluppo (`docker-compose.yml`)
Neo4j 5.26 (7474/7687) + Postgres 16 (5432), healthcheck, volumi persistenti,
`db/neo4j` montato in `/import`. `.env` con credenziali dev e variabili `KM_*`.

### 9.2 Stack prod-like (`deploy/docker-compose.yml`)
```
nginx (HTTP:80, rate limit 5r/s auth + 20r/s api, header sicurezza)
  → km-api ×2 (Dockerfile multi-stage con uv, python:3.12-slim, utente non-root)
  → neo4j + postgres (porte DB NON pubblicate)
```
TLS e RTO fuori scope MVP (decisione 2026-08-29); TLS in iterazione 1/2.

### 9.3 Operatività (`docs/runbook.md`, `scripts/`)
- `scripts/backup.sh` — dump Neo4j (offline coerente) + `pg_dump -Fc`, tar +
  cifratura AES-256 (openssl -pbkdf2), retention 7gg → **RPO 24h**.
- `scripts/restore.sh` — decifratura, drop/recreate PG + pg_restore, load Neo4j
  `--overwrite-destination`, smoke test.
- `scripts/recycle_unhealthy.sh` — healthcheck recycle (cron 1min).
- `scripts/loadtest.py` — load test 100 utenti (asyncio+httpx, login reali,
  X-Forwarded-For per-utente) → report markdown.
- `scripts/migrate_graphjson.py` — migrazione graph.json→Neo4j.
- `scripts/generate_openapi.py` — rigenera `docs/openapi.json`.

---

## 10. Qualità e test

| Livello | Dove | Esito |
|---|---|---|
| Unit | `tests/storage`, `tests/auth`, `tests/query`, `tests/ingest`, `tests/conflict`, `tests/invalidation` | 207 test verdi |
| Integration | `tests/integration` (G3 tenant isolation) | 12 test |
| E2E | `tests/e2e` (flusso 7 step: bootstrap→login→ingest→query→conflict→invalidate→audit) | 1 test stabile |
| Deploy | `tests/deploy` (healthz pytest + backup/restore bash + failover bash) | PASS |
| Copertura | pytest-cov | **91% totale**; core: storage 92, auth 97, query 87, conflict 89, ingest 91, invalidation 95 |
| Lint | ruff | pulito |
| Benchmark | `scripts/loadtest.py` | 100 utenti, 1100 richieste, **0 errori**; search p95 629ms (<2s NFR1); entities p95 3.3s sotto carico (1 worker dev, login storm argon2id); baseline single-user ~7ms |

**Gate di qualità (work-plan):** G1 storage ✅ · G2 auth ✅ · G3 integrazione ✅ ·
G4 ingestione+parità graphify ✅ · G5 query+API+FR9 ✅ · G6 conflict ✅ ·
G7 invalidation ✅ · G8 deploy+resilienza ✅ · G9 QA finale ✅

---

## 11. Decisioni architetturali (ADR)

| ADR | Decisione |
|---|---|
| ADR-001 | Neo4j = fonte di verità del grafo; bitemporalità con version-nodes + VERSION_OF; visibilità come proprietà (default-deny); migrazione graph.json→Neo4j con parità |
| ADR-002 | JWT access 15' + refresh 14gg con rotazione e revoca (hash in PG); RBAC 4 ruoli + teams; argon2id; audit append-only; OIDC-ready (interfaccia unica) |
| ADR-003 | Singolo server, nginx gateway → app stateless ×N → Neo4j+Postgres; healthcheck; backup giornaliero cifrato (RPO 24h); failover minimale; **TLS/RTO fuori scope MVP** |

---

## 12. Limiti noti e iterazione 1

**Gap dichiarati (dettaglio in `docs/comparison-graphify-km-engine.md`):**
1. CLI `km` (FR8.1) non realizzata — solo REST API + scripts.
2. Clustering/community (FR2.2) e query NL/path/god-node (FR3.1/3.2) non implementati.
3. Q12: ricalcolo automatico dei fatti derivati dopo invalidazione (oggi restano `under_review`).
4. Traduzione LLM vera all'ingestione (FR9.2) e descrizione immagini (FR1.3): adattatore pronto, manca l'integrazione con una chiave LLM.
5. Invalidazione automatica su cambio sorgente (FR7.1/7.3): oggi manuale via API.
6. Full-text del contenuto originale (FR3.5): search usa CONTAINS, servono indici full-text Neo4j.
7. Benchmark 10GB reale (NFR6) e misure formali NFR3/NFR8.

**Raccomandazioni iterazione 1 (da QA/SRE):** indici full-text Neo4j, tuning
heap/pagecache, caching TTL, hashing async (login storm), verifica NFR1 su stack
prod (nginx+2 repliche), benchmark 10GB in 6 fasi (report §8), Web UI, multi-tenant,
OIDC, retention policy.

## 13. Iterazione A — Domain Knowledge Layer

Aggiunta al prototipo MVP (gate GA1–GA6). Pipeline a due stadi IR markdown:
`translated.md` (traduzione EN P2-safe) → `canonical.md` (normalizzazione
deterministica) → sotto-grafo canonico Neo4j con round-trip garantito.

- **Domain Pack** (`domain-packs/ricette/`): `pack.yaml`, `template.md`,
  glossari seed (`tecnica`, `ingredienti`, `stati`), `units.yaml`, regole.
  Validazione pydantic in `app/domain/pack.py`; bootstrap idempotente in
  `scripts/load_domain_pack.py` (`:DomainPack`, `:CanonicalTerm`).
- **Stadi**: `translate_document` (P2: numeri mascherati, mai alterati),
  `canonicalize` (unità Decimal esatte + termini glossario, mai id inventati),
  `extract_document` (md→grafo) e `recompose_document` (grafo→md).
- **Verifica a 3 livelli** (`app/domain/verify.py`): L1 struttura/numeri
  deterministico, L2 sezioni semantiche, L3 coda adjudication Postgres.
  Canon-log (`canon_log`) spiega il 100% del diff translated→canonical.
- **Schema domain** (`db/neo4j/002_domain_schema.cypher`): `:Document`,
  `:CanonicalTerm`, `:DomainPack`, `PART_OF_PACK`, `PART_OF_DOC`,
  `NORMALIZED_TO`, fulltext e indice vettoriale 384d. Visibilità default-deny
  estesa a Document/CanonicalTerm in `app/query/domain.py`.
- **Round-trip**: `recompose(extract(canonical.md)) == canonical.md`
  byte-identico sul corpus pilota (15 ricette). Estrattore idempotente su
  `canonical_hash` (MERGE deterministici, zero duplicati).

---

*Documento generato a chiusura del prototipo. Tutti i dettagli operativi sono in
`docs/runbook.md`, `docs/benchmark-report.md`, `docs/accuracy-fr9-report.md` e nei
singoli moduli (docstring).*
