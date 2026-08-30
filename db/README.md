# km_engine — Schemi database

Schemi applicativi di baseline (WP1): Neo4j per il grafo (ADR-001), PostgreSQL 16 per identità/audit/workflow (ADR-002).

| File | Target | Contenuto |
|---|---|---|
| `neo4j/schema.cypher` | Neo4j 5.x (`km-neo4j`) | Uniqueness constraint, indici (nodi, relazioni, full-text) del modello §2.1 del work-plan |
| `neo4j/002_domain_schema.cypher` | Neo4j 5.x (`km-neo4j`) | Domain layer (Iterazione A, WP-A4): `:Document`, `:CanonicalTerm`, `:DomainPack`, relazioni, full-text e indice vettoriale 384-dim |
| `postgres/001_init.sql` | PostgreSQL 16 (`km-postgres`) | DDL: users, roles, user_roles, teams, user_teams, permissions, refresh_tokens, audit_log, ingest_jobs, conflicts + seed dei 4 ruoli |

Entrambi gli script sono **idempotenti**: possono essere ri-applicati senza errori.

## Prerequisiti

Il compose base (fase fondamenta) deve essere attivo (dal repository root):

```bash
docker compose up -d
docker compose ps          # km-neo4j e km-postgres devono risultare healthy
```

Le credenziali sono quelle del file `.env` (vedi `.env.example`):
default di sviluppo `NEO4J_AUTH=neo4j/km_dev_password`, `POSTGRES_USER=km`, `POSTGRES_PASSWORD=km_dev_password`, `POSTGRES_DB=km_engine`.

---

## Neo4j — applicare `schema.cypher`

Il compose monta già `./db/neo4j/` in `/import` (read-only) dentro il container `km-neo4j`:

```bash
# Dal repository root (password = la parte dopo 'neo4j/' in NEO4J_AUTH)
docker compose exec -T neo4j cypher-shell   -u neo4j -p km_dev_password   -f /import/schema.cypher
```

Alternativa via stdin da host (utile se il mount cambia):

```bash
cat db/neo4j/schema.cypher |   docker exec -i km-neo4j cypher-shell -u neo4j -p km_dev_password
```

Verifica:

```bash
docker compose exec -T neo4j cypher-shell -u neo4j -p km_dev_password   "SHOW CONSTRAINTS;"
docker compose exec -T neo4j cypher-shell -u neo4j -p km_dev_password   "SHOW INDEXES;"
```

Attesi: 4 constraint di unicità (`entity_id_unique`, `fact_id_unique`, `source_id_unique`, `version_id_unique`) e gli indici elencati nello schema.

### Neo4j — applicare `002_domain_schema.cypher` (Iterazione A, WP-A4)

```bash
# Dal repository root (stessa modalità del 001: mount ./db/neo4j/ in /import)
docker compose exec -T neo4j cypher-shell -u neo4j -p km_dev_password -f /import/002_domain_schema.cypher
```

Verifica:

```bash
docker compose exec -T neo4j cypher-shell -u neo4j -p km_dev_password "SHOW CONSTRAINTS;"
docker compose exec -T neo4j cypher-shell -u neo4j -p km_dev_password "SHOW INDEXES;"
```

Attesi: 3 nuovi constraint di unicità (`document_id_unique`, `canonical_term_id_unique`, `domain_pack_id_unique`), 2 indici full-text (`document_title_fulltext`, `canonical_term_label_en_fulltext`) e 1 indice vettoriale (`document_embedding_vector`, 384 dimensioni, cosine). L'indice vettoriale è supportato da Neo4j 5.26.30 Community (verificato empiricamente).

## PostgreSQL — applicare `001_init.sql`

```bash
# Dal repository root
docker compose exec -T postgres   psql -U km -d km_engine   -v ON_ERROR_STOP=1   -f - < db/postgres/001_init.sql
```

Alternativa con `docker exec` diretto:

```bash
docker exec -i km-postgres psql -U km -d km_engine -v ON_ERROR_STOP=1   < db/postgres/001_init.sql
```

Verifica:

```bash
docker compose exec -T postgres psql -U km -d km_engine -c "\dt"
docker compose exec -T postgres psql -U km -d km_engine   -c "SELECT id, name FROM roles ORDER BY id;"
```

Attesi: 10 tabelle (`users`, `roles`, `user_roles`, `teams`, `user_teams`, `permissions`, `refresh_tokens`, `audit_log`, `ingest_jobs`, `conflicts`) e i 4 ruoli seed (admin, editor, viewer, ingestor).

## Nota operativa — audit log append-only

`audit_log` è progettata append-only (ADR-002 D4). Dopo la creazione dell'utente applicativo del servizio (WP3), revocare UPDATE/DELETE sulla tabella:

```sql
-- esempio (eseguire quando il role applicativo esiste)
REVOKE UPDATE, DELETE ON audit_log FROM km_app;
```

## Note

- Le porte 7474/7687/5432 pubblicate dal compose base sono **solo per sviluppo**; in produzione vanno limitate alla rete interna del compose (ADR-003 D1).
- I constraint di esistenza proprietà su Neo4j sono Enterprise-only e **non** sono usati: la completezza delle proprietà è responsabilità dello storage layer Python (nota in `schema.cypher`).
- Il dump/restore dei due DB (backup giornaliero, RPO 24h) è definito in ADR-003 D5/D6 e va implementato in WP7.
