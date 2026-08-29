"""Test hashing password (argon2id, fallback bcrypt, verifica constant-time)."""
from __future__ import annotations

import pytest

from app.auth import hash_password, hashing, verify_password


class TestArgon2:
    def test_hash_and_verify_roundtrip(self):
        pw = "correct-horse-battery"
        h = hash_password(pw)
        assert h.startswith("$argon2")
        assert verify_password(pw, h) is True

    def test_wrong_password_returns_false(self):
        h = hash_password("correct-horse-battery")
        assert verify_password("wrong-password-123", h) is False

    def test_salt_is_random_each_hash(self):
        h1 = hash_password("same-password-123")
        h2 = hash_password("same-password-123")
        assert h1 != h2
        assert verify_password("same-password-123", h1)
        assert verify_password("same-password-123", h2)

    def test_password_never_in_hash(self):
        pw = "secret-password-123"
        assert pw not in hash_password(pw)

    def test_short_password_rejected_by_policy(self):
        with pytest.raises(ValueError, match="lunghezza minima"):
            hash_password("short")

    def test_verify_garbage_hash_raises_explicit(self):
        with pytest.raises(hashing.HashingError, match="non riconosciuto"):
            verify_password("whatever-password", "not-a-hash")


class TestBcryptFallback:
    """Fallback documentato (ADR-002 D5): attivo solo se bcrypt e' installato."""

    def test_fallback_hash_and_verify(self, monkeypatch):
        if hashing._bcrypt is None:
            pytest.skip("pacchetto bcrypt non installato: fallback non esercitabile")
        monkeypatch.setattr(hashing, "_ARGON2", None)
        h = hash_password("fallback-password-123")
        assert h.startswith("$2")
        assert verify_password("fallback-password-123", h) is True
        assert verify_password("other-password-123", h) is False

    def test_no_backend_available_raises_explicit(self, monkeypatch):
        monkeypatch.setattr(hashing, "_ARGON2", None)
        monkeypatch.setattr(hashing, "_bcrypt", None)
        with pytest.raises(hashing.HashingError, match="Nessun backend"):
            hash_password("some-password-12345")

    def test_argon2_hash_without_argon2_backend_raises(self, monkeypatch):
        monkeypatch.setattr(hashing, "_ARGON2", None)
        h = "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$hash"
        with pytest.raises(hashing.HashingError, match="argon2-cffi non e' installato"):
            verify_password("some-password-12345", h)
