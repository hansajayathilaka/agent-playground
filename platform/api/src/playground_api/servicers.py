"""gRPC servicers: thin translation between protobuf and core services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from grpc import aio

from playground_api import convert
from playground_api.guard import current_principal, current_token
from playground_core.errors import UnauthenticatedError
from playground_core.services.agents import AgentService
from playground_core.services.auth import AuthService
from playground_core.services.files import FileService
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    file_pb2,
    file_pb2_grpc,
)

Ctx = aio.ServicerContext[Any, Any]

READ_CHUNK = 1024 * 1024


class AuthServicer(auth_pb2_grpc.AuthServiceServicer):
    def __init__(self, auth: AuthService) -> None:
        self._auth = auth

    async def Login(self, request: auth_pb2.LoginRequest, context: Ctx) -> auth_pb2.LoginResponse:
        session = await self._auth.login(request.username, request.password)
        return auth_pb2.LoginResponse(
            token=session.token, expires_at=convert.ts(session.expires_at)
        )

    async def Logout(
        self, request: auth_pb2.LogoutRequest, context: Ctx
    ) -> auth_pb2.LogoutResponse:
        await self._auth.logout(current_token.get())
        return auth_pb2.LogoutResponse()

    async def WhoAmI(
        self, request: auth_pb2.WhoAmIRequest, context: Ctx
    ) -> auth_pb2.WhoAmIResponse:
        principal = current_principal.get()
        if principal is None:
            raise UnauthenticatedError("not logged in")
        return auth_pb2.WhoAmIResponse(username=principal.username)


class AgentServicer(agent_pb2_grpc.AgentServiceServicer):
    def __init__(self, agents: AgentService) -> None:
        self._agents = agents

    async def ListAgents(
        self, request: agent_pb2.ListAgentsRequest, context: Ctx
    ) -> agent_pb2.ListAgentsResponse:
        records = await self._agents.list()
        return agent_pb2.ListAgentsResponse(agents=[convert.agent(r) for r in records])

    async def GetAgent(
        self, request: agent_pb2.GetAgentRequest, context: Ctx
    ) -> agent_pb2.GetAgentResponse:
        d = await self._agents.get(request.slug)
        return agent_pb2.GetAgentResponse(
            agent=convert.agent(d.record),
            manifest_json=d.manifest_json,
            diagnostics=[convert.diagnostic(x) for x in d.diagnostics],
        )

    async def CreateAgent(
        self, request: agent_pb2.CreateAgentRequest, context: Ctx
    ) -> agent_pb2.CreateAgentResponse:
        record = await self._agents.create(
            request.slug, request.name, request.description, request.template
        )
        return agent_pb2.CreateAgentResponse(agent=convert.agent(record))

    async def DeleteAgent(
        self, request: agent_pb2.DeleteAgentRequest, context: Ctx
    ) -> agent_pb2.DeleteAgentResponse:
        await self._agents.delete(request.slug)
        return agent_pb2.DeleteAgentResponse()

    async def ValidateAgent(
        self, request: agent_pb2.ValidateAgentRequest, context: Ctx
    ) -> agent_pb2.ValidateAgentResponse:
        report = await self._agents.validate(request.slug)
        return agent_pb2.ValidateAgentResponse(
            diagnostics=[convert.diagnostic(x) for x in report.diagnostics],
            runnable=report.runnable,
        )

    async def ListTemplates(
        self, request: agent_pb2.ListTemplatesRequest, context: Ctx
    ) -> agent_pb2.ListTemplatesResponse:
        templates = await self._agents.list_templates()
        return agent_pb2.ListTemplatesResponse(templates=[convert.template(t) for t in templates])


class FileServicer(file_pb2_grpc.FileServiceServicer):
    def __init__(self, files: FileService) -> None:
        self._files = files

    async def GetTree(
        self, request: file_pb2.GetTreeRequest, context: Ctx
    ) -> file_pb2.GetTreeResponse:
        entries = await self._files.tree(request.slug, include_hidden=request.include_hidden)
        return file_pb2.GetTreeResponse(entries=[convert.file_entry(e) for e in entries])

    async def ReadFile(
        self, request: file_pb2.ReadFileRequest, context: Ctx
    ) -> AsyncIterator[file_pb2.ReadFileResponse]:
        content = await self._files.read(request.slug, request.path)
        data = content.data
        yield file_pb2.ReadFileResponse(
            entry=convert.file_entry(content.entry), etag=content.etag, chunk=data[:READ_CHUNK]
        )
        for offset in range(READ_CHUNK, len(data), READ_CHUNK):
            yield file_pb2.ReadFileResponse(chunk=data[offset : offset + READ_CHUNK])

    async def WriteFile(
        self, request: file_pb2.WriteFileRequest, context: Ctx
    ) -> file_pb2.WriteFileResponse:
        out = await self._files.write(
            request.slug,
            request.path,
            request.content,
            if_match=request.if_match,
            create_only=request.create_only,
        )
        return file_pb2.WriteFileResponse(
            entry=convert.file_entry(out.result.entry),
            etag=out.result.etag,
            diagnostics=[convert.diagnostic(d) for d in out.diagnostics],
        )

    async def DeleteFile(
        self, request: file_pb2.DeleteFileRequest, context: Ctx
    ) -> file_pb2.DeleteFileResponse:
        await self._files.delete(request.slug, request.path, recursive=request.recursive)
        return file_pb2.DeleteFileResponse()

    async def MoveFile(
        self, request: file_pb2.MoveFileRequest, context: Ctx
    ) -> file_pb2.MoveFileResponse:
        entry = await self._files.move(request.slug, request.from_path, request.to_path)
        return file_pb2.MoveFileResponse(entry=convert.file_entry(entry))

    async def CreateDirectory(
        self, request: file_pb2.CreateDirectoryRequest, context: Ctx
    ) -> file_pb2.CreateDirectoryResponse:
        entry = await self._files.mkdir(request.slug, request.path)
        return file_pb2.CreateDirectoryResponse(entry=convert.file_entry(entry))
