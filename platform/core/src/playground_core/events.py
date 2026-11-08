"""The platform's versioned event envelope.

Clients never see SDK message shapes. Workers translate SDK output into these
events, the EventLog stores them per conversation, and the API maps each kind
one-to-one onto a field of the ``platform.v1.Event`` oneof (same names, same
payload keys), so adding a kind means adding it here and in event.proto.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

ENVELOPE_VERSION = 1


class EventKind(StrEnum):
    USER_MESSAGE = "user_message"  # {"text"}
    RUN_STATUS = "run_status"  # {"status", "error"}
    TEXT_DELTA = "text_delta"  # {"message_id", "index", "text"} partial tokens
    ASSISTANT_TEXT = "assistant_text"  # {"message_id", "index", "text"} final block
    THINKING = "thinking"  # {"message_id", "index", "text"}
    TOOL_CALL = "tool_call"  # {"tool_use_id", "name", "input_json"}
    TOOL_RESULT = "tool_result"  # {"tool_use_id", "content", "is_error"}
    SUBAGENT = "subagent"  # {"task_id", "tool_use_id", "description", "status", "summary"}
    RESULT = "result"  # {"is_error", "text", "num_turns", "cost_usd", ...}
    NOTICE = "notice"  # {"level", "message"}


@dataclass(frozen=True, slots=True)
class Event:
    conversation_id: str
    run_id: str
    kind: EventKind
    data: Mapping[str, Any]
    # Set when the event comes from a subagent (SDK Task/Agent tool); clients
    # nest it under that tool call.
    parent_tool_use_id: str = ""
    time: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Assigned by the EventLog on append; 0 before that. Strictly increasing
    # per conversation, starting at 1.
    offset: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": ENVELOPE_VERSION,
                "conversation_id": self.conversation_id,
                "run_id": self.run_id,
                "kind": self.kind.value,
                "data": dict(self.data),
                "parent_tool_use_id": self.parent_tool_use_id,
                "time": self.time.isoformat(),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes, offset: int) -> Event:
        d = json.loads(raw)
        return cls(
            conversation_id=d["conversation_id"],
            run_id=d["run_id"],
            kind=EventKind(d["kind"]),
            data=d["data"],
            parent_tool_use_id=d.get("parent_tool_use_id", ""),
            time=datetime.fromisoformat(d["time"]),
            offset=offset,
        )
