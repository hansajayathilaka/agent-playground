"""SessionStore on Redis: one key per session with a TTL."""

from __future__ import annotations

from datetime import timedelta

from redis.asyncio import Redis

from playground_adapters.memory import hash_token
from playground_core.models import Principal


class RedisSessionStore:
    def __init__(self, client: Redis, prefix: str = "ap:session:") -> None:
        self._r = client
        self._prefix = prefix

    def _key(self, token: str) -> str:
        return self._prefix + hash_token(token)

    async def put(self, token: str, principal: Principal, ttl: timedelta) -> None:
        await self._r.set(self._key(token), principal.username, px=int(ttl.total_seconds() * 1000))

    async def get(self, token: str) -> Principal | None:
        raw: bytes | str | None = await self._r.get(self._key(token))
        if raw is None:
            return None
        return Principal(raw.decode() if isinstance(raw, bytes) else raw)

    async def delete(self, token: str) -> None:
        await self._r.delete(self._key(token))
