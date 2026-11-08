from __future__ import annotations

import json

from playground_core.models import Severity
from playground_core.validation import validate_file


def _j(value: object) -> bytes:
    return json.dumps(value).encode()


def test_valid_manifest() -> None:
    assert validate_file("agent.json", _j({"name": "A", "approvals": {"mode": "off"}})) == []


def test_manifest_errors_have_locations() -> None:
    diags = validate_file("agent.json", _j({"name": "A", "approvals": {"mode": "sometimes"}}))
    assert [d.location for d in diags] == ["/approvals/mode"]
    assert diags[0].severity is Severity.ERROR


def test_manifest_rejects_unknown_keys() -> None:
    diags = validate_file("agent.json", _j({"name": "A", "modle": "x"}))
    assert diags and diags[0].location == "/modle"


def test_invalid_json_reports_position() -> None:
    (d,) = validate_file("agent.json", b'{"name": ')
    assert "invalid JSON" in d.message and "line 1" in d.message


def test_top_level_must_be_object() -> None:
    (d,) = validate_file(".mcp.json", b"[]")
    assert "object" in d.message


def test_mcp_server_shapes() -> None:
    diags = validate_file(
        ".mcp.json",
        _j(
            {
                "mcpServers": {
                    "ok": {"command": "npx", "args": ["x"]},
                    "web": {"type": "http", "url": "https://example.com/mcp"},
                    "bad": {"type": "http"},
                    "odd": {"type": "carrier-pigeon"},
                }
            }
        ),
    )
    assert sorted(d.location for d in diags) == ["/mcpServers/bad", "/mcpServers/odd/type"]


def test_literal_secrets_warn_but_references_do_not() -> None:
    diags = validate_file(
        ".claude/settings.json",
        _j({"env": {"GITHUB_TOKEN": "ghp_live", "API_KEY": "${API_KEY}", "MODE": "x"}}),
    )
    assert [(d.severity, d.location) for d in diags] == [(Severity.WARNING, "/env/GITHUB_TOKEN")]


def test_other_files_are_not_validated() -> None:
    assert validate_file("CLAUDE.md", b"\xff not utf8") == []
