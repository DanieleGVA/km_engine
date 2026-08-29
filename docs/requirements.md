# Documento di Requisiti — km_engine

**Versione:** 1.0 (baseline approvata)
**Data:** 2026-08-29
**Stato:** ✅ CONGELATO (baseline per la squadra)

---

## 1. Contesto

Graphify (open-source) è uno strumento di code-intelligence/memoria personale per assistenti AI di coding. Non soddisfa i requisiti enterprise: storage a file JSON unico (cap 512 MiB), nessun RBAC/profilazione, nessun DB/replica, tracciabilità parziale, conflict check solo per PR, fact invalidation solo parziale.

**km_engine** è la riscrittura completa di Graphify come piattaforma enterprise di knowledge management.

## 2. Obiettivi

| # | Obiettivo | Requisiti collegati |
|---|---|---|
| O1 | Gestire un knowledge base di ~10GB di contenuti misti | FR1, NFR1 |
| O2 | Servire ~100 utenti concorrenti con profilazione delle informazioni | FR4, NFR2 |
| O3 | Garantire resilienza (backup, recovery) | NFR3, NFR4 |
| O4 | Tracciare ogni informazione (provenance, versioni, audit) | FR5 |
| O5 | Rilevare e gestire i conflitti tra informazioni | FR6 |
| O6 | Invalidare automaticamente i fatti obsoleti | FR7 |
| O7 | Supporto multilingue: contenuti in più lingue, risposte in più lingue, lingua interna inglese | FR9 |

## 3. Utenti e ruoli

| Ruolo | Descrizione | Permessi tipici |
|---|---|---|
| Admin | Gestione sistema, utenti, ruoli, backup, risoluzione conflitti | Tutto |
| Editor | Inserisce/corregge informazioni | Lettura+scrittura su scope autorizzato |
| Viewer | Consulta il knowledge base | Solo lettura su scope autorizzato |
| Ingestor (servizio) | Pipeline automatica di ingestione | Scrittura via job, nessun accesso UI |

**Decisioni:**
- **Q1 (default):** si aggiungono **team** come dimensione organizzativa (es. "Team Engineering", "Team Marketing") per la profilazione delle informazioni. I ruoli restano 4 (Admin, Editor, Viewer, Ingestor).

## 4. Requisiti funzionali (FR)

### FR1 — Ingestione contenuti misti
- FR1.1 Ingestione codice via AST (tree-sitter) — riuso extract.py
- FR1.2 Ingestione documenti (markdown, PDF, Office) via passaggio semantico LLM
- FR1.3 Ingestione immagini con descrizione semantica
- FR1.4 Ingestione incrementale (solo file cambiati) con cache
- FR1.5 Job-based con stato persistente e resume dopo interruzione
- FR1.6 Deduplicazione entità (riuso dedup.py)

**Decisioni:**
- **Q2 (default):** linguaggi prioritari: **Python, JavaScript/TypeScript, Go, Java, C/C++** (il resto degli ~40 linguaggi resta disponibile ma a priorità minore).
- **Q3 (default):** **video/audio rimandati** — il prototipo gestisce codice + documenti + PDF + immagini.
- **Q4 (default):** aggiornamento sorgenti a **frequenza giornaliera**. (Modello: ingestione incrementale giornaliera via job.)

### FR2 — Knowledge graph
- FR2.1 Nodi/archi con confidence (EXTRACTED/INFERRED/AMBIGUOUS)
- FR2.2 Community detection (Leiden) e label
- FR2.3 Versioning bitemporale dei fatti (valid_from/valid_to + validità sorgente)
- FR2.4 Attributi di visibilità su nodi/archi (public, roles, teams)

### FR3 — Query e ricerca
- FR3.1 Query in linguaggio naturale (riuso query engine graphify)
- FR3.2 Path query, explain, neighbors, god nodes
- FR3.3 Filtro automatico del sottografo per visibilità dell'utente
- FR3.4 Query temporale ("com'era la conoscenza al tempo T")
- FR3.5 Retrieval del contenuto sorgente (full-text del contenuto originale)

**Decisioni:**
- **Q5 (default):** **sì**, ricerca full-text del contenuto originale (es. trovare un paragrafo in un PDF), oltre al grafo.
- **Q6 (default):** nel prototipo risposte **solo come sottografo testuale** (niente RAG con generazione LLM); RAG in iterazione 2.

### FR4 — Identità e accesso
- FR4.1 Registrazione/login con JWT (access + refresh)
- FR4.2 RBAC: ruoli e permessi
- FR4.3 Visibilità per ruolo/team sui contenuti
- FR4.4 Gestione utenti e ruoli (admin)
- FR4.5 Revoca sessioni

**Decisioni:**
- **Q7 (default):** profilazione = **solo controllo accessi** (chi vede cosa). Niente personalizzazione/raccomandazioni nel prototipo.
- **Q8 (default):** **tenant unico** aziendale (niente multi-tenant nel prototipo).

### FR5 — Tracciabilità
- FR5.1 Provenance per fatto: sorgente, autore, timestamp, confidence
- FR5.2 Audit log persistente (chi ha fatto cosa, quando, prima/dopo)
- FR5.3 Versioni dei fatti consultabili e confrontabili
- FR5.4 Tracciabilità della catena: fatto → sorgente → file → riga

**Decisioni:**
- **Q9 (default):** audit log **solo sulle modifiche**, non sulle query degli utenti.

### FR6 — Conflict check
- FR6.1 Rilevamento automatico di conflitti (stesso fatto, valori diversi da sorgenti/utenti diversi)
- FR6.2 Workflow di risoluzione: pending → approved/rejected
- FR6.3 Storico delle risoluzioni

**Decisioni:**
- **Q10 (default):** risoluzione **manuale (approve/reject) con suggerimento automatico** (es. "la sorgente B è più recente della A").
- **Q11:** per ora la risoluzione è **solo Admin**. (Estendibile agli Editor autorizzati in iterazione 2.)

### FR7 — Fact invalidation
- FR7.1 Invalidazione automatica quando la sorgente cambia o sparisce
- FR7.2 Propagazione dell'invalidazione ai fatti dipendenti
- FR7.3 Ri-estrazione automatica delle sorgenti cambiate
- FR7.4 Visibilità dello stato: valido / obsoleto / in verifica

**Decisioni:**
- **Q12:** i fatti derivati (INFERRED) **si ricalcolano automaticamente** dopo l'invalidazione del fatto padre (non si limitano a marcarsi "in verifica").

### FR8 — API e interfacce
- FR8.1 CLI (comandi km)
- FR8.2 REST API (OpenAPI) — necessaria per test E2E e integrazioni

**Decisioni:**
- **Q13 (confermato):** Web UI **rimandata**; MCP server **rimandato**; nel MVP **CLI + REST API**.

### FR9 — Supporto multilingue (nuovo requisito)
- FR9.1 **Lingua interna di lavoro: inglese.** Tutti i contenuti, i fatti e i metadati sono rappresentati in inglese nel knowledge base (rappresentazione canonica).
- FR9.2 **Traduzione semantica all'ingestione:** un documento in francese (o altra lingua) viene caricato in inglese con traduzione **semantica attenta** (preserva significato, terminologia, relazioni — non traduzione letterale).
- FR9.3 **Risposte multilingue:** il sistema risponde nella lingua dell'utente (es. domanda in francese → risposta in francese), attingendo dalla rappresentazione inglese canonica.
- FR9.4 **Tracciabilità della traduzione:** ogni fatto conserva la lingua originale della sorgente e il riferimento alla sorgente originale (per audit e verifica).
- FR9.5 **Set lingue iniziale:** inglese, francese, tedesco, italiano, spagnolo. (Estensibile ad altre lingue in iterazioni successive.)

## 5. Requisiti non funzionali (NFR)

| ID | Requisito | Target (MVP) |
|---|---|---|
| NFR1 | Latenza query | p95 < 2s |
| NFR2 | Utenti concorrenti | 100 |
| NFR3 | Disponibilità | **90%** (MVP) |
| NFR4 | RPO | Non definito per MVP (default operativo: backup giornaliero) |
| NFR5 | RTO | Non definito per MVP |
| NFR6 | Throughput ingestione | 10GB indicizzabili in **< 24 ore** (Q17: ok) |
| NFR7 | Sicurezza | GDPR + cifratura at-rest (default Q18) |
| NFR8 | Retention backup | Non definito per MVP |
| NFR9 | Budget LLM | Non definito per MVP |
| NFR10 | Lingua | Interna: inglese; interfacce utente: multilingue (FR9) |

## 6. Vincoli (decisioni già prese)

| Vincolo | Scelta |
|---|---|
| Storage grafo | Neo4j |
| Storage relazionale | PostgreSQL (utenti, audit, workflow) |
| Deploy | Docker Compose su singolo server |
| Autenticazione | JWT semplice (utenti+ruoli nel nostro DB) |
| Contenuto | Misto: codice + docs + PDF + immagini (no video/audio nel MVP) |
| Compatibilità | Rottura pulita con graphify (nuove interfacce) |
| Stack | Python |
| Timeline | Prototipo in ~2 settimane, poi iterazioni |

## 7. Fuori scope (MVP)

- Video/audio (Q3)
- Web UI e MCP server (Q13 — nel MVP: CLI + REST API)
- SSO/OIDC enterprise (iterazione 2)
- Multi-tenant (Q8)
- RAG con generazione LLM (Q6 — iterazione 2)
- Personalizzazione/raccomandazioni (Q7)
- Scaling multi-server (iterazione 2)
- Audit delle query utente (Q9)
- Risoluzione conflitti da parte di Editor (Q11 — solo Admin per ora)

## 8. Decisioni approvate (riepilogo)

| ID | Decisione |
|---|---|
| Q1 | Team come dimensione organizzativa + 4 ruoli |
| Q2 | Python, JS/TS, Go, Java, C/C++ prioritari |
| Q3 | Video/audio rimandati |
| Q4 | Aggiornamento giornaliero delle sorgenti |
| Q5 | Full-text del contenuto originale: sì |
| Q6 | Solo sottografo testuale nel MVP; RAG in iterazione 2 |
| Q7 | Profilazione = solo controllo accessi |
| Q8 | Tenant unico |
| Q9 | Audit log solo modifiche (non query) |
| Q10 | Risoluzione manuale + suggerimento automatico |
| Q11 | Risoluzione conflitti: solo Admin |
| Q12 | Fatti derivati ricalcolati automaticamente |
| Q13 | Web UI rimandata; MCP rimandato; CLI + REST API nel MVP |
| Q14 | Disponibilità 90% per MVP |
| Q15/Q16/Q19/Q20/Q21 | Non necessario per MVP |
| Q17 | Indicizzazione 10GB < 24 ore |
| NEW | Multilingue: lingua interna inglese, traduzione semantica all'ingestione, risposte nella lingua dell'utente |

## 9. Note e punti da confermare

- **Nota 1 (Q13) — RISOLTA:** la REST API è **inclusa nel MVP** (confermato 2026-08-29). MCP server rimandato a iterazione 1.
- **Nota 2 (FR9):** per il MVP la traduzione semantica all'ingestione richiede chiamate LLM su tutti i contenuti non-inglesi. Il costo dipende dal volume di contenuti non-inglesi nei 10GB (Q20 rinviato).
- **Nota 3 (Q14):** disponibilità 90% = ~36 giorni/anno di indisponibilità accettabile. Adeguato per MVP, da rivedere in produzione.

---

*Baseline approvata il 2026-08-29. Documenti correlati: `graphify-gap-analysis.md`, `graphify-squad.md`, `work-plan.md` (stessa cartella).*
