"""Plain domain records shared by ports and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """A row in the agent index. The folder on disk is the source of content."""

    slug: str
    name: str
    description: str
    template: str
    created_at: datetime
    updated_at: datetime


class FileKind(Enum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str  # relative to the agent root, "/" separated
    kind: FileKind
    size: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class FileContent:
    entry: FileEntry
    etag: str
    data: bytes


@dataclass(frozen=True, slots=True)
class WriteResult:
    entry: FileEntry
    etag: str


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    severity: Severity
    message: str
    location: str = ""


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    description: str
    # Relative path -> file content. Text files may contain {{agent_name}}
    # and {{agent_description}} placeholders.
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])


@dataclass(frozen=True, slots=True)
class Principal:
    username: str


@dataclass(frozen=True, slots=True)
class AuthSession:
    token: str
    principal: Principal
    expires_at: datetime


# -- runs and conversations (Phase 2) -----------------------------------------


class RunStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class Conversation:
    """One SDK session. Each user message is one queued run (turn)."""

    id: str
    agent_slug: str
    title: str
    created_at: datetime
    updated_at: datetime
    # SDK session id once the first turn has run; used to resume.
    session_id: str = ""
    # Credential set override for every turn (empty: agent.json, then default).
    credential_set_id: str = ""


@dataclass(frozen=True, slots=True)
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    conversation_id: str
    agent_slug: str
    prompt: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    worker: str = ""
    error: str = ""
    credential_set_id: str = ""  # resolved set, never the secret
    usage: RunUsage = field(default_factory=RunUsage)


@dataclass(frozen=True, slots=True)
class Job:
    """What travels through the JobQueue: just enough to find the run."""

    run_id: str
    agent_slug: str


@dataclass(frozen=True, slots=True)
class Lease:
    """A claimed job. While a lease is held, the agent lock is held."""

    job: Job
    token: str
    # The previous holder of this agent's lock died with this job in flight.
    recovered: bool = False


class ControlKind(Enum):
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ControlMessage:
    run_id: str
    kind: ControlKind


# -- credentials ---------------------------------------------------------------


class Provider(Enum):
    ANTHROPIC = "anthropic"
    GATEWAY = "gateway"  # Anthropic-compatible base URL (for example DeepSeek)
    LITELLM = "litellm"
    BEDROCK = "bedrock"
    VERTEX = "vertex"


class AuthType(Enum):
    API_KEY = "api_key"  # ANTHROPIC_API_KEY
    BEARER = "bearer"  # ANTHROPIC_AUTH_TOKEN


@dataclass(frozen=True, slots=True)
class CredentialSet:
    """A named endpoint + secret. The secret is stored encrypted and never
    leaves the repository/worker boundary in plaintext."""

    id: str
    name: str
    description: str
    provider: Provider
    base_url: str
    auth_type: AuthType
    headers: dict[str, str]
    default_model: str
    allowed_models: list[str]
    is_default: bool
    created_at: datetime
    updated_at: datetime
    secret_ciphertext: bytes = b""
    owner: str = ""  # empty in v1 (global)

    @property
    def has_secret(self) -> bool:
        return bool(self.secret_ciphertext)
