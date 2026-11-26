# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-attempt boundaries. Replay is imported only when recording is enabled."""

from __future__ import annotations

import copy
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.rewind.codec import (
    decode, decode_failure, encode, encode_failure, merged_response, plain, unwrap_response, wrap_response,
)
from jiuwenswarm.rewind.storage import BudgetExhausted, UsageJournal
from jiuwenswarm.rewind.contracts import MEMORY_ITEM_CHAR_LIMIT
from jiuwenswarm.rewind.model_retry import ModelRetryPolicy, retry_cause


class AttemptBoundaries:
    def __init__(self, session: Any, journal: UsageJournal, *, max_output_tokens: int,
                 rewind_enabled: bool = False, rewinds_left: int = 0, model_parameters: dict | None = None,
                 rate_limit_retries: int = 0, model_instructions: str = "",
                 preserve_output_limit: bool = False, code_recovery: bool = False,
                 retry_policy: ModelRetryPolicy | None = None):
        self.session = session
        self.journal = journal
        self.max_output_tokens = max_output_tokens
        self.rewind_enabled = rewind_enabled
        self.rewinds_left = rewinds_left
        self.model_parameters = dict(model_parameters or {})
        self.rate_limit_retries = rate_limit_retries
        self.model_instructions = model_instructions
        self.preserve_output_limit = preserve_output_limit
        self.code_recovery = code_recovery
        self.retry_policy = retry_policy
        self.observations: list[dict] = []
        self.memory: list[dict] = []
        self.active_tools = 0
        self._candidate_ids: set[str] = set()
        self._prefix_memories: list[list[dict]] = []
        self._model_index = 0
        self._fork_live = False
        self._prefix_records: list[dict] = []
        self._prefix_position = 0
        if session is not None and getattr(session, "base_run", None):
            from jiuwenswarm.rewind.memory import is_recovery_message
            from replay.storage import load_raw_records, run_path

            records = load_raw_records(run_path(Path(session.log_dir), session.base_run))
            self._prefix_records = [record for record in records if record.get("kind") in {"llm", "tool"}]
            self._prefix_memories = [
                [copy.deepcopy(message) for message in record["input"].get("messages", [])
                 if is_recovery_message(message)]
                for record in records if record.get("kind") == "llm"
            ]

    def audit(self, event: str, **details: Any) -> None:
        if not self.code_recovery or self.session is None:
            return
        path = Path(self.session.log_dir).parent / "policy-events.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"run_id": getattr(self.session, "fork_run", None) or getattr(self.session, "run_id", None),
                                     "event": event, **details}, ensure_ascii=False) + "\n")

    def observe_tool(self, tool_call: Any, *, success: bool) -> None:
        name = tool_call.name.rsplit(".", 1)[-1]
        if self.code_recovery and not name.startswith("backtrack_"):
            self.observations.append({"id": tool_call.id, "name": name,
                                      "arguments": plain(tool_call.arguments), "success": success,
                                      "index": len(self.observations)})

    def verify_prefix(self, kind: str, request: dict) -> None:
        """Do not inherit Replay's materialized-prefix path-only fallback."""
        if not self._prefix_records or self._fork_live:
            return
        from replay.errors import ReplayMissError
        from replay.ids import build_input_record, compute_input_id

        if self._prefix_position >= len(self._prefix_records):
            raise ReplayMissError("Strict Jiuwen prefix exhausted before target")
        expected = self._prefix_records[self._prefix_position]
        normalized = self.session.workspace_manager.normalize_paths(request)
        actual = build_input_record(normalized) if kind == "llm" else normalized
        if expected["kind"] != kind or compute_input_id(actual) != expected["input_id"]:
            from jiuwenswarm.rewind.storage import atomic_json

            atomic_json(Path(self.session.log_dir) / "prefix-mismatch.json",
                        {"expected": expected["input"], "actual": actual})
            raise ReplayMissError(f"Strict Jiuwen {kind} input mismatch at {expected['record_uid']}")
        self._prefix_position += 1

    def check_target_reached(self) -> None:
        if self._prefix_records and not self._fork_live:
            from replay.errors import ReplayMissError

            raise ReplayMissError("Agent ended before the selected replay target")

    def flush(self) -> None:
        """Publish only settled boundaries, independent of asyncio task owner."""
        if self.active_tools:
            raise RuntimeError("Cannot publish recovery points while a tool is active")
        if self.session is None:
            return
        flush = getattr(self.session, "flush_pending_fork_records", None)
        if flush is None:
            flush = self.session.flush_pending_records
        flush()
        # Replay's writer flushes Python buffers; handoff additionally requires
        # fsync before a point can be offered to another process.
        for path in Path(self.session.log_dir).glob("*.jsonl"):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())

    async def model(self, operation: str, request: dict, invoke: Any) -> Any:
        request = {**request, **self.model_parameters}
        if self._prefix_memories and not self._fork_live:
            if self._model_index >= len(self._prefix_memories):
                raise ValueError("Replay model prefix exhausted before reaching its target")
            # A previous fork's attachment is policy state at this boundary.
            # Restore it BEFORE matching, including when rewinding to the same
            # decision again. Other request fields still match strictly.
            self.memory = copy.deepcopy(self._prefix_memories[self._model_index])
        self._model_index += 1
        # Runtime credentials are forwarded live but never embedded inside the
        # codec extension (which would bypass Replay's top-level sanitization).
        secret_keys = {"api_key", "authorization", "headers", "extra_headers", "default_headers",
                       "access_token", "refresh_token", "credentials", "http_client"}
        private = {key: value for key, value in request.items() if key.lower() in secret_keys
                   or key.lower().endswith("_api_key")}
        request = copy.deepcopy({key: value for key, value in request.items() if key not in private})
        if request.get("output_parser") is not None:
            raise ValueError("v1 recovery does not support custom output parsers")
        message_objects = isinstance(request["messages"], list) and any(
            hasattr(message, "model_dump") for message in request["messages"]
        )
        messages = plain(request["messages"])
        if message_objects:
            # ReAct recomputes latency statistics after a cached model return.
            # Native providers do not send historical usage_metadata as model
            # input, so telemetry must not invalidate an otherwise exact prefix.
            # The full usage remains preserved in response/chunk envelopes.
            for message in messages:
                if message.get("role") == "assistant":
                    message.pop("usage_metadata", None)
                # ContextEngine allocates a fresh trajectory anchor UUID in
                # every process. Providers do not send this bookkeeping ID.
                metadata = message.get("metadata")
                if isinstance(metadata, dict):
                    metadata.pop("context_message_id", None)
                    if not metadata:
                        message.pop("metadata", None)
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if self.model_instructions:
            messages.insert(0, {"role": "system", "content": self.model_instructions})
        if self.memory:
            # A persistent effective-input attachment, also reconstructed from
            # replayed envelopes. No private ContextEngine state is modified.
            from jiuwenswarm.rewind.memory import NOTICE_KEY, NOTICE_POSITION_KEY

            for attachment in copy.deepcopy(self.memory):
                metadata = attachment.get("metadata") or {}
                if metadata.get(NOTICE_KEY):
                    position = metadata[NOTICE_POSITION_KEY]
                    if not 0 <= position <= len(messages):
                        raise ValueError("Recovery notification history anchor was removed")
                    messages.insert(position, attachment)
                else:
                    messages.append(attachment)
        record_kwargs = {"messages": messages, "jiuwen_request": encode({
            key: value for key, value in request.items() if key != "messages"
        }), "jiuwen_operation": operation}

        async def live(overridden: dict | None = None):
            if self._prefix_records and not self._fork_live and overridden is None:
                from replay.errors import ReplayMissError

                raise ReplayMissError("Live model fallback is forbidden inside the replay prefix")
            effective = record_kwargs if overridden is None else self.session.workspace_manager.denormalize_paths(overridden)
            actual = decode(effective["jiuwen_request"])
            actual.update(private)
            actual["messages"] = copy.deepcopy(effective["messages"])
            if message_objects:
                from openjiuwen.core.foundation.llm.schema.message import (
                    AssistantMessage, BaseMessage, SystemMessage, ToolMessage, UserMessage,
                )

                kinds = {"assistant": AssistantMessage, "system": SystemMessage,
                         "user": UserMessage, "tool": ToolMessage}
                actual["messages"] = [kinds.get(message["role"], BaseMessage).model_validate(message)
                                      for message in actual["messages"]]
            if not self.preserve_output_limit:
                actual["max_tokens"] = min(actual.get("max_tokens") or self.max_output_tokens,
                                           self.max_output_tokens)
            # Conservative UTF-8 byte reservation includes declared tools and
            # output allowance. Interrupted/failed calls keep this reservation.
            reserved = len(json.dumps(effective, ensure_ascii=False).encode("utf-8"))
            retries = self.retry_policy.max_retries if self.retry_policy else self.rate_limit_retries
            for retry in range(retries + 1):
                try:
                    call_id = self.journal.reserve(reserved + (actual.get("max_tokens") or self.max_output_tokens))
                except BudgetExhausted as error:
                    self.stop_for_budget(str(error))
                try:
                    if self.retry_policy is None:
                        result = await invoke(actual)
                    else:
                        remaining = self.journal.deadline - time.time()
                        if remaining <= 0:
                            self.stop_for_budget("Task wall-time budget exhausted before model call")
                        self.journal.model_event("call_started", call_id=call_id, retry=retry)
                        result = await asyncio.wait_for(invoke(actual), timeout=min(
                            self.retry_policy.call_timeout_seconds, remaining))
                        self.journal.model_event("call_returned", call_id=call_id, retry=retry)
                    break
                except Exception as error:
                    if self.retry_policy is None:
                        if retry >= retries or not is_rate_limit(error):
                            raise
                        await asyncio.sleep(2 ** retry)
                        continue
                    cause = retry_cause(error)
                    details = {"call_id": call_id, "retry": retry,
                               "reason": cause.reason if cause else "non_retryable_model_error",
                               "status": cause.status if cause else None, "error_type": type(error).__name__}
                    self.journal.model_event("call_failed", **details)
                    if cause is None:
                        self.journal.model_event("permanent_failure", **details)
                        raise
                    if time.time() >= self.journal.deadline:
                        self.journal.model_event("retry_deadline_exhausted", **details)
                        self.stop_for_budget("Task wall-time budget exhausted during model call")
                    if retry >= retries:
                        self.journal.model_event("retry_exhausted", **details)
                        raise
                    delay = self.retry_policy.delay(retry, cause)
                    if delay >= self.journal.deadline - time.time():
                        self.journal.model_event("retry_deadline_exhausted", **details)
                        self.stop_for_budget("Remaining task time cannot accommodate provider retry delay")
                    self.journal.model_event("retry_scheduled", delay_seconds=delay, **details)
                    await asyncio.sleep(delay)
            merged = plain(merged_response(operation, result))
            usage = merged.get("usage_metadata")
            if usage is None or not isinstance(usage.get("total_tokens"), int):
                raise ValueError("Provider must report total_tokens; reservation retained")
            try:
                self.journal.settle_usage(call_id, usage)
            except BudgetExhausted as error:
                self.stop_for_budget(str(error))
            if overridden is not None:
                from jiuwenswarm.rewind.memory import is_recovery_message

                self.memory = [copy.deepcopy(message) for message in effective["messages"]
                               if is_recovery_message(message)]
                self._fork_live = True
                self.audit("target_regenerated", record_uid=self._prefix_records[self._prefix_position - 1]["record_uid"])
            return wrap_response(operation, result, self.memory) if self.session is not None else result

        if self.session is None:
            return await live()
        self.flush()
        self.verify_prefix("llm", record_kwargs)
        response = await self.session.handle_async_llm_boundary(
            name="jiuwen.model", record_kwargs=record_kwargs, invoke=live,
            metadata={"integration": "jiuwenswarm", "codec_version": 1},
        )
        result, self.memory = unwrap_response(response, operation)
        if self._prefix_records and not self._fork_live:
            self.audit("prefix_model_replayed", record_uid=self._prefix_records[self._prefix_position - 1]["record_uid"])
        self.flush()
        return result

    async def tool(self, tool_call: Any, invoke: Any) -> Any:
        if self.session is None:
            return await invoke()
        self.flush()
        # Commit transfers control and intentionally has no ordinary tool result.
        if tool_call.name == "backtrack_commit":
            if not self.code_recovery:
                self.check_target_reached()
                return await invoke()
            if not self._prefix_records or self._fork_live:
                arguments = tool_call.arguments
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if self.commit_rejection(**arguments) is None:
                    self.check_target_reached()
                    try:
                        return await invoke()
                    except Exception as error:
                        # Upstream can still reject duplicate/invalid memory.
                        # Record that ordinary failure without invoking twice.
                        rejected_error = error

                        async def rejected():
                            raise rejected_error

                        invoke = rejected
            # Rejected commits have ordinary, recorded results. They must be
            # replayable when a later accepted rewind retains them in its prefix.

        original_error = None

        async def live():
            nonlocal original_error
            if self._prefix_records and not self._fork_live:
                from replay.errors import ReplayMissError

                raise ReplayMissError("Live tool fallback is forbidden inside the replay prefix")
            self.active_tools += 1
            try:
                result = await invoke()
                return {"version": 1, "ok": True, "value": encode(result)}
            except Exception as error:
                original_error = error
                return {"version": 1, "ok": False, "failure": encode_failure(error)}
            finally:
                self.active_tools -= 1

        tool_input = {"name": tool_call.name, "id": tool_call.id, "arguments": tool_call.arguments}
        self.verify_prefix("tool", tool_input)
        result = await self.session.handle_async_tool_boundary(
            tool_name=tool_call.name,
            input_record=tool_input,
            invoke=live,
            metadata={"integration": "jiuwenswarm", "codec_version": 1},
            metadata_factory=lambda: {"tool_failed": original_error is not None},
        )
        self.flush()
        if result.get("version") != 1:
            raise ValueError("Incompatible tool result codec")
        if not result["ok"]:
            self.observe_tool(tool_call, success=False)
            # Replay's generic ReplayedToolError adds a prefix to str(error),
            # which would change AbilityManager's model-facing ToolMessage.
            raise original_error if original_error is not None else decode_failure(result["failure"])
        value = decode(result["value"])
        if self.code_recovery:
            output = plain(value)
            success = not isinstance(output, dict) or output.get("success", True) is not False
            self.observe_tool(tool_call, success=success)
            if self._prefix_records and not self._fork_live:
                self.audit("prefix_tool_replayed", tool_call_id=tool_call.id)
        return value

    def candidates(self, reason: str) -> list[dict]:
        if self.rewind_enabled and self.rewinds_left <= 0:
            if self.code_recovery:
                self._candidate_ids.clear()
                self.audit("rewind_unavailable", reason="count_limit")
                return []
            self.stop_for_budget("Maximum rewind count reached")
        if not self.rewind_enabled:
            raise ValueError("No rewinds are available")
        from replay.backtrack import backtrack_candidates

        # The model boundary was durably flushed before the current tool began.
        candidates = backtrack_candidates(reason=reason)
        candidates = self.session.workspace_manager.denormalize_paths(candidates)
        self._candidate_ids = {item["record_uid"] for item in candidates}
        return candidates

    def commit_rejection(self, *, record_uid: str, memory_summary: str, reason: str | None = None,
                         review: dict | None = None) -> str | None:
        from jiuwenswarm.rewind.code_policy import MEMORY_ITEM_LIMIT, review_rejection

        if not self.rewind_enabled or self.rewinds_left <= 0:
            return "No rewinds remain; continue ordinary repair or finish within the remaining token/time budget."
        if record_uid not in self._candidate_ids:
            return "Query and select a returned candidate before committing."
        if not isinstance(memory_summary, str) or not memory_summary.strip() or len(memory_summary) > MEMORY_ITEM_LIMIT:
            return f"Keep nonempty recovery memory within {MEMORY_ITEM_LIMIT} characters."
        return review_rejection(review, self.observations)

    def commit(self, *, record_uid: str, memory_summary: str, reason: str | None = None,
               review: dict | None = None) -> dict | None:
        if self.code_recovery:
            rejection = self.commit_rejection(record_uid=record_uid, memory_summary=memory_summary,
                                              reason=reason, review=review)
            if rejection is not None:
                self.audit("commit_rejected", reason=rejection, review=review)
                return {"accepted": False, "reason": rejection, "continue_solving": True}
        if not self.rewind_enabled or self.rewinds_left <= 0 or record_uid not in self._candidate_ids:
            raise ValueError("Select a current durable LLM candidate before committing a rewind")
        if len(memory_summary) > MEMORY_ITEM_CHAR_LIMIT:
            raise ValueError(f"Keep failure memory within {MEMORY_ITEM_CHAR_LIMIT} characters; put corrective actions first")
        from openjiuwen.core.execution import ExecutionRequest, current_execution_scope
        from replay.backtrack import BacktrackRequested, backtrack_commit

        scope = current_execution_scope()
        if scope is None:
            raise RuntimeError("A recovery runner scope is required")
        try:
            backtrack_commit(record_uid=record_uid, memory_summary=memory_summary, reason=reason)
        except BacktrackRequested as request:
            self.audit("commit_accepted", record_uid=record_uid, review=review)
            scope.request_control(ExecutionRequest("rewind", {
                "record_uid": request.record_uid, "memory_summary": request.memory_summary,
                "reason": request.reason,
            }))

    @staticmethod
    def stop_for_budget(reason: str) -> None:
        from openjiuwen.core.execution import ExecutionRequest, current_execution_scope

        scope = current_execution_scope()
        if scope is None:
            raise BudgetExhausted(reason)
        scope.request_control(ExecutionRequest("budget_exhausted", {"reason": reason}))


def is_rate_limit(error: Exception) -> bool:
    """Recognize provider 429 through core's wrapped error chain only."""
    seen: set[int] = set()
    current: BaseException | None = error
    recognized = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if status is not None:
            return status == 429
        if type(current).__name__ == "RateLimitError" or "rate limit reached" in str(current).lower():
            recognized = True
        current = current.__cause__ or current.__context__
    return recognized
