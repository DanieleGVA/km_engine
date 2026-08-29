# ADR-003 — Deploy: Docker Compose su singolo server

**Numero:** ADR-003
**Titolo:** Docker Compose su singolo server con gateway nginx, app layer stateless multi-istanza, healthcheck, backup/restore e failover minimale
**Data:** 2026-08-29
**Stato:** Approved (baseline WP1 — riferimenti: `requirements.md` NFR2/NFR3/NFR4/NFR7, §6 vincoli; `work-plan.md` §1, WP7, G8; decisioni 2)

---

## Status

Accettato. Formalizza la decisione congelata #2 (Docker Compose su singolo server). L'architettura è quella del diagramma §1 del work-plan. Lo scaling multi-server è esplicitamente fuori dal MVP (requirements §7).

## Context

- Obiettivi NFR del MVP: **100 utenti concorrenti** (NFR2), **disponibilità 90%** (NFR3, ~36 giorni/anno di tolleranza — nota 3 dei requirements), backup giornaliero come default operativo (NFR4: RPO non definito formalmente), **GDPR + cifratura at-rest** (NFR7).
- Vincoli di baseline: un solo server, Docker Compose, Neo4j + Postgres come storage (ADR-001), app layer Python **stateless** riuso graphify (decisione 6).
- Il compose base esiste già (`docker-compose.yml`, fase fondamenta: `km-neo4j` + `km-postgres` con healthcheck e volumi nominali). nginx e app layer verranno aggiunti in WP5/WP7.
- Il prototipo deve essere dimostrabile al giorno 14 con backup/restore e failover testati al gate G8.

## Decision

### D1 — Topologia: singolo server, compose completo

Servizi previsti nel compose target (estensione del compose base esistente):

```
nginx (gateway) → km-api (×N, stateless) → { km-neo4j, km-postgres }
```

- Un solo host. Tutti i servizi sullo stesso compose project (`km-engine`), rete interna dedicata; i volumi persistono su disco del server.
- Le porte 7474/7687 (Neo4j) e 5432 (Postgres) restano **pubblicate solo per sviluppo** (come nel compose base); in produzione vengono rimosse dalle interfacce pubbliche e restano raggiungibili solo sulla rete interna del compose (amministrazione via `docker compose exec`).

### D2 — nginx come gateway applicativo

- **TLS termination** su nginx (443; HTTP 80 solo redirect). Certificati Let's Encrypt (renew via schedulazione) o certificati aziendali — da confermare con ops (punto aperto 2).
- **Reverse proxy** verso le istanze dell'app layer (upstream con più container `km-api`).
- **Rate limiting** (moduli `limit_req`/`limit_conn`) applicato agli endpoint pubblici, con soglie più strette su `/auth/*` (protezione brute-force, ADR-002 D5) e su query costose; tuning al load test G8.
- Header di sicurezza standard (HSTS, dimensioni corpo limitate); nessuna logica di business nel gateway.

### D3 — App layer stateless, multi-istanza

- Le istanze `km-api` (Python, WP5) non tengono **alcuno stato locale**: sessioni = JWT (ADR-002), stato dei job = Postgres, grafo = Neo4j, file temporanei solo in scratch per-container.
- Scaling orizzontale sul singolo server: `docker compose up -d --scale km-api=N` con nginx upstream dinamico (DNS del compose). Nessuno sticky session.
- La pipeline di ingestione gira come servizio/schedulazione interna (Q4: giornaliera) usando lo stesso account Ingestor (ADR-002 D2) e lo stato su `ingest_jobs` (resume, FR1.5).

### D4 — Healthcheck

- Ogni servizio dichiara un healthcheck nel compose (già presente per Neo4j/Postgres nel compose base; da aggiungere per nginx e `km-api`).
- `km-api` espone `/healthz` composito: processo vivo + connettività Neo4j (bolt) + connettività Postgres. Un'istanza non-healthy viene isolata da nginx.
- I healthcheck alimentano anche il chaos test del gate G8 (kill container → restart automatico → recovery).

### D5 — Backup

- **Neo4j:** dump coerente con `neo4j-admin database dump` (offline del singolo DB o con il servizio fermato brevemente) schedulato **giornaliero** (allineato a Q4).
- **Postgres:** `pg_dump -Fc` giornaliero (utenti, ruoli, audit, job, conflitti).
- I backup finiscono **fuori dal server** (secondo disco/NAS/object storage) con retention da definire (NFR8 aperto). Cifratura at-rest dei backup per NFR7/GDPR (es. gpg/age prima dell'upload).
- **RPO = 24h** (default operativo NFR4, backup giornaliero). Miglioramento del RPO (dump incrementali/WAL shipping) è un'opzione per l'iterazione 3, non un impegno del MVP.

### D6 — Restore e RTO

- Procedura documentata e **testata al gate G8**: restore del volume Neo4j + restore Postgres + riavvio compose + smoke test (login → query di parità).
- **RTO target: 4 ore** dal riconoscimento del guasto al servizio operativo (da validare con la squadra; NFR5 non definito formalmente). Il tempo dominante è il restore dei dati, non il riavvio dei servizi.

### D7 — Failover minimale per il prototipo

- Nessun clustering/HA: Neo4j e Postgres sono **single-node** su questo server (lo scaling multi-server è fuori scope MVP).
- Failover = **restart automatico** (`restart: unless-stopped` su tutti i servizi, già nel compose base) + healthcheck che forza il recycle dei container non-healthy.
- Failover del server (guasto host): **procedura manuale** — nuovo server, restore compose + backup D5 (RTO D6). Runbook parte del deliverable WP7.
- Disponibilità 90% (NFR3) è compatibile con questo modello: guasti transitori gestiti dai restart, guasti gravi entro il budget annuo di tolleranza.

## Alternatives considered

| Alternativa | Perché scartata |
|---|---|
| **Kubernetes / orchestratore** | Overkill per un singolo server e un prototipo in 2 settimane; costo operativo non giustificato dai NFR del MVP. |
| **Servizi gestiti (cloud: Neo4j Aura, RDS, ECS)** | Fuori dai vincoli di baseline (self-hosted su singolo server); costi e dipendenza esterna non approvati. |
| **HA attivo/attivo (replica Neo4j, Patroni per Postgres)** | Complessità operativa non compatibile con la squadra/tempo del prototipo; il target di disponibilità 90% non lo richiede. Rimandato a iterazioni successive. |
| **Bare-metal senza container** | Perde riproducibilità, healthcheck nativi e isolamento; il compose base è già operativo e validato. |
| **Più server con compose distribuito** | Lo scaling multi-server è esplicitamente fuori scope MVP (requirements §7, iterazione 2). |

## Consequences

**Positive:**
- Un solo artefatto di deploy (compose) riproducibile dev→prod; il compose base è già funzionante con Neo4j+Postgres e healthcheck.
- App stateless → scaling orizzontale a costo zero di design (NFR2: 100 utenti concorrenti) e nessun single point di stato nell'app.
- Backup/restore semplici e testabili automaticamente al gate G8 (chaos test + parità post-restore).

**Negative / costi:**
- Single point of failure a livello host: un guasto del server ferma tutto il servizio fino al restore manuale (entro RTO 4h target).
- RPO 24h: fino a un giorno di lavoro (ingestioni/modifiche) perso nel caso peggiore; accettato dal default NFR4, da ribadire con gli stakeholder.
- Le porte di sviluppo (7474/7474/5432/7687 esposte) vanno rimosse/limitate prima della demo se il server è raggiungibile fuori dalla rete fiduciaria.

**Rischi e mitigazioni:**
- *Backup Neo4j con servizio attivo*: dump coerente richiede fermo breve del DB o uso del comando supportato per la versione in uso → schedulazione notturna (finestra Q4) e test al gate G8.
- *Fuga di segreti nel compose*: tutti i segreti via `.env` (mai committato, `.env.example` come template), rotazione del secret JWT documentata.
- *Rate limiting troppo aggressivo/tenero*: tuning empirico al load test del gate G8.

---

**Punti aperti (da validizzare in squadra):**
1. RTO 4h: conferma o aggiustamento del target prima del gate G8.
2. Certificati TLS: Let's Encrypt (rinnovo automatico) vs certificati aziendali (processo manuale).
3. Destinazione dei backup off-server (NAS interno vs object storage) e retention (NFR8).
4. Modalità del dump Neo4j in produzione (fermo notturno breve vs approccio online supportato) — da verificare sulla versione 5.26 in uso.
5. Cifratura at-rest dei volumi sul server (LUKS sul disco?) oltre alla cifratura dei backup, per NFR7.

*Correlati: ADR-001 (storage Neo4j), ADR-002 (auth stateless), `docker-compose.yml` (compose base), `db/README.md`.*
