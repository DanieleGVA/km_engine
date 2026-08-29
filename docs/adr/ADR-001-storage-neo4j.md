# ADR-001 — Storage: Neo4j come fonte di verità del grafo

**Numero:** ADR-001
**Titolo:** Neo4j come storage primario del knowledge graph, con versioning bitemporale, visibilità per attributi e migrazione una tantum da graph.json
**Data:** 2026-08-29
**Stato:** Approved (baseline WP1 — riferimenti: `requirements.md` FR2/FR3/FR5/FR7/FR9, NFR1/NFR6; `work-plan.md` §1, §2.1; decisioni 1, 5, 6, 7, 8)

---

## Status

Accettato. Questo ADR formalizza le decisioni già congelate nella baseline (decisioni #1, #5, #7, #8 di `work-plan.md`). Non rinegoziable nel MVP; revisione possibile solo nelle iterazioni successive, con nuovo ADR.

## Context

Graphify persiste l'intero grafo in un **singolo file `graph.json`** (node-link JSON) caricato integralmente in RAM come grafo NetworkX, con cap di default 512 MiB, nessuna transazionalità, nessun indice, nessun controllo accessi a livello di storage (vedi `graphify-gap-analysis.md`, R1/R3/R4).

km_engine deve invece:

- gestire un knowledge base di **~10GB di contenuti misti** (codice + docs + PDF + immagini; FR1, NFR6);
- servire **~100 utenti concorrenti** con p95 < 2s (NFR1, NFR2);
- garantire **versioning bitemporale** dei fatti (FR2.3): tempo di sistema (`valid_from`/`valid_to`) e validità dichiarata dalla sorgente (`source_valid_from`/`source_valid_to`), con versioni mai cancellate;
- applicare **visibilità** `{public, roles, teams}` su Entity/Fact e filtrare il sottografo nel query engine (FR2.4, FR3.3);
- supportare query temporali "com'era la conoscenza al tempo T" (FR3.4), provenance completa (FR5) e invalidazione con propagazione (FR7);
- supportare la **lingua interna inglese** con provenance della lingua originale (FR9.1, FR9.4).

La squadra ha già deciso: **Neo4j come grafo primario ACID**, Postgres per identità/audit/workflow, rottura pulita con graphify e migrazione una tantum.

## Decision

### D1 — Neo4j è la fonte di verità del grafo

- Neo4j (5.x, container `neo4j:5.26` del compose base) è l'unico storage autoritativo per nodi, archi, fatti, versioni e visibilità.
- Postgres ospita identità, ruoli, audit log, job di ingestione e workflow conflitti (ADR-002, `db/postgres/001_init.sql`). La separazione delle responsabilità è netta: **nessun dato di grafo in Postgres, nessun dato di identità/audit in Neo4j** (i riferimenti incrociati sono per `id`/`user_id`, non per FK).
- Tutte le scritture sul grafo passano dallo storage layer Python (WP2) in transazione Neo4j. Nessun accesso diretto in scrittura da parte di CLI/utenti.

### D2 — Modello dati (label e relazioni)

Conforme a `work-plan.md` §2.1 e implementato in `db/neo4j/schema.cypher`:

- Label: `:Entity`, `:Fact`, `:Source`, `:Version`.
- Relazioni: `(:Entity)-[:HAS_FACT]->(:Fact)`, `(:Entity)-[:RELATES_TO {relation, confidence, valid_from, valid_to, source_valid_from, source_valid_to}]->(:Entity)`, `(:Fact)-[:DERIVED_FROM]->(:Source)`, `(:Fact)-[:VERSION_OF]->(:Fact)`, `(:Version)-[:VERSIONS]->(:Entity|:Fact)` per l'audit nel grafo.
- Ogni nodo `Fact` e ogni arco `RELATES_TO` porta: `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}`, `status ∈ {valid, obsolete, under_review}` (FR7.4), i quattro timestamp bitemporali e il riferimento `source_id`/`author_id` (provenance, FR5.1).

### D3 — Versioning bitemporale: nodi versione + VERSION_OF, mai delete

Neo4j non ha bitemporalità nativa; il pattern scelto è **version-nodes + VERSION_OF**:

- **Convenzione degli intervalli:** `valid_to IS NULL` = intervallo di sistema aperto (versione corrente). L'invalidazione *chiude* l'intervallo scrivendo `valid_to = now()` e `status = obsolete`. Non esiste alcun `DELETE` applicativo su Entity/Fact/Version (FR2.3, FR7.1).
- **Update di un fatto = nuova versione:** si crea un nuovo nodo `Fact` (nuovo `id`), si chiude l'intervallo della versione precedente e si crea l'arco `(old)-[:VERSION_OF]->(new)`. La catena VERSION_OF è ordinata e consultabile/confrontabile (FR5.3).
- **Due assi temporali:** `valid_from`/`valid_to` (tempo di sistema: quando il fatto è entrato/uscito dal KB) e `source_valid_from`/`source_valid_to` (validità dichiarata dalla sorgente). La query "al tempo T" (FR3.4) filtra sull'asse di sistema; i fatti storici restano interrogabili su entrambi.
- **Fatti derivati (INFERRED, Q12):** quando il fatto padre viene invalidato, i derivati **si ricalcolano**: il risultato del ricalcolo è una nuova versione VERSION_OF del fatto derivato con `change_type = 'recomputed'`; il precedente resta nella catena. Gli archi RELATES_TO derivati seguono la stessa regola.
- `:Version {id, created_at, author_id, change_type}` registra nel grafo ogni atto di versioning (create / update / invalidate / recompute) per l'audit di grafo; l'audit amministrativo completo resta su Postgres (ADR-002).

### D4 — Visibilità come attributi + filtro nel query engine

- Visibilità come **proprietà piatte** su Entity e Fact: `is_public: boolean`, `roles: [string]`, `teams: [string]` (invece di nodi/relazioni `VISIBLE_TO`). Motivi: (a) la visibilità viaggia con il dato e con le sue versioni; (b) un filtro su proprietà è un semplice predicato `WHERE` nel query engine; (c) evita l'esplosione di nodi ACL per 10GB di contenuti.
- Il **query engine applica il filtro** (WP5): per ogni utente, permessi effettivi = ruoli ∪ team (risolti da ADR-002); il sottografo restituito esclude tutto ciò che non è visibile (FR3.3). Default **deny**: un nodo senza attributi di visibilità è visibile solo a Admin/Editor con scope autorizzato, mai pubblico per default.
- La visibilità è ereditata nel flusso: un `Fact` senza attributi propri eredita quelli dell'`Entity` di appartenenza; la policy di preferenza (esplicito vince su ereditato) è implementata nel query engine, non nello schema.

### D5 — Gestione confidence

- `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}` su `:Entity`, `:Fact` e su ogni arco `RELATES_TO` (FR2.1), riuso del rubric di graphify.
- La confidence è **provenance-carrying**: i fatti `INFERRED` hanno sempre `DERIVED_FROM` verso la Source (o il fatto) da cui derivano; la catena fatto → sorgente → file → riga (FR5.4) è ricostruibile tramite `source_file`/`source_location` su Entity e `uri`/`hash` su Source.
- La confidence partecipa al suggerimento automatico di risoluzione dei conflitti (Q10: "la sorgente B è più recente/EXTRACTED della A"); il workflow di risoluzione è su Postgres (ADR-002, tabella `conflicts`).

### D6 — Migrazione graph.json → Neo4j (una tantum)

- Rottura pulita (decisione #5): **nessuna compatibilità di formato**. La migrazione è uno script Python una tantum (WP2) che legge `graph.json` (formato node-link di graphify) e scrive su Neo4j con `MERGE` + transazioni chunked.
- Mapping: nodi graphify → `:Entity` (con `source_file`, `source_location`, `confidence`); archi → `RELATES_TO` (con `relation`, `confidence`); ogni nodo/arche migra con `valid_from = ts_migrazione`, `valid_to = NULL`, `source_valid_* = NULL` (graphify non dichiara validità sorgente), `confidence` invariata, visibilità di default (non pubblico).
- Le sorgenti originali vengono re-registrate come `:Source` con `uri` e `hash` per abilitare l'incrementale successivo (FR1.4).
- **Test di parità obbligatorio** (work-plan, regola QA 4): conteggio nodi/archi e confronto id-per-id tra graph.json e Neo4j prima del go-live. Il `graph.json` originale resta in archivio read-only; non è più fonte di verità.

### D7 — Full-text del contenuto originale (Q5, FR3.5)

- Nel MVP: indici full-text Neo4j su `Entity.label` e `Fact.value` (vedi `schema.cypher`) per la ricerca sul grafo e sui valori dei fatti.
- Il testo integrale dei documenti (paragrafi PDF ecc.) non risiede nel modello §2.1: vedi "Punti aperti" — da validizzare con la squadra dove persistere i chunk di contenuto (property `text` su `:Source`/nodi chunk vs filesystem indicizzato).

## Alternatives considered

| Alternativa | Perché scartata |
|---|---|
| **FalkorDB** (Redis-based) | Buone performance, ma ecosistema/strumentazione meno maturi di Neo4j per ACID, backup/restore e operatività enterprise; nessun vantaggio decisivo sul carico target. |
| **PostgreSQL + Apache AGE** (grafo su relazionale) | Un solo DB da operare, ma query ricorsive/path-finding meno naturali e performanti; AGE meno maturo del driver Neo4j ufficiale; il modello bitemporale a version-nodes funziona meglio su DB nativo. |
| **Postgres puro (tabelle nodes/edges)** | Perde il query engine a traversata; community detection e path query (FR3.2) richiederebbero SQL ricorsivo pesante; riuso del query engine graphify più difficile. |
| **File graph.json + cache (status quo graphify)** | Respinto dalla gap analysis: cap 512 MiB, tutto in RAM, nessuna concorrenza/ACID (R1/R2/R4 critici). |
| **ArangoDB / multi-model** | Validato tecnicamente ma nessun membro della squadra lo conosce; Neo4j massimizza riuso dell'exporter esistente di graphify e del driver ufficiale Python. |

## Consequences

**Positive:**
- ACID + indici + query traversative native → NFR1 (p95 < 2s) raggiungibile con indici mirati (`schema.cypher`).
- Bitemporalità implementata con un pattern esplicito, testabile (gate G1: test valid_from/valid_to) e senza feature enterprise-only.
- Migrazione una tantum con test di parità → nessun vincolo di compatibilità con graphify sul lungo periodo.
- Visibilità per attributi → filtro semplice e veloce, nessuna esplosione di nodi ACL.

**Negative / costi:**
- Il versioning a nodi aumenta il numero di nodi `Fact` (una versione per update): crescita lineare con le modifiche; mitigazione: chiusura intervalli, niente copie intermedie, compattazione solo in iterazioni future (mai delete nel MVP).
- Il filtro visibilità è applicato a livello applicativo, non dal DB: correttezza dipende dal query engine → test obbligatori al gate G5 (utente vede solo ciò che può).
- Constraint di esistenza proprietà e node-key sono Enterprise-only: la garanzia di completezza delle proprietà è responsabilità dello storage layer Python (documentato in `schema.cypher`).

**Rischi e mitigazioni:**
- *Neo4j bitemporal non nativo* (rischio alto nel work-plan) → pattern version-nodes + VERSION_OF, test dedicati al gate G1.
- *Migrazione da graph.json fragile* → script + test di parità automatico (regola QA 4), archivio read-only del file originale.
- *Query temporali costose su catene lunghe* → indici su `valid_from`, `status`, `property` (vedi schema); eventuale indice composito da valutare al benchmark G8.

---

**Punti aperti (da validizzare in squadra):**
1. Dove persistere il full-text integrale dei contenuti (Q5): property `text` su Source/chunk-node in Neo4j vs filesystem/object store indicizzato a parte.
2. Indice composito `(property, status)` su `:Fact` se il benchmark G8 lo richiede.
3. Limite di profondità della catena VERSION_OF per la UI di confronto versioni (FR5.3).
4. Politica di ereditarietà visibilità Fact→Entity: confermare "esplicito vince su ereditato".

*Correlati: ADR-002 (identità/audit su Postgres), ADR-003 (deploy/backup), `db/neo4j/schema.cypher`, `db/postgres/001_init.sql`, `db/README.md`.*
