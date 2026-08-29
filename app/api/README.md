# API Layer (WP5, Gate G5)

Applicazione FastAPI che espone endpoint REST per il query engine visibility-aware.

## Endpoint

### Auth (monta `app.auth.routes`)
- `POST /auth/login` - Login username/password → access+refresh token
- `POST /auth/refresh` - Refresh token → nuova coppia
- `POST /auth/logout` - Logout (revoca refresh token)

### Entities
- `GET /api/v1/entities` - Lista entità filtrate per visibilità
  - Query params: `label`, `type`, `lang`
- `GET /api/v1/entities/{id}` - Ottieni entità con fatti e storico
- `GET /api/v1/entities/{id}/facts` - Ottieni fatti (con `at_time` opzionale)
- `GET /api/v1/entities/{id}/relations` - Ottieni relazioni RELATES_TO

### Search
- `GET /api/v1/search?q=<text>` - Ricerca full-text
  - Query params: `label`, `lang`

### Health
- `GET /api/v1/healthz` - Health check composito (Neo4j + Postgres)

## Autenticazione

Tutti gli endpoint `/api/v1/*` richiedono `Authorization: Bearer <access_token>`.

Status code:
- `401 Unauthorized`: token mancante o invalido
- `403 Forbidden`: ruoli/teams insufficienti
- `404 Not Found`: risorsa non esiste o non visibile
- `422 Validation Error`: parametri invalidi

## Rate Limiting

Token bucket in-memory per IP (limite del prototipo, per-istanza):
- Endpoint normali: 10 req/s
- `/auth/*`: 5 req/s (più stretto)

Risposta `429 Too Many Requests` con header `Retry-After: 60`.

## FR9 - Multilingua

Parametro `lang` o header `Accept-Language`:
- `en` (inglese): lingua canonica, nessun flag
- Altre lingue (`fr`, `de`, `es`, `it`): se `translation_state=pending` → `untranslated=True`

## OpenAPI

Genera docs/openapi.json:
```bash
cd /Users/daniele.buonaiuto/km_engine
uv run python -c "from app.api.app import app; import json; json.dump(app.openapi(), open('docs/openapi.json', 'w'), indent=2)"
```

## Limiti del prototipo (iterazione 1)

1. Rate limiting in-memory (non condiviso tra istanze)
2. Ricerca full-text con LIKE (performance limitate)
3. Traduzione semantica non implementata (solo flag untranslated)
4. Editor bypass limitato alla lettura
