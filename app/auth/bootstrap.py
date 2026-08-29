"""Bootstrap idempotente dell'Admin iniziale da env (ADR-002 punto aperto 3).

Al primo avvio crea l'utente Admin da KM_ADMIN_USERNAME / KM_ADMIN_PASSWORD.
Riesecuzioni successive: nessun cambio password (il bootstrap non deve
sovrascrivere una password gia' rotata), ma ripara stato incoerente:
ruolo admin mancante o utente disattivato. Email generata
<username>@km-engine.local se non fornita via KM_ADMIN_EMAIL.
"""
from __future__ import annotations

import psycopg

from .config import AuthSettings
from .hashing import validate_password_policy
from .users import assign_role, create_user, get_user, set_user_active


def bootstrap_admin(
    conn: psycopg.Connection, settings: AuthSettings | None = None
) -> dict:
    """Crea/ripara l'utente Admin. Idempotente: mai un secondo utente, mai un cambio password."""
    s = settings or AuthSettings()
    if not s.admin_username or not s.admin_password:
        raise ValueError(
            "Bootstrap admin richiede KM_ADMIN_USERNAME e KM_ADMIN_PASSWORD impostati."
        )
    validate_password_policy(s.admin_password)
    existing = get_user(conn, username=s.admin_username)
    if existing is None:
        created = create_user(
            conn,
            s.admin_username,
            f"{s.admin_username}@km-engine.local",
            s.admin_password,
            roles=("admin",),
        )
        return {"created": True, "repaired": False, "user_id": created["id"]}
    repaired = False
    if not existing["active"]:
        set_user_active(conn, existing["id"], active=True)
        repaired = True
    if "admin" not in existing["roles"]:
        assign_role(conn, existing["id"], "admin")
        repaired = True
    return {"created": False, "repaired": repaired, "user_id": existing["id"]}
