"""gRPC servicers: thin translation between protobuf and core services."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from grpc import aio

from playground_api import convert
from playground_api.guard import current_principal, current_token
from playground_core.errors import UnauthenticatedError
from playground_core.events import Event
from playground_core.services.agents import AgentService
from playground_core.services.auth import AuthService
from playground_core.services.conversations import ConversationService
from playground_core.services.credentials import CredentialService
from playground_core.services.files import FileService
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    credential_pb2,
    credential_pb2_grpc,
    event_pb2,
    event_pb2_grpc,
    file_pb2,
    file_pb2_grpc,
    run_pb2,
    run_pb2_grpc,
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


# -- Phase 2 ---------------------------------------------------------------------


class RunServicer(run_pb2_grpc.RunServiceServicer):
    def __init__(self, conversations: ConversationService) -> None:
        self._c = conversations

    async def StartRun(
        self, request: run_pb2.StartRunRequest, context: Ctx
    ) -> run_pb2.StartRunResponse:
        conv, run = await self._c.start_run(
            request.agent_slug, request.prompt, request.credential_set_id
        )
        return run_pb2.StartRunResponse(
            run=convert.run(run), conversation=convert.conversation(conv)
        )

    async def GetRun(self, request: run_pb2.GetRunRequest, context: Ctx) -> run_pb2.GetRunResponse:
        return run_pb2.GetRunResponse(run=convert.run(await self._c.get_run(request.run_id)))

    async def ListRuns(
        self, request: run_pb2.ListRunsRequest, context: Ctx
    ) -> run_pb2.ListRunsResponse:
        runs = await self._c.list_runs(
            agent_slug=request.agent_slug,
            conversation_id=request.conversation_id,
            limit=request.limit,
        )
        return run_pb2.ListRunsResponse(runs=[convert.run(r) for r in runs])

    async def CancelRun(
        self, request: run_pb2.CancelRunRequest, context: Ctx
    ) -> run_pb2.CancelRunResponse:
        return run_pb2.CancelRunResponse(run=convert.run(await self._c.cancel(request.run_id)))


class ConversationServicer(run_pb2_grpc.ConversationServiceServicer):
    def __init__(self, conversations: ConversationService) -> None:
        self._c = conversations

    async def CreateConversation(
        self, request: run_pb2.CreateConversationRequest, context: Ctx
    ) -> run_pb2.CreateConversationResponse:
        conv = await self._c.create(request.agent_slug, request.title, request.credential_set_id)
        return run_pb2.CreateConversationResponse(conversation=convert.conversation(conv))

    async def SendMessage(
        self, request: run_pb2.SendMessageRequest, context: Ctx
    ) -> run_pb2.SendMessageResponse:
        run = await self._c.send_message(
            request.conversation_id, request.text, request.credential_set_id
        )
        return run_pb2.SendMessageResponse(run=convert.run(run))

    async def ListConversations(
        self, request: run_pb2.ListConversationsRequest, context: Ctx
    ) -> run_pb2.ListConversationsResponse:
        convs = await self._c.list(request.agent_slug)
        return run_pb2.ListConversationsResponse(
            conversations=[convert.conversation(c) for c in convs]
        )

    async def GetConversation(
        self, request: run_pb2.GetConversationRequest, context: Ctx
    ) -> run_pb2.GetConversationResponse:
        d = await self._c.get(request.conversation_id)
        queue = await self._c.queued_runs(d.conversation.agent_slug)
        return run_pb2.GetConversationResponse(
            conversation=convert.conversation(d.conversation),
            runs=[convert.run(r) for r in d.runs],
            agent_queue=list(queue),
        )


_DONE = object()


class EventServicer(event_pb2_grpc.EventServiceServicer):
    def __init__(self, conversations: ConversationService, heartbeat_seconds: float) -> None:
        self._c = conversations
        self._heartbeat = heartbeat_seconds

    async def WatchEvents(
        self, request: event_pb2.WatchEventsRequest, context: Ctx
    ) -> AsyncIterator[event_pb2.WatchEventsResponse]:
        await self._c.require(request.conversation_id)
        # A pump task feeds a queue so idle periods can emit heartbeats
        # without cancelling the (blocking) log tail mid-read.
        queue: asyncio.Queue[Event | BaseException | object] = asyncio.Queue(maxsize=1000)

        async def pump() -> None:
            try:
                async for ev in self._c.watch(request.conversation_id, request.after_offset):
                    await queue.put(ev)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(exc)
            else:
                await queue.put(_DONE)

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), self._heartbeat)
                except TimeoutError:
                    yield event_pb2.WatchEventsResponse(event=convert.heartbeat())
                    continue
                if item is _DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, Event)
                yield event_pb2.WatchEventsResponse(event=convert.event(item))
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class CredentialServicer(credential_pb2_grpc.CredentialServiceServicer):
    def __init__(self, credentials: CredentialService) -> None:
        self._c = credentials

    async def ListCredentialSets(
        self, request: credential_pb2.ListCredentialSetsRequest, context: Ctx
    ) -> credential_pb2.ListCredentialSetsResponse:
        sets = await self._c.list()
        return credential_pb2.ListCredentialSetsResponse(
            credential_sets=[convert.credential_set(c) for c in sets]
        )

    async def CreateCredentialSet(
        self, request: credential_pb2.CreateCredentialSetRequest, context: Ctx
    ) -> credential_pb2.CreateCredentialSetResponse:
        cred = await self._c.create(
            convert.credential_input(request.input),
            request.secret,
            make_default=request.make_default,
        )
        return credential_pb2.CreateCredentialSetResponse(
            credential_set=convert.credential_set(cred)
        )

    async def UpdateCredentialSet(
        self, request: credential_pb2.UpdateCredentialSetRequest, context: Ctx
    ) -> credential_pb2.UpdateCredentialSetResponse:
        secret = request.secret if request.HasField("secret") else None
        cred = await self._c.update(request.id, convert.credential_input(request.input), secret)
        return credential_pb2.UpdateCredentialSetResponse(
            credential_set=convert.credential_set(cred)
        )

    async def DeleteCredentialSet(
        self, request: credential_pb2.DeleteCredentialSetRequest, context: Ctx
    ) -> credential_pb2.DeleteCredentialSetResponse:
        await self._c.delete(request.id)
        return credential_pb2.DeleteCredentialSetResponse()

    async def MakeDefault(
        self, request: credential_pb2.MakeDefaultRequest, context: Ctx
    ) -> credential_pb2.MakeDefaultResponse:
        await self._c.make_default(request.id)
        return credential_pb2.MakeDefaultResponse()
