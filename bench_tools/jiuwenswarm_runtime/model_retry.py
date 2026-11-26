# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Normalize transient provider failures, including HTTP-200 SSE error frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import json
import random
import re

TRANSIENT_STATUS = {429, 500, 502, 503, 504}
TRANSIENT_CODES = {"service_unavailable", "system_error", "server_error", "internal_server_error",
                   "rate_limit_exceeded", "rate_limit_error", "too_many_requests", "overloaded_error",
                   "server_overloaded", "request_timeout", "timeout"}
PERMANENT_CODES = {"invalid_api_key", "authentication_error", "permission_denied", "invalid_request_error",
                   "invalid_request", "insufficient_quota", "model_not_found", "context_length_exceeded"}
STATUS_MESSAGE = re.compile(r"(?:API返回错误|error code|status code|HTTP(?: error)?)\s*[:：=\-]?\s*(429|500|502|503|504)\b", re.I)


@dataclass(frozen=True)
class RetryCause:
    reason: str
    status: int | None = None
    retry_after: float = 0


@dataclass(frozen=True)
class ModelRetryPolicy:
    max_retries: int = 12
    call_timeout_seconds: float = 600
    initial_delay_seconds: float = 15
    max_delay_seconds: float = 120

    def __post_init__(self):
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 32:
            raise ValueError("model_max_retries must be an integer between 0 and 32")
        for value in (self.call_timeout_seconds, self.initial_delay_seconds, self.max_delay_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Model timeout and retry delays must be finite positive numbers")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("Initial retry delay cannot exceed maximum retry delay")

    def delay(self, retry: int, cause: RetryCause) -> float:
        backoff = min(self.max_delay_seconds, self.initial_delay_seconds * 2 ** retry)
        jittered = min(self.max_delay_seconds, backoff * random.uniform(0.8, 1.2))
        return max(jittered, cause.retry_after)


def _chain(error: BaseException) -> list[BaseException]:
    pending, seen, result = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for next_error in (current.__cause__, current.__context__):
            if next_error is not None:
                pending.append(next_error)
    return result


def _retry_after(error: BaseException) -> float:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return 0
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return 0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(str(value))
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def retry_cause(error: BaseException) -> RetryCause | None:
    """Explicit permanent HTTP/status codes override suggestive wrapper text."""
    errors = _chain(error)
    statuses, codes = [], set()
    for current in errors:
        status = getattr(current, "status_code", None)
        if type(status) is int and 400 <= status <= 599:
            statuses.append(status)
        body = getattr(current, "body", None)
        bodies = [body] if isinstance(body, dict) else []
        if bodies and isinstance(body.get("error"), dict):
            bodies.append(body["error"])
        for value in [getattr(current, "code", None), getattr(current, "type", None),
                      *(item.get(key) for item in bodies for key in ("code", "type"))]:
            if isinstance(value, str):
                codes.add(value.lower())
    if any(400 <= status < 500 and status != 429 for status in statuses) or codes & PERMANENT_CODES:
        return None
    retry_after = max((_retry_after(current) for current in errors), default=0)
    for status in statuses:
        if status in TRANSIENT_STATUS:
            return RetryCause("http_" + str(status), status, retry_after)
    matched = codes & TRANSIENT_CODES
    if matched:
        return RetryCause("provider_" + sorted(matched)[0], retry_after=retry_after)
    for current in errors:
        if isinstance(current, json.JSONDecodeError) or (
            'JSONDecodeError' in str(current)
            and any(marker in str(current).lower() for marker in ('stream', 'sse'))
        ):
            return RetryCause("model_stream_json_decode", retry_after=retry_after)
        # A streaming response truncated mid-payload surfaces as bare wrapper
        # text ("unexpected end of JSON input") whose JSONDecodeError type does
        # not survive the provider chain; treat the marker itself as transient
        # (real run: v8.4 episode died as permanent_failure on exactly this).
        if 'unexpected end of json input' in str(current).lower():
            return RetryCause("model_stream_json_decode", retry_after=retry_after)
        if isinstance(current, TimeoutError) or type(current).__name__ in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
            return RetryCause("model_timeout", retry_after=retry_after)
        if isinstance(current, ConnectionError) or type(current).__name__ in {"APIConnectionError", "ConnectError", "ReadError", "WriteError", "RemoteProtocolError"}:
            return RetryCause("model_connection", retry_after=retry_after)
        # Compatibility with wrappers that serialize provider status and drop
        # the SDK cause. Do not infer from an arbitrary number such as 181001.
        match = STATUS_MESSAGE.search(str(current))
        if match:
            status = int(match.group(1))
            return RetryCause("serialized_http_" + str(status), status, retry_after)
        if type(current).__name__ == "RateLimitError" or "rate limit reached" in str(current).lower():
            return RetryCause("rate_limit", 429, retry_after)
    return None


def event_summary(path) -> dict:
    result = {"retries": 0, "scheduled_retries": 0, "failures": 0, "timeouts": 0, "retry_exhaustions": 0}
    if not path.exists():
        return result
    import json

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("event")
        result["retries"] += kind == "call_started" and event.get("retry", 0) > 0
        result["scheduled_retries"] += kind == "retry_scheduled"
        result["failures"] += kind == "call_failed"
        result["timeouts"] += kind == "call_failed" and event.get("reason", "").endswith("timeout")
        result["retry_exhaustions"] += kind == "retry_exhausted"
        if kind in {"retry_exhausted", "permanent_failure", "retry_deadline_exhausted"}:
            result["last_terminal"] = event
        elif kind == "call_returned":
            result.pop("last_terminal", None)
    return result
