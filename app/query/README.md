# Query Layer (WP5, Gate G5)

Questo modulo fornisce query visibility-aware sopra `app/storage`.

## Politica di Visibilità (default-deny)

**Regola fondamentale:** se un oggetto ha UNA QUALSIASI restrizione esplicita (teams/roles) e NON è esplicitamente `is_public=True`, è trattato come **NON pubblico**.

### Dimension-based isolation

Una restrizione esplicita su una dimensione rende l'oggetto privato per tutte le altre dimensioni:

- Se `roles = ["admin"]` e `is_public = False` → solo admin vedono (anche se `teams` è vuoto)
- Se `teams = ["engineering"]` e `is_public = False` → solo engineering vede (anche se `roles` è vuoto)
- Se `is_public = True` → tutti vedono (indipendentemente da roles/teams)
- Se nessun attributo è impostato → **default-deny** (nessuno vede, tranne admin/editor con bypass)

### Bypass

- **Admin** (`is_admin=True`): bypass completo del filtro visibilità
- **Editor** (`is_editor=True`): bypass solo in lettura (scrittura limitata allo scope autorizzato, iterazione 2)

### Ponte auth → storage

Usa SEMPRE `principal_visibility_context(principal)` come unico ponte:

```python
from app.auth import principal_visibility_context
from app.storage.visibility import is_visible, effective_visibility

ctx = principal_visibility_context(principal)
is_visible(visibility, **ctx)  # Ruoli, teams, is_admin, is_editor
```

## API del Query Engine

### `query_entities(client, principal, label=None, entity_type=None)`

Query entità filtrate per visibilità.

### `query_facts(client, principal, entity_id=None, at_time=None)`

Query fatti con supporto temporale (FR5.3 "al tempo T").

### `query_relations(client, principal, entity_id)`

Query relazioni RELATES_TO filtrate per visibilità del target.

### `search(client, principal, text, label=None)`

Ricerca full-text su label/value (implementazione: LIKE, documentato).

### `get_entity_with_history(client, principal, entity_id)`

Ottieni entità con catena VERSION_OF filtrata.

## FR9 - Multilingua

### `localize_response(response_data, lang)`

Per il prototipo (iterazione 1):
- Lingua canonica: inglese
- Se `lang != "en"` e `translation_state == "pending"` → aggiunge `untranslated=True`
- La traduzione vera è WP4/LLM

## Limiti del prototipo (iterazione 1)

1. Ricerca full-text usa LIKE (performance limitate senza indici Neo4j)
2. Traduzione semantica non implementata (solo flag untranslated)
3. Editor bypass limitato alla lettura
4. Rate limiting: in-memory per istanza (vedi app/api/)
