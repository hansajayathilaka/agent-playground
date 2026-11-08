"""One interceptor for every RPC: bearer-token auth and domain error mapping.

Wrapping the handlers here (rather than in each servicer) means a new RPC is
authenticated by default; only PUBLIC_METHODS skip the token check.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any, cast

import grpc
from grpc import aio

from playground_core import errors
from playground_core.models import Principal
from playground_core.services.auth import AuthService

log = logging.getLogger(__name__)

PUBLIC_METHODS = frozenset({"/platform.v1.AuthService/Login"})
PUBLIC_PREFIXES = ("/grpc.health.v1.", "/grpc.reflection.")

current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
current_token: ContextVar[str] = ContextVar("current_token", default="")

_STATUS: list[tuple[type[errors.DomainError], grpc.StatusCode]] = [
    (errors.UnauthenticatedError, grpc.StatusCode.UNAUTHENTICATED),
    (errors.NotFoundError, grpc.StatusCode.NOT_FOUND),
    (errors.AlreadyExistsError, grpc.StatusCode.ALREADY_EXISTS),
    (errors.ConflictError, grpc.StatusCode.ABORTED),
    (errors.PermissionDeniedError, grpc.StatusCode.PERMISSION_DENIED),
    (errors.InvalidArgumentError, grpc.StatusCode.INVALID_ARGUMENT),
]


def status_for(exc: errors.DomainError) -> grpc.StatusCode:
    for cls, code in _STATUS:
        if isinstance(exc, cls):
            return code
    return grpc.StatusCode.FAILED_PRECONDITION


def bearer_token(context: aio.ServicerContext[Any, Any]) -> str:
    for key, value in context.invocation_metadata() or ():
        if key == "authorization" and isinstance(value, str):
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer":
                return token.strip()
    return ""


Ctx = aio.ServicerContext[Any, Any]
Unary = Callable[[Any, Ctx], Awaitable[Any]]
Stream = Callable[[Any, Ctx], AsyncIterator[Any]]


class GuardInterceptor(aio.ServerInterceptor):
    def __init__(self, auth: AuthService) -> None:
        self._auth = auth

    async def _enter(self, method: str, context: Ctx) -> None:
        token = bearer_token(context)
        current_token.set(token)
        if method in PUBLIC_METHODS or method.startswith(PUBLIC_PREFIXES):
            return
        current_principal.set(await self._auth.authenticate_token(token))

    async def _fail(self, method: str, context: Ctx, exc: Exception) -> None:
        if isinstance(exc, errors.DomainError):
            await context.abort(status_for(exc), str(exc))
        log.exception("unhandled error in %s", method)
        await context.abort(grpc.StatusCode.INTERNAL, "internal error")

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        method = handler_call_details.method

        if handler.unary_unary is not None:
            inner_uu = cast(Unary, handler.unary_unary)

            async def unary_unary(request: Any, context: Ctx) -> Any:
                try:
                    await self._enter(method, context)
                    return await inner_uu(request, context)
                except (errors.DomainError, Exception) as exc:
                    if isinstance(exc, aio.AbortError):
                        raise
                    await self._fail(method, context, exc)

            return grpc.unary_unary_rpc_method_handler(
                unary_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.unary_stream is not None:
            inner_us = cast(Stream, handler.unary_stream)

            async def unary_stream(request: Any, context: Ctx) -> AsyncIterator[Any]:
                try:
                    await self._enter(method, context)
                    async for item in inner_us(request, context):
                        yield item
                except (errors.DomainError, Exception) as exc:
                    if isinstance(exc, aio.AbortError):
                        raise
                    await self._fail(method, context, exc)

            return grpc.unary_stream_rpc_method_handler(
                unary_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        # Client and bidirectional streams are not allowed in platform.v1.
        log.error("refusing to serve streaming-request method %s", method)

        async def refuse(request: Any, context: Ctx) -> Any:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "streaming requests not supported")

        return grpc.unary_unary_rpc_method_handler(refuse)
