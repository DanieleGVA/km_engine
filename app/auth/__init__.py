"""Layer auth di km_engine: JWT access+refresh, RBAC 4 ruoli+teams, audit (ADR-002).

Interfaccia interna unica (ADR-002 D7): l'implementazione MVP e' "local IdP";
l'iterazione 2 sostituira' il verificatore del token con JWKS OIDC senza
cambiare questi contratti.
"""
from __future__ import annotations

from .audit import record as record_audit
from .bootstrap import bootstrap_admin
from .config import AuthSettings, get_auth_settings
from .db import connect
from .deps import Principal, auth_required, principal_from_claims, require_roles
from .errors import (
    AuthError,
    DuplicateUserError,
    InactiveUserError,
    InvalidCredentialsError,
    TokenError,
    TokenExpiredError,
    TokenReuseError,
    UserNotFoundError,
)
from .hashing import HashingError, hash_password, verify_password
from .routes import router
from .tokens import (
    decode_token,
    issue_access_token,
    list_refresh_tokens,
    login,
    logout,
    refresh,
    revoke_all_user_tokens,
    revoke_refresh,
)
from .users import (
    VALID_ROLES,
    assign_role,
    assign_team,
    create_user,
    get_or_create_team,
    get_user,
    list_users,
    resolve_identity,
    revoke_role,
    revoke_team,
    set_user_active,
)


def principal_visibility_context(principal: Principal) -> dict[str, object]:
    """Bridge ADR-002 Principal -> ADR-001 visibility filter (auth -> storage).

    Converts the resolved identity into the keyword arguments accepted by
    ``app.storage.visibility.is_visible`` (roles, teams, is_admin, is_editor),
    so the query engine can feed the storage visibility filter without ever
    seeing the token format (ADR-002 D7). Admin/Editor bypass is a storage-level
    simplification; the authorized-scope check is applied in WP5.

    Example:
        from app.storage.visibility import effective_visibility, is_visible
        is_visible(effective_visibility(fact, entity), **principal_visibility_context(p))
    """
    return {
        "roles": principal.roles,
        "teams": principal.teams,
        "is_admin": "admin" in principal.roles,
        "is_editor": "editor" in principal.roles,
    }

__all__ = [
    "VALID_ROLES",
    "AuthError",
    "AuthSettings",
    "DuplicateUserError",
    "HashingError",
    "InactiveUserError",
    "InvalidCredentialsError",
    "Principal",
    "TokenError",
    "TokenExpiredError",
    "TokenReuseError",
    "UserNotFoundError",
    "assign_role",
    "assign_team",
    "auth_required",
    "bootstrap_admin",
    "connect",
    "create_user",
    "decode_token",
    "get_auth_settings",
    "get_or_create_team",
    "get_user",
    "hash_password",
    "issue_access_token",
    "list_refresh_tokens",
    "list_users",
    "login",
    "logout",
    "principal_from_claims",
    "principal_visibility_context",
    "record_audit",
    "refresh",
    "require_roles",
    "resolve_identity",
    "revoke_all_user_tokens",
    "revoke_refresh",
    "revoke_role",
    "revoke_team",
    "router",
    "set_user_active",
    "verify_password",
]
