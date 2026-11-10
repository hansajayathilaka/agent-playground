"""Tests for plugins/playground/scripts/pg.py.

    python -m unittest discover -s tests -v

Unit tests run a fake `claude` that prints canned stream-json. The end-to-end
test runs the real Claude Code CLI against tests/fake_model_api.py (no key or
subscription needed) and is skipped when `claude` or `uv` is missing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "playground" / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import pg  # noqa: E402

FAKE_CLAUDE = """#!{python}
import json, os, sys
args = sys.argv[1:]
task = args[args.index("-p") + 1]
with open(os.path.join(os.getcwd(), "argv.json"), "w") as f:
    json.dump(args, f)
def emit(e): print(json.dumps(e), flush=True)
emit({{"type": "system", "subtype": "init", "session_id": "sess-1", "cwd": os.getcwd()}})
emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Write", "input": {{"file_path": "workspace/out.txt"}}}}]}}}})
if task == "fail":
    print("boom", file=sys.stderr)
    emit({{"type": "result", "subtype": "error", "is_error": True, "result": "it broke", "session_id": "sess-1"}})
    sys.exit(1)
emit({{"type": "result", "subtype": "success", "is_error": False, "result": "did: " + task,
       "session_id": "sess-1", "total_cost_usd": 0.01, "num_turns": 2}})
"""


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pg-"))
        self.root = self.tmp / "ws"
        shutil.copytree(REPO / "templates", self.root / "templates")
        shutil.copytree(REPO / "catalog", self.root / "catalog")
        shutil.copy(REPO / "playground.json", self.root / "playground.json")
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = {"PLAYGROUND_ROOT": str(self.root), "PLAYGROUND_CLAUDE": str(fake)}
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self) -> None:
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class AgentTests(Workspace):
    def test_new_list_check(self) -> None:
        folder = pg.new_agent(self.root, "writer", "Writes blog posts")
        self.assertTrue((folder / "CLAUDE.md").read_text().startswith("# writer"))
        self.assertEqual(json.loads((folder / "agent.json").read_text())["description"], "Writes blog posts")
        skill = folder / ".claude" / "skills" / "draft" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: draft\ndescription: Draft a post\n---\nsteps")
        [agent] = pg.list_agents(self.root)
        self.assertEqual(agent["name"], "writer")
        self.assertEqual(agent["skills"], ["draft"])
        self.assertEqual(pg.check_agent(self.root, "writer"), [])

    def test_check_reports_problems(self) -> None:
        folder = pg.new_agent(self.root, "broken", "x")
        (folder / ".mcp.json").write_text('{"mcpServers": {"bad": {}}}')
        sub = folder / ".claude" / "agents" / "helper.md"
        sub.write_text("no frontmatter")
        problems = pg.check_agent(self.root, "broken")
        self.assertTrue(any("bad" in p for p in problems))
        self.assertTrue(any("helper" in p for p in problems))

    def test_names_are_checked(self) -> None:
        for bad in ("../etc", "Upper", "a/b", "templates", ""):
            with self.assertRaises(pg.PgError):
                pg.agent_path(self.root, bad)

    def test_duplicate_agent_is_refused(self) -> None:
        pg.new_agent(self.root, "dup", "x")
        with self.assertRaises(pg.PgError):
            pg.new_agent(self.root, "dup", "x")

    def test_add_mcp_uses_relative_root(self) -> None:
        pg.new_agent(self.root, "media", "x")
        server = pg.add_mcp(self.root, "media", "media-tools")
        self.assertIn("../../catalog/tools/media/server.py", server["args"])
        mcp = json.loads((self.root / "agents" / "media" / ".mcp.json").read_text())
        self.assertIn("media-tools", mcp["mcpServers"])
        with self.assertRaises(pg.PgError):
            pg.add_mcp(self.root, "media", "no-such-entry")

    def test_find_root_walks_up(self) -> None:
        os.environ.pop("PLAYGROUND_ROOT")
        deep = self.root / "agents" / "x" / "workspace"
        deep.mkdir(parents=True)
        self.assertEqual(pg.find_root(deep), self.root.resolve())
        with self.assertRaises(pg.PgError):
            pg.find_root(self.tmp)


class RunTests(Workspace):
    def test_run_records_result(self) -> None:
        pg.new_agent(self.root, "worker", "x")
        run = pg.run_agent(self.root, "worker", "hello")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["session_id"], "sess-1")
        self.assertEqual(run["cost_usd"], 0.01)
        rdir = self.root / ".playground" / "runs" / run["id"]
        self.assertEqual((rdir / "result.md").read_text(), "did: hello")
        self.assertEqual(len((rdir / "events.jsonl").read_text().splitlines()), 3)
        argv = json.loads((self.root / "agents" / "worker" / "argv.json").read_text())
        self.assertIn("acceptEdits", argv)
        self.assertNotIn("--mcp-config", argv)  # the template has no servers

    def test_run_passes_agent_settings(self) -> None:
        folder = pg.new_agent(self.root, "worker", "x")
        meta = json.loads((folder / "agent.json").read_text())
        meta.update(model="sonnet", workdirs=["/data/in"], permission_mode="bypassPermissions")
        (folder / "agent.json").write_text(json.dumps(meta))
        pg.add_mcp(self.root, "worker", "fetch")
        pg.run_agent(self.root, "worker", "hello")
        argv = json.loads((folder / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(Path("/data/in")))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(folder / ".mcp.json"))

    def test_failed_run(self) -> None:
        pg.new_agent(self.root, "worker", "x")
        run = pg.run_agent(self.root, "worker", "fail")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "boom")

    def test_resume_uses_session(self) -> None:
        folder = pg.new_agent(self.root, "worker", "x")
        first = pg.run_agent(self.root, "worker", "one")
        pg.run_agent(self.root, "worker", "two", resume=first["id"])
        argv = json.loads((folder / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        pg.new_agent(self.root, "other", "x")
        with self.assertRaises(pg.PgError):
            pg.run_agent(self.root, "other", "three", resume=first["id"])

    def test_background_run_and_listing(self) -> None:
        pg.new_agent(self.root, "worker", "x")
        run_id = pg.start_background(self.root, "worker", "bg task", None)
        deadline = time.time() + 30
        while time.time() < deadline:
            runs = pg.list_runs(self.root)
            if runs and runs[0]["status"] == "done":
                break
            time.sleep(0.2)
        self.assertEqual(runs[0]["id"], run_id)
        self.assertEqual(runs[0]["status"], "done")
        code, out, _ = quiet(pg.main, ["runs"])
        self.assertEqual(code, 0)
        self.assertIn(run_id, out)

    def test_run_refuses_broken_agent(self) -> None:
        folder = pg.new_agent(self.root, "worker", "x")
        (folder / "CLAUDE.md").unlink()
        with self.assertRaises(pg.PgError):
            pg.run_agent(self.root, "worker", "hello")


@unittest.skipUnless(shutil.which("claude") and shutil.which("uv"), "needs the claude CLI and uv")
class EndToEnd(Workspace):
    """The real Claude Code CLI runs the example agent and calls its MCP server."""

    def test_media_editor_resizes_through_mcp(self) -> None:
        from fake_model_api import FakeModelAPI

        shutil.copytree(REPO / "agents" / "media-editor", self.root / "agents" / "media-editor",
                        ignore=shutil.ignore_patterns("argv.json"))
        folder = self.root / "agents" / "media-editor"
        subprocess.run(["uv", "run", "--with", "pillow", "python", "-c",
                        "from PIL import Image; import sys; Image.new('RGB', (320, 200)).save(sys.argv[1])",
                        str(folder / "workspace" / "in" / "e2e.png")], check=True, capture_output=True)
        config = self.tmp / "claude-config"
        config.mkdir()
        # Project settings (allow rules) only apply in a trusted workspace.
        (config / ".claude.json").write_text(json.dumps(
            {"projects": {str(self.root.resolve()): {"hasTrustDialogAccepted": True},
                          str(folder.resolve()): {"hasTrustDialogAccepted": True}}}))
        with FakeModelAPI() as api:
            os.environ.pop("PLAYGROUND_CLAUDE")
            env = {"ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": "sk-fake", "CLAUDE_CONFIG_DIR": str(config)}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                task = ('call-tool: mcp__media-tools__image_resize '
                        '{"path": "workspace/in/e2e.png", "width": 160, "output": "workspace/out/e2e-small.png"}')
                run = pg.run_agent(self.root, "media-editor", task)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        self.assertEqual(run["status"], "done", run)
        self.assertTrue((folder / "workspace" / "out" / "e2e-small.png").is_file())


if __name__ == "__main__":
    unittest.main()
