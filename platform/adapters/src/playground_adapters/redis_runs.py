"""Redis adapters for Phase 2: JobQueue, EventLog and ControlChannel.

JobQueue layout (prefix ``ap:q:``):

- ``agent:<slug>``   list, the agent's FIFO of waiting jobs
- ``active:<slug>``  string, the job the lock holder is running
- ``lock:<slug>``    string, lock token with a TTL (the lease)
- ``agents``         set of slugs with waiting or active jobs
- ``wake``           stream (consumer group ``workers``) of "look at <slug>"

A wake is only a hint: whoever reads it tries the agent lock and, with it,
takes the active job (left by a dead holder, so ``recovered``) or pops the
next one. The holder releases the lock *before* checking for more work and
re-waking, so a wake dropped because the lock was busy is never lost. The
lock is a token compared in Lua on renew/release, so an expired holder can't
release someone else's lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from playground_core.events import Event
from playground_core.models import ControlKind, ControlMessage, Job, Lease

log = logging.getLogger(__name__)

GROUP = "workers"
# Blocking reads stay well under the client's socket timeout (redis-py 8
# defaults to a few seconds), so callers loop instead of blocking long.
MAX_BLOCK_MS = 2_000

_CLAIM = """
local lock, active, pending, agents = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local token, ttl, slug = ARGV[1], ARGV[2], ARGV[3]
if not redis.call('SET', lock, token, 'NX', 'PX', ttl) then return nil end
local job = redis.call('GET', active)
if job then return {job, 1} end
job = redis.call('LPOP', pending)
if not job then
  redis.call('DEL', lock)
  redis.call('SREM', agents, slug)
  return nil
end
redis.call('SET', active, job)
return {job, 0}
"""

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_COMPLETE = """
local lock, active, pending, agents, wake = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5]
local token, slug = ARGV[1], ARGV[2]
if redis.call('GET', lock) ~= token then return 0 end
redis.call('DEL', active)
redis.call('DEL', lock)
if redis.call('LLEN', pending) > 0 then
  redis.call('XADD', wake, 'MAXLEN', '~', '10000', '*', 'slug', slug)
else
  redis.call('SREM', agents, slug)
end
return 1
"""

_RECOVER = """
local n = 0
for _, slug in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  local p = ARGV[1]
  if redis.call('EXISTS', p .. 'lock:' .. slug) == 0 then
    if redis.call('EXISTS', p .. 'active:' .. slug) == 1
       or redis.call('LLEN', p .. 'agent:' .. slug) > 0 then
      redis.call('XADD', KEYS[2], 'MAXLEN', '~', '10000', '*', 'slug', slug)
      n = n + 1
    else
      redis.call('SREM', KEYS[1], slug)
    end
  end
end
return n
"""


# Edit lock: same key as a run's lease, so edits and runs exclude each other.
_UNLOCK = """
local lock, active, pending, agents, wake = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5]
if redis.call('GET', lock) ~= ARGV[1] then return 0 end
redis.call('DEL', lock)
if redis.call('LLEN', pending) > 0 or redis.call('EXISTS', active) == 1 then
  redis.call('XADD', wake, 'MAXLEN', '~', '10000', '*', 'slug', ARGV[2])
end
return 1
"""


def _s(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _job_json(job: Job) -> str:
    return json.dumps({"run_id": job.run_id, "agent_slug": job.agent_slug})


def _job(raw: bytes | str) -> Job:
    d = json.loads(raw)
    return Job(d["run_id"], d["agent_slug"])


class RedisJobQueue:
    def __init__(self, client: Redis, prefix: str = "ap:q:", lock_ttl: float = 30.0) -> None:
        self._r = client
        self._p = prefix
        self._ttl_ms = int(lock_ttl * 1000)
        self._wake = prefix + "wake"
        self._agents = prefix + "agents"
        self._group_ready = False
        self._claim = client.register_script(_CLAIM)
        self._renew = client.register_script(_RENEW)
        self._complete = client.register_script(_COMPLETE)
        self._recover = client.register_script(_RECOVER)
        self._unlock = client.register_script(_UNLOCK)

    def _keys(self, slug: str) -> list[str]:
        p = self._p
        return [p + "lock:" + slug, p + "active:" + slug, p + "agent:" + slug, self._agents]

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._r.xgroup_create(self._wake, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        self._group_ready = True

    async def enqueue(self, job: Job) -> None:
        slug = job.agent_slug
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.rpush(self._p + "agent:" + slug, _job_json(job))
            pipe.sadd(self._agents, slug)
            pipe.xadd(self._wake, {"slug": slug}, maxlen=10000, approximate=True)
            await pipe.execute()

    async def claim(self, consumer: str, timeout: float) -> Lease | None:
        await self._ensure_group()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            block = max(1, min(int(remaining * 1000), MAX_BLOCK_MS))
            resp: Any = await self._r.xreadgroup(
                GROUP, consumer, {self._wake: ">"}, count=1, block=block
            )
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    # Acked at once: a crash from here on is covered by recover().
                    await self._r.xack(self._wake, GROUP, entry_id)
                    await self._r.xdel(self._wake, entry_id)
                    raw_slug = fields.get(b"slug") or fields.get("slug")
                    if raw_slug is None:
                        continue
                    lease = await self._try_claim(_s(raw_slug))
                    if lease is not None:
                        return lease

    async def _try_claim(self, slug: str) -> Lease | None:
        token = secrets.token_hex(16)
        out: Any = await self._claim(keys=self._keys(slug), args=[token, self._ttl_ms, slug])
        if out is None:
            return None
        raw, recovered = cast(list[Any], out)
        return Lease(_job(raw), token, recovered=bool(int(recovered)))

    async def renew(self, lease: Lease, ttl: timedelta) -> bool:
        keys = self._keys(lease.job.agent_slug)[:1]
        ms = int(ttl.total_seconds() * 1000)
        return bool(await self._renew(keys=keys, args=[lease.token, ms]))

    async def complete(self, lease: Lease) -> None:
        slug = lease.job.agent_slug
        await self._complete(keys=[*self._keys(slug), self._wake], args=[lease.token, slug])

    async def queued(self, agent_slug: str) -> Sequence[str]:
        raw: list[bytes | str] = await self._r.lrange(self._p + "agent:" + agent_slug, 0, -1)  # pyright: ignore[reportUnknownMemberType]
        return [_job(r).run_id for r in raw]

    async def recover(self) -> int:
        await self._ensure_group()
        return int(await self._recover(keys=[self._agents, self._wake], args=[self._p]))

    async def lock(self, agent_slug: str, ttl: timedelta) -> str | None:
        token = secrets.token_hex(16)
        ms = max(1, int(ttl.total_seconds() * 1000))
        ok = await self._r.set(self._p + "lock:" + agent_slug, token, nx=True, px=ms)
        return token if ok else None

    async def unlock(self, agent_slug: str, token: str) -> None:
        keys = [*self._keys(agent_slug), self._wake]
        await self._unlock(keys=keys, args=[token, agent_slug])


# -- EventLog --------------------------------------------------------------------

# Offsets are our own per-conversation counter, used as the stream entry id
# "0-<offset>" so XRANGE/XREAD can resume from any offset directly.
_APPEND = """
local n = redis.call('INCR', KEYS[2])
redis.call('XADD', KEYS[1], '0-' .. n, 'e', ARGV[1])
return n
"""


class RedisEventLog:
    def __init__(
        self, client: Redis, prefix: str = "ap:events:", block_ms: int = MAX_BLOCK_MS
    ) -> None:
        self._r = client
        self._p = prefix
        self._block_ms = min(block_ms, MAX_BLOCK_MS)
        self._append = client.register_script(_APPEND)

    def _key(self, conversation_id: str) -> str:
        return self._p + conversation_id

    async def append(self, event: Event) -> int:
        key = self._key(event.conversation_id)
        return int(await self._append(keys=[key, key + ":seq"], args=[event.to_json()]))

    @staticmethod
    def _decode(entries: Any) -> list[Event]:
        out: list[Event] = []
        for entry_id, fields in entries:
            offset = int(_s(entry_id).split("-", 1)[1])
            raw = fields.get(b"e") or fields.get("e")
            out.append(Event.from_json(raw, offset))
        return out

    async def read(self, conversation_id: str, after: int, limit: int) -> Sequence[Event]:
        entries: Any = await self._r.xrange(
            self._key(conversation_id), min=f"(0-{after}", max="+", count=limit
        )
        return self._decode(entries)

    async def tail(self, conversation_id: str, after: int) -> AsyncIterator[Event]:
        key = self._key(conversation_id)
        while True:
            batch = await self.read(conversation_id, after, 500)
            for event in batch:
                yield event
                after = event.offset
            if len(batch) == 500:
                continue
            resp: Any = await self._r.xread({key: f"0-{after}"}, count=500, block=self._block_ms)
            for _stream, entries in resp or ():
                for event in self._decode(entries):
                    yield event
                    after = event.offset


# -- ControlChannel ----------------------------------------------------------------


class RedisControlChannel:
    def __init__(self, client: Redis, prefix: str = "ap:control:") -> None:
        self._r = client
        self._p = prefix

    async def publish(self, message: ControlMessage) -> None:
        payload = json.dumps({"run_id": message.run_id, "kind": message.kind.value})
        await self._r.publish(self._p + message.run_id, payload)  # pyright: ignore[reportUnknownMemberType]

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[AsyncIterator[ControlMessage]]:
        channel = self._p + run_id
        pubsub = self._r.pubsub()  # pyright: ignore[reportUnknownMemberType]
        await pubsub.subscribe(channel)  # pyright: ignore[reportUnknownMemberType]
        # Wait for the server's confirmation so the caller is really subscribed.
        while True:
            first: Any = await pubsub.get_message(timeout=5.0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if first is None:
                raise TimeoutError("control channel subscribe timed out")
            if first["type"] == "subscribe":
                break

        async def messages() -> AsyncIterator[ControlMessage]:
            # Short polls rather than listen(): an idle read longer than the
            # client's socket timeout would raise instead of waiting.
            while True:
                raw: Any = await pubsub.get_message(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if raw is None:
                    continue
                m = cast(dict[str, Any], raw)
                if m.get("type") != "message":
                    continue
                try:
                    d = json.loads(m["data"])
                    yield ControlMessage(d["run_id"], ControlKind(d["kind"]))
                except (ValueError, KeyError):
                    log.warning("ignoring malformed control message on %s", channel)

        try:
            yield messages()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]
            await pubsub.aclose()  # pyright: ignore[reportUnknownMemberType]
