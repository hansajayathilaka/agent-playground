"""AuthProvider for the single v1 admin user."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets

from playground_core.models import Principal


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class StaticAdminAuthProvider:
    """One admin account configured from the environment. The password is kept
    only as a salted scrypt hash in memory and compared in constant time."""

    def __init__(self, username: str, password: str) -> None:
        if not password:
            raise ValueError("ADMIN_PASSWORD must be set")
        self._username = username
        self._salt = secrets.token_bytes(16)
        self._hash = _scrypt(password, self._salt)

    async def authenticate(self, username: str, password: str) -> Principal | None:
        # Always hash, so a wrong username costs the same as a wrong password.
        candidate = await asyncio.to_thread(_scrypt, password, self._salt)
        ok_pw = hmac.compare_digest(candidate, self._hash)
        ok_user = hmac.compare_digest(username.encode(), self._username.encode())
        return Principal(self._username) if ok_pw and ok_user else None
