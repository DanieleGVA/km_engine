"""Test hashing asincrono: argon2id fuori dall'event loop (WP-E1, GE1)."""
from __future__ import annotations

import asyncio
import time

from app.auth import hashing
from app.auth.tokens import login_async


async def test_login_does_not_block_event_loop(conn, make_user, monkeypatch) -> None:
    """Un login lento (hash argon2id simulato) non blocca un task concorrente."""
    make_user("async_login", password="test-password-123")

    original_verify = hashing.verify_password

    def slow_verify(password: str, password_hash: str) -> bool:
        time.sleep(0.3)
        return original_verify(password, password_hash)

    monkeypatch.setattr(hashing, "verify_password", slow_verify)

    order: list[str] = []

    async def fast_task() -> None:
        await asyncio.sleep(0.05)
        order.append("fast")

    async def do_login() -> None:
        result = await login_async(
            conn, "test_async_login", "test-password-123"
        )
        assert result["access_token"]
        order.append("login")

    await asyncio.gather(do_login(), fast_task())
    assert order == ["fast", "login"]


async def test_verify_password_async_offloads(monkeypatch) -> None:
    """``verify_password_async`` esegue la verifica in un worker thread."""
    calls: list[str] = []

    def fake_verify(password: str, password_hash: str) -> bool:
        calls.append("verify")
        return True

    monkeypatch.setattr(hashing, "verify_password", fake_verify)
    result = await hashing.verify_password_async("pw", "hash")
    assert result is True
    assert calls == ["verify"]
