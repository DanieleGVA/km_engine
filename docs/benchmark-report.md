# Benchmark Report — km_engine (WP7, gate G8)

**Data:** 2026-08-29
**Autore:** DevOps/SRE (WP7)
**Scope:** load test 100 utenti concorrenti (NFR2) + baseline latenza (NFR1)

---

## 1. Ambiente di misura

| Parametro | Valore |
|---|---|
| Host | MacBook (dev), Docker Desktop, Docker 29 / Compose v5 |
| App layer | 1 istanza uvicorn locale (`app.api.app:app`), 1 worker |
| Storage | Container dev `km-neo4j` (Neo4j 5.26, heap 1g) + `km-postgres` (PG 16) |
| Gateway | nessuno (misura diretta sull'app; nginx non attivo in dev) |
| Dati | 100 entità `wp7_load_*` + 100 utenti `wp7_load_*` (ruolo viewer) |
| Client | `scripts/loadtest.py` (asyncio + httpx), 100 utenti concorrenti, 10 richieste/utente dopo login reale, X-Forwarded-For per-utente |

**Nota:** la misura è su hardware di sviluppo, singola istanza, senza nginx.
I valori in produzione (2 repliche km-api dietro nginx, server dedicato)
sono attesi migliori; il benchmark 10GB/100 utenti formale è deliverable WP8.

## 2. Metodologia

- Ogni utente esegue un **login reale** (POST /auth/login, hash argon2id) e poi
  10 richieste alternate: `GET /api/v1/entities` e `GET /api/v1/search?q=wp7_load_`.
- Latenze misurate client-side (incluso tempo di rete locale).
- Percentili calcolati con metodo inclusivo su tutti i campioni per endpoint.
- Errori = eccezioni di rete o status HTTP >= 400.

## 3. Risultati

### 3.1 Load test — 100 utenti concorrenti (gate G8)

- Data: 2026-08-29 21:34:51
- Target: `http://127.0.0.1:8000` — utenti: 100 — richieste/utente: 10
- Durata totale: 11.6s — richieste totali: 1100

| Endpoint | Richieste | p50 (ms) | p95 (ms) | p99 (ms) | Errori | HTTP>=400 |
|---|---|---|---|---|---|---|
| entities | 500 | 647 | 3227 | 4708 | 0 | 0 |
| login | 100 | 3180 | 4749 | 4971 | 0 | 0 |
| search | 500 | 644 | 833 | 886 | 0 | 0 |

### 3.2 Interpretazione

- **Zero errori** su 1100 richieste: il sistema regge 100 utenti concorrenti
  senza fallimenti (NFR2 soddisfatto lato correttezza).
- **search** è veloce e stabile (p95 833ms < 2s NFR1).
- **entities** p95 3.2s supera il target NFR1 (p95 < 2s): la coda è causata
  dalla saturazione CPU del login storm (argon2id, ~2-3s/hash su questo host)
  che rallenta le query concorrenti nella prima fase del test.
- **login** p50 3.2s: dominato dal costo di argon2id con parametri di default.
  In produzione: 2 repliche (CPU parallela), parametri argon2 regolabili
  (time_cost/memory_cost) e rate limiting nginx su /auth (già configurato).

### 3.3 Punti aperti per WP8

1. **NFR1 (p95 < 2s)**: da verificare sul benchmark 10GB con stack prod
   (nginx + 2 repliche). Se entities resta sopra soglia: indici Neo4j
   full-text (il search usa CONTAINS, documentato in app.py), tuning heap,
   caching.
2. **Login storm**: valutare parametri argon2id più leggeri (trade-off
   sicurezza) o hashing asincrono fuori dal path request.
3. **Rate limiting nginx**: soglie (20r/s API, 5r/s auth) da confermare col
   benchmark prod; il load test per-IP non le ha esercitate.
4. **Ripetibilità**: il test è deterministico (setup/cleanup automatico dei
   dati `wp7_load_*`); rieseguibile con `scripts/loadtest.py`.

