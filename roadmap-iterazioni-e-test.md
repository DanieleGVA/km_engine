# km_engine — Domain Knowledge Layer
# Roadmap completa delle iterazioni e piano di test

**Versione:** 1.0 — 2026-08-30
**Riferimenti:** `architecture.md` v1.0 (MVP, G1–G9) · `spec-iterazione-A-domain-layer.md`
**Ambito:** dal prototipo km_engine al layer di gestione della conoscenza completo:
IR markdown a due stadi, sotto-grafo canonico, RAG, layer di agenti per la
generazione di Domain Pack, industrializzazione.

---

## 0. Quadro d'insieme

| Iterazione | Titolo | Obiettivo sintetico | Durata stimata | Dipende da |
|---|---|---|---|---|
| **A** | Domain layer manuale (ricette) | Fondamenta: IR a due stadi, verifica, sotto-grafo canonico, round-trip | 2–3 settimane | MVP |
| **B** | RAG e scala | Retrieval completo, corpus intero (~1.650 ricette), performance | 2–3 settimane | A |
| **C** | Layer di agenti | Agenti che generano/evolvono Domain Pack; loop del Curator | 3–4 settimane | B |
| **D** | Generalizzazione | Secondo dominio (codice) come Domain Pack; prova dell'astrazione | 1–2 settimane | C |
| **E** | Industrializzazione | Hardening piattaforma (TLS, OIDC, full-text, 10GB), ops, rollout | 2–3 settimane | B (parallelo a C/D) |

Due binari: **A→B→C→D** costruisce il knowledge layer; **E** industrializza la
piattaforma (assorbe le "iterazioni 1/2" già indicate in `architecture.md`) e può
correre in parallelo da fine B.

**Principi vincolanti per tutte le iterazioni** (dettaglio in spec A, §1):
P1 due stadi translated/canonical · P2 LLM mai sui numeri · P3 normalizzazione mai
distruttiva · P4 glossario nel grafo · P5 gate umano sull'ontologia · P6 riuso
interfacce km_engine · P7 standard esterni prima di canoni proprietari.

**Metodo per ogni iterazione:** work-plan con gate → approvazione umana → implementazione
per WP con test verdi a ogni gate → ADR + aggiornamento `architecture.md`/`runbook.md`
→ report di iterazione con metriche.

---

## 1. Iterazione A — Domain layer manuale (ricette)

*Specifica completa in `spec-iterazione-A-domain-layer.md`. Qui il riepilogo e il piano di test.*

### Work package
- **WP-A1** Template IR + struttura Domain Pack (pack.yaml, glossari seed, regole)
- **WP-A2** Stadio 1: traduzione (`translated.md`), LLMSemanticService reale (chiude FR9.2)
- **WP-A3** Motore di verifica coerenza md↔originale a 3 livelli
- **WP-A4** Estensioni Neo4j: meta-schema, sotto-grafo canonico, `:Document`, indice vettoriale
- **WP-A5** Stadio 2: canonicalizzazione deterministica + canon-log + coda proposte glossario
- **WP-A6** Estrattore md→grafo + ricompositore + round-trip

### Piano di test
| Tipo | Test | Criterio di passaggio |
|---|---|---|
| Unit | Parser template (frontmatter, strutture inline, casi malformati) | 100% casi previsti + errori espliciti sui malformati |
| Unit | Conversioni unità (ogni riga di `units.yaml`, valori limite, arrotondamenti) | Aritmetica esatta, `rule_id` sempre presente |
| Unit | Invarianti P2 (estrazione numeri originale vs tradotto) | Nessun numero alterato in traduzione |
| Unit | Verifica L1 (documenti corrotti sintetici: numero alterato, ingrediente rimosso, step aggiunto) | Ogni corruzione intercettata a L1 |
| Integration | Verifica L2 (sezioni riscritte semanticamente) | Divergenza localizzata alla sezione giusta, escalation L3 |
| Integration | Coda L3 e adjudication (approve/reject + audit) | Workflow completo, stato riflesso su Source e frontmatter |
| Integration | Bootstrap pack idempotente (doppia esecuzione `load_domain_pack.py`) | Nessun duplicato, MERGE stabile |
| Integration | Visibilità sui nuovi nodi (Document, CanonicalTerm) | Default-deny confermato; admin bypass; nessuna lettura non filtrata |
| Integration | Canon-log completo (diff translated↔canonical vs log) | 100% delle differenze spiegate da una riga di log |
| Integration | Termini irrisolti | Vanno in coda proposte, mai normalizzazioni inventate nel md |
| **E2E** | **Round-trip**: `recompose(ingest(canonical.md)) == canonical.md` | **Verde sul 100% del corpus (50–100 ricette)** |
| E2E | Flusso completo: bootstrap → load pack → translate → verify → canonicalize → ingest → query → recompose | 1 test stabile, in aggiunta all'e2e MVP esistente |
| Qualità | Copertura ≥90%, ruff pulito, 207 test MVP ancora verdi (no regressioni) | Standard repo |

**Gate:** GA1–GA6 (dettaglio in spec A). **Uscita:** ADR-004, report con distribuzione
L1/L2/L3, copertura glossario (% mention risolte), termini in coda.

---

## 2. Iterazione B — RAG completo e scala

### Obiettivo
Il sistema risponde a interrogazioni in linguaggio naturale sul corpus intero:
il vettoriale trova il candidato, il grafo restituisce la versione canonica esatta
con provenance. Scala dal corpus pilota (~50–100) all'intero set Pareto (~1.650 ricette).

### Work package
- **WP-B1 Retrieval ibrido.** Endpoint `POST /api/v1/rag/query`: embedding della query
  → ricerca vettoriale su `Document.embedding` → espansione nel grafo (componenti,
  glossario, provenance) → risposta = documento/i canonici + metadati. Filtro
  visibilità sul risultato vettoriale **prima** della restituzione (il vettoriale non
  deve far trapelare documenti non visibili). Nessun ranking LLM in questa fase:
  ranking = similarità + boost deterministici (lingua utente, verification_level).
- **WP-B2 Query strutturate dal glossario.** Ricerche per termine canonico
  ("tutte le ricette che usano TECH-BLANCH", "ingrediente FoodOn X"): percorsi
  `CanonicalTerm ← NORMALIZED_TO ← Entity → PART_OF_DOC → Document`. Copre in forma
  domain-aware parte del gap FR3.1/3.2 (query per percorso).
- **WP-B3 Scala del corpus.** Pipeline translate→verify→canonicalize→ingest
  sull'intero set (~1.650): batch job con resume, monitoraggio della coda L3 e della
  coda proposte glossario (qui si scopre quanto i glossari seed coprono davvero).
- **WP-B4 Localizzazione delle risposte.** `localize_response` esteso ai Document:
  l'utente IT riceve la ricetta nella sua lingua se disponibile (source_language IT
  servita nativa; altrimenti canonica EN con flag, coerente con FR9.3).
- **WP-B5 Performance.** Tuning heap/pagecache Neo4j, caching TTL sulle letture di
  glossario, verifica NFR1 su stack prod-like (nginx + 2 repliche).

### Piano di test
| Tipo | Test | Criterio di passaggio |
|---|---|---|
| Unit | Ranking deterministico (similarità + boost) | Ordine stabile e spiegabile per ogni risposta |
| Integration | Visibilità nel retrieval (utenti con ruoli/team diversi, stessa query) | Zero documenti non autorizzati nei risultati, anche vettoriali |
| Integration | Query da glossario (per tecnica, ingrediente, stato) | Risultati completi e corretti su fixture note |
| Integration | Localizzazione (utente IT/FR/EN su documenti con source diverse) | Semantica FR9.3 rispettata, flag corretti |
| **E2E** | **Golden set retrieval**: ≥100 query naturali con risposta attesa (costruite dal corpus validato dallo chef) | **Recall@5 ≥ 0.9; la ricetta restituita è *esattamente* quella canonica (hash match)** |
| E2E | Round-trip sull'intero corpus (~1.650) | Verde al 100% — il criterio A regge alla scala |
| Carico | Load test retrieval (riuso `loadtest.py`: 100 utenti, mix query RAG + API) | p95 < 2s (NFR1) su stack prod-like |
| Dati | Report copertura glossario a fine B3 | ≥95% mention risolte; il residuo tracciato in coda proposte |
| Qualità | Copertura ≥90%, no regressioni A/MVP | Standard repo |

**Gate:** GB1 retrieval ✅ · GB2 query glossario ✅ · GB3 corpus intero ✅ ·
GB4 localizzazione ✅ · GB5 performance ✅.
**Uscita:** ADR-005 (retrieval ibrido), report metriche retrieval + copertura.

---

## 3. Iterazione C — Layer di agenti (Domain Pack generator)

### Obiettivo
Automatizzare i 7 passi (comprensione dominio → design → estrattori/normalizzatori →
estrazione → test → miglioramento → documentazione) come pipeline di agenti che
**produce e fa evolvere Domain Pack**, con gate umano obbligatorio sulle decisioni
di ontologia (P5). Il pack ricette dell'iterazione A è il riferimento di output.

### Work package
- **WP-C1 Domain Analyst.** Input: corpus `translated.md` (stadio 1 è domain-agnostic,
  quindi eseguibile su un dominio nuovo senza pack). Output: *domain brief* strutturato:
  entità candidate con frequenze, vocabolari da normalizzare, unità rilevate,
  ambiguità, ontologie esterne candidate (P7). Formato brief versionato nel repo.
- **WP-C2 Ontology Designer.** Dal brief genera la bozza del Domain Pack (pack.yaml,
  template.md, glossari seed, regole) conforme allo schema pydantic di A.
  Output = pull request; il merge è il gate umano.
- **WP-C3 Codegen.** Genera l'adattatore di canonicalizzazione del dominio contro le
  interfacce esistenti (parser inline specifici, regole deterministiche); vincolo:
  il codice generato deve passare gli stessi test-tipo di A (invarianti P2, canon-log
  completo). Il codice entra anch'esso via PR.
- **WP-C4 Evaluator.** Agente che costruisce ed esegue il golden set del dominio:
  campiona il corpus, genera coppie query/attesa, misura round-trip, copertura
  glossario, precision/recall normalizzazione; produce il report di gate.
- **WP-C5 Curator (loop di miglioramento).** Job periodico: mina fatti `AMBIGUOUS`,
  conflitti pending, flag `untranslated`, coda proposte → propone estensioni glossario
  (PR sui seed / proposte nel grafo) → dopo adjudication, ri-canonicalizzazione
  **incrementale** dei soli documenti toccati (via invalidation + hash cache; risolve
  in forma domain-aware il gap Q12: ricalcolo dopo invalidazione).
- **WP-C6 Documenter.** Genera la documentazione del pack da grafo + canon-log
  (decision record per ogni mappatura adjudicata: chi, quando, perché) e mantiene
  il changelog del pack tra versioni.

### Piano di test
| Tipo | Test | Criterio di passaggio |
|---|---|---|
| Unit | Schema del brief e del pack generato (validazione pydantic) | Ogni output degli agenti valida contro schema, o fallisce esplicitamente |
| Integration | Determinismo del Codegen (stesso brief → pack funzionalmente equivalente) | I test-tipo passano su rigenerazioni ripetute |
| Integration | Curator: ciclo AMBIGUOUS → proposta → approvazione → ri-canonicalizzazione incrementale | Solo i documenti toccati rielaborati; storico bitemporale intatto |
| Integration | Gate umano non aggirabile | Nessun percorso in cui glossario/template cambiano senza approvazione (test negativi espliciti) |
| **E2E** | **Rigenerazione del pack ricette**: gli agenti, dato il corpus translated delle ricette, producono un pack che raggiunge i gate di A | **Round-trip ≥ 100%, copertura glossario ≥ 90% del pack manuale — misura di quanto gli agenti replicano il riferimento** |
| E2E | Loop completo Curator su corpus con ambiguità iniettate | Ambiguità ridotte ≥ 80% dopo N cicli con adjudication simulata |
| Metriche | Riduzione del carico umano | # decisioni portate all'umano per documento in calo tra cicli (trend misurato, target definito nel work-plan) |
| Qualità | Copertura ≥90%, no regressioni | Standard repo |

**Gate:** GC1 Analyst ✅ · GC2 Designer+gate umano ✅ · GC3 Codegen ✅ ·
GC4 Evaluator ✅ · GC5 Curator loop ✅ · GC6 Documenter ✅.
**Uscita:** ADR-006 (architettura agenti, formato brief/pack, politica PR),
report "pack rigenerato vs pack manuale".

---

## 4. Iterazione D — Generalizzazione (secondo dominio: codice)

### Obiettivo
Provare che l'astrazione regge oltre le ricette, al costo più basso possibile:
riformulare la code-intelligence già presente (graphify) come Domain Pack.
IR = documentazione generata dal codice; "normalizzazione" = dedup esistente +
glossario dei concetti (Module, Function, Class, dipendenze).

### Work package
- **WP-D1** Domain Pack "code": template md (doc per modulo/file), glossario concetti,
  mapping dell'output graphify sul modello Document/Entity/NORMALIZED_TO.
- **WP-D2** Esecuzione della pipeline agenti (C) sul dominio code: il brief e la bozza
  pack sono generati dagli agenti, non a mano — è il vero test di C.
- **WP-D3** Retrieval RAG sul dominio code (riuso B senza modifiche strutturali).

### Piano di test
| Tipo | Test | Criterio di passaggio |
|---|---|---|
| Integration | Parità con graphify (già testata live nell'MVP) mantenuta passando dal pack | Stessi Entity/Fact/relazioni del percorso legacy |
| E2E | Round-trip sul dominio code (doc md ↔ grafo) | Verde sul repo di riferimento |
| E2E | Pipeline C su dominio nuovo senza modifiche al core | Zero modifiche a `app/*` per aggiungere il dominio: solo contenuto del pack |
| Retrieval | Golden set di query sul codice (≥50) | Recall@5 ≥ 0.85 |

**Gate:** GD1 pack code ✅ · GD2 generato da agenti ✅ · GD3 retrieval ✅.
**Uscita:** report "costo di un nuovo dominio" (ore umane, decisioni adjudicate,
righe di pack) — il KPI che giustifica l'intero layer.

---

## 5. Iterazione E — Industrializzazione (parallela da fine B)

### Obiettivo
Portare la piattaforma da prototipo a servizio: assorbe le raccomandazioni
iterazione 1/2 di `architecture.md` più l'operatività del knowledge layer.

### Work package
- **WP-E1 Sicurezza**: TLS sul gateway; OIDC (interfaccia già pronta); revisione
  rate limiting (store condiviso tra repliche, non in-memory); hashing async per
  il login storm.
- **WP-E2 Ricerca piattaforma**: indici full-text Neo4j al posto di CONTAINS (FR3.5).
- **WP-E3 Scala dati**: benchmark 10GB in 6 fasi (NFR6) con il knowledge layer attivo;
  misure formali NFR3/NFR8; tuning conseguente.
- **WP-E4 Operatività knowledge layer**: backup/restore estesi a corpus md + indice
  vettoriale; runbook per ri-canonicalizzazione massiva e rollback di versione pack;
  retention policy audit; metriche/alerting (code L3, proposte glossario, job falliti).
- **WP-E5 Multi-tenant** (se richiesto dal rollout): isolamento per tenant su
  Document/CanonicalTerm; decidere se i glossari sono per-tenant o condivisi (ADR).
- **WP-E6 Interfaccia di adjudication**: Web UI minima per le code L3/proposte
  glossario/conflitti — è il punto dove lavora l'esperto di dominio; finché non
  esiste, l'adjudication via API è il collo di bottiglia umano.

### Piano di test
| Tipo | Test | Criterio di passaggio |
|---|---|---|
| Sicurezza | TLS end-to-end, OIDC login+refresh, rate limit distribuito | Handshake e flussi verdi su stack prod; test di riuso token invariati |
| Carico | Benchmark 10GB (ingest + retrieval + query concorrenti) | NFR1 rispettato; report 6 fasi |
| Resilienza | Backup/restore completo (Neo4j+PG+corpus+vettoriale) su ambiente pulito | Smoke test post-restore verde; round-trip su campione post-restore |
| Resilienza | Failover replica API sotto carico | 0 errori utente oltre soglia definita |
| Ops | Rollback di versione pack (da vN a vN-1) su ambiente di prova | Storico bitemporale intatto; documenti coerenti con la versione ripristinata |
| UI | Flusso adjudication end-to-end via Web UI | L3 e proposte processabili senza chiamate API manuali |

**Gate:** GE1 sicurezza ✅ · GE2 full-text ✅ · GE3 10GB ✅ · GE4 ops ✅ ·
GE5 (multi-tenant, se attivo) ✅ · GE6 UI ✅.

---

## 6. Strategia di test trasversale

**I tre test che non si negoziano mai, in nessuna iterazione:**
1. **Round-trip** `recompose(ingest(md)) == md` — l'unico test che garantisce che il
   grafo restituisce *esattamente* la conoscenza di riferimento. Corre in CI su un
   campione fisso a ogni commit e sull'intero corpus a ogni gate.
2. **Invarianti numerici P2** — nessun passaggio (traduzione, canonicalizzazione,
   ricomposizione) altera un numero fuori da una regola tracciata.
3. **Non-regressione** — l'intera suite delle iterazioni precedenti (a partire dai
   207 test MVP) resta verde. Nessun gate si chiude con regressioni.

**Golden set:** unico asset curato che cresce per iterazione — (A) corpus pilota
validato dallo chef; (B) +query retrieval con risposte attese; (C) +ambiguità
iniettate per il Curator; (D) +dominio code. Versionato nel repo, ogni modifica
approvata da esperto di dominio.

**Metriche per il report di ogni iterazione:** round-trip pass rate · copertura
glossario (% mention risolte) · distribuzione verifiche L1/L2/L3 · precision/recall
normalizzazione sul golden set · recall@5 retrieval (da B) · decisioni umane per
documento (da C, in trend) · costo nuovo dominio (D).

**Ambienti:** dev compose per unit/integration; stack prod-like per e2e, carico e
resilienza; il benchmark 10GB solo su prod-like.

---

## 7. Rischi principali e mitigazioni

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Glossari seed insufficienti → coda L3/proposte esplode in B3 | Collo di bottiglia umano | Misurare copertura sul pilota in A; soglia minima 85% prima di scalare; UI adjudication anticipabile da E6 |
| Deriva dell'LLM in traduzione (omissioni sottili) | Conoscenza errata nel grafo | Invarianti P2 + L2 per sezione + campione umano fisso; mai fidarsi del solo score |
| Agenti (C) che aggirano il gate umano | Ontologia non governata | Test negativi espliciti; ogni modifica a pack passa da PR; audit |
| Ri-canonicalizzazione massiva su cambio glossario | Costo/tempo | Incrementale via hash cache + invalidation (WP-C5); rollback pack testato (E) |
| Vettoriale che espone documenti non visibili | Sicurezza | Filtro visibilità post-retrieval testato con utenti multipli (GB1) |
| Round-trip degrada alla scala | Perdita silenziosa d'informazione | Round-trip sul corpus intero a ogni gate, non solo sul campione |

---

*Ogni iterazione produce: work-plan approvato, ADR, aggiornamento architecture.md e
runbook.md, report con le metriche di sezione 6. La specifica di dettaglio esiste per
l'iterazione A; le specifiche B–E si scrivono alla chiusura dell'iterazione precedente,
incorporando ciò che i report hanno insegnato.*
