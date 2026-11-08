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
