"""Errori espliciti del layer auth (nessuna eccezione generica verso i client)."""
from __future__ import annotations


class AuthError(Exception):
    """Errore base del layer auth."""


class InvalidCredentialsError(AuthError):
    """Username/password non validi (non rivela se l'utente esiste o e' attivo)."""


class DuplicateUserError(AuthError):
    """Username o email gia' registrati (vincolo UNIQUE)."""


class UserNotFoundError(AuthError):
    """Utente non trovato per id o username."""


class TokenError(AuthError):
    """Token JWT non valido (firma, formato o claim)."""


class TokenExpiredError(TokenError):
    """Token scaduto (exp nel passato)."""


class TokenReuseError(TokenError):
    """Riuso di un refresh token gia' revocato: possibile furto (ADR-002 D1)."""


class InactiveUserError(AuthError):
    """Utente disattivato: rinnovo vietato e sessioni revocate (FR4.5)."""
