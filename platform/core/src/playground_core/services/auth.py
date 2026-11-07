"""Login sessions on top of AuthProvider and SessionStore."""

from __future__ import annotations

import secrets
from datetime import timedelta

from playground_core.errors import UnauthenticatedError
from playground_core.models import AuthSession, Principal
from playground_core.ports import AuthProvider, Clock, SessionStore


class AuthService:
    def __init__(
        self, provider: AuthProvider, sessions: SessionStore, clock: Clock, ttl: timedelta
    ) -> None:
        self._provider = provider
        self._sessions = sessions
        self._clock = clock
        self._ttl = ttl

    async def login(self, username: str, password: str) -> AuthSession:
        principal = await self._provider.authenticate(username, password)
        if principal is None:
            raise UnauthenticatedError("invalid username or password")
        token = secrets.token_urlsafe(32)
        await self._sessions.put(token, principal, self._ttl)
        return AuthSession(token, principal, self._clock.now() + self._ttl)

    async def logout(self, token: str) -> None:
        await self._sessions.delete(token)

    async def authenticate_token(self, token: str) -> Principal:
        if not token:
            raise UnauthenticatedError("missing bearer token")
        principal = await self._sessions.get(token)
        if principal is None:
            raise UnauthenticatedError("session expired or invalid")
        return principal
