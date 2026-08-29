"""Hashing password: argon2id primario, fallback bcrypt costo 12 (ADR-002 D5).

Backend primario: ``argon2-cffi`` (argon2id, salt casuale, verifica constant-time
per costruzione). Fallback documentato: ``bcrypt`` costo 12, usato solo se
argon2-cffi non e' importabile nell'ambiente target; in tal caso serve che il
pacchetto ``bcrypt`` sia installato. La verifica instrada l'hash al backend
giusto in base al prefisso ($argon2 / $2), cosi' hash legacy restano validi.
"""
from __future__ import annotations

try:  # backend primario
    from argon2 import PasswordHasher
    from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError

    _ARGON2: PasswordHasher | None = PasswordHasher()
except ImportError:  # pragma: no cover - ambiente senza argon2-cffi
    _ARGON2 = None

try:  # fallback documentato, non e' una dipendenza del progetto
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover
    _bcrypt = None

MIN_PASSWORD_LENGTH = 12  # politica minima, nessuna complessita' decorativa (ADR-002 D5)
BCRYPT_ROUNDS = 12


class HashingError(RuntimeError):
    """Nessun backend di hashing disponibile o formato hash non riconosciuto."""


def validate_password_policy(password: str) -> None:
    """Politica minima: lunghezza >= 12 caratteri (ADR-002 D5)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password non valida: lunghezza minima {MIN_PASSWORD_LENGTH} caratteri."
        )


def hash_password(password: str) -> str:
    """Restituisce l'hash della password (argon2id, o bcrypt se argon2 manca)."""
    validate_password_policy(password)
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    if _bcrypt is not None:
        return _bcrypt.hashpw(
            password.encode(), _bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        ).decode()
    raise HashingError(
        "Nessun backend di hashing disponibile: servono argon2-cffi o bcrypt."
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica password contro hash esistente, in tempo costante.

    Restituisce False se la password non corrisponde; solleva HashingError se
    il formato dell'hash non e' gestito o il backend richiesto non e' installato.
    """
    if not isinstance(password_hash, str) or not password_hash:
        raise HashingError("Hash password vuoto o non testuale.")
    if password_hash.startswith("$argon2"):
        if _ARGON2 is None:
            raise HashingError("Hash argon2 ma argon2-cffi non e' installato.")
        try:
            return _ARGON2.verify(password_hash, password)
        except VerifyMismatchError:
            return False
        except (InvalidHashError, Argon2Error) as exc:
            raise HashingError(f"Hash argon2 non valido: {exc}") from exc
    if password_hash.startswith("$2"):  # formato bcrypt: $2a$/$2b$/$2y$
        if _bcrypt is None:
            raise HashingError(
                "Hash bcrypt ma il pacchetto bcrypt non e' installato (fallback non disponibile)."
            )
        return _bcrypt.checkpw(password.encode(), password_hash.encode())
    raise HashingError(
        f"Formato hash password non riconosciuto: {password_hash[:12]!r}..."
    )
