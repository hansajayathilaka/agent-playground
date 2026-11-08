"""Turn a credential set into the environment for one run (ModelProvider).

The result is the only provider configuration a run gets: the worker builds
each run's env from scratch, so nothing from its own environment leaks in.
"""

from __future__ import annotations

from urllib.parse import urlparse

from playground_core.errors import FailedPreconditionError, InvalidArgumentError
from playground_core.models import AuthType, CredentialSet, Provider

# Providers this phase can run. Bedrock and Vertex need cloud credential
# files and land with the provider work in Phase 5.
SUPPORTED_PROVIDERS = frozenset({Provider.ANTHROPIC, Provider.GATEWAY, Provider.LITELLM})

_HEADER_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def validate_credential_fields(
    provider: Provider, base_url: str, headers: dict[str, str], name: str
) -> None:
    if not name.strip() or len(name) > 100:
        raise InvalidArgumentError("credential set name must be 1-100 characters")
    if provider not in SUPPORTED_PROVIDERS:
        raise InvalidArgumentError(f"provider {provider.value!r} is not supported yet")
    if provider in (Provider.GATEWAY, Provider.LITELLM) and not base_url:
        raise InvalidArgumentError(f"provider {provider.value!r} needs a base_url")
    if base_url:
        u = urlparse(base_url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise InvalidArgumentError("base_url must be an http(s) URL")
        if u.username or u.password:
            raise InvalidArgumentError("put credentials in the secret, not in base_url")
    for key, value in headers.items():
        if not key or not set(key) <= _HEADER_NAME_CHARS:
            raise InvalidArgumentError(f"invalid header name {key!r}")
        if "\n" in value or "\r" in value:
            raise InvalidArgumentError(f"header {key!r} contains a newline")


def provider_env(cred: CredentialSet, secret: str) -> dict[str, str]:
    """Env vars the Claude Agent SDK reads for this credential set."""
    if cred.provider not in SUPPORTED_PROVIDERS:
        raise FailedPreconditionError(f"provider {cred.provider.value!r} is not supported yet")
    if not secret:
        raise FailedPreconditionError(f"credential set {cred.name!r} has no secret")
    env: dict[str, str] = {}
    if cred.base_url:
        env["ANTHROPIC_BASE_URL"] = cred.base_url
    if cred.auth_type is AuthType.BEARER:
        env["ANTHROPIC_AUTH_TOKEN"] = secret
    else:
        env["ANTHROPIC_API_KEY"] = secret
    if cred.headers:
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in cred.headers.items())
    return env


def resolve_model(manifest_model: str | None, cred: CredentialSet) -> str | None:
    """agent.json model wins, then the set's default; None = SDK default."""
    model = manifest_model or cred.default_model or None
    if model and cred.allowed_models and model not in cred.allowed_models:
        raise FailedPreconditionError(
            f"model {model!r} is not allowed by credential set {cred.name!r}"
        )
    return model
