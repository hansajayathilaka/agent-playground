"""Domain <-> protobuf conversion."""

from __future__ import annotations

from datetime import datetime

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp

from playground_core.events import Event
from playground_core.models import (
    AgentRecord,
    AuthType,
    Conversation,
    CredentialSet,
    Diagnostic,
    FileEntry,
    FileKind,
    Provider,
    Run,
    RunStatus,
    Severity,
    Template,
)
from playground_core.services.credentials import CredentialInput
from playground_proto.platform.v1 import (
    agent_pb2,
    common_pb2,
    credential_pb2,
    event_pb2,
    file_pb2,
    run_pb2,
)


def ts(value: datetime) -> Timestamp:
    out = Timestamp()
    out.FromDatetime(value)
    return out


def agent(record: AgentRecord) -> agent_pb2.Agent:
    return agent_pb2.Agent(
        slug=record.slug,
        name=record.name,
        description=record.description,
        template=record.template,
        created_at=ts(record.created_at),
        updated_at=ts(record.updated_at),
    )


def template(t: Template) -> agent_pb2.Template:
    return agent_pb2.Template(name=t.name, description=t.description)


_SEVERITY = {
    Severity.ERROR: common_pb2.Diagnostic.SEVERITY_ERROR,
    Severity.WARNING: common_pb2.Diagnostic.SEVERITY_WARNING,
}


def diagnostic(d: Diagnostic) -> common_pb2.Diagnostic:
    return common_pb2.Diagnostic(
        path=d.path, severity=_SEVERITY[d.severity], message=d.message, location=d.location
    )


_KIND = {
    FileKind.FILE: file_pb2.FileEntry.KIND_FILE,
    FileKind.DIRECTORY: file_pb2.FileEntry.KIND_DIRECTORY,
}


def file_entry(e: FileEntry) -> file_pb2.FileEntry:
    return file_pb2.FileEntry(
        path=e.path, kind=_KIND[e.kind], size=e.size, modified_at=ts(e.modified_at)
    )


# -- Phase 2 ---------------------------------------------------------------------

_RUN_STATUS = {
    RunStatus.QUEUED: run_pb2.RUN_STATUS_QUEUED,
    RunStatus.RUNNING: run_pb2.RUN_STATUS_RUNNING,
    RunStatus.SUCCEEDED: run_pb2.RUN_STATUS_SUCCEEDED,
    RunStatus.FAILED: run_pb2.RUN_STATUS_FAILED,
    RunStatus.CANCELLED: run_pb2.RUN_STATUS_CANCELLED,
    RunStatus.INTERRUPTED: run_pb2.RUN_STATUS_INTERRUPTED,
}


def run(r: Run) -> run_pb2.Run:
    u = r.usage
    out = run_pb2.Run(
        id=r.id,
        conversation_id=r.conversation_id,
        agent_slug=r.agent_slug,
        prompt=r.prompt,
        status=_RUN_STATUS[r.status],
        created_at=ts(r.created_at),
        error=r.error,
        credential_set_id=r.credential_set_id,
        usage=run_pb2.RunUsage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cost_usd=u.cost_usd,
            num_turns=u.num_turns,
            duration_ms=u.duration_ms,
        ),
    )
    if r.started_at:
        out.started_at.CopyFrom(ts(r.started_at))
    if r.finished_at:
        out.finished_at.CopyFrom(ts(r.finished_at))
    return out


def conversation(c: Conversation) -> run_pb2.Conversation:
    return run_pb2.Conversation(
        id=c.id,
        agent_slug=c.agent_slug,
        title=c.title,
        created_at=ts(c.created_at),
        updated_at=ts(c.updated_at),
        credential_set_id=c.credential_set_id,
    )


def event(e: Event) -> event_pb2.Event:
    """Each EventKind is the name of a field in the Event.body oneof and its
    data keys are that message's fields."""
    out = event_pb2.Event(
        offset=e.offset,
        conversation_id=e.conversation_id,
        run_id=e.run_id,
        time=ts(e.time),
        parent_tool_use_id=e.parent_tool_use_id,
    )
    body = getattr(out, e.kind.value)
    json_format.ParseDict(dict(e.data), body, ignore_unknown_fields=True)
    body.SetInParent()
    return out


def heartbeat() -> event_pb2.Event:
    out = event_pb2.Event()
    out.heartbeat.SetInParent()
    return out


_PROVIDER = {
    Provider.ANTHROPIC: credential_pb2.PROVIDER_ANTHROPIC,
    Provider.GATEWAY: credential_pb2.PROVIDER_GATEWAY,
    Provider.LITELLM: credential_pb2.PROVIDER_LITELLM,
    Provider.BEDROCK: credential_pb2.PROVIDER_BEDROCK,
    Provider.VERTEX: credential_pb2.PROVIDER_VERTEX,
}
_PROVIDER_IN = {v: k for k, v in _PROVIDER.items()}
_AUTH = {
    AuthType.API_KEY: credential_pb2.AUTH_TYPE_API_KEY,
    AuthType.BEARER: credential_pb2.AUTH_TYPE_BEARER,
}
_AUTH_IN = {v: k for k, v in _AUTH.items()}


def credential_set(c: CredentialSet) -> credential_pb2.CredentialSet:
    return credential_pb2.CredentialSet(
        id=c.id,
        name=c.name,
        description=c.description,
        provider=_PROVIDER[c.provider],
        base_url=c.base_url,
        auth_type=_AUTH[c.auth_type],
        headers=c.headers,
        default_model=c.default_model,
        allowed_models=c.allowed_models,
        is_default=c.is_default,
        has_secret=c.has_secret,
        created_at=ts(c.created_at),
        updated_at=ts(c.updated_at),
    )


def credential_input(i: credential_pb2.CredentialSetInput) -> CredentialInput:
    return CredentialInput(
        name=i.name,
        description=i.description,
        provider=_PROVIDER_IN.get(i.provider, Provider.ANTHROPIC),
        base_url=i.base_url.strip(),
        auth_type=_AUTH_IN.get(i.auth_type, AuthType.API_KEY),
        headers=dict(i.headers),
        default_model=i.default_model.strip(),
        allowed_models=[m.strip() for m in i.allowed_models if m.strip()],
    )
