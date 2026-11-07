"""The agent.json platform manifest."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_FILE = "agent.json"
SCHEMA_VERSION = 1


class ApprovalsMode(StrEnum):
    OFF = "off"
    RISKY = "risky"
    ALL = "all"


class Approvals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ApprovalsMode = ApprovalsMode.RISKY


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_turns: int | None = Field(default=None, ge=1, le=1000)
    max_budget_usd: float | None = Field(default=None, gt=0)


class Network(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Empty means open internet (decision 14).
    allowlist: list[str] = Field(default_factory=list[str])


class AgentManifest(BaseModel):
    """Schema for agent.json. Unknown keys are errors so typos surface early."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1, le=SCHEMA_VERSION)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    model: str | None = None
    credential_set: str | None = None
    tool_packs: list[str] = Field(default_factory=list[str])
    approvals: Approvals = Field(default_factory=Approvals)
    limits: Limits = Field(default_factory=Limits)
    network: Network = Field(default_factory=Network)
