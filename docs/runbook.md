# Runbook — km_engine (WP7, gate G8)

Operazioni operative per lo stack prod-like `deploy/docker-compose.yml`
(ADR-003: nginx gateway → km-api stateless ×N → neo4j + postgres).

**Scope change 2026-08-29:** TLS e RTO formale sono **fuori MVP** (iterazione
1/2). nginx serve in plain HTTP; il restore è testato ma senza obiettivo RTO
formale. RPO = 24h (backup giornaliero, default NFR4).

---

## 1. Panoramica dello stack

| Servizio | Container | Porte pubbliche | Note |
|---|---|---|---|
| nginx | `km-nginx` | 80 (HTTP) | gateway, rate limiting, header sicurezza |
| km-api | `km-engine-prod-km-api-1/-2` | — | app stateless, 2 repliche, healthcheck `/api/v1/healthz` |
| neo4j | `km-neo4j-prod` | — | grafo; porte NON pubblicate (solo rete interna) |
| postgres | `km-postgres-prod` | — | identità/audit/workflow; porte NON pubblicate |

Tutti i servizi: `restart: unless-stopped` + healthcheck (ADR-003 D4/D7).
Volumi: `km-engine-prod_neo4j-data`, `km-engine-prod_postgres-data`.

---

## 2. Avvio dello stack

```bash
cd /path/to/km_engine

# 1) ambiente: copiare e personalizzare i segreti
cp deploy/.env.example deploy/.env
#    editare deploy/.env: NEO4J_AUTH, POSTGRES_PASSWORD, KM_JWT_SECRET,
#    KM_ADMIN_PASSWORD, BACKUP_PASSPHRASE
#    KM_JWT_SECRET:  openssl rand -hex 32
#    BACKUP_PASSPHRASE: openssl rand -base64 32

# 2) build immagine km-api + avvio stack (2 repliche)
docker compose -f deploy/docker-compose.yml up -d --build --scale km-api=2

# 3) verifica
docker compose -f deploy/docker-compose.yml ps
curl http://localhost/api/v1/healthz        # {"status":"healthy",...}
```

### 2.1 Bootstrap admin e schema DB (primo avvio)

Lo schema DB viene creato al primo avvio di Postgres (init script del volume
`postgres-data`). Il bootstrap dell'utente Admin (`KM_ADMIN_USERNAME` /
`KM_ADMIN_PASSWORD`) è idempotente e va eseguito una volta:

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U km -d km_engine -v ON_ERROR_STOP=1 -f - < db/postgres/001_init.sql

# bootstrap admin (usa le credenziali di deploy/.env)
docker compose -f deploy/docker-compose.yml exec -T km-api \
  python -c "import psycopg; from app.auth import bootstrap_admin, get_auth_settings; \
  c=psycopg.connect(get_auth_settings().pg_dsn, autocommit=True); \
  print(bootstrap_admin(c)); c.close()"
```

Schema Neo4j (idempotente):

```bash
docker compose -f deploy/docker-compose.yml exec -T neo4j \
  cypher-shell -u neo4j -p "$(grep NEO4J_AUTH deploy/.env | cut -d/ -f2)" \
  -f /import/schema.cypher
```

Flusso Iterazione A (domain layer): bootstrap pack → translate → verify →
canonicalize → extract → query → recompose; test E2E
`uv run pytest tests/e2e/test_iteration_a_flow.py` (dati `ia6_`, pulizia automatica).

### 2.2 Scaling

```bash
# 3 repliche
docker compose -f deploy/docker-compose.yml up -d --scale km-api=3
# nginx fa load balance via DNS del compose (nessuno sticky session, ADR-003 D3)
```

---

## 3. Backup (RPO 24h)

Backup **giornaliero** (finestra notturna, allineato a Q4): dump Neo4j offline
coerente + `pg_dump -Fc`, tar + cifratura AES-256 (NFR7), retention 7 giorni.

```bash
# manuale
BACKUP_PASSPHRASE="$(cat /path/to/secret)" scripts/backup.sh

# schedulazione (cron, es. 02:30)
30 2 * * * cd /path/to/km_engine && BACKUP_PASSPHRASE="$(cat /path/to/secret)" scripts/backup.sh >> /var/log/km-backup.log 2>&1
```

Output: `./backups/km_engine_YYYYmmdd_HHMMSS.enc` + log `./backups/backup.log`.

**RPO raggiungibile: 24h** (default NFR4). Nel caso peggiore si perdono al
massimo le modifiche dell'ultimo giorno. Miglioramenti (dump incrementali,
WAL shipping) sono opzione per l'iterazione 3, non impegno MVP.

**Destinazione off-server (punto aperto ADR-003 #3):** in produzione il
`BACKUP_DIR` deve essere un mount off-server (secondo disco, NAS o object
storage montato). Configurare `BACKUP_DIR=/mnt/backups` e verificare che il
mount sia visibile dall'host. La cifratura AES-256 rende i backup sicuri
at-rest anche se il supporto viene compromesso (NFR7).

**Verifica periodica:** rieseguire il roundtrip con
`tests/deploy/test_backup_restore.sh` (su ambiente dev) o un restore di prova
in staging.

---

## 4. Restore

```bash
# ultimo backup
BACKUP_PASSPHRASE="$(cat /path/to/secret)" scripts/restore.sh

# backup specifico
BACKUP_PASSPHRASE="$(cat /path/to/secret)" scripts/restore.sh ./backups/km_engine_20260829_023000.enc
```

Procedura (distruttiva — sostituisce i dati correnti):
1. decifratura AES-256 + estrazione
2. Postgres: drop/recreate database + `pg_restore -Fc`
3. Neo4j: stop → `neo4j-admin database load --overwrite-destination` → start
4. smoke test (healthz dei servizi)

**RTO:** nessun target formale nel MVP (NFR5 non definito; scope change
2026-08-29). Su dati di prova il restore completo richiede pochi minuti
(dominato dal load del dump Neo4j). Prima della produzione va concordato un
target RTO col committente e misurato su dati reali.

---

## 5. Failover

### 5.1 Guasto di un'istanza km-api (automatico)

`restart: unless-stopped` riavvia il container se esce; l'healthcheck
`/api/v1/healthz` (composito bolt+psql) isola l'istanza non-healthy da nginx.
Con 2 repliche il servizio non si interrompe.

Test automatico (richiede stack prod attivo):

```bash
tests/deploy/test_failover.sh
```

### 5.2 Healthcheck recycle (container vivo ma non-healthy)

`restart: unless-stopped` NON riavvia i container che restano vivi ma
unhealthy. Schedulare il recycle via cron (ogni minuto):

```bash
* * * * * cd /path/to/km_engine && scripts/recycle_unhealthy.sh >> /var/log/km-recycle.log 2>&1
```

### 5.3 Guasto host (manuale — runbook)

1. Provisioning di un nuovo server (stesso OS/Docker).
2. Copia del repo + `deploy/.env` (segreti) sul nuovo host.
3. `docker compose -f deploy/docker-compose.yml up -d --build --scale km-api=2`
4. Restore dell'ultimo backup (sezione 4).
5. Smoke test: `curl http://<host>/api/v1/healthz` + login admin.
6. Ripristino schedulazioni (backup cron, recycle cron).

Disponibilità 90% (NFR3) è compatibile: guasti transitori coperti dai restart,
guasti gravi entro il budget annuo di tolleranza (~36 giorni/anno).

---

## 6. Rotazione dei segreti

### 6.1 JWT secret

```bash
# 1) nuovo secret
NEW_SECRET="$(openssl rand -hex 32)"
# 2) aggiornare deploy/.env (KM_JWT_SECRET) e riavviare le repliche
sed -i '' "s/^KM_JWT_SECRET=.*/KM_JWT_SECRET=$NEW_SECRET/" deploy/.env
docker compose -f deploy/docker-compose.yml up -d --force-recreate km-api
# 3) i token emessi col vecchio secret diventano invalidi: gli utenti
#    rifanno il login (refresh token revocati alla prima rotazione)
```

### 6.2 Password DB / Neo4j

Cambiare `NEO4J_AUTH` e `POSTGRES_PASSWORD` in `deploy/.env`, poi
`docker compose up -d --force-recreate neo4j postgres km-api` e aggiornare
`KM_NEO4J_PASSWORD` / `KM_PG_DSN` di conseguenza. I volumi persistono i dati;
la password Neo4j si cambia con `neo4j-admin dbms set-initial-password` solo
su DB nuovo (per un DB esistente: `ALTER USER` via cypher-shell).

### 6.3 Passphrase backup

La passphrase è necessaria per decifrare i backup esistenti: conservarla in un
secret manager. Se ruotata, i backup vecchi restano cifrati con la vecchia
passphrase (documentare la rotazione nel log).

---

## 7. TLS (fuori MVP)

Il gateway serve **plain HTTP** (scope change 2026-08-29). La terminazione TLS
(Let's Encrypt o certificati aziendali) arriva in iterazione 1/2; ADR-003 D2
resta il riferimento. Quando attivata: nginx su 443 + redirect 80→443, HSTS,
rinnovo automatico certificati.

---

## 8. Troubleshooting

| Sintomo | Causa probabile | Azione |
|---|---|---|
| `curl /api/v1/healthz` → `"status":"unhealthy"` | Neo4j o Postgres giù | `docker compose ps`; `docker compose logs neo4j postgres`; riavviare il servizio interessato |
| Container `unhealthy` ma vivo | dipendenza lenta/irraggiungibile | `scripts/recycle_unhealthy.sh`; verificare i log dell'app |
| 429 Too Many Requests | rate limiting nginx (20r/s API, 5r/s auth) | attendere; verificare che il client non sia in loop; tuning soglie in `deploy/nginx/nginx.conf` |
| Login fallisce dopo restore | utenti non nel dump (backup vecchio) | verificare data del backup; RPO 24h atteso |
| Backup fallisce: "database is mounted" | dump Neo4j con servizio attivo | lo script ferma neo4j prima del dump; verificare che nessun altro processo usi il volume |
| `BACKUP_PASSPHRASE` mancante | env non esportata | `export BACKUP_PASSPHRASE=...` o passarla inline |
| Porte DB raggiungibili dall'esterno | stack dev (root compose) attivo | in prod usare solo `deploy/docker-compose.yml` (porte DB non pubblicate) |
| nginx 502 | repliche km-api non healthy | `docker compose ps`; `docker compose logs km-api`; healthcheck `/api/v1/healthz` |

### Log utili

```bash
docker compose -f deploy/docker-compose.yml logs -f nginx km-api neo4j postgres
tail -f backups/backup.log backups/restore.log
```

---

## 9. Test del gate G8 (riepilogo)

| Test | Comando | Dove gira |
|---|---|---|
| Healthcheck composito | `pytest tests/deploy/test_healthz.py` | dev (container attivi) |
| Backup/restore roundtrip | `tests/deploy/test_backup_restore.sh` | dev (container attivi) |
| Failover (kill km-api) | `tests/deploy/test_failover.sh` | prod stack (SKIP se non attivo) |
| Load test 100 utenti | `scripts/loadtest.py --users 100` | app locale o stack prod |
