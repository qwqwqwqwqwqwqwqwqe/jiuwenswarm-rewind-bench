from __future__ import annotations

import copy
import importlib
import inspect
from collections.abc import AsyncIterator, Iterable
from typing import Any

from .context import get_current_session
from .semantic_runtime import RUNTIME


_installed = False
_original_completion = None
_original_acompletion = None
_litellm_module = None

_COMPLETION_POSITIONAL_NAMES = (
    "model",
    "messages",
    "timeout",
    "temperature",
    "top_p",
    "n",
    "stream",
    "stream_options",
    "stop",
    "max_completion_tokens",
    "max_tokens",
)
_ACOMPLETION_POSITIONAL_NAMES = (
    "model",
    "messages",
    "functions",
    "function_call",
    "timeout",
    "temperature",
    "top_p",
    "n",
    "stream",
    "stream_options",
    "stop",
    "max_tokens",
    "max_completion_tokens",
)
_SENSITIVE_KEY_NAMES = {
    "api_key",
    "authorization",
    "proxy_authorization",
    "x_api_key",
}


def _load_litellm_module():
    try:
        return importlib.import_module("litellm")
    except ModuleNotFoundError as exc:
        if exc.name != "litellm":
            raise
        return None
    except ImportError:
        # LiteLLM is optional.  Incompatible installed distributions (for
        # example, a provider package expecting a symbol from another version)
        # must not make core Replay installation fail.
        return None


def _patched_completion(*args: Any, **kwargs: Any):
    session = get_current_session()
    if session is None or _in_external_boundary():
        return _original_completion(*args, **kwargs)

    record_kwargs = _record_kwargs(args, kwargs, _COMPLETION_POSITIONAL_NAMES)
    stream = record_kwargs.get("stream") is True

    def invoke(override_kwargs: dict[str, Any] | None = None):
        live_kwargs = kwargs if override_kwargs is None else _restore_redacted_values(override_kwargs, kwargs)
        live_kwargs = _with_litellm_compatibility_kwargs(live_kwargs)
        call_args, call_kwargs = RUNTIME.plain_call_args(args, live_kwargs)
        response = _original_completion(*call_args, **call_kwargs)
        if stream:
            return list(response)
        return response

    response = session.handle_sync_llm_boundary(
        name="litellm.completion",
        record_kwargs=_redact_sensitive_values(record_kwargs),
        invoke=invoke,
        metadata=_metadata(method="completion", stream=stream),
        provider="litellm",
        api="completion",
    )
    if stream:
        return iter(response)
    return response


async def _patched_acompletion(*args: Any, **kwargs: Any):
    session = get_current_session()
    if session is None or _in_external_boundary():
        response = _original_acompletion(*args, **kwargs)
        return await response if inspect.isawaitable(response) else response

    record_kwargs = _record_kwargs(args, kwargs, _ACOMPLETION_POSITIONAL_NAMES)
    stream = record_kwargs.get("stream") is True

    async def invoke(override_kwargs: dict[str, Any] | None = None):
        live_kwargs = kwargs if override_kwargs is None else _restore_redacted_values(override_kwargs, kwargs)
        live_kwargs = _with_litellm_compatibility_kwargs(live_kwargs)
        call_args, call_kwargs = RUNTIME.plain_call_args(args, live_kwargs)
        response = _original_acompletion(*call_args, **call_kwargs)
        response = await response if inspect.isawaitable(response) else response
        if stream:
            return await _collect_async_or_sync(response)
        return response

    response = await session.handle_async_llm_boundary(
        name="litellm.acompletion",
        record_kwargs=_redact_sensitive_values(record_kwargs),
        invoke=invoke,
        metadata=_metadata(method="acompletion", stream=stream),
        provider="litellm",
        api="acompletion",
    )
    if stream:
        return _async_iter(response)
    return response


def _record_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any], positional_names: tuple[str, ...]) -> dict[str, Any]:
    record_kwargs: dict[str, Any] = {}
    for index, value in enumerate(args):
        if index < len(positional_names):
            record_kwargs[positional_names[index]] = value
        else:
            record_kwargs[f"arg_{index}"] = value
    record_kwargs.update(kwargs)
    return record_kwargs


def _redact_sensitive_values(value: Any, *, key: Any = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return {"redacted": True}
    if isinstance(value, dict):
        return {
            copy.deepcopy(item_key): _redact_sensitive_values(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_values(item) for item in value)
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _restore_redacted_values(value: Any, live_value: Any) -> Any:
    if _is_redacted_sentinel(value):
        return copy.deepcopy(live_value)
    if isinstance(value, dict):
        restored: dict[Any, Any] = {}
        live_dict = live_value if isinstance(live_value, dict) else {}
        for item_key, item_value in value.items():
            if item_key in live_dict:
                restored[copy.deepcopy(item_key)] = _restore_redacted_values(
                    item_value,
                    live_dict[item_key],
                )
            else:
                restored[copy.deepcopy(item_key)] = copy.deepcopy(item_value)
        return restored
    if isinstance(value, list):
        live_list = live_value if isinstance(live_value, list) else []
        return [
            _restore_redacted_values(item, live_list[index])
            if index < len(live_list)
            else copy.deepcopy(item)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        live_tuple = live_value if isinstance(live_value, tuple) else ()
        return tuple(
            _restore_redacted_values(item, live_tuple[index])
            if index < len(live_tuple)
            else copy.deepcopy(item)
            for index, item in enumerate(value)
        )
    return copy.deepcopy(value)


def _is_redacted_sentinel(value: Any) -> bool:
    return isinstance(value, dict) and value == {"redacted": True}


def _with_litellm_compatibility_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if not _needs_gpt55_drop_params(kwargs):
        return kwargs
    adjusted = dict(kwargs)
    adjusted.setdefault("drop_params", True)
    return adjusted


def _needs_gpt55_drop_params(kwargs: dict[str, Any]) -> bool:
    model = str(kwargs.get("model") or "").strip()
    if model not in {"gpt-5.5", "openai/gpt-5.5"}:
        return False
    return "top_p" in kwargs


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEY_NAMES
        or normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or (normalized.endswith("_token") and not normalized.endswith("_tokens"))
    )


def _metadata(*, method: str, stream: bool) -> dict[str, Any]:
    return {
        "provider": "litellm",
        "api": method,
        "method": method,
        "stream": stream,
    }


async def _collect_async_or_sync(value: Any) -> list[Any]:
    if hasattr(value, "__aiter__"):
        return [item async for item in value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


async def _async_iter(values: Iterable[Any]) -> AsyncIterator[Any]:
    for value in values:
        yield value


def _in_external_boundary() -> bool:
    snapshot = getattr(RUNTIME, "current_kinds_snapshot", None)
    if not callable(snapshot):
        return False
    kinds = snapshot()
    return bool(kinds and kinds[-1] in {"llm", "tool"})


def install_litellm_patch() -> None:
    global _installed
    global _original_completion
    global _original_acompletion
    global _litellm_module

    if _installed:
        return

    module = _load_litellm_module()
    if module is None:
        return

    original_completion = getattr(module, "completion", None)
    original_acompletion = getattr(module, "acompletion", None)
    if not callable(original_completion) or not callable(original_acompletion):
        return

    completion_patched = False
    try:
        module.completion = _patched_completion
        completion_patched = True
        module.acompletion = _patched_acompletion
    except Exception:
        if completion_patched:
            module.completion = original_completion
        raise

    _original_completion = original_completion
    _original_acompletion = original_acompletion
    _litellm_module = module
    _installed = True


def uninstall_litellm_patch() -> None:
    global _installed
    global _original_completion
    global _original_acompletion
    global _litellm_module

    if not _installed:
        return

    if _litellm_module is not None and _original_completion is not None:
        _litellm_module.completion = _original_completion
    if _litellm_module is not None and _original_acompletion is not None:
        _litellm_module.acompletion = _original_acompletion

    _installed = False
    _original_completion = None
    _original_acompletion = None
    _litellm_module = None
