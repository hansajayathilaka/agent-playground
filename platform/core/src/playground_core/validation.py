"""Validation of schema-checked agent files.

Validation never blocks a save. An error diagnostic blocks runs instead, so a
half-edited file can still be saved from the editor.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import Diagnostic, Severity

SETTINGS_FILE = ".claude/settings.json"
MCP_FILE = ".mcp.json"
VALIDATED_FILES = frozenset({MANIFEST_FILE, SETTINGS_FILE, MCP_FILE})

# Env keys that look like secrets. Secrets belong in the credential manager,
# never in agent folders.
_SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_SECRET_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _error(path: str, message: str, location: str = "") -> Diagnostic:
    return Diagnostic(path, Severity.ERROR, message, location)


def _warning(path: str, message: str, location: str = "") -> Diagnostic:
    return Diagnostic(path, Severity.WARNING, message, location)


def _parse_object(path: str, content: bytes) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    try:
        value: object = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None, [_error(path, "file is not valid UTF-8")]
    except json.JSONDecodeError as e:
        return None, [_error(path, f"invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})")]
    if not isinstance(value, dict):
        return None, [_error(path, "top-level value must be a JSON object")]
    return cast(dict[str, Any], value), []


def _check_env_secrets(path: str, env: object, location: str) -> list[Diagnostic]:
    if not isinstance(env, Mapping):
        return []
    out: list[Diagnostic] = []
    for key, value in cast(Mapping[str, object], env).items():
        literal = isinstance(value, str) and value and not _SECRET_REF_RE.match(value)
        if _SECRET_KEY_RE.search(key) and literal:
            out.append(
                _warning(
                    path,
                    f"env {key!r} looks like a secret; store it in the credential manager "
                    "and reference it as ${NAME}",
                    f"{location}/{key}",
                )
            )
    return out


def validate_manifest(content: bytes) -> list[Diagnostic]:
    data, diags = _parse_object(MANIFEST_FILE, content)
    if data is None:
        return diags
    try:
        AgentManifest.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            loc = "/" + "/".join(str(p) for p in err["loc"])
            diags.append(_error(MANIFEST_FILE, err["msg"], loc))
    return diags


def validate_settings(content: bytes) -> list[Diagnostic]:
    data, diags = _parse_object(SETTINGS_FILE, content)
    if data is None:
        return diags
    for key in ("permissions", "hooks", "sandbox", "env"):
        if key in data and not isinstance(data[key], dict):
            diags.append(_error(SETTINGS_FILE, f"{key!r} must be an object", f"/{key}"))
    diags += _check_env_secrets(SETTINGS_FILE, data.get("env"), "/env")
    return diags


def validate_mcp(content: bytes) -> list[Diagnostic]:
    data, diags = _parse_object(MCP_FILE, content)
    if data is None:
        return diags
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return [*diags, _error(MCP_FILE, "'mcpServers' must be an object", "/mcpServers")]
    for name, raw in cast(dict[str, object], servers).items():
        loc = f"/mcpServers/{name}"
        if not isinstance(raw, dict):
            diags.append(_error(MCP_FILE, "server entry must be an object", loc))
            continue
        server = cast(dict[str, object], raw)
        kind = server.get("type", "stdio")
        if kind == "stdio":
            if not isinstance(server.get("command"), str):
                diags.append(_error(MCP_FILE, "stdio server needs a 'command' string", loc))
        elif kind in ("http", "sse"):
            if not isinstance(server.get("url"), str):
                diags.append(_error(MCP_FILE, f"{kind} server needs a 'url' string", loc))
        else:
            diags.append(_error(MCP_FILE, f"unknown server type {kind!r}", f"{loc}/type"))
        diags += _check_env_secrets(MCP_FILE, server.get("env"), f"{loc}/env")
    return diags


_VALIDATORS = {
    MANIFEST_FILE: validate_manifest,
    SETTINGS_FILE: validate_settings,
    MCP_FILE: validate_mcp,
}


def validate_file(rel_path: str, content: bytes) -> list[Diagnostic]:
    """Validate one file by its path relative to the agent root."""
    validator = _VALIDATORS.get(rel_path)
    return validator(content) if validator else []


def has_errors(diags: list[Diagnostic]) -> bool:
    return any(d.severity is Severity.ERROR for d in diags)
