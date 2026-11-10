"""A tiny stand-in for the Anthropic Messages API, enough for the real Claude
CLI to complete a turn. Every reply is "pong: <last user text>". Trigger
words in the user text first ask for one tool call (see TOOL_TRIGGERS),
"run-bash: <command>" asks for one Bash call running <command>, and "slow"
streams the reply very slowly so a cancel lands mid-stream.

A first user message starting with "script: " followed by a JSON list of
``[tool_name, input]`` pairs plays those tool calls in order, one per model
turn (the step is the number of tool results so far), then replies
"script done"."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _last_user_text(body: dict[str, Any]) -> str:
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        texts = [
            c.get("text", "")
            for c in content
            if c.get("type") == "text" and not c.get("text", "").startswith("<system-reminder>")
        ]
        if texts:
            return texts[-1]
        if any(c.get("type") == "tool_result" for c in content):
            return "<tool_result>"
    return ""


TOOL_TRIGGERS: dict[str, tuple[str, dict[str, Any]]] = {
    "use-tool": ("Write", {"file_path": "workspace/out.txt", "content": "written"}),
    "use-escape": ("Write", {"file_path": "../escaped.txt", "content": "x"}),
    "use-bash": ("Bash", {"command": "env > workspace/env.txt", "description": "dump env"}),
}
BASH_PREFIX = "run-bash: "


SCRIPT_PREFIX = "script: "


def _first_user_text(body: dict[str, Any]) -> str:
    for msg in body.get("messages", []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        for c in content:
            text = c.get("text", "") if c.get("type") == "text" else ""
            if text and not text.startswith("<system-reminder>"):
                return text
    return ""


def _script_step(body: dict[str, Any]) -> tuple[str, dict[str, Any]] | str | None:
    """The next scripted tool call, "done" when the script is over, or None
    when this conversation is not scripted."""
    first = _first_user_text(body)
    if not first.startswith(SCRIPT_PREFIX):
        return None
    steps: list[list[Any]] = json.loads(first[len(SCRIPT_PREFIX) :])
    done = sum(
        1
        for msg in body.get("messages", [])
        if msg.get("role") == "user" and isinstance(msg.get("content"), list)
        for c in msg["content"]
        if c.get("type") == "tool_result"
    )
    if done >= len(steps):
        return "done"
    name, tool_input = steps[done]
    return str(name), dict(tool_input)


def _tool_request(text: str) -> tuple[str, dict[str, Any]] | None:
    if text.startswith(BASH_PREFIX):
        command = text[len(BASH_PREFIX) :]
        return "Bash", {"command": command, "description": "test command", "timeout": 60000}
    trigger = next((t for t in TOOL_TRIGGERS if t in text), None)
    return TOOL_TRIGGERS[trigger] if trigger else None


class FakeModelAPI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.headers: list[dict[str, str]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_HEAD(self) -> None:
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body: dict[str, Any] = json.loads(raw)
                except ValueError:
                    body = {}
                if not self.path.startswith("/v1/messages") or self.path.startswith(
                    "/v1/messages/count_tokens"
                ):
                    payload = json.dumps({"input_tokens": 10}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                api.requests.append(body)
                api.headers.append({k.lower(): v for k, v in self.headers.items()})
                api.respond(self, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeModelAPI:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True
        self._server.shutdown()
        self._server.server_close()

    def agent_requests(self) -> list[dict[str, Any]]:
        """Main-loop requests (they carry tools); skips side calls such as titles."""
        return [r for r in self.requests if r.get("tools")]

    def respond(self, h: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        text = _last_user_text(body)
        n = len(self.requests)
        tool = _tool_request(text)
        scripted = _script_step(body) if body.get("tools") else None
        if scripted == "done":
            tool, text = None, "script"
        elif isinstance(scripted, tuple):
            tool = scripted
        blocks: list[dict[str, Any]]
        if tool and body.get("tools"):
            name, tool_input = tool
            blocks = [{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": tool_input}]
            stop = "tool_use"
        else:
            reply = "done" if text == "<tool_result>" else f"pong: {text}"
            if scripted == "done":
                reply = "script done"
            blocks = [{"type": "text", "text": reply}]
            stop = "end_turn"
        message: dict[str, Any] = {
            "id": f"msg_{n}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-test"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
        if not body.get("stream"):
            payload = json.dumps({**message, "content": blocks, "stop_reason": stop}).encode()
            h.send_response(200)
            h.send_header("content-type", "application/json")
            h.send_header("content-length", str(len(payload)))
            h.end_headers()
            h.wfile.write(payload)
            return
        h.send_response(200)
        h.send_header("content-type", "text/event-stream")
        h.send_header("cache-control", "no-cache")
        h.end_headers()

        def send(event: str, data: dict[str, Any]) -> None:
            h.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            h.wfile.flush()

        send("message_start", {"type": "message_start", "message": message})
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                send(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                for piece in (block["text"][:4], block["text"][4:]):
                    if "slow" in text:
                        time.sleep(0.2)
                        if self.closed:
                            return
                        for _ in range(100):
                            send(
                                "ping",
                                {"type": "ping"},
                            )
                            time.sleep(0.1)
                            if self.closed:
                                return
                    send(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "text_delta", "text": piece},
                        },
                    )
            else:
                start: dict[str, Any] = {**block, "input": {}}
                send(
                    "content_block_start",
                    {"type": "content_block_start", "index": i, "content_block": start},
                )
                send(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block["input"]),
                        },
                    },
                )
            send("content_block_stop", {"type": "content_block_stop", "index": i})
        send(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        )
        send("message_stop", {"type": "message_stop"})
