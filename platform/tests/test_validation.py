from __future__ import annotations

import json

from playground_core.models import Severity
from playground_core.validation import validate_file, validate_folder


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


def test_skill_and_subagent_frontmatter() -> None:
    good = b"---\nname: research\ndescription: Look things up. Use when asked.\n---\n\n1. Search.\n"
    assert validate_file(".claude/skills/research/SKILL.md", good) == []
    assert validate_file(".claude/agents/reviewer.md", good) == []

    (missing,) = validate_file(".claude/skills/x/SKILL.md", b"# no frontmatter\n")
    assert missing.severity is Severity.WARNING and "missing frontmatter" in missing.message

    diags = validate_file(".claude/agents/r.md", b"---\nname: Bad Name\n---\nbody\n")
    assert {d.location for d in diags} == {"/name", "/description"}
    assert all(d.severity is Severity.WARNING for d in diags)

    (unclosed,) = validate_file(".claude/skills/x/SKILL.md", b"---\nname: x\n")
    assert "not closed" in unclosed.message
    # Quoted and multi-line values count as present.
    multi = b'---\nname: "x"\ndescription: >\n  Long text\n  here\n---\nSteps\n'
    assert validate_file(".claude/skills/x/SKILL.md", multi) == []
    # Commands only need well-formed frontmatter, if any.
    assert validate_file(".claude/commands/go.md", b"Do the thing\n") == []


def test_claude_md_imports_must_exist() -> None:
    text = (
        b"# A\n\nSee @memory/MEMORY.md and @docs/missing.md.\n"
        b"Email me@example.com, ping @alice, `@not/an/import`.\n"
        b"```\n@also/ignored.md\n```\n@../outside.md\n"
    )
    diags = validate_folder({"CLAUDE.md": text}, {"CLAUDE.md", "memory/MEMORY.md"})
    assert [d.message for d in diags] == [
        "@docs/missing.md imports a file that does not exist",
        "@../outside.md points outside the agent folder",
    ]
    (no_context,) = validate_folder({}, set())
    assert no_context.path == "CLAUDE.md" and no_context.severity is Severity.WARNING
