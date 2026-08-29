# Gap Analysis — Graphify vs Knowledge Enterprise

**Repository analizzato:** https://github.com/Graphify-Labs/graphify
**Versione:** 0.9.51 (pyproject.toml) — commit `2026-08-28` (bump to 0.9.51)
**Data analisi:** 2026-08-29
**Metodo:** analisi statica del codice sorgente (moduli Python), documentazione (README, ARCHITECTURE, BENCHMARKS, SECURITY) e struttura dei test (237 file di test).

---

## 1. Sintesi esecutiva

Graphify è uno strumento di **code-intelligence / memoria personale** per assistenti AI di coding (Claude Code, Cursor, Codex, ecc.), non una piattaforma enterprise di knowledge management. Il prodotto "Graphify Enterprise" citato nel README è un'offerta commerciale separata (graphify.com) **non presente in questo repository open-source**.

| # | Requisito | Stato | Gravità | Giudizio |
|---|---|---|---|---|
| R1 | Scala ~10GB di informazioni | ❌ Non soddisfatto | **Critico** | Storage a file JSON unico, cap 512 MiB, tutto in RAM |
| R2 | ~100 utenti concorrenti | ⚠️ Parziale | Alto | Server mono-processo, no scaling orizzontale nativo |
| R3 | Profilazione / accesso per utente | ❌ Assente | **Critico** | Nessun RBAC/ACL/tenant; un solo API key condiviso |
| R4 | Resilienza | ⚠️ Debole | Alto | Nessun DB, replica o failover; file JSON corruttibile |
| R5 | Tracciabilità delle informazioni | ⚠️ Parziale | Medio | Provenance per nodo/arco, ma nessun audit trail completo |
| R6 | Conflict check | ⚠️ Limitato | Medio | Solo conflitti tra PR di codice; merge union silenzioso |
| R7 | Fact invalidation | ⚠️ Parziale | Medio | Solo "re-verify" del learning overlay; nessuna truth-maintenance |

**Verdetto:** Graphify è eccellente come layer di estrazione/query per singolo sviluppatore o piccolo team, ma **non è adatto** come knowledge management enterprise per 10GB/100 utenti con profilazione, resilienza, tracciabilità, conflict check e fact invalidation.

---

## 2. Architettura attuale (come funziona oggi)

### 2.1 Pipeline

```
detect() → extract() → build() → cluster() → analyze() → report.generate() → export.to_*()
```

Ogni stadio vive in un modulo separato e comunica tramite dict Python e grafi NetworkX. Nessuno stato condiviso, nessun side effect fuori da `graphify-out/` (ARCHITECTURE.md).

### 2.2 Modello di storage (il punto critico)

- Il grafo è serializzato in **un singolo file `graph.json`** in formato node-link JSON (`paths.py: default_graph_json()` → `graphify-out/graph.json`).
- A runtime viene **caricato interamente in memoria** come grafo NetworkX (`serve.py:25 _load_graph` → `json.loads` + `json_graph.node_link_graph`).
- Scrittura atomica via file temporaneo + `os.replace` (`paths.py:29 _atomic_replace`, `paths.py:96 write_json_atomic`). Protezione da kill/OOM a metà scrittura, ma **nessuna garanzia di durabilità** (niente fsync, dichiarato nel docstring).
- **Cap di default: 512 MiB** sul file `graph.json` (`security.py:32 _MAX_GRAPH_FILE_BYTES`), superabile solo via env `GRAPHIFY_MAX_GRAPH_BYTES` (`security.py:35 _max_graph_file_bytes`).
- Aggiornamenti incrementali: `build.py:1626 build_merge` carica il `graph.json` esistente, fonde i nuovi chunk, e riscrive il file. I file ri-estratti **sostituiscono** il loro contributo precedente per tier (AST vs semantico).

### 2.3 Serving

- **MCP server** (`serve.py:1508 _build_server`) con due transport:
  - `stdio` (default): un server locale per sviluppatore.
  - `http` (Streamable HTTP, `serve.py:2259 serve_http`): un processo condiviso per il team.
- Cache dei grafi: `serve.py:102 _GraphContextCache` — un grafo default "pinned" + LRU di progetti (`GRAPHIFY_MAX_CONTEXTS`, default 8).
- Autenticazione: **un singolo API key** condiviso (`serve.py:2136 _ApiKeyMiddleware`, Bearer o X-API-Key, confronto constant-time). Il README dichiara: *"OAuth is a deliberate follow-up"* — non implementato.
- Hot-reload: il grafo viene ricaricato quando `mtime`/`size` del file cambiano (`_GraphContextCache.load`).

### 2.4 Modello dati

Nodo: `{id, label, source_file, source_location, file_type, ...}`
Arco: `{source, target, relation, confidence}` con `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}`.

---

## 3. Gap analysis dettagliata

### R1 — Scala ~10GB di informazioni ❌ CRITICO

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Storage | Singolo file `graph.json` (node-link JSON) | Nessun partizionamento/sharding; un file unico per 10GB di sorgenti |
| Memoria | Grafo NetworkX interamente in RAM (`serve.py:25`) | Con 10GB di sorgenti il grafo può superare facilmente il cap 512 MiB e richiedere RAM enorme per processo |
| Cap file | 512 MiB default (`security.py:32`), override via env | Limite architetturale, non solo configurazione |
| Contenuto | Il grafo contiene nodi/archi con riferimenti a `source_file`, **non il contenuto integrale** dei documenti | Le query restituiscono sottografi testuali con token-budget (default 2000 token, `serve.py` `_subgraph_to_text`); per il contenuto completo serve comunque il filesystem sorgente |
| Database | Nessuno. Neo4j/FalkorDB sono solo **target di export** (`exporters/graphdb.py:9 push_to_neo4j`, `:80 push_to_falkordb`), non storage primario | Nessuna indicizzazione DB, nessuna query nativa del grafo su larga scala |

**Evidenze:**
- `security.py:32` — `_MAX_GRAPH_FILE_BYTES = 512 * 1024 * 1024`
- `serve.py:25-60` — `_load_graph` fa `json.loads` dell'intero file
- `paths.py:29-94` — scrittura atomica ma senza fsync
- `exporters/graphdb.py:9,80` — push one-shot verso Neo4j/FalkorDB (MERGE upsert), non storage primario

**Impatto:** per un knowledge base enterprise di 10GB, il modello "un file JSON in RAM" non scala. Serve un database grafo (Neo4j/FalkorDB) come storage primario con sharding/partizionamento.

---

### R2 — ~100 utenti concorrenti ⚠️ PARZIALE

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Architettura server | Mono-processo Starlette + MCP Streamable HTTP (`serve.py:2177 _build_http_app`) | Nessuno scaling orizzontale nativo; un solo processo serve tutti |
| Load balancing | Flag `--stateless` per deploy dietro LB (documentato) | Esiste ma non testato/garantito per 100 utenti; il grafo è in memoria per processo |
| Sessioni | Session manager MCP con idle timeout (`--session-timeout`, default 3600s) | Gestione sessioni presente, ma nessun pooling/rate-limiting |
| Rate limiting | **Assente** (nessun throttle nel codice) | Un utente può saturare il server |
| Concorrenza | `_GraphContextCache` thread-safe con lock (`serve.py:102-163`) | Solo la cache è thread-safe; il grafo NetworkX condiviso tra richieste concorrenti non è pensato per scritture |

**Evidenze:**
- `serve.py:2177` — `_build_http_app` costruisce una singola app Starlette
- `serve.py:102-163` — `_GraphContextCache` con `threading.Lock` per la sola cache
- Nessun rate limiter in `serve.py` (grep: nessun match per rate-limit/throttle)

**Impatto:** per 100 utenti servono più istanze stateless dietro un load balancer, con il grafo in un DB condiviso invece che in memoria per processo. Il flag `--stateless` è il punto di partenza, ma manca l'infrastruttura.

---

### R3 — Profilazione / accesso per utente ❌ ASSENTE (CRITICO)

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Autenticazione | Un singolo API key condiviso (`serve.py:2136 _ApiKeyMiddleware`) | Nessuna identità per utente; OAuth dichiarato "follow-up" |
| Autorizzazione | **Nessuna** | Nessun RBAC, ACL, ruolo, permesso |
| Tenant isolation | **Nessuna** | Tutti gli utenti vedono l'intero grafo |
| Profilazione | **Nessuna** | Nessun concetto di profilo utente, preferenze, visibilità per ruolo/team |
| Audit per utente | **Nessuna** | Il query log (`querylog.py`) è opt-in e non legato a identità utente |

**Evidenze:**
- `serve.py:2136-2175` — `_ApiKeyMiddleware`: un solo `_expected` key, confronto `hmac.compare_digest`
- README: *"OAuth is a deliberate follow-up"*
- Grep su tutto il codice: nessun match per `rbac`, `acl`, `tenant`, `permission`, `role-based`, `sso`, `ldap` (solo falsi positivi in commenti/docstring)
- `querylog.py:15` — log opt-in, senza campo utente

**Impatto:** requisito **bloccante** per un knowledge enterprise con profilazione. Richiede: autenticazione per utente (OIDC/SSO), attributi di visibilità sui nodi (`visibility: [role, team, tenant]`), filtro del sottografo nel query engine, e audit per utente.

---

### R4 — Resilienza ⚠️ DEBOLE

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Persistenza | File JSON su disco, scrittura atomica (`paths.py:29`) | Nessun DB con transazioni/WAL; corruzione possibile |
| Corruzione | `serve.py:25-60` gestisce `JSONDecodeError` con messaggio di recovery | Il server **esce** e chiede di ricostruire il grafo; nessun auto-recovery |
| Replica/failover | **Nessuna** | Un solo file, un solo processo |
| Backup | **Nessuno** (solo git per il repo) | Nessun backup/restore del knowledge base |
| Protezione dati | Shrink-guard (`watch.py:892 _check_shrink`): rifiuta di sovrascrivere un grafo più grande con uno più piccolo | Protezione presente ma solo per il caso "estrazione incompleta" |
| Concorrenza di scrittura | Lock di rebuild (`watch.py:159 _rebuild_lock`) | Pensato per un singolo repo/dev, non per scritture concorrenti di 100 utenti |

**Evidenze:**
- `serve.py:25-60` — `_load_graph` su `JSONDecodeError`: `print("error: graph.json is corrupted...")` + `sys.exit(1)`
- `watch.py:892` — `_check_shrink` (shrink guard)
- `watch.py:159` — `_rebuild_lock`
- `paths.py:29-94` — atomic replace senza fsync (durabilità non garantita, dichiarato)

**Impatto:** per un sistema enterprise serve un database con transazioni, replica (read replicas), backup/restore e failover. Il modello file JSON è fragile.

---

### R5 — Tracciabilità delle informazioni ⚠️ PARZIALE

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Provenance per nodo | `source_file` + `source_location` su ogni nodo | Buono, ma nessun campo "autore/utente che ha inserito" |
| Provenance per arco | `confidence` (EXTRACTED/INFERRED/AMBIGUOUS) | Buono per la fiducia, non per l'audit |
| Diff tra snapshot | `analyze.py:556 graph_diff(G_old, G_new)` → nodi/archi aggiunti/rimossi | Confronto di due snapshot, non un journal persistente |
| Learning overlay | `reflect.py:46 .graphify_learning.json` sidecar con provenance per nodo (max 5 voci, `_PROVENANCE_CAP`) | Provenance limitata a Q&A di lavoro, non al knowledge base |
| Versioning temporale | **Assente** | Nessun `valid_from`/`valid_to`; non si può rispondere "com'era la conoscenza al tempo T" |
| Audit trail | **Assente** | Nessun log di chi ha modificato cosa e quando |
| Merge | Merge driver git che **union-mergea** `graph.json` silenziosamente | L'informazione sui conflitti va persa |

**Evidenze:**
- `analyze.py:556` — `graph_diff` (confronto snapshot)
- `reflect.py:46-48` — `LEARNING_SIDECAR_NAME`, `_PROVENANCE_CAP = 5`
- `reflect.py:840-880` — `load_learning_overlay` + `_is_stale` (fingerprint del file sorgente)
- README: merge driver git per `graph.json` (union merge)

**Impatto:** la tracciabilità per-nodo è buona, ma manca il livello enterprise: versioning temporale dei fatti, audit log persistente, e attribuzione per utente.

---

### R6 — Conflict check ⚠️ LIMITATO

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Conflitti tra PR di codice | `prs.py` — `graphify prs --conflicts`: PR che condividono community del grafo (rischio ordine di merge) | Specifico per PR di codice, non per fatti/conoscenza |
| Nodi "contested" | `reflect.py` — nodi con segnali positivi e negativi; decide la recency | Solo per il learning overlay (Q&A), non per il knowledge base |
| Conflitti tra fatti | **Assente** | Nessun rilevamento di affermazioni contraddittorie sullo stesso nodo da sorgenti diverse |
| Risoluzione conflitti | **Assente** | Nessun workflow approve/reject; il merge driver union-mergea silenziosamente |

**Evidenze:**
- `prs.py:12` — docstring: "PRs sharing graph communities (merge-order risk)"
- `reflect.py:9` — "Contested — nodes with both positive and negative signals; recency decides"
- README: merge driver union per `graph.json`

**Impatto:** serve un rilevamento di conflitti a livello di fatti (stesso attributo con valori diversi da sorgenti/utenti diversi) con workflow di risoluzione.

---

### R7 — Fact invalidation ⚠️ PARZIALE

| Aspetto | Stato attuale | Gap |
|---|---|---|
| Invalidation su cambio sorgente | `reflect.py:868 _is_stale` — confronta `file_hash(source_file)` con il fingerprint memorizzato; flag "code changed — re-verify" | Solo per il learning overlay; nessuna invalidazione automatica dei fatti nel grafo |
| Re-estrazione | `watch.py:1889 check_update`, `watch.py:1961 watch` — ri-estrae i file cambiati | Aggiorna il grafo ma non invalida esplicitamente i fatti derivati |
| Truth-maintenance | **Assente** | Nessun sistema di propagazione dell'invalidazione lungo il grafo (se un fatto cambia, cosa ne dipende?) |
| Temporal reasoning | **Assente** | Nessuna distinzione tra fatti validi e obsoleti nel tempo |
| Recency | `reflect.py:51 _DEFAULT_HALF_LIFE_DAYS = 30.0` — peso dei segnali decade con emivita 30 giorni | Solo per il learning overlay |

**Evidenze:**
- `reflect.py:868-880` — `_is_stale`: fingerprint del file sorgente, flag stale
- `reflect.py:51` — half-life 30 giorni per i segnali
- `watch.py:1889,1961` — `check_update` / `watch`

**Impatto:** serve un sistema di truth-maintenance: invalidazione automatica dei fatti quando la sorgente cambia, propagazione dell'invalidazione ai fatti dipendenti, e versioning temporale.

---

## 4. Componenti riusabili (punti di forza)

| Componente | Modulo | Perché è riusabile |
|---|---|---|
| Estrazione codice | `extract.py`, `extractors/` | Tree-sitter AST, ~40 linguaggi, deterministica, zero LLM |
| Estrazione semantica | `llm.py` | Docs/PDF/immagini/video con chunking, retry, bisection |
| Clustering | `cluster.py:223` | Leiden + label per community |
| Query engine | `serve.py` | Scoring, BFS/DFS, subgraph-to-text, trigram index |
| Diff | `analyze.py:556 graph_diff` | Confronto snapshot già implementato |
| Export DB | `exporters/graphdb.py` | Mapping Neo4j/FalkorDB già esistente (MERGE upsert) |
| Cache | `cache.py` | Cache AST/semantica per file con hash |
| Sicurezza input | `security.py` | SSRF, path traversal, XSS, prompt injection mitigati |
| Scrittura atomica | `paths.py:29` | `_atomic_replace` riusabile per qualsiasi storage |

---

## 5. Roadmap di evoluzione (per requisito)

### Fase 1 — Storage e scala (R1, R4)
1. **Storage primario su DB grafo**: Neo4j/FalkorDB al posto di `graph.json` come fonte di verità. Il mapping esiste già (`exporters/graphdb.py`); va invertito il flusso (DB → query, non export one-shot).
2. **Rimuovere il cap 512 MiB** come limite architetturale.
3. **Partizionamento per dominio** con query che attraversano solo i partizionamenti rilevanti.
4. **Replica + backup/restore** sul DB.

### Fase 2 — Multi-utente (R2, R3)
5. **Autenticazione per utente** (OIDC/SSO) al posto del singolo API key.
6. **RBAC/ACL a livello di nodo/arco**: attributi di visibilità + filtro nel query engine.
7. **Tenant isolation** se multi-tenant.
8. **Rate limiting** e pooling connessioni.

### Fase 3 — Tracciabilità e conflitti (R5, R6)
9. **Versioning temporale dei fatti** (`valid_from`/`valid_to`).
10. **Audit log persistente** (chi, cosa, quando).
11. **Rilevamento conflitti tra fatti** + workflow di risoluzione (approve/reject).

### Fase 4 — Fact invalidation (R7)
12. **Truth-maintenance**: invalidazione automatica su cambio sorgente + propagazione ai fatti dipendenti.
13. **Temporal reasoning**: distinguere fatti validi da obsoleti.

### Stima sforzo

| Fase | Sforzo | Complessità |
|---|---|---|
| Fase 1 (storage/scala) | Alto (settimane) | Media |
| Fase 2 (multi-utente) | **Molto alto (mesi)** | **Alta** — tocca modello dati + query engine |
| Fase 3 (tracciabilità/conflitti) | Alto | Alta |
| Fase 4 (fact invalidation) | Alto | Alta |

---

## 6. Alternative strategiche

**A) Evolvere Graphify** — riusare estrazione + query engine, riscrivere storage e aggiungere RBAC/versioning. Progetto di mesi; di fatto si costruisce una piattaforma enterprise attorno a un tool pensato per un'altra cosa.

**B) Usare Graphify come componente di ingestione** — tenere Graphify per l'estrazione (il suo punto di forza) e costruire il layer enterprise (storage, auth, versioning, truth-maintenance) su una piattaforma dedicata (Neo4j + backend custom, o piattaforma knowledge management esistente). Graphify esporta già in Neo4j/FalkorDB/GraphML.

---

## 7. Conclusioni

1. **Graphify non è adatto** come knowledge management enterprise per 10GB/100 utenti con profilazione, resilienza, tracciabilità, conflict check e fact invalidation.
2. **I gap critici** sono: storage a file unico (R1), assenza totale di RBAC/profilazione (R3), e assenza di DB/replica (R4).
3. **I gap medi** sono: tracciabilità incompleta (R5), conflict check solo per PR (R6), fact invalidation solo parziale (R7).
4. **L'evoluzione è possibile** ma richiede un cambio architetturale significativo (storage su DB grafo + layer di identità/autorizzazione), stimabile in mesi di lavoro.
5. **L'alternativa più rapida** è usare Graphify come motore di estrazione e costruire il layer enterprise su una piattaforma dedicata.

---

*Documento generato da analisi statica del codice. Le righe citate si riferiscono al commit analizzato (v0.9.51, 2026-08-28).*
