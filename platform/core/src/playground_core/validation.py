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


# -- markdown with frontmatter (skills, subagents, commands) ----------------------

CONTEXT_FILE = "CLAUDE.md"
_SKILL_RE = re.compile(r"^\.claude/skills/[^/]+/SKILL\.md$")
_SUBAGENT_RE = re.compile(r"^\.claude/agents/[^/]+\.md$")
_COMMAND_RE = re.compile(r"^\.claude/commands/.+\.md$")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")


def parse_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """Top-level ``key: value`` pairs of a leading ``---`` block, and the
    body. Nested YAML values are kept as raw text. None when there is no
    frontmatter block; ValueError when it is never closed."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    out: dict[str, str] = {}
    key = ""
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return out, "\n".join(lines[i + 1 :])
        m = _FM_KEY_RE.match(line)
        if m and not line.startswith((" ", "\t")):
            key = m.group(1)
            out[key] = _unquote(m.group(2).strip())
        elif key and line.strip():
            out[key] = (out[key] + "\n" + line.strip()).strip()
    raise ValueError("frontmatter block is not closed with '---'")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _validate_named_markdown(path: str, content: bytes, what: str) -> list[Diagnostic]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [_error(path, "file is not valid UTF-8")]
    try:
        meta, body = parse_frontmatter(text)
    except ValueError as e:
        return [_warning(path, f"{e}; the SDK won't load this {what}")]
    if meta is None:
        return [
            _warning(
                path,
                f"missing frontmatter: start the file with '---', 'name:' and "
                f"'description:' lines so the SDK loads this {what}",
            )
        ]
    out: list[Diagnostic] = []
    name = meta.get("name", "")
    if not name:
        out.append(_warning(path, f"frontmatter needs a 'name' for this {what}", "/name"))
    elif not _NAME_RE.fullmatch(name) or len(name) > 64:
        out.append(_warning(path, "'name' should be lowercase letters, digits and dashes", "/name"))
    if not meta.get("description", ""):
        out.append(
            _warning(
                path,
                f"frontmatter needs a 'description'; it tells the model when to use this {what}",
                "/description",
            )
        )
    if not body.strip():
        out.append(_warning(path, f"this {what} has no instructions after the frontmatter"))
    return out


def validate_skill(path: str, content: bytes) -> list[Diagnostic]:
    return _validate_named_markdown(path, content, "skill")


def validate_subagent(path: str, content: bytes) -> list[Diagnostic]:
    return _validate_named_markdown(path, content, "subagent")


def validate_command(path: str, content: bytes) -> list[Diagnostic]:
    try:
        parse_frontmatter(content.decode("utf-8"))
    except UnicodeDecodeError:
        return [_error(path, "file is not valid UTF-8")]
    except ValueError as e:
        return [_warning(path, str(e))]
    return []


# "@path" imports in CLAUDE.md, outside code. Claude Code resolves them
# relative to the importing file.
_IMPORT_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_.][^\s`]*)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def claude_md_imports(text: str) -> list[str]:
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "", line)
        out += [m.group(1).rstrip(".,;:)") for m in _IMPORT_RE.finditer(line)]
    return out


def validate_context_imports(path: str, content: bytes, paths: set[str]) -> list[Diagnostic]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [_error(path, "file is not valid UTF-8")]
    out: list[Diagnostic] = []
    for ref in claude_md_imports(text):
        if ref.startswith("~") or ("/" not in ref and "." not in ref):
            continue  # home-dir imports and plain @mentions are not checked
        if ref.startswith(("/", "../")) or "/../" in ref:
            out.append(_warning(path, f"@{ref} points outside the agent folder"))
            continue
        clean = ref.removeprefix("./")
        if clean not in paths:
            out.append(_warning(path, f"@{ref} imports a file that does not exist"))
    return out


_VALIDATORS = {
    MANIFEST_FILE: validate_manifest,
    SETTINGS_FILE: validate_settings,
    MCP_FILE: validate_mcp,
}


def wants_content(rel_path: str) -> bool:
    """Whether folder validation needs this file's content."""
    return (
        rel_path in _VALIDATORS
        or rel_path == CONTEXT_FILE
        or bool(_SKILL_RE.match(rel_path) or _SUBAGENT_RE.match(rel_path))
        or bool(_COMMAND_RE.match(rel_path))
    )


def validate_file(rel_path: str, content: bytes) -> list[Diagnostic]:
    """Validate one file by its path relative to the agent root (checks that
    need the rest of the folder are in ``validate_folder``)."""
    validator = _VALIDATORS.get(rel_path)
    if validator:
        return validator(content)
    if _SKILL_RE.match(rel_path):
        return validate_skill(rel_path, content)
    if _SUBAGENT_RE.match(rel_path):
        return validate_subagent(rel_path, content)
    if _COMMAND_RE.match(rel_path):
        return validate_command(rel_path, content)
    return []


def validate_folder(files: Mapping[str, bytes], paths: set[str]) -> list[Diagnostic]:
    """Validate a whole agent folder. ``files`` holds the content of every
    file ``wants_content`` asked for; ``paths`` every path in the tree."""
    diags: list[Diagnostic] = []
    for path in sorted(files):
        diags += validate_file(path, files[path])
    context = files.get(CONTEXT_FILE)
    if context is None:
        diags.append(
            _warning(CONTEXT_FILE, "CLAUDE.md is missing; the agent starts with no instructions")
        )
    else:
        diags += validate_context_imports(CONTEXT_FILE, context, paths)
    return diags


def has_errors(diags: list[Diagnostic]) -> bool:
    return any(d.severity is Severity.ERROR for d in diags)
