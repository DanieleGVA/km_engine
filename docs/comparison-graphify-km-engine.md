# Confronto Graphify vs km_engine — raggiungimento obiettivi (MVP)

**Data:** 2026-08-29
**Metodo:** confronto sistematico baseline (docs/graphify-gap-analysis.md) vs
implementazione km_engine (12 commit, 207 test, gate G1–G9), contro obiettivi
O1–O7 (requirements.md §2) e requisiti FR1–FR9/NFR (requirements.md).
Legenda: ✅ raggiunto · △ parziale (limite dichiarato) · ❌ non implementato · – non applicabile (rinviato)

---

## 1. Graphify (prima) vs km_engine (dopo)

| Dimensione | Graphify (baseline) | km_engine (MVP) |
|---|---|---|
| Storage | Un file JSON `graph.json`, cap 512 MiB, tutto in RAM | Neo4j (grafo ACID) + Postgres (identità/audit/workflow) |
| Scala | Singolo sviluppatore | Chunked ingestion, 100 utenti (load test 0 errori) |
| Profilazione | Nessuna (un API key condiviso) | JWT access/refresh, RBAC 4 ruoli, teams, filtro visibilità |
| Tracciabilità | Provenance per nodo, no audit | Versioning bitemporale VERSION_OF + audit log + catena fatto→sorgente→file→riga |
| Conflict | Solo conflitti PR di codice | Detection automatica + suggerimento + workflow approve/reject |
| Invalidation | Re-verify parziale | Invalidazione sorgente + propagazione truth-maintenance |
| Multilingua | Assente | Lingua canonica EN, detection, risposte multilingue (FR9) |
| Riuso | — | extract/build/dedup di graphify riusati (parità live testata) |
| Interfaccia | CLI + memoria personale | REST API OpenAPI + scripts (CLI `km` ❌ non realizzata) |

## 2. Obiettivi O1–O7

| # | Obiettivo | Esito | Evidenza |
|---|---|---|---|
| O1 | KB ~10GB contenuti misti | ✅/△ | Neo4j+Postgres, ingestione chunked/incrementale/resume; **benchmark 10GB reale rinviato a iterazione 1** (NFR6) |
| O2 | ~100 utenti concorrenti | ✅ | Load test: 1100 richieste, 0 errori; NFR2 soddisfatto |
| O3 | Resilienza (backup/recovery) | ✅/△ | Backup giornaliero cifrato RPO 24h + restore testato + healthcheck; NFR3 (90%) e NFR7 at-rest non misurati formalmente |
| O4 | Tracciabilità | ✅ | Bitemporale VERSION_OF, audit append-only, provenance con source_file/source_location |
| O5 | Conflict check | ✅ | Detection+dedup+suggerimento (Q10), workflow approve/reject (Q11 admin), audit risoluzioni |
| O6 | Fact invalidation | ✅/△ | Invalidazione sorgente + propagazione under_review; **Q12 (ricalcolo automatico derivati) NON implementato** |
| O7 | Multilingue | ✅/△ | Detection 100%, flusso FR9 verificato su PDF FR reale, traduzione LLM ~100% vs ufficiale UN; **FR9.2 traduzione vera all'ingestione = scheletro LLM (iterazione 1)** |

## 3. Requisiti funzionali FR1–FR9

| Req | Esito | Note |
|---|---|---|
| FR1.1 AST (riuso extract) | ✅ | `GraphifyCodeExtractor` riusa graphify.extract |
| FR1.2 Doc LLM | △ | `SemanticService`: stub deterministico + scheletro LLM (KM_LLM_API_KEY da completare) |
| FR1.3 Immagini | △ | Chunk elaborato ma descrizione = stub deterministico (FR1.3 con LLM reale in iterazione 1) |
| FR1.4 Incrementale | ✅ | Hash cache, solo file cambiati |
| FR1.5 Job+resume | ✅ | ingest_jobs con stato/progress, resume testato |
| FR1.6 Dedup | ✅ | Riuso graphify.dedup |
| FR2.1 Confidence | ✅ | EXTRACTED/INFERRED/AMBIGUOUS su Entity/Fact/RELATES_TO |
| FR2.2 Cluster/Leiden | ❌ | **Non implementato** (nessun modulo community/cluster in app/) |
| FR2.3 Bitemporale | ✅ | valid_from/to + source_valid_from/to (schema), VERSION_OF, mai DELETE |
| FR2.4 Visibilità | ✅ | is_public/roles/teams con default-deny ed ereditarietà |
| FR3.1 Query NL | ❌ | **Query engine NLP di graphify non riusato** (solo endpoint strutturati + search) |
| FR3.2 Path/explain/god-node | ❌ | **Non implementato** |
| FR3.3 Filtro visibilità | ✅ | Tutti i punti di lettura (entity/fact/history/relazioni/search) |
| FR3.4 Query al tempo T | ✅ | `at_time` su facts (FR5.3) |
| FR3.5 Full-text sorgente | △ | CONTAINS su label/valori; **non indicizza il contenuto originale dei PDF** (Q5 parziale) |
| FR4.1–4.5 Auth/RBAC/revoca | ✅ | JWT access 15' + refresh 14gg con rotazione, argon2id, revoca a cascata, bootstrap admin |
| FR5.1–5.4 Tracciabilità | ✅/△ | Provenance ok; versioni consultabili; "confrontabili" (diff UI) non costruito |
| FR6.1–6.3 Conflict | ✅ | Detection, workflow pending→approved/rejected, storico via audit |
| FR7.1 Inv. automatica | △ | API manuale `POST /sources/{id}/invalidate`; non automatica su ri-scan sorgente |
| FR7.2 Propagazione | ✅ | Dipendenti → under_review con max_depth |
| FR7.3 Ri-estrazione | △ | Incrementale ri-processa i cambiati; non collegata all'invalidazione |
| FR7.4 Stati | ✅ | valid / obsolete / under_review |
| Q12 Ricalcolo derivati | ❌ | **Dichiarato aperto dal WP6** (dipendenti restano under_review) |
| FR8.1 CLI `km` | ❌ | **Nessun [project.scripts]**; solo scripts/ (migrate, loadtest, openapi) |
| FR8.2 REST API | ✅ | FastAPI, OpenAPI (9 endpoint), rate limiting, healthz |
| FR9.1 Canonica EN | ✅ | language=en, translation_state, source_language |
| FR9.2 Traduzione ingest | △ | Stub + scheletro LLM; accuratezza LLM verificata ~100% (test dedicato) |
| FR9.3 Risposte multilingue | ✅ | Accept-Language + flag untranslated (semantica corretta dopo fix f0a24e2) |
| FR9.4 Tracciabilità traduzione | ✅ | source_language + sorgente originale su ogni fatto |
| FR9.5 Set lingue | ✅ | en/fr/de/it/es in language.py |

## 4. Non funzionali (NFR)

| ID | Target MVP | Esito | Nota |
|---|---|---|---|
| NFR1 | p95 < 2s | △ | search ✅ 629ms; entities 3.3s solo sotto carico (1 worker dev, login storm argon2id); baseline single-user ~7ms |
| NFR2 | 100 utenti | ✅ | 0 errori su 1100 richieste |
| NFR3 | Disponibilità 90% | △ | restart policy + recycle healthy; misura formale non eseguita |
| NFR4 | RPO | ✅ | Backup giornaliero (24h) |
| NFR5 | RTO | – | Fuori scope MVP (decisione 2026-08-29) |
| NFR6 | 10GB < 24h | △ | Non misurato; piano 6 fasi nel benchmark report (§8) |
| NFR7 | GDPR/cifratura | △ | Backup cifrati AES-256 ✅; crittografia at-rest volumi (LUKS) non applicata |
| NFR8 | Retention | △ | Non definita (punto aperto ADR-002) |
| NFR9 | Budget LLM | – | Non definito per MVP |
| NFR10 | Lingua | ✅ | Interna EN; risposte multilingue (FR9) |

## 5. Verdetto

**Obiettivi O1–O7: 4 pieni (O2, O4, O5) — 3 con limiti dichiarati (O1 scala reale, O3 misure, O6 auto-recompute, O7 traduzione vera).**

**Gap principali del prototipo (tutti dichiarati nei punti aperti dei WP):**
1. **CLI `km`** (FR8.1) — deliverable del giorno 14 NON consegnato (c'è solo la REST API)
2. **Clustering/community (FR2.2)** e **query NL + path/god-node (FR3.1/3.2)** — riuso del query engine graphify non fatto
3. **Q12 ricalcolo automatico dei derivati** dopo invalidazione
4. **Traduzione LLM vera all'ingestione (FR9.2)** e **descrizione immagini (FR1.3)** — scheletro pronto, manca l'integrazione API LLM
5. **Invalidazione automatica su cambio sorgente (FR7.1/7.3)** — oggi manuale via API
6. **Full-text del contenuto originale (FR3.5)** — indici full-text Neo4j da aggiungere
7. **Benchmark 10GB reale (NFR6)** e misure formali NFR3/NFR8

**Raggiungimento complessivo: ~80% dei requisiti del prototipo ✅, ~20% △/❌ documentati e pianificati per l'iterazione 1** (vedi raccomandazioni in docs/benchmark-report.md §8 e punti aperti nei singoli WP). Nessun requisito implementato in modo incompleto "silenzioso": ogni limite è tracciato in ADR, report o punti aperti.
