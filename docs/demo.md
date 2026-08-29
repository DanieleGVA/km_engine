# Demo end-to-end — km_engine (giorno 14, gate G9)

Script di presentazione del prototipo: stack dev (Neo4j + Postgres) + app
FastAPI. Tutti i comandi sono pronti per copia-incolla da terminale nella
root del repo (`/Users/daniele.buonaiuto/km_engine`).

I dati demo usano il prefisso `demo_*` e vengono rimossi alla fine (§9).

---

## 0. Prerequisiti

- Docker Desktop attivo, container dev su: `docker ps` → `km-neo4j`, `km-postgres`
- `.env` presente (già in repo): credenziali dev + `KM_ADMIN_USERNAME=admin`
- Python: `uv run` (venv del progetto)

## 1. Avvio stack dev

```bash
# stack dev (root compose: Neo4j + Postgres, porte pubblicate per dev)
docker compose up -d
docker compose ps          # km-neo4j e km-postgres: Up (healthy)

# verifica connettività diretta
uv run python -c "from app.storage.client import Neo4jClient; c=Neo4jClient.from_env(); c.verify_connectivity(); print('Neo4j OK'); c.close()"
```

## 2. Avvio app API

```bash
# terminale 1 — app FastAPI (uvicorn, 1 worker)
uv run uvicorn app.api.app:app --host 127.0.0.1 --port 8000

# terminale 2 — health check composito (Neo4j + Postgres)
curl -s http://127.0.0.1:8000/api/v1/healthz
# → {"status":"healthy","checks":{"bolt":"ok","psql":"ok"}}
```

## 3. Bootstrap admin (idempotente)

```bash
uv run python - <<'PY'
import psycopg
from app.auth import bootstrap_admin, get_auth_settings
s = get_auth_settings()
with psycopg.connect(s.pg_dsn, autocommit=True) as c:
    print(bootstrap_admin(c, s))
PY
# → {'created': True, 'repaired': False, 'user_id': ...}  (prima volta)
# → {'created': False, 'repaired': False, 'user_id': ...} (riesecuzioni)
```

## 4. Login (JWT access + refresh)

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"adm1n-dev-pass"}' | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$TOKEN" | head -c 40; echo " ..."
```

## 5. Ingestione corpus di esempio (job code + document)

```bash
uv run python - <<'PY'
import psycopg
from pathlib import Path
from app.ingest.config import IngestSettings
from app.ingest.pipeline import IngestPipeline
from app.ingest.semantic import StubSemanticService
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

settings = IngestSettings(chunk_size=2, cache_dir=Path(".km_demo_cache"))
conn = psycopg.connect(settings.pg_dsn, autocommit=True)
client = Neo4jClient.from_env(); client.verify_connectivity()
repo = GraphRepository(client)
pipe = IngestPipeline(repo=repo, client=client, conn=conn, settings=settings,
                      semantic_service=StubSemanticService(), prefix="demo_")
corpus = Path("tests/fixtures/wp4_corpus")
for jt in ("code", "document"):
    job = pipe.run(f"demo://corpus/{jt}", corpus, job_type=jt, chunk_size=2)
    print(f"job {jt}: status={job.status} progress={job.progress}%")
conn.close(); client.close()
PY
# → job code: status=completed progress=100%
# → job document: status=completed progress=100%
```

## 6. Query entità / fatti / ricerca (con visibilità)

```bash
# entità ingerite (admin vede tutto)
curl -s http://127.0.0.1:8000/api/v1/entities -H "Authorization: Bearer $TOKEN" | \
  python3 -c "import sys,json; [print(e['id'], e['label']) for e in json.load(sys.stdin) if e['id'].startswith('demo_')]"

# fatti di un'entità (es. Calculator)
EID=$(curl -s http://127.0.0.1:8000/api/v1/entities -H "Authorization: Bearer $TOKEN" | \
  python3 -c "import sys,json; print(next(e['id'] for e in json.load(sys.stdin) if e['label']=='Calculator'))")
curl -s "http://127.0.0.1:8000/api/v1/entities/$EID/facts" -H "Authorization: Bearer $TOKEN"

# ricerca full-text
curl -s "http://127.0.0.1:8000/api/v1/search?q=Calculator" -H "Authorization: Bearer $TOKEN"

# visibilità: viewer vede solo ciò che può (default-deny)
uv run python - <<'PY'
import psycopg
from app.auth.users import create_user
from app.ingest.config import IngestSettings
s = IngestSettings()
with psycopg.connect(s.pg_dsn, autocommit=True) as c:
    create_user(c, "demo_viewer", "demo_viewer@example.test", "demo-viewer-password-123",
                roles=("viewer",), teams=("demo_team_a",))
    print("viewer creato")
PY
VTOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo_viewer","password":"demo-viewer-password-123"}' | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
# entità admin-only: il viewer NON la vede, l'admin sì
uv run python - <<'PY'
import psycopg
from app.ingest.config import IngestSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility
s = IngestSettings()
client = Neo4jClient.from_env(); repo = GraphRepository(client)
repo.create_entity(entity_id="demo_private", label="DemoPrivate", type="code",
                   visibility=Visibility(roles=("admin",)))
repo.create_entity(entity_id="demo_public", label="DemoPublic", type="code",
                   visibility=Visibility(is_public=True))
client.close(); print("entità visibilità create")
PY
curl -s http://127.0.0.1:8000/api/v1/entities -H "Authorization: Bearer $VTOKEN" | \
  python3 -c "import sys,json; ids={e['id'] for e in json.load(sys.stdin)}; print('viewer vede demo_public:', 'demo_public' in ids, '| demo_private:', 'demo_private' in ids)"
```

## 7. Conflict check: rilevamento + workflow approve/reject

```bash
# crea 2 fatti conflittuali (stessa entità/proprietà, sorgenti diverse)
uv run python - <<'PY'
import psycopg
from datetime import UTC, datetime
from app.ingest.config import IngestSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility
from app.conflict.detection import detect_conflicts_for_entity

s = IngestSettings()
conn = psycopg.connect(s.pg_dsn, autocommit=True)
client = Neo4jClient.from_env(); repo = GraphRepository(client)
repo.create_entity(entity_id="demo_conflict", label="DemoConflict", type="code",
                   visibility=Visibility(is_public=True))
for sid, val in (("demo_src_a", "1.0"), ("demo_src_b", "2.0")):
    with client.session() as session:
        session.run("MERGE (s:Source {id: $id}) SET s.uri = $uri, s.type='file', s.hash='h', s.language='en', s.ingested_at=$t",
                    id=sid, uri=f"demo://{sid}", t=datetime.now(UTC))
    repo.create_fact(fact_id=f"demo_fact_{sid}", entity_id="demo_conflict",
                     property="version", value=val, source_id=sid)
created = detect_conflicts_for_entity(repo, conn, "demo_conflict")
print("conflitti rilevati:", [(c['id'], c['value_a'], c['value_b'], c['suggestion']) for c in created])
conn.close(); client.close()
PY

# lista conflitti pending via API
curl -s "http://127.0.0.1:8000/api/v1/conflicts?status=pending" -H "Authorization: Bearer $TOKEN"

# approve (sceglie 'a', invalida il fatto perdente nel grafo)
CID=$(curl -s "http://127.0.0.1:8000/api/v1/conflicts?status=pending" -H "Authorization: Bearer $TOKEN" | \
  python3 -c "import sys,json; print([c['id'] for c in json.load(sys.stdin) if c['entity_id']=='demo_conflict'][0])")
curl -s -X POST "http://127.0.0.1:8000/api/v1/conflicts/$CID/approve" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"choice":"a"}'
# → {"id":..., "status":"approved", ...}

# reject (secondo conflitto: nessuna modifica al grafo)
uv run python - <<'PY'
import psycopg
from app.ingest.config import IngestSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility
from app.conflict.detection import detect_conflicts_for_entity
s = IngestSettings()
conn = psycopg.connect(s.pg_dsn, autocommit=True)
client = Neo4jClient.from_env(); repo = GraphRepository(client)
repo.create_entity(entity_id="demo_reject", label="DemoReject", type="code",
                   visibility=Visibility(is_public=True))
repo.create_fact(fact_id="demo_reject_a", entity_id="demo_reject", property="mode", value="fast", source_id="demo_src_a")
repo.create_fact(fact_id="demo_reject_b", entity_id="demo_reject", property="mode", value="safe", source_id="demo_src_b")
print("conflitto reject:", detect_conflicts_for_entity(repo, conn, "demo_reject")[0]['id'])
conn.close(); client.close()
PY
RID=$(curl -s "http://127.0.0.1:8000/api/v1/conflicts?status=pending" -H "Authorization: Bearer $TOKEN" | \
  python3 -c "import sys,json; print([c['id'] for c in json.load(sys.stdin) if c['entity_id']=='demo_reject'][0])")
curl -s -X POST "http://127.0.0.1:8000/api/v1/conflicts/$RID/reject" -H "Authorization: Bearer $TOKEN"
# → {"id":..., "status":"rejected", ...}
```

## 8. Fact invalidation con propagazione (truth-maintenance)

```bash
# sorgente con fatto diretto + fatto derivato INFERRED dipendente
uv run python - <<'PY'
import psycopg
from datetime import UTC, datetime
from app.ingest.config import IngestSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility
s = IngestSettings()
conn = psycopg.connect(s.pg_dsn, autocommit=True)
client = Neo4jClient.from_env(); repo = GraphRepository(client)
repo.create_entity(entity_id="demo_inv", label="DemoInv", type="code", visibility=Visibility(is_public=True))
with client.session() as session:
    session.run("MERGE (s:Source {id: 'demo_src_inv'}) SET s.uri='demo://src_inv', s.type='file', s.hash='h', s.language='en', s.ingested_at=$t", t=datetime.now(UTC))
repo.create_fact(fact_id="demo_inv_fact", entity_id="demo_inv", property="status", value="active",
                 source_id="demo_src_inv", confidence="EXTRACTED")
repo.create_fact(fact_id="demo_inv_dep", entity_id="demo_inv", property="derived", value="derived-value", confidence="INFERRED")
with client.session() as session:
    session.run("MATCH (d:Fact {id:'demo_inv_dep'}), (p:Fact {id:'demo_inv_fact'}) MERGE (d)-[:DERIVED_FROM]->(p)")
    session.run("MATCH (f:Fact {id:'demo_inv_fact'}), (s:Source {id:'demo_src_inv'}) MERGE (f)-[:DERIVED_FROM]->(s)")
conn.close(); client.close(); print("setup invalidation ok")
PY

# invalida la sorgente via API → fatto diretto obsolete + dipendente under_review
curl -s -X POST "http://127.0.0.1:8000/api/v1/sources/demo_src_inv/invalidate" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"demo: source changed"}'
# → {"source_id":"demo_src_inv","invalidated_facts":["demo_inv_fact"],"under_review_facts":["demo_inv_dep"],...}

# verifica audit log (FR5.2): RESOLVE + INVALIDATE_SOURCE
uv run python - <<'PY'
import psycopg
from app.ingest.config import IngestSettings
s = IngestSettings()
with psycopg.connect(s.pg_dsn) as c:
    rows = c.execute("SELECT action, entity_id, entity_type FROM audit_log WHERE entity_id LIKE 'demo\_%' OR entity_id LIKE 'demo%' ORDER BY id").fetchall()
    for r in rows:
        print(r)
PY
```

## 9. Pulizia dati demo

```bash
uv run python - <<'PY'
import psycopg
from app.ingest.config import IngestSettings
from app.storage.client import Neo4jClient
s = IngestSettings()
with psycopg.connect(s.pg_dsn, autocommit=True) as c, c.transaction():
    # NOTA: pattern 'demo%' (non 'demo\_%'): i job hanno source_uri 'demo://...'
    c.execute("DELETE FROM conflicts WHERE entity_id LIKE 'demo%'")
    c.execute("DELETE FROM audit_log WHERE entity_id LIKE 'demo%'")
    c.execute("DELETE FROM ingest_jobs WHERE source_uri LIKE 'demo%'")
    rows = c.execute("SELECT id FROM users WHERE username LIKE 'demo%'").fetchall()
    ids = [r[0] for r in rows]
    if ids:
        c.execute("DELETE FROM audit_log WHERE user_id = ANY(%s)", (ids,))
        c.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
    c.execute("DELETE FROM teams WHERE name LIKE 'demo%'")
client = Neo4jClient.from_env()
with client.session() as session:
    session.run("MATCH (n) WHERE (n:Entity OR n:Fact OR n:Source OR n:Version) AND n.id STARTS WITH 'demo_' DETACH DELETE n")
    session.run("MATCH (s:Source) WHERE s.uri STARTS WITH 'demo_' DETACH DELETE s")
client.close()
print("cleanup demo ok")
PY
```

## 10. Verifica finale (gate G9)

```bash
# suite completa (186+ test unit/integrazione + 1 E2E)
uv run pytest -q

# suite E2E dedicata
uv run pytest tests/e2e -q

# copertura moduli core (≥80%: storage, auth, query, conflict, ingest, invalidation)
uv run pytest --cov=app --cov-report=term-missing -q | tail -20

# load test 100 utenti (NFR2) — richiede app attiva su :8000
uv run python scripts/loadtest.py --base-url http://127.0.0.1:8000 --users 100
```

---

*Demo del prototipo km_engine — giorno 14. Riferimenti: `docs/runbook.md`
(operazioni stack), `docs/benchmark-report.md` (numeri G8/G9), `docs/work-plan.md`
(deliverable §8).*
