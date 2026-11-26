# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scoped interception and an explicit control mailbox across background tasks."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import aclosing
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping, Protocol


@dataclass(frozen=True)
class ExecutionRequest:
    """Runner-owned control request; core assigns no meaning to its payload."""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


class ExecutionControl(BaseException):
    """Control flow, never a tool error or a retryable model failure.

    The mailbox is the authoritative delivery path. This exception only unwinds
    the producer task; exceptions do not travel across asynchronous queues.
    """

    def __init__(self, request: ExecutionRequest):
        super().__init__(request.kind)
        self.request = request


class ModelBoundary(Protocol):
    async def __call__(
        self,
        operation: str,
        request: dict[str, Any],
        invoke: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> Any:
        """Invoke with possibly replaced arguments, or return a recorded value."""


class ToolBoundary(Protocol):
    async def __call__(self, tool_call: Any, invoke: Callable[[], Awaitable[Any]]) -> Any:
        """Intercept one permitted tool execution, including its result message."""


_scope: ContextVar[ExecutionScope | None] = ContextVar("execution_scope", default=None)


def current_execution_scope() -> ExecutionScope | None:
    """Return the inherited scope, or None on the ordinary execution path."""
    return _scope.get()


class ExecutionScope:
    """One task attempt's optional boundaries and shared control mailbox.

    Scope objects are single-use. Streaming is buffered only when explicitly
    enabled; with no scope the original generator and delivery remain unchanged.
    Hooks are supplied by integrations, with no recorder dependency in core.
    """

    def __init__(
        self,
        *,
        model_boundary: ModelBoundary | None = None,
        tool_boundary: ToolBoundary | None = None,
        buffer_stream: bool = False,
        serial_tools: bool = False,
    ) -> None:
        self.model_boundary = model_boundary
        self.tool_boundary = tool_boundary
        self.buffer_stream = buffer_stream
        self.serial_tools = serial_tools
        self._token: Token | None = None
        self._entered = False
        self._closed = False
        self._control: ExecutionRequest | None = None
        self._control_event = asyncio.Event()

    def __enter__(self) -> ExecutionScope:
        if self._entered:
            raise RuntimeError("ExecutionScope cannot be reused")
        self._entered = True
        self._token = _scope.set(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._closed = True
        _scope.reset(self._token)

    @property
    def control(self) -> ExecutionRequest | None:
        return self._control

    def check_running(self) -> None:
        """Refuse new work once control has been transferred to the runner."""
        if self._control is not None:
            raise ExecutionControl(self._control)
        if self._closed:
            raise RuntimeError("ExecutionScope is closed")

    def request_control(self, request: ExecutionRequest) -> None:
        """Publish once, then stop the caller even if a queue hides its error."""
        if self._closed:
            raise RuntimeError("ExecutionScope is closed")
        if self._control is None:
            self._control = request
            self._control_event.set()
        raise ExecutionControl(self._control)

    async def wait_control(self) -> ExecutionRequest:
        await self._control_event.wait()
        return self._control

    async def run_tool(self, tool_call: Any, invoke: Callable[[], Awaitable[Any]]) -> Any:
        self.check_running()
        if self.tool_boundary is None:
            return await invoke()
        return await self.tool_boundary(tool_call, invoke)


def model_boundary(*, streaming: bool = False) -> Callable:
    """Wrap Model methods without changing their unscoped execution semantics."""
    def decorate(function: Callable) -> Callable:
        signature = inspect.signature(function)

        def request_for(instance: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
            bound = signature.bind(instance, *args, **kwargs)
            bound.apply_defaults()
            request = dict(bound.arguments)
            request.pop("self")
            request.update(request.pop("kwargs", {}))
            return request

        if streaming:
            @wraps(function)
            async def stream(instance: Any, *args: Any, **kwargs: Any):
                scope = current_execution_scope()
                if scope is None:
                    async with aclosing(function(instance, *args, **kwargs)) as source:
                        async for chunk in source:
                            yield chunk
                    return
                scope.check_running()
                if not scope.buffer_stream and scope.model_boundary is None:
                    async with aclosing(function(instance, *args, **kwargs)) as source:
                        async for chunk in source:
                            yield chunk
                    return

                async def invoke(request: dict[str, Any]) -> list[Any]:
                    return [chunk async for chunk in function(instance, **request)]

                request = request_for(instance, args, kwargs)
                if scope.model_boundary is None:
                    chunks = await invoke(request)
                else:
                    chunks = await scope.model_boundary("stream", request, invoke)
                for chunk in chunks:
                    scope.check_running()
                    yield chunk
            return stream

        @wraps(function)
        async def invoke_model(instance: Any, *args: Any, **kwargs: Any) -> Any:
            scope = current_execution_scope()
            if scope is None:
                return await function(instance, *args, **kwargs)
            scope.check_running()
            if scope.model_boundary is None:
                return await function(instance, *args, **kwargs)

            async def invoke(request: dict[str, Any]) -> Any:
                return await function(instance, **request)

            return await scope.model_boundary("invoke", request_for(instance, args, kwargs), invoke)
        return invoke_model
    return decorate
