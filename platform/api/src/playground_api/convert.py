"""Domain <-> protobuf conversion."""

from __future__ import annotations

from datetime import datetime

from google.protobuf.timestamp_pb2 import Timestamp

from playground_core.models import AgentRecord, Diagnostic, FileEntry, FileKind, Severity, Template
from playground_proto.platform.v1 import agent_pb2, common_pb2, file_pb2


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
