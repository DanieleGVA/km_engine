# Gate G3 — Integrazione storage+auth e tenant isolation

Test di integrazione (pytest) che chiudono il gate G3 del work-plan:
storage (WP2) + auth (WP3) collegati end-to-end.

## Cosa coprono

| File | Casi |
|---|---|
| `test_auth_storage_flow.py` | login reale JWT → claims (sub/typ/roles/teams/tenant/jti); coerenza claim ↔ `resolve_identity`; rotazione refresh; `auth_required` → `Principal`; ponte `principal_visibility_context` → `is_visible`; bypass Admin sul default-deny |
| `test_tenant_isolation.py` | isolation su Neo4j: dati `teams=[teamA]` invisibili a teamB, visibili a teamA; `public` visibile a tutti; default-deny visibile solo ad Admin; "esplicito vince" (fatto teamA su entità pubblica); verifica fisica dei nodi nel grafo |

## Requisiti

- Neo4j dev `bolt://localhost:7687` (neo4j/km_dev_password) con schema applicato
- Postgres dev `localhost:5432` (km/km_dev_password, db `km_engine`) con schema applicato
- Variabili: `KM_NEO4J_URI`, `KM_NEO4J_USER`, `KM_NEO4J_PASSWORD`, `KM_PG_DSN`, `KM_JWT_SECRET` (in `.env` / default)

## Esecuzione

```bash
uv run pytest tests/integration -v
```

Tutti i dati creati dai test usano il prefisso `g3_` (utenti, teams, entità,
fatti) e vengono rimossi dopo ogni test; lo schema non viene mai modificato.
