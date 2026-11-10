#!/usr/bin/env python3
"""Agent Playground helper CLI (stdlib only, Python 3.10+).

Every agent is a plain Claude Code folder under the workspace's agents dir.
This script does the bookkeeping Claude Code itself doesn't: listing agents,
copying the template, wiring catalog MCP servers, and running an agent
headless (`claude -p`) in its own folder while recording the run under
`.playground/runs/<id>/` so the HUD mod and `status` can show it.

    python pg.py list [--json]
    python pg.py new <name> --description "..."
    python pg.py check <name>
    python pg.py catalog [--json]
    python pg.py add-mcp <agent> <catalog-entry>
    python pg.py run <agent> "<task>" [--background] [--resume <run-id>]
    python pg.py runs [--json] [--limit N]
    python pg.py show <run-id>

The workspace root is the nearest folder above the current directory that
holds `playground.json`, or `$PLAYGROUND_ROOT`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

MARKER = "playground.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
DEFAULTS: dict[str, Any] = {
    "agents_dir": "agents",
    "runs_dir": ".playground/runs",
    "catalog_dir": "catalog",
    "template_dir": "templates/agent",
}
RESERVED = {"templates", "_template"}


class PgError(Exception):
    pass


# ---------------------------------------------------------------- workspace


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("PLAYGROUND_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if (root / MARKER).is_file():
            return root
        raise PgError(f"PLAYGROUND_ROOT={env} has no {MARKER}")
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / MARKER).is_file():
            return folder
    raise PgError(f"no {MARKER} found above {here}; run inside an Agent Playground workspace")


def config(root: Path) -> dict[str, Any]:
    data = read_json(root / MARKER, {})
    return {**DEFAULTS, **data}


def agents_dir(root: Path) -> Path:
    return root / config(root)["agents_dir"]


def runs_dir(root: Path) -> Path:
    return root / config(root)["runs_dir"]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def agent_path(root: Path, name: str) -> Path:
    """The folder of agent `name`; refuses anything that isn't a plain name."""
    if not NAME_RE.match(name) or name in RESERVED:
        raise PgError(f"bad agent name {name!r}: use lowercase letters, digits and dashes")
    return agents_dir(root) / name


# ------------------------------------------------------------------- agents


def frontmatter(path: Path) -> dict[str, str]:
    """name/description from a SKILL.md or subagent .md frontmatter (flat keys only)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def describe_agent(folder: Path) -> dict[str, Any]:
    meta = read_json(folder / "agent.json", {}) or {}
    claude = folder / ".claude"
    skills = sorted(p.parent.name for p in claude.glob("skills/*/SKILL.md"))
    subagents = sorted(p.stem for p in claude.glob("agents/*.md"))
    commands = sorted(p.stem for p in claude.glob("commands/*.md"))
    mcp = read_json(folder / ".mcp.json", {}) or {}
    tools_dir = folder / "tools"
    tools = sorted(
        p.name for p in tools_dir.iterdir() if not p.name.startswith(".")
    ) if tools_dir.is_dir() else []
    return {
        "name": folder.name,
        "path": str(folder),
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        "status": meta.get("status", "active"),
        "workdirs": meta.get("workdirs", []),
        "connectors": meta.get("connectors", []),
        "skills": skills,
        "subagents": subagents,
        "commands": commands,
        "mcp_servers": sorted((mcp.get("mcpServers") or {}).keys()),
        "tools": tools,
    }


def list_agents(root: Path) -> list[dict[str, Any]]:
    base = agents_dir(root)
    if not base.is_dir():
        return []
    return [
        describe_agent(p)
        for p in sorted(base.iterdir())
        if p.is_dir() and (p / "CLAUDE.md").is_file() and p.name not in RESERVED
    ]


def new_agent(root: Path, name: str, description: str) -> Path:
    dest = agent_path(root, name)
    if dest.exists():
        raise PgError(f"agent {name} already exists at {dest}")
    template = root / config(root)["template_dir"]
    if not template.is_dir():
        raise PgError(f"template folder {template} is missing")
    shutil.copytree(template, dest)
    for path in dest.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".json", ".txt", ".py"}:
            text = path.read_text(encoding="utf-8")
            text = text.replace("{{name}}", name).replace("{{description}}", description)
            path.write_text(text, encoding="utf-8")
    return dest


def check_agent(root: Path, name: str) -> list[str]:
    """Problems with an agent folder; empty when it looks runnable."""
    folder = agent_path(root, name)
    problems: list[str] = []
    if not folder.is_dir():
        return [f"{folder} does not exist"]
    if not (folder / "CLAUDE.md").is_file():
        problems.append("CLAUDE.md is missing")
    for rel in ("agent.json", ".mcp.json", ".claude/settings.json"):
        path = folder / rel
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                problems.append(f"{rel} is not valid JSON: {exc}")
    meta = read_json(folder / "agent.json", {}) or {}
    if not meta.get("description"):
        problems.append("agent.json has no description")
    mode = meta.get("permission_mode")
    if mode and mode not in {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}:
        problems.append(f"agent.json permission_mode {mode!r} is unknown")
    try:
        servers = (read_json(folder / ".mcp.json", {}) or {}).get("mcpServers") or {}
    except json.JSONDecodeError:
        servers = {}
    for key, server in servers.items():
        if not isinstance(server, dict) or not (server.get("command") or server.get("url")):
            problems.append(f".mcp.json server {key!r} needs a command or a url")
    for skill in (folder / ".claude").glob("skills/*/SKILL.md"):
        fm = frontmatter(skill)
        if not fm.get("description"):
            problems.append(f"skill {skill.parent.name} has no description in its frontmatter")
    for sub in (folder / ".claude").glob("agents/*.md"):
        fm = frontmatter(sub)
        if not fm.get("name") or not fm.get("description"):
            problems.append(f"subagent {sub.stem} needs name and description in its frontmatter")
    return problems


# ------------------------------------------------------------------ catalog


def list_catalog(root: Path) -> list[dict[str, Any]]:
    base = root / config(root)["catalog_dir"] / "mcp"
    out = []
    for path in sorted(base.glob("*.json")):
        entry = read_json(path, {}) or {}
        entry.setdefault("name", path.stem)
        out.append(entry)
    return out


def add_mcp(root: Path, agent: str, entry_name: str) -> dict[str, Any]:
    folder = agent_path(root, agent)
    if not folder.is_dir():
        raise PgError(f"agent {agent} does not exist")
    entry = next((e for e in list_catalog(root) if e["name"] == entry_name), None)
    if entry is None:
        raise PgError(f"catalog has no MCP entry {entry_name!r}")
    # ${PLAYGROUND_ROOT} becomes a path relative to the agent folder (where Claude Code
    # starts stdio servers), so the agent keeps working when the repo is cloned elsewhere.
    rel = os.path.relpath(root, folder).replace("\\", "/")
    server = json.loads(json.dumps(entry["server"]).replace("${PLAYGROUND_ROOT}", rel))
    mcp_path = folder / ".mcp.json"
    mcp = read_json(mcp_path, {}) or {}
    mcp.setdefault("mcpServers", {})[entry.get("key", entry_name)] = server
    write_json(mcp_path, mcp)
    return server


# --------------------------------------------------------------------- runs


def claude_argv(folder: Path, meta: dict[str, Any], task: str, resume_session: str | None) -> list[str]:
    exe = os.environ.get("PLAYGROUND_CLAUDE", "claude")
    argv = [exe, "-p", task, "--output-format", "stream-json", "--verbose"]
    # Project .mcp.json servers wait for a one-time approval in an interactive
    # session; a headless run can't give it, so pass the agent's servers explicitly.
    mcp = read_json(folder / ".mcp.json", {}) or {}
    if mcp.get("mcpServers"):
        argv += ["--mcp-config", str(folder / ".mcp.json")]
    argv += ["--permission-mode", meta.get("permission_mode") or "acceptEdits"]
    if meta.get("model"):
        argv += ["--model", meta["model"]]
    if meta.get("max_turns"):
        argv += ["--max-turns", str(meta["max_turns"])]
    for extra in meta.get("workdirs", []):
        argv += ["--add-dir", str(Path(extra).expanduser())]
    if resume_session:
        argv += ["--resume", resume_session]
    return argv


def summarize_tool(block: dict[str, Any]) -> str:
    name = block.get("name", "tool")
    args = block.get("input") or {}
    hint = args.get("description") or args.get("file_path") or args.get("pattern") or args.get("url") or ""
    return f"{name} {hint}".strip()[:120]


def run_agent(root: Path, agent: str, task: str, run_id: str | None = None,
              resume: str | None = None) -> dict[str, Any]:
    folder = agent_path(root, agent)
    problems = check_agent(root, agent)
    if problems:
        raise PgError(f"agent {agent} has problems: " + "; ".join(problems))
    meta_agent = read_json(folder / "agent.json", {}) or {}
    resume_session = None
    if resume:
        previous = read_json(runs_dir(root) / resume / "run.json")
        if not previous or not previous.get("session_id"):
            raise PgError(f"run {resume} has no session to resume")
        if previous.get("status") in {"running", "queued"}:
            raise PgError(f"run {resume} is still {previous['status']}; wait for it to finish")
        if previous.get("agent") != agent:
            raise PgError(f"run {resume} belongs to agent {previous.get('agent')}")
        resume_session = previous["session_id"]

    run_id = run_id or new_run_id(agent)
    rdir = runs_dir(root) / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    run: dict[str, Any] = {
        "id": run_id, "agent": agent, "task": task, "status": "running",
        "started_at": now(), "updated_at": now(), "pid": os.getpid(),
        "activity": "starting", "turns": 0, "resumed_from": resume,
    }
    write_json(rdir / "run.json", run)

    argv = claude_argv(folder, meta_agent, task, resume_session)
    env = {**os.environ, "PLAYGROUND_ROOT": str(root), "PLAYGROUND_RUN_ID": run_id}
    err_path = rdir / "stderr.log"
    err_file = err_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(argv, cwd=folder, env=env, stdout=subprocess.PIPE,
                                stderr=err_file, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        err_file.close()
        run.update(status="failed", error=f"{argv[0]} not found on PATH", updated_at=now())
        write_json(rdir / "run.json", run)
        raise PgError(run["error"]) from None

    result_text = ""
    with (rdir / "events.jsonl").open("w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            changed = False
            if kind == "system" and event.get("session_id"):
                run["session_id"] = event["session_id"]
                changed = True
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        run["activity"] = summarize_tool(block)
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        run["activity"] = block["text"].strip().splitlines()[0][:120]
                run["turns"] = run.get("turns", 0) + 1
                changed = True
            elif kind == "result":
                result_text = event.get("result") or ""
                run["session_id"] = event.get("session_id", run.get("session_id"))
                run["cost_usd"] = event.get("total_cost_usd")
                run["num_turns"] = event.get("num_turns")
                run["is_error"] = bool(event.get("is_error"))
                changed = True
            if changed:
                run["updated_at"] = now()
                write_json(rdir / "run.json", run)
        proc.stdout.close()
    code = proc.wait()
    err_file.close()
    stderr = err_path.read_text(encoding="utf-8", errors="replace")
    if not stderr.strip():
        err_path.unlink()
    (rdir / "result.md").write_text(result_text, encoding="utf-8")
    failed = code != 0 or run.get("is_error")
    run.update(status="failed" if failed else "done", exit_code=code,
               finished_at=now(), updated_at=now(), activity="")
    if failed:
        run["error"] = (stderr.strip().splitlines() or [result_text[:200] or f"exit {code}"])[-1][:300]
    write_json(rdir / "run.json", run)
    return run


def new_run_id(agent: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{agent}-{uuid.uuid4().hex[:4]}"


def start_background(root: Path, agent: str, task: str, resume: str | None) -> str:
    """Starts `run` in a detached child so it outlives the calling session."""
    agent_path(root, agent)
    run_id = new_run_id(agent)
    argv = [sys.executable, str(Path(__file__).resolve()), "run", agent, task, "--id", run_id]
    if resume:
        argv += ["--resume", resume]
    rdir = runs_dir(root) / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    write_json(rdir / "run.json", {"id": run_id, "agent": agent, "task": task, "status": "queued",
                                   "started_at": now(), "updated_at": now(), "activity": "starting"})
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with (rdir / "runner.log").open("w", encoding="utf-8") as out:
        subprocess.Popen(argv, cwd=root, stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env={**os.environ, "PLAYGROUND_ROOT": str(root)}, **kwargs)
    return run_id


def pid_alive(pid: int) -> bool | None:
    if os.name == "nt":
        return None  # os.kill(pid, 0) would terminate the process on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_runs(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    base = runs_dir(root)
    if not base.is_dir():
        return []
    runs = []
    for rdir in sorted(base.iterdir(), key=lambda p: p.name, reverse=True)[:limit]:
        run = read_json(rdir / "run.json")
        if not run:
            continue
        if run.get("status") == "running" and run.get("pid") and pid_alive(run["pid"]) is False:
            run["status"] = "lost"
        runs.append(run)
    return runs


# ---------------------------------------------------------------------- cli


def table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pg", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("root")
    p = sub.add_parser("list"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("new"); p.add_argument("name"); p.add_argument("--description", required=True)
    p = sub.add_parser("check"); p.add_argument("name")
    p = sub.add_parser("catalog"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("add-mcp"); p.add_argument("agent"); p.add_argument("entry")
    p = sub.add_parser("run"); p.add_argument("agent"); p.add_argument("task")
    p.add_argument("--background", action="store_true"); p.add_argument("--resume")
    p.add_argument("--id", help=argparse.SUPPRESS)
    p = sub.add_parser("runs"); p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show"); p.add_argument("run_id")
    args = parser.parse_args(argv)

    try:
        root = find_root()
        if args.cmd == "root":
            print(root)
        elif args.cmd == "list":
            agents = list_agents(root)
            if args.json:
                print(json.dumps(agents, indent=2))
            elif not agents:
                print("No agents yet. Build one with /playground:build-agent.")
            else:
                for a in agents:
                    print(f"{a['name']}: {a['description']}")
                    parts = [f"skills: {', '.join(a['skills']) or '-'}",
                             f"subagents: {', '.join(a['subagents']) or '-'}",
                             f"mcp: {', '.join(a['mcp_servers']) or '-'}",
                             f"tools: {', '.join(a['tools']) or '-'}"]
                    if a["connectors"]:
                        parts.append(f"connectors: {', '.join(a['connectors'])}")
                    print("  " + " | ".join(parts))
        elif args.cmd == "new":
            print(new_agent(root, args.name, args.description))
        elif args.cmd == "check":
            problems = check_agent(root, args.name)
            print("\n".join(problems) if problems else f"{args.name}: ok")
            return 1 if problems else 0
        elif args.cmd == "catalog":
            entries = list_catalog(root)
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                print(table([[e["name"], e.get("description", "")] for e in entries]))
        elif args.cmd == "add-mcp":
            print(json.dumps(add_mcp(root, args.agent, args.entry), indent=2))
        elif args.cmd == "run":
            if args.background:
                run_id = start_background(root, args.agent, args.task, args.resume)
                print(f"started run {run_id}; follow it with: pg.py show {run_id}")
            else:
                run = run_agent(root, args.agent, args.task, args.id, args.resume)
                print((runs_dir(root) / run["id"] / "result.md").read_text(encoding="utf-8"))
                print(f"\n[run {run['id']}: {run['status']}]")
                return 0 if run["status"] == "done" else 1
        elif args.cmd == "runs":
            runs = list_runs(root, args.limit)
            if args.json:
                print(json.dumps(runs, indent=2))
            elif not runs:
                print("No runs yet.")
            else:
                print(table([[r["id"], r["status"], r.get("activity") or "", r["task"][:60]] for r in runs]))
        elif args.cmd == "show":
            rdir = runs_dir(root) / Path(args.run_id).name
            run = read_json(rdir / "run.json")
            if not run:
                raise PgError(f"no run {args.run_id}")
            print(json.dumps(run, indent=2))
            result = rdir / "result.md"
            if result.is_file() and result.read_text(encoding="utf-8").strip():
                print("\n--- result ---\n" + result.read_text(encoding="utf-8"))
    except PgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
