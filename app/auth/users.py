"""Gestione utenti, ruoli e teams (psycopg; schema 001_init.sql, ADR-002 D2).

Ogni funzione pubblica gestisce la propria transazione (commit a fine blocco).
I permessi effettivi di un utente = unione dei ruoli assegnati (many-to-many su
user_roles); teams su user_teams. Le password non vengono mai loggate ne' messe
in audit (solo username/email/ruoli nei new_value).
"""
from __future__ import annotations

import uuid

import psycopg

from . import audit
from .errors import DuplicateUserError, UserNotFoundError
from .hashing import hash_password

VALID_ROLES = ("admin", "editor", "viewer", "ingestor")


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _require_user(conn: psycopg.Connection, user_id: uuid.UUID) -> None:
    if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
        raise UserNotFoundError(f"Utente non trovato: {user_id}")


def _role_id(conn: psycopg.Connection, role: str) -> int:
    """Ruoli come enumerazione chiusa in Postgres (CHECK nel DDL): errore esplicito se sconosciuto."""
    row = conn.execute("SELECT id FROM roles WHERE name = %s", (role,)).fetchone()
    if row is None:
        raise ValueError(f"Ruolo non valido: {role!r} (attesi: {VALID_ROLES})")
    return row[0]


def _grant_role(
    conn: psycopg.Connection, user_id: uuid.UUID, role: str, granted_by: uuid.UUID | None
) -> None:
    """Helper interno: INSERT idempotente in user_roles (senza audit)."""
    conn.execute(
        """
        INSERT INTO user_roles (user_id, role_id, granted_by)
        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """,
        (user_id, _role_id(conn, role), granted_by),
    )


def _join_team(conn: psycopg.Connection, user_id: uuid.UUID, team: str) -> None:
    """Helper interno: assegna un team esistente o nuovo (senza audit)."""
    conn.execute(
        "INSERT INTO user_teams (user_id, team_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, get_or_create_team(conn, team)),
    )


def create_user(
    conn: psycopg.Connection,
    username: str,
    email: str,
    password: str,
    *,
    roles: tuple[str, ...] | list[str] = (),
    teams: tuple[str, ...] | list[str] = (),
    granted_by: uuid.UUID | str | None = None,
    actor_id: uuid.UUID | str | None = None,
) -> dict:
    """Crea un utente (hash argon2id) con ruoli e teams facoltativi; audit CREATE."""
    password_hash = hash_password(password)
    granted_by = _as_uuid(granted_by) if granted_by is not None else None
    with conn.transaction():
        try:
            row = conn.execute(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES (%s, %s, %s)
                RETURNING id, username, email, active, created_at
                """,
                (username, email, password_hash),
            ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateUserError(
                f"Username o email gia' registrati: {username} / {email}"
            ) from exc
        user_id: uuid.UUID = row[0]
        for role in roles:
            _grant_role(conn, user_id, role, granted_by)
        for team in teams:
            _join_team(conn, user_id, team)
        audit.record(
            conn,
            actor_id,
            "CREATE",
            str(user_id),
            "User",
            old_value=None,
            new_value={
                "username": username,
                "email": email,
                "active": True,
                "roles": sorted(roles),
                "teams": sorted(teams),
            },
        )
    return {
        "id": user_id,
        "username": row[1],
        "email": row[2],
        "active": row[3],
        "created_at": row[4],
        "roles": sorted(roles),
        "teams": sorted(teams),
    }


def get_user(
    conn: psycopg.Connection,
    *,
    username: str | None = None,
    user_id: uuid.UUID | str | None = None,
) -> dict | None:
    """Ricerca utente per username o id; include ruoli e teams risolti (None se assente)."""
    if (username is None) == (user_id is None):
        raise ValueError("Passare esattamente uno tra username e user_id.")
    if username is not None:
        row = conn.execute(
            "SELECT id, username, email, active, created_at FROM users WHERE username = %s",
            (username,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, username, email, active, created_at FROM users WHERE id = %s",
            (_as_uuid(user_id),),
        ).fetchone()
    if row is None:
        return None
    roles, teams = resolve_identity(conn, row[0])
    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "active": row[3],
        "created_at": row[4],
        "roles": roles,
        "teams": teams,
    }


def list_users(
    conn: psycopg.Connection, *, active: bool | None = None
) -> list[dict]:
    """Elenco utenti con ruoli e teams aggregati (permesso effettivo = unione)."""
    sql = """
        SELECT u.id, u.username, u.email, u.active, u.created_at,
               COALESCE(array_agg(DISTINCT r.name) FILTER (WHERE r.name IS NOT NULL), '{}') AS roles,
               COALESCE(array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL), '{}') AS teams
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r ON r.id = ur.role_id
        LEFT JOIN user_teams ut ON ut.user_id = u.id
        LEFT JOIN teams t ON t.id = ut.team_id
        WHERE (%s::boolean IS NULL OR u.active = %s::boolean)
        GROUP BY u.id
        ORDER BY u.username
    """
    rows = conn.execute(sql, (active, active)).fetchall()
    return [
        {
            "id": r[0],
            "username": r[1],
            "email": r[2],
            "active": r[3],
            "created_at": r[4],
            "roles": sorted(r[5]),
            "teams": sorted(r[6]),
        }
        for r in rows
    ]


def set_user_active(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str,
    *,
    active: bool,
    actor_id: uuid.UUID | str | None = None,
) -> dict:
    """Attiva/disattiva un utente; la disattivazione revoca tutti i refresh (FR4.5)."""
    uid = _as_uuid(user_id)
    with conn.transaction():
        row = conn.execute(
            "UPDATE users SET active = %s, updated_at = now() WHERE id = %s RETURNING username",
            (active, uid),
        ).fetchone()
        if row is None:
            raise UserNotFoundError(f"Utente non trovato: {uid}")
        if not active:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (uid,),
            )
        audit.record(
            conn,
            actor_id,
            "INVALIDATE" if not active else "UPDATE",
            str(uid),
            "User",
            old_value={"active": not active},
            new_value={"active": active},
        )
    return {"id": uid, "username": row[0], "active": active}


def assign_role(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str,
    role: str,
    *,
    granted_by: uuid.UUID | str | None = None,
    actor_id: uuid.UUID | str | None = None,
) -> None:
    """Assegna un ruolo all'utente (idempotente); audit GRANT_ROLE."""
    uid = _as_uuid(user_id)
    with conn.transaction():
        _require_user(conn, uid)
        rid = _role_id(conn, role)
        conn.execute(
            """
            INSERT INTO user_roles (user_id, role_id, granted_by)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (uid, rid, _as_uuid(granted_by) if granted_by is not None else None),
        )
        audit.record(
            conn, actor_id, "GRANT_ROLE", str(uid), "User", new_value={"role": role}
        )


def revoke_role(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str,
    role: str,
    *,
    actor_id: uuid.UUID | str | None = None,
) -> bool:
    """Rimuove un ruolo dall'utente; True se era assegnato; audit REVOKE_ROLE."""
    uid = _as_uuid(user_id)
    with conn.transaction():
        rid = _role_id(conn, role)
        cur = conn.execute(
            "DELETE FROM user_roles WHERE user_id = %s AND role_id = %s", (uid, rid)
        )
        if cur.rowcount:
            audit.record(
                conn, actor_id, "REVOKE_ROLE", str(uid), "User", old_value={"role": role}
            )
    return bool(cur.rowcount)


def get_or_create_team(
    conn: psycopg.Connection, name: str, *, description: str | None = None
) -> int:
    """Idempotente: ritorna l'id del team, creandolo se manca."""
    row = conn.execute(
        """
        WITH ins AS (
            INSERT INTO teams (name, description) VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
        )
        SELECT id FROM ins
        UNION ALL
        SELECT id FROM teams WHERE name = %s
        LIMIT 1
        """,
        (name, description, name),
    ).fetchone()
    return row[0]


def assign_team(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str,
    team: str,
    *,
    actor_id: uuid.UUID | str | None = None,
) -> None:
    """Assegna un team all'utente (crea il team se manca); audit ASSIGN_TEAM."""
    uid = _as_uuid(user_id)
    with conn.transaction():
        _require_user(conn, uid)
        team_id = get_or_create_team(conn, team)
        conn.execute(
            "INSERT INTO user_teams (user_id, team_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (uid, team_id),
        )
        audit.record(
            conn, actor_id, "ASSIGN_TEAM", str(uid), "User", new_value={"team": team}
        )


def revoke_team(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str,
    team: str,
    *,
    actor_id: uuid.UUID | str | None = None,
) -> bool:
    """Rimuove un team dall'utente; True se era assegnato; audit REVOKE_TEAM."""
    uid = _as_uuid(user_id)
    with conn.transaction():
        cur = conn.execute(
            "DELETE FROM user_teams WHERE user_id = %s AND team_id = (SELECT id FROM teams WHERE name = %s)",
            (uid, team),
        )
        if cur.rowcount:
            audit.record(
                conn, actor_id, "REVOKE_TEAM", str(uid), "User", old_value={"team": team}
            )
    return bool(cur.rowcount)


def resolve_identity(
    conn: psycopg.Connection, user_id: uuid.UUID | str
) -> tuple[list[str], list[str]]:
    """Risolve (roles, teams) effettivi dell'utente: finiscono nei claim del token."""
    uid = _as_uuid(user_id)
    roles = [
        r[0]
        for r in conn.execute(
            """
            SELECT r.name FROM roles r
            JOIN user_roles ur ON ur.role_id = r.id
            WHERE ur.user_id = %s ORDER BY r.name
            """,
            (uid,),
        ).fetchall()
    ]
    teams = [
        r[0]
        for r in conn.execute(
            """
            SELECT t.name FROM teams t
            JOIN user_teams ut ON ut.team_id = t.id
            WHERE ut.user_id = %s ORDER BY t.name
            """,
            (uid,),
        ).fetchall()
    ]
    return roles, teams
