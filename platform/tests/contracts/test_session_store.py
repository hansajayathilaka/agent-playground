"""Contract: every SessionStore adapter behaves the same."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from playground_core.models import Principal
from playground_core.ports import SessionStore

ADMIN = Principal("admin")


async def test_put_get_delete(session_store: SessionStore) -> None:
    await session_store.put("tok-1", ADMIN, timedelta(minutes=5))
    assert await session_store.get("tok-1") == ADMIN
    assert await session_store.get("tok-2") is None
    await session_store.delete("tok-1")
    await session_store.delete("tok-1")
    assert await session_store.get("tok-1") is None


async def test_expiry(session_store: SessionStore) -> None:
    await session_store.put("short", ADMIN, timedelta(milliseconds=50))
    await asyncio.sleep(0.15)
    assert await session_store.get("short") is None
