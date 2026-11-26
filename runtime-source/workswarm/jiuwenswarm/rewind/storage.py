# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable control files and a parent-readable API expenditure journal."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class BudgetExhausted(Exception):
    """The parent-owned cumulative budget cannot admit another operation."""


class UsageJournal:
    """One writer per attempt; all attempts share an append-only cost ledger.

    The standalone legacy policy retains conservative reservations. Benchmark
    provider_usage counts only settled provider tokens; unfinished calls remain
    explicitly unknown. Replayed responses never create journal entries.
    """

    def __init__(self, path: Path, *, token_limit: int, deadline: float,
                 accounting: str = "conservative", run_id: str | None = None):
        if accounting not in {"conservative", "provider_usage", "uncached_usage"}:
            raise ValueError("Unsupported budget accounting policy")
        self.path = path
        self.token_limit = token_limit
        self.deadline = deadline
        self.accounting = accounting
        self.run_id = run_id

    def model_event(self, event: str, **details: Any) -> None:
        path = self.path.with_name("model-events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "run_id": self.run_id,
                                     "timestamp": time.time(), **details}, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def totals(self) -> dict[str, int]:
        calls: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
            lines = raw.splitlines()
            if raw and not raw.endswith("\n"):
                # A killed writer may leave an unfinished final settlement.
                # Keep the previous durable reservation for that API call.
                lines = lines[:-1]
            for line in lines:
                entry = json.loads(line)
                calls[entry["id"]] = entry
        result = {
            "tokens": sum(entry["tokens"] for entry in calls.values()
                          if self.accounting == "conservative" or entry["phase"] == "settled"),
            "api_calls": len(calls),
            "unsettled_calls": sum(entry["phase"] == "reserved" for entry in calls.values()),
        }
        if self.accounting == "uncached_usage":
            settled = [entry for entry in calls.values() if entry["phase"] == "settled"]
            for field in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens"):
                result[field] = sum(entry.get(field, 0) for entry in settled)
            result["cache_usage_missing_calls"] = sum(entry.get("cache_usage_missing", False) for entry in settled)
        return result

    def _append(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, tokens: int) -> str:
        used = self.totals()["tokens"]
        exhausted = (used >= self.token_limit if self.accounting != "conservative"
                     else used + tokens > self.token_limit)
        if time.time() >= self.deadline or exhausted:
            raise BudgetExhausted("Cumulative token or wall-time budget exhausted")
        call_id = uuid.uuid4().hex
        entry = {"id": call_id, "phase": "reserved", "tokens": tokens,
                 "accounting": self.accounting}
        if self.accounting != "conservative":
            entry.update(tokens=0, estimated_tokens=tokens)
        self._append(entry)
        return call_id

    def settle(self, call_id: str, tokens: int) -> None:
        if type(tokens) is not int or tokens < 0:
            raise ValueError("Provider usage must contain a nonnegative token count")
        self._append({"id": call_id, "phase": "settled", "tokens": tokens})
        if self.totals()["tokens"] > self.token_limit:
            raise BudgetExhausted("Provider usage exceeded the cumulative token limit")

    def settle_usage(self, call_id: str, usage: dict) -> None:
        """Use the benchmark's input-minus-cache-plus-output token metric."""
        if self.accounting != "uncached_usage":
            self.settle(call_id, usage["total_tokens"])
            return
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        cached = usage.get("cache_read_tokens")
        missing = cached is None
        cached = 0 if missing else cached
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens, cached)):
            raise ValueError("Provider must report nonnegative input/output/cache token counts")
        if cached > input_tokens:
            raise ValueError("Provider cache-read count exceeds input tokens")
        if input_tokens + output_tokens == 0 and usage.get("total_tokens", 0) > 0:
            raise ValueError("Input/output usage missing; refusing to record a zero cost")
        cost = input_tokens - cached + output_tokens
        self._append({"id": call_id, "phase": "settled", "tokens": cost,
                      "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_read_tokens": cached, "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
                      "cache_usage_missing": missing, "cache_source": usage.get("cache_source"),
                      "accounting": self.accounting})
        if self.totals()["tokens"] > self.token_limit:
            raise BudgetExhausted("Uncached token budget exceeded")
