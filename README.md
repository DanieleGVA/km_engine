# km_engine

Riscrittura enterprise di [Graphify](https://github.com/Graphify-Labs/graphify) come piattaforma
di knowledge management: ~10GB di contenuti misti, ~100 utenti concorrenti, profilazione (RBAC/teams),
resilienza, tracciabilità bitemporale, conflict check e fact invalidation.

Stato: **fase 0 - fondamenta** (WP1 ADR + WP7 base infra). Vedi `docs/work-plan.md`.

## Struttura
- `app/` - application layer stateless (storage, auth, ingest, query, api)
- `db/` - schema e migrazioni (neo4j + postgres)
- `docs/` - ADR, requisiti, piano di lavoro
- `deploy/` - template di deploy (prod)
- `docker-compose.yml` - ambiente di sviluppo (Neo4j + Postgres)

## Quickstart (dev)
```bash
cp .env.example .env
docker compose up -d --wait
```
