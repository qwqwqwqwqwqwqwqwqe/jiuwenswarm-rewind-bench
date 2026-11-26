# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Validated, JSON-only parent/worker contracts without agent dependencies."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA = "jiuwenswarm.rewind.v1"
MEMORY_ITEM_CHAR_LIMIT = 8_000
MEMORY_NOTEBOOK_CHAR_LIMIT = 32_000
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED", "REWIND_READY"}
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


@dataclass(frozen=True)
class Budget:
    tokens: int
    seconds: float
    max_rewinds: int = 3
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        for name in ("tokens", "max_output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.seconds) not in (int, float) or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("seconds must be positive")
        if type(self.max_rewinds) is not int or self.max_rewinds < 0:
            raise ValueError("max_rewinds must be a nonnegative integer")


@dataclass(frozen=True)
class Task:
    instance_id: str
    repository: str
    base_commit: str
    prompt: str

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        if not all(isinstance(value, str) and value.strip() for value in
                   (self.repository, self.base_commit, self.prompt)):
            raise ValueError("repository, base_commit and prompt must be nonempty strings")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", self.base_commit):
            raise ValueError("base_commit must be a frozen full Git SHA-1")


@dataclass(frozen=True)
class Manifest:
    experiment_id: str
    model: str
    budget: Budget
    tasks: tuple[Task, ...]
    tools: tuple[str, ...]
    instructions: str = "Solve the user's code task. Work only in the supplied repository."
    max_iterations: int = 50
    image: str | None = None
    driver: str = "jiuwenswarm.rewind.native:run"
    environment: tuple[str, ...] = ()
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int | None = None
    reasoning_effort: str | None = None
    api_mode: str = "chat_completions"
    rate_limit_retries: int = 0

    def __post_init__(self) -> None:
        identifier(self.experiment_id)
        if not self.model or not self.tasks:
            raise ValueError("model and tasks are required")
        if not 0 <= self.temperature <= 2 or not 0 < self.top_p <= 1:
            raise ValueError("temperature/top_p are outside the supported range")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("seed must be an integer or null")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a nonempty provider-supported value or null")
        if self.api_mode not in {"chat_completions", "responses"}:
            raise ValueError("api_mode must be chat_completions or responses")
        if type(self.rate_limit_retries) is not int or not 0 <= self.rate_limit_retries <= 3:
            raise ValueError("rate_limit_retries must be an integer between zero and three")
        if len({task.instance_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("Duplicate instance_id")
        if type(self.max_iterations) is not int or self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        if any(not isinstance(name, str) or not name.strip() for name in self.tools):
            raise ValueError("Tool names must be nonempty strings")
        if "*" in self.tools or len(set(self.tools)) != len(self.tools):
            raise ValueError("An explicit, unique tool allowlist is required")
        # Stateful/background/delegation tools have no replay contract in v1.
        forbidden = ("subagent", "delegate", "team", "memory", "background", "skill", "schedule", "task_")
        if any(any(part in name.lower() for part in forbidden) for name in self.tools):
            raise ValueError("v1 supports workspace tools only; stateful tools are not recoverable")
        if self.image is not None and not re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", self.image):
            raise ValueError("Pin the container image by registry digest or local image ID")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in self.environment):
            raise ValueError("environment must contain variable names, not values")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Manifest:
        fields = dict(value)
        if fields.pop("schema", None) != SCHEMA:
            raise ValueError(f"Expected schema {SCHEMA}")
        fields["budget"] = Budget(**fields["budget"])
        fields["tasks"] = tuple(Task(**task) for task in fields["tasks"])
        for field in ("tools", "environment"):
            if not isinstance(fields.get(field, ()), (list, tuple)):
                raise ValueError(f"{field} must be a list")
            fields[field] = tuple(fields.get(field, ()))
        return cls(**fields)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
        manifest = cls.from_dict(value)
        # Resolve once in the parent; child paths must not depend on its cwd.
        tasks = tuple(Task(task.instance_id, str((path.parent / task.repository).resolve()),
                           task.base_commit, task.prompt) for task in manifest.tasks)
        return cls(**{**manifest.__dict__, "tasks": tasks})
