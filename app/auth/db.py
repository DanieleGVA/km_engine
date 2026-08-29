"""Connessione Postgres per il layer auth (psycopg 3, DSN da AuthSettings)."""
from __future__ import annotations

import psycopg

from .config import AuthSettings


def connect(settings: AuthSettings | None = None) -> psycopg.Connection:
    """Apre una connessione al Postgres delle identita' (KM_PG_DSN).

    autocommit=True: ogni SELECT gira senza transazione; le scritture usano
    blocchi ``conn.transaction()`` espliciti (BEGIN/COMMIT veri, non savepoint
    di una transazione implicita). Cosi' il commit avviene a fine blocco e la
    connessione resta coerente anche se chiusa senza commit.
    """
    s = settings or AuthSettings()
    return psycopg.connect(s.pg_dsn, autocommit=True)
