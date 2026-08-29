"""Test delle API REST (WP5, Gate G5).

Test:
- Contratti endpoint (status/JSON schema)
- Auth su ogni endpoint (401 senza token, 403 senza ruolo)
- Filtro visibilità via API (casi G3 ripetuti via HTTP)
- Rate limiting (429 dopo N richieste)
- FR9 (Accept-Language → flag untranslated)
- Health check
"""
