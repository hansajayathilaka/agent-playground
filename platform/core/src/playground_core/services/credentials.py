"""Credential manager: named, encrypted endpoint + secret sets with one default."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from playground_core.errors import FailedPreconditionError, NotFoundError
from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import AuthType, CredentialSet, Provider
from playground_core.ports import AgentStore, Clock, CredentialRepository, SecretStore
from playground_core.providers import validate_credential_fields


def secret_context(cred_id: str) -> str:
    return f"credential-set:{cred_id}"


@dataclass(frozen=True, slots=True)
class CredentialInput:
    name: str
    description: str = ""
    provider: Provider = Provider.ANTHROPIC
    base_url: str = ""
    auth_type: AuthType = AuthType.API_KEY
    headers: dict[str, str] | None = None
    default_model: str = ""
    allowed_models: list[str] | None = None


class CredentialService:
    def __init__(
        self,
        repo: CredentialRepository,
        secrets: SecretStore,
        store: AgentStore,
        clock: Clock,
    ) -> None:
        self._repo = repo
        self._secrets = secrets
        self._store = store
        self._clock = clock

    async def list(self) -> Sequence[CredentialSet]:
        return await self._repo.list()

    async def get(self, cred_id: str) -> CredentialSet:
        cred = await self._repo.get(cred_id)
        if cred is None:
            raise NotFoundError(f"credential set {cred_id!r} not found")
        return cred

    async def create(
        self, data: CredentialInput, secret: str, *, make_default: bool
    ) -> CredentialSet:
        headers = dict(data.headers or {})
        validate_credential_fields(data.provider, data.base_url, headers, data.name)
        cred_id = str(uuid.uuid4())
        now = self._clock.now()
        first = not await self._repo.list()
        cred = CredentialSet(
            id=cred_id,
            name=data.name.strip(),
            description=data.description,
            provider=data.provider,
            base_url=data.base_url,
            auth_type=data.auth_type,
            headers=headers,
            default_model=data.default_model,
            allowed_models=list(data.allowed_models or []),
            is_default=False,
            created_at=now,
            updated_at=now,
            secret_ciphertext=self._encrypt(cred_id, secret),
        )
        await self._repo.add(cred)
        if make_default or first:
            await self._repo.set_default(cred_id)
        return await self.get(cred_id)

    async def update(
        self, cred_id: str, data: CredentialInput, secret: str | None
    ) -> CredentialSet:
        """``secret=None`` keeps the stored secret (it is write-only)."""
        cur = await self.get(cred_id)
        headers = dict(data.headers or {})
        validate_credential_fields(data.provider, data.base_url, headers, data.name)
        cred = replace(
            cur,
            name=data.name.strip(),
            description=data.description,
            provider=data.provider,
            base_url=data.base_url,
            auth_type=data.auth_type,
            headers=headers,
            default_model=data.default_model,
            allowed_models=list(data.allowed_models or []),
            updated_at=self._clock.now(),
            secret_ciphertext=(
                cur.secret_ciphertext if secret is None else self._encrypt(cred_id, secret)
            ),
        )
        await self._repo.update(cred)
        return cred

    async def make_default(self, cred_id: str) -> None:
        await self.get(cred_id)
        await self._repo.set_default(cred_id)

    async def delete(self, cred_id: str) -> None:
        cred = await self.get(cred_id)
        users = await self.agents_using(cred.name)
        if users:
            raise FailedPreconditionError(
                f"credential set {cred.name!r} is used by {', '.join(users)}; move them first"
            )
        others = [c for c in await self._repo.list() if c.id != cred_id]
        if cred.is_default and others:
            raise FailedPreconditionError("make another set the default before deleting this one")
        await self._repo.remove(cred_id)

    async def agents_using(self, name: str) -> list[str]:
        users: list[str] = []
        for slug in await self._store.list_agent_slugs():
            try:
                raw = (await self._store.read(slug, MANIFEST_FILE)).data
                if AgentManifest.model_validate_json(raw).credential_set == name:
                    users.append(slug)
            except (NotFoundError, ValueError):
                continue
        return users

    async def resolve(self, *, cred_id: str = "", name: str | None = None) -> CredentialSet:
        """Pick the set for a run: explicit id, then agent.json name, then the
        default. FailedPreconditionError when nothing matches."""
        if cred_id:
            cred = await self._repo.get(cred_id)
            if cred is None:
                raise FailedPreconditionError(f"credential set {cred_id!r} no longer exists")
            return cred
        if name:
            cred = await self._repo.get_by_name(name)
            if cred is None:
                raise FailedPreconditionError(f"credential set {name!r} from agent.json not found")
            return cred
        cred = await self._repo.get_default()
        if cred is None:
            raise FailedPreconditionError("no credential set configured; add one first")
        return cred

    def reveal(self, cred: CredentialSet) -> str:
        """Plaintext secret. Only the worker calls this, to build a run's env."""
        if not cred.secret_ciphertext:
            return ""
        return self._secrets.decrypt(
            cred.secret_ciphertext, context=secret_context(cred.id)
        ).decode()

    def _encrypt(self, cred_id: str, secret: str) -> bytes:
        if not secret:
            return b""
        return self._secrets.encrypt(secret.encode(), context=secret_context(cred_id))
