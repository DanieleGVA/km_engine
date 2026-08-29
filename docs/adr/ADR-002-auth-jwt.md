# ADR-002 — Identità e accesso: JWT access+refresh, RBAC e audit

**Numero:** ADR-002
**Titolo:** JWT semplice (access + refresh) con revoca, RBAC a 4 ruoli + teams, tenant scope, audit log su Postgres; estensione OIDC-ready
**Data:** 2026-08-29
**Stato:** Approved (baseline WP1 — riferimenti: `requirements.md` FR4, FR5.2, Q1/Q7/Q8/Q9/Q11; `work-plan.md` §1, §2.2, WP3; decisioni 3, 8, 9)

---

## Status

Accettato. Formalizza le decisioni congelate #3 (JWT semplice), #8 (ruoli + teams), #9 (RBAC). L'estensione OIDC/SSO è pianificata per l'iterazione 2 con **API compatibile da subito** (D7). Non rinegoziable nel MVP.

## Context

Graphify autentica con **una sola API key condivisa** (`serve.py`, confronto constant-time): nessun concetto di utente, ruolo, scopo, revoca o audit (gap R3, critico). km_engine deve invece (FR4):

- registrazione/login per utenti reali (FR4.1) e un account di servizio per la pipeline di ingestione;
- RBAC con 4 ruoli — **Admin, Editor, Viewer, Ingestor** — più la dimensione **teams** (FR4.2, Q1);
- visibilità per ruolo/team sui contenuti (FR4.3, collegata ad ADR-001 D4);
- gestione utenti e ruoli da parte dell'Admin (FR4.4);
- revoca delle sessioni (FR4.5);
- audit log persistente delle sole **modifiche** (FR5.2, Q9);
- **tenant unico** aziendale nel MVP (Q8), ma senza chiudere la porta al multi-tenant dell'iterazione 1.

Vincoli di baseline: utenti e ruoli nel **nostro** Postgres (nessun IdP esterno nel MVP), stack Python, app layer stateless (ADR-003), risoluzione conflitti solo Admin (Q11).

## Decision

### D1 — JWT access + refresh con rotazione e revoca

- **Access token**: JWT firmato (HS256 con secret gestito via env/segreto del compose; migrazione ad asimmetrico RS256 quando arriva OIDC), scadenza corta — **15 minuti**. Claims: `sub` (user UUID), `typ: "access"`, `roles: [string]`, `teams: [string]`, `tenant: "default"`, `iat`, `exp`, `jti`.
- **Refresh token**: JWT opaco-ish con scadenza **14 giorni**, claims `sub`, `typ: "refresh"`, `jti`. **Rotazione ad ogni uso**: ogni refresh emette un nuovo refresh token e invalida il precedente (rilevataione del riutilizzo = possibile furto → revoca a cascata della famiglia).
- **Revoca (FR4.5):** la validità dei refresh token è verificata contro la tabella Postgres `refresh_tokens` (hash del token, `expires_at`, `revoked_at`). Gli access token non sono revocabili singolarmente (scadenza 15 min accettabile per il MVP); la revoca di un utente (`users.active = false`) e il logout-all invalidano tutti i refresh e ne impediscono il rinnovo.
- Lo stato di revoca vive in **Postgres**, non in memoria: l'app layer resta stateless e multi-istanza (ADR-003 D3).

### D2 — RBAC: 4 ruoli + dimensione teams

- Ruoli come **enumerazione chiusa in Postgres** (`roles.name ∈ {admin, editor, viewer, ingestor}`, CHECK + seed in `001_init.sql`), assegnazione many-to-many `user_roles` (un utente può avere più ruoli; il permesso effettivo è l'unione).
- `teams` e `user_teams` many-to-many (Q1). **Permessi effettivi = ruoli ∪ teams** dell'utente, risolti dall'auth layer e messi nei claim del token; il query engine li usa per il filtro visibilità (ADR-001 D4).
- `permissions(user_id, entity_pattern, access)` resta come **ACL granulare opzionale** (work-plan §2.2): non usata dal filtro di default nel MVP, disponibile per Admin senza modifiche allo schema.
- Risoluzione conflitti: solo **Admin** (Q11); l'estensione agli Editor autorizzati (iterazione 2) richiede solo una regola applicativa, non modifiche allo schema.
- **Ingestor** è un account di servizio: solo scrittura via job, nessun accesso UI/CLI interattivo, nessuna query del KB oltre ai job propri.

### D3 — Tenant scope: unico, ma astratto da subito

- MVP = **tenant unico** (Q8): ogni token porta `tenant: "default"` e l'auth layer risolve lo scope tenant dal claim, non da costanti sparse nel codice.
- Nessuna colonna `tenant_id` nei dati nel MVP (evita requirements non approvati); l'isolamento logico è nel layer auth + nel test di tenant isolation del gate G3. Se l'iterazione 1 introduce il multi-tenant, l'aggiunta di `tenant_id` sarà una migrazione additiva senza rotture API.

### D4 — Audit log su Postgres (solo modifiche)

- Tabella `audit_log` (FR5.2): `user_id`, `action`, `entity_id`, `entity_type`, `old_value`/`new_value` (jsonb), `ts`.
- **Solo modifiche** (Q9): CREATE/UPDATE/INVALIDATE/RESOLVE su grafo, utenti, ruoli e job. Nessun log delle query utente.
- Append-only a livello di privilegi: l'utente applicativo non ha grant di `UPDATE`/`DELETE` su `audit_log`. `user_id` nullable per azioni di sistema (pipeline, migrazione).
- L'audit di grafo fine-grained (catene di versioni) resta su Neo4j come nodi `:Version` (ADR-001 D3); `audit_log` è il registro amministrativo relazionale.

### D5 — Gestione password

- Hash con **argon2id** (libreria `argon2-cffi`; fallback configurato a **bcrypt** costo 12 se argon2 non è disponibile nell'ambiente target). Nessun password in chiaro o reversibile, mai in log né in audit.
- Politica minima: lunghezza ≥ 12, nessun requisito di complessità decorativa nel MVP (baseline NFR7: GDPR + cifratura at-rest).
- Verifica password in tempo costante; lockout progressivo dopo ripetuti fallimenti (implementazione nel WP3, rate limiting a livello nginx: ADR-003 D2).

### D6 — API e middleware

- Tutte le API (REST + CLI) autenticano via `Authorization: Bearer <access JWT>`; gli endpoint `/auth/login`, `/auth/refresh`, `/auth/register` sono i soli pubblici (dietro rate limiting).
- Il middleware risolve identità → ruoli/team/tenant → contesto di richiesta; il query engine e lo storage layer non vedono mai token, solo il contesto.
- Tenant isolation e RBAC testati ai gate G2/G3 (work-plan §4).

### D7 — Percorso di estensione a OIDC/SSO (iterazione 2), API compatibile da subito

- Il modulo auth espone un'**interfaccia interna unica** (issue/verify/refresh/revoke + resolve identity). L'implementazione MVP è "local IdP" (login+password nostro); l'implementazione iterazione 2 sostituisce il verificatore del token con la validazione JWKS di un provider OIDC **senza cambiare i contratti API**.
- I claim del token (`sub`, `roles`, `teams`, `tenant`, `typ`) sono già il modello OIDC-compatible: i client non cambiano né header né formato.
- Conseguenza architetturale: non legare alcuna logica di business al formato di emissione del token, solo ai claim risolti dal middleware.

## Alternatives considered

| Alternativa | Perché scartata |
|---|---|
| **Sessioni server-side (cookie + store)** | Coerente con la revoca immediata ma introduce stato nell'app layer (contro il principio stateless di ADR-003) e cookie nei flussi CLI/API. |
| **Token opachi + introspection endpoint** | Revoca perfetta, ma ogni richiesta costa una query al DB; con 100 utenti concorrenti e NFR1 p95<2s è overhead non necessario nel MVP. |
| **OIDC/SSO da subito** | Fuori scope MVP (requirements §7: SSO/OIDC = iterazione 2); aggiunge un IdP al compose e complessità operativa incompatibile con il prototipo in 2 settimane. |
| **API key singola (status quo graphify)** | Respinto dalla gap analysis (R3 critico): nessuna identità, nessun RBAC, nessuna revoca. |
| **Ruoli come ACL per nodo nel DB** | Granularità non richiesta (Q7: profilazione = solo controllo accessi); `permissions` resta opzionale per non gonfiare il modello. |

## Consequences

**Positive:**
- App layer stateless: il JWT access consente N istanze dietro nginx senza sticky session (ADR-003 D3).
- RBAC + teams risolve FR4.2/FR4.3 con tabelle relazionali semplici e seed già pronto in `001_init.sql`.
- Percorso OIDC protetto: claim e middleware disegnati per la sostituzione del verificatore senza rotture client.
- Audit append-only su Postgres: consultabile con SQL, non intralcia il grafo.

**Negative / costi:**
- Finestra di revoca degli access token fino a 15 min: accettata per il MVP; da ridurre (scadenza più corta o denylist `jti`) se la security review lo richiede.
- Rotazione refresh + tabella `refresh_tokens` aggiunge una tabella oltre al work-plan §2.2: è il costo minimo per FR4.5 (revoca) in architettura stateless.
- Tenant senza `tenant_id` fisico: l'isolamento è solo logico/applicativo nel MVP (validato dal test G3).

**Rischi e mitigazioni:**
- *JWT semplice insufficiente per enterprise* (rischio work-plan): mitigato da D7 (OIDC-ready) e claims stabili.
- *Secret HS256 nel compose*: rotazione del secret documentata in ADR-003; migrazione a RS256 pianificata con l'OIDC.
- *Ruoli multipli per utente con semantica non ovvia*: policy documentata (unione permissiva) e test RBAC al gate G2.

---

**Punti aperti (da validizzare in squadra):**
1. Durata finale access token (15 min proposta) e se introdurre denylist `jti` per revoca immediata.
2. Retention dell'audit log e delle righe `refresh_tokens` scadute (NFR8 non definito).
3. Seed dell'utente Admin iniziale (bootstrap: env var `ADMIN_INITIAL_PASSWORD` al primo avvio?) — da definire in WP3.
4. Se `permissions` (ACL granulare) resta nel MVP come tabella vuota o si rimanda: attualmente creata ma non usata dal filtro.

*Correlati: ADR-001 (visibilità nel grafo), ADR-003 (deploy, TLS, rate limiting), `db/postgres/001_init.sql`, `db/README.md`.*
