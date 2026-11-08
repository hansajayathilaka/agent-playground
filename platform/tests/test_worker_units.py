"""Guard hook, launcher env scrubbing, SDK message translation, secrets,
providers and the credential service."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import InMemoryCredentialRepository
from playground_adapters.secrets import AesGcmSecretStore, generate_master_key, parse_master_key
from playground_core.errors import FailedPreconditionError, InvalidArgumentError
from playground_core.events import EventKind
from playground_core.models import AuthType, CredentialSet, Provider
from playground_core.providers import provider_env, resolve_model
from playground_core.services.agents import AgentService
from playground_core.services.credentials import CredentialInput, CredentialService
from playground_worker.guard import check_tool
from playground_worker.launcher import DROP_VAR, REAL_CLI_VAR, scrubbed_env
from playground_worker.sdk_runner import Translator

from .conftest import FixedClock

# -- guard -----------------------------------------------------------------------


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agents" / "alpha"
    for d in ("memory", "workspace", "state", ".git", "tools"):
        (root / d).mkdir(parents=True)
    (root / "CLAUDE.md").write_text("hi")
    (tmp_path / "agents" / "beta").mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    return root


@pytest.mark.parametrize(
    ("tool", "inp", "allowed"),
    [
        ("Read", {"file_path": "CLAUDE.md"}, True),
        ("Read", {"file_path": "{root}/CLAUDE.md"}, True),
        ("Read", {"file_path": "{root}/../beta/x"}, False),
        ("Read", {"file_path": "../secret.txt"}, False),
        ("Read", {"file_path": "/etc/passwd"}, False),
        ("Read", {"file_path": "state/session.json"}, False),
        ("Read", {"file_path": ".git/config"}, False),
        ("Glob", {"pattern": "**/*"}, True),
        ("Grep", {"pattern": "x", "path": "/"}, False),
        ("Write", {"file_path": "workspace/out.txt"}, True),
        ("Write", {"file_path": "memory/MEMORY.md"}, True),
        ("Write", {"file_path": "CLAUDE.md"}, False),
        ("Edit", {"file_path": "tools/run.sh"}, False),
        ("Write", {"file_path": "agent.json"}, False),
        ("Write", {"file_path": "workspace/../CLAUDE.md"}, False),
        ("NotebookEdit", {"notebook_path": "workspace/n.ipynb"}, True),
        ("Bash", {"command": "cat /etc/passwd"}, True),  # OS sandbox's job (Phase 3)
    ],
)
def test_guard(agent_dir: Path, tool: str, inp: dict[str, str], allowed: bool) -> None:
    inp = {k: v.replace("{root}", str(agent_dir)) for k, v in inp.items()}
    assert (check_tool(tool, inp, agent_dir) is None) is allowed


def test_guard_symlink_escape(agent_dir: Path) -> None:
    (agent_dir / "workspace" / "link").symlink_to(agent_dir.parent / "beta")
    assert check_tool("Write", {"file_path": "workspace/link/x"}, agent_dir) is not None
    assert check_tool("Read", {"file_path": "workspace/link"}, agent_dir) is not None


# -- launcher ----------------------------------------------------------------------


def test_launcher_drops_worker_env() -> None:
    env = {
        "DATABASE_URL": "postgres://secret",
        "SECRETS_KEY": "k",
        "ANTHROPIC_API_KEY": "run-key",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        DROP_VAR: "DATABASE_URL,SECRETS_KEY",
        REAL_CLI_VAR: "/x/claude",
    }
    assert scrubbed_env(env) == {"ANTHROPIC_API_KEY": "run-key", "CLAUDE_CODE_ENTRYPOINT": "sdk-py"}


def test_runner_env_is_built_from_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from playground_core.ports import RunRequest
    from playground_worker.sdk_runner import ClaudeAgentRunner

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    runner = ClaudeAgentRunner(tmp_path / "agents", tmp_path / "sessions", ["HTTPS_PROXY"])
    req = RunRequest("r", "c", "alpha", "p", "", None, None, None, {"ANTHROPIC_API_KEY": "k"})
    env = runner.run_env(req)
    final = scrubbed_env({**os.environ, **env})
    assert "DATABASE_URL" not in final
    assert final["ANTHROPIC_API_KEY"] == "k"
    assert final["HTTPS_PROXY"] == "http://proxy:3128"
    assert final["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "alpha" / ".claude")
    assert set(final) == set(env) - {DROP_VAR, REAL_CLI_VAR}


# -- translator --------------------------------------------------------------------


def _stream(ev: dict[str, object], parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=ev, parent_tool_use_id=parent)


def test_translator_matches_final_blocks_to_stream_indexes() -> None:
    tr = Translator()
    msgs = [
        _stream({"type": "message_start", "message": {"id": "msg_1"}}),
        _stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            }
        ),
        _stream({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use"}}),
        _stream({"type": "content_block_start", "index": 2, "content_block": {"type": "text"}}),
        # The CLI may deliver final blocks one message at a time.
        AssistantMessage(content=[TextBlock("Hi there")], model="m", message_id="msg_1"),
        AssistantMessage(
            content=[ToolUseBlock("toolu_1", "Task", {"prompt": "go"})],
            model="m",
            message_id="msg_1",
        ),
        AssistantMessage(content=[TextBlock("bye")], model="m", message_id="msg_1"),
        UserMessage(
            content=[ToolResultBlock("toolu_1", [{"type": "text", "text": "done"}], False)],
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[TextBlock("sub")], model="m", parent_tool_use_id="toolu_1", message_id="msg_2"
        ),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=2,
            session_id="sess-1",
            total_cost_usd=0.01,
            usage={"input_tokens": 3, "output_tokens": 4},
            result="bye",
        ),
    ]
    out = [ev for m in msgs for ev in tr.translate(m)]
    assert out[0] == (EventKind.TEXT_DELTA, {"message_id": "msg_1", "index": 0, "text": "Hi"}, "")
    texts = [(d["index"], d["text"], p) for k, d, p in out if k is EventKind.ASSISTANT_TEXT]
    assert texts == [(0, "Hi there", ""), (2, "bye", ""), (0, "sub", "toolu_1")]
    call = next(d for k, d, _ in out if k is EventKind.TOOL_CALL)
    assert call == {"tool_use_id": "toolu_1", "name": "Task", "input_json": '{"prompt": "go"}'}
    result = next(d for k, d, _ in out if k is EventKind.TOOL_RESULT)
    assert result == {"tool_use_id": "toolu_1", "content": "done", "is_error": False}
    assert tr.session_id == "sess-1"
    usage = tr.usage()
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.num_turns) == (
        3,
        4,
        0.01,
        2,
    )


# -- secrets and providers -----------------------------------------------------------


def test_secret_store_round_trip_and_binding() -> None:
    store = AesGcmSecretStore(parse_master_key(generate_master_key()))
    ct = store.encrypt(b"sk-1", context="credential-set:a")
    assert b"sk-1" not in ct
    assert store.decrypt(ct, context="credential-set:a") == b"sk-1"
    with pytest.raises(ValueError, match="decrypted"):
        store.decrypt(ct, context="credential-set:b")
    other = AesGcmSecretStore(parse_master_key(generate_master_key()))
    with pytest.raises(ValueError):
        other.decrypt(ct, context="credential-set:a")
    with pytest.raises(ValueError):
        parse_master_key("")
    with pytest.raises(ValueError):
        parse_master_key("c2hvcnQ=")


def _cred(**kw: object) -> CredentialSet:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    base: dict[str, object] = {
        "id": "1",
        "name": "n",
        "description": "",
        "provider": Provider.ANTHROPIC,
        "base_url": "",
        "auth_type": AuthType.API_KEY,
        "headers": {},
        "default_model": "",
        "allowed_models": [],
        "is_default": True,
        "created_at": t,
        "updated_at": t,
    }
    base.update(kw)
    return CredentialSet(**base)  # pyright: ignore[reportArgumentType]


def test_provider_env() -> None:
    assert provider_env(_cred(), "k") == {"ANTHROPIC_API_KEY": "k"}
    gw = _cred(
        provider=Provider.GATEWAY,
        base_url="https://api.deepseek.test/anthropic",
        auth_type=AuthType.BEARER,
        headers={"X-A": "1", "X-B": "2"},
    )
    assert provider_env(gw, "t") == {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.test/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_CUSTOM_HEADERS": "X-A: 1\nX-B: 2",
    }
    with pytest.raises(FailedPreconditionError):
        provider_env(_cred(), "")
    with pytest.raises(FailedPreconditionError):
        provider_env(_cred(provider=Provider.BEDROCK), "k")


def test_resolve_model() -> None:
    assert resolve_model(None, _cred()) is None
    assert resolve_model(None, _cred(default_model="d")) == "d"
    assert resolve_model("a", _cred(default_model="d", allowed_models=["a"])) == "a"
    with pytest.raises(FailedPreconditionError):
        resolve_model("x", _cred(allowed_models=["a"]))


async def test_credential_service(agent_service: AgentService, store: LocalAgentStore) -> None:
    svc = CredentialService(
        InMemoryCredentialRepository(), AesGcmSecretStore(b"k" * 32), store, FixedClock()
    )
    first = await svc.create(CredentialInput(name="one"), "s1", make_default=False)
    assert first.is_default  # first set is the default
    second = await svc.create(
        CredentialInput(name="two", provider=Provider.GATEWAY, base_url="https://gw.test"),
        "s2",
        make_default=False,
    )
    assert not second.is_default
    assert svc.reveal(second) == "s2"
    kept = await svc.update(second.id, CredentialInput(name="two", description="x"), None)
    assert svc.reveal(kept) == "s2" and kept.description == "x"
    with pytest.raises(InvalidArgumentError):
        await svc.create(
            CredentialInput(name="bad", provider=Provider.GATEWAY), "s", make_default=False
        )
    with pytest.raises(InvalidArgumentError):
        await svc.create(
            CredentialInput(name="bad", base_url="https://u:p@x.test"), "s", make_default=False
        )
    with pytest.raises(FailedPreconditionError, match="default"):
        await svc.delete(first.id)

    await agent_service.create("alpha", "Alpha", "", "blank")
    await store.write("alpha", "agent.json", b'{"name": "Alpha", "credential_set": "two"}')
    with pytest.raises(FailedPreconditionError, match="alpha"):
        await svc.delete(second.id)
    assert (await svc.resolve(name="two")).id == second.id
    assert (await svc.resolve()).id == first.id
    await store.write("alpha", "agent.json", b'{"name": "Alpha"}')
    await svc.delete(second.id)
    await svc.delete(first.id)  # last one may go
    with pytest.raises(FailedPreconditionError):
        await svc.resolve()
