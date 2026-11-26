# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lossless, versioned values carried inside Replay's supported OpenAI codec."""

from __future__ import annotations

from importlib import import_module
from typing import Any


VERSION = 1
_MODELS = {
    "AssistantMessage": "openjiuwen.core.foundation.llm.schema.message",
    "BaseMessage": "openjiuwen.core.foundation.llm.schema.message",
    "UserMessage": "openjiuwen.core.foundation.llm.schema.message",
    "SystemMessage": "openjiuwen.core.foundation.llm.schema.message",
    "ToolMessage": "openjiuwen.core.foundation.llm.schema.message",
    "AssistantMessageChunk": "openjiuwen.core.foundation.llm.schema.message_chunk",
    "ToolInfo": "openjiuwen.core.foundation.tool.schema",
    "ToolOutput": "openjiuwen.core.foundation.tool.schema",
}


def encode(value: Any) -> Any:
    """Reject unsupported objects rather than silently stringifying agent state."""
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, list):
        return {"type": "list", "value": [encode(item) for item in value]}
    if isinstance(value, tuple):
        return {"type": "tuple", "value": [encode(item) for item in value]}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {"type": "dict", "value": {key: encode(item) for key, item in value.items()}}
    name = type(value).__name__
    if _MODELS.get(name) == type(value).__module__:
        return {"type": name, "value": value.model_dump(mode="json", round_trip=True)}
    raise TypeError(f"Unsupported recovery value: {type(value).__module__}.{name}")


def decode(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    kind, payload = value["type"], value["value"]
    if kind == "list":
        return [decode(item) for item in payload]
    if kind == "tuple":
        return tuple(decode(item) for item in payload)
    if kind == "dict":
        return {key: decode(item) for key, item in payload.items()}
    module = _MODELS.get(kind)
    if module is None:
        raise ValueError(f"Unsupported recovery type: {kind}")
    return getattr(import_module(module), kind).model_validate(payload)


def plain(value: Any) -> Any:
    """Canonical model-facing request fields, including reasoning/tool-call IDs."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", round_trip=True)
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError(f"Unsupported model parameter: {type(value).__name__}")


def merged_response(operation: str, result: Any) -> Any:
    if operation != "stream":
        return result
    if not result:
        raise ValueError("Cannot record an empty model stream")
    merged = result[0]
    for chunk in result[1:]:
        merged = merged + chunk
    return merged


def wrap_response(operation: str, result: Any, memory: list[dict]) -> Any:
    from openai.types.chat import ChatCompletion

    merged = merged_response(operation, result)
    data = plain(merged)
    # The supported codec exposes a readable summary; the extension is the
    # authoritative complete core value, including every original stream chunk.
    tool_calls = [call if "function" in call else {
        "id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": call["arguments"],
        }} for call in data.get("tool_calls") or []]
    usage = data.get("usage_metadata") or {}
    return ChatCompletion.model_validate({
        "id": data.get("response_id") or "jiuwen-boundary",
        "object": "chat.completion", "created": 0,
        "model": data.get("response_model") or usage.get("model_name") or "jiuwen",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                     "message": {"role": "assistant", "content": data.get("content") if
                                 isinstance(data.get("content"), str) else "",
                                 "tool_calls": tool_calls or None}}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
        "jiuwen_boundary": {"version": VERSION, "operation": operation,
                            "value": encode(result), "memory": memory},
    })


def unwrap_response(response: Any, operation: str) -> tuple[Any, list[dict]]:
    envelope = response.model_dump(mode="json").get("jiuwen_boundary", {})
    if envelope.get("version") != VERSION or envelope.get("operation") != operation:
        raise ValueError("Incompatible Jiuwen recovery codec or model operation")
    return decode(envelope["value"]), envelope["memory"]


class HistoricalToolFailure(Exception):
    """A foreign tool failure with its original model-visible text."""


def encode_failure(error: Exception) -> dict:
    from openjiuwen.core.common.exception.errors import AgentError

    failure = {"type": type(error).__name__, "message": str(error)}
    if isinstance(error, AgentError):
        failure["agent_error"] = {"status": error.status.name, "message": error.message,
                                  "details": encode(error.details), "params": encode(error.params)}
    return failure


def decode_failure(failure: dict) -> Exception:
    if "agent_error" in failure:
        from openjiuwen.core.common.exception.codes import StatusCode
        from openjiuwen.core.common.exception.errors import AgentError

        data = failure["agent_error"]
        return AgentError(StatusCode[data["status"]], msg=data["message"],
                          details=decode(data["details"]), **decode(data["params"]))
    return HistoricalToolFailure(failure["message"])
