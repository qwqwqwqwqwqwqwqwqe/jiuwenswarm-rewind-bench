"""Opt-in benchmark contract, shared by the driver and the Runtime route."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path

SCHEMA = "jiuwenswarm.rewind.bench.v1"
PROFILE = "single-agent-code-v1"
WORKSPACE_TOOLS = ("read_file", "write_file", "edit_file", "glob", "grep", "list_files", "bash")


def wire_value(value):
    """Encode protocol dataclasses and enum values without lossy stringification."""
    if is_dataclass(value):
        return wire_value(asdict(value))
    if isinstance(value, Enum):
        return wire_value(value.value)
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire_value(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"Unsupported benchmark IPC value: {type(value).__name__}")


@dataclass(frozen=True)
class BenchOptions:
    mode: str
    token_limit: int
    remaining_seconds: float
    max_rewinds: int = 3
    schema: str = SCHEMA
    profile: str = PROFILE
    model_max_retries: int = 12
    model_call_timeout_seconds: float = 600
    model_retry_initial_delay_seconds: float = 15
    model_retry_max_delay_seconds: float = 120

    def __post_init__(self):
        if self.schema != SCHEMA or self.profile != PROFILE:
            raise ValueError("Unsupported rewind benchmark schema/profile")
        if self.mode not in {"off", "record-only", "on"}:
            raise ValueError("rewind mode must be off, record-only or on")
        if type(self.token_limit) is not int or self.token_limit <= 0:
            raise ValueError("token_limit must be an explicit positive integer")
        if type(self.max_rewinds) is not int or self.max_rewinds < 0:
            raise ValueError("max_rewinds must be a nonnegative integer")
        if (type(self.remaining_seconds) not in (int, float)
                or not math.isfinite(self.remaining_seconds) or self.remaining_seconds <= 0):
            raise ValueError("remaining_seconds must be finite and positive")
        self.retry_policy()

    def retry_policy(self):
        from jiuwenswarm.rewind.model_retry import ModelRetryPolicy

        return ModelRetryPolicy(self.model_max_retries, self.model_call_timeout_seconds,
                                self.model_retry_initial_delay_seconds, self.model_retry_max_delay_seconds)

    def to_dict(self):
        return asdict(self)


def load_driver_options(runtime: Path, remaining_seconds: float) -> dict | None:
    """A missing file preserves the original driver behavior; invalid files fail."""
    path = runtime / "rewind-bench.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or "remaining_seconds" in value:
        raise ValueError("rewind-bench.json must be an object without remaining_seconds")
    return BenchOptions(**value, remaining_seconds=remaining_seconds).to_dict()


def single_agent_config(config: dict) -> dict:
    """Overlay the agreed capability restrictions without replacing model settings."""
    config = copy.deepcopy(config)
    config.setdefault("memory", {})["engine"] = "none"
    for key in ("task_memory", "proactive_recommendation", "auto_recap", "rsi", "symphony"):
        config.setdefault(key, {})["enabled"] = False
    config["progressive_tool_enabled"] = False
    react = config.setdefault("react", {})
    react["enable_task_loop"] = False
    react.setdefault("evolution", {})["skill_evolution"] = False
    react.setdefault("ttse", {})["enabled"] = False
    for item in react.get("subagents", {}).values():
        if isinstance(item, dict):
            item["enabled"] = False
    for name in ("agent", "code"):
        mode = config.setdefault("modes", {}).setdefault(name, {})
        mode.setdefault("memory", {})["enabled"] = False
        mode["tools"] = []
        mode["rails"] = []
    config.setdefault("execution_guard", {}).setdefault("model_anomaly_detection_rail", {})["enabled"] = False

    def no_hidden_retries(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "max_retries":
                    value[key] = 0
                else:
                    no_hidden_retries(item)
        elif isinstance(value, list):
            for item in value:
                no_hidden_retries(item)
    no_hidden_retries(config)
    return config


def public_config(config):
    """Credentials stay in private attempt configuration, never audit reports."""
    if isinstance(config, dict):
        return {key: ("<redacted>" if any(part in key.lower() for part in
                ("key", "token", "secret", "password", "authorization", "headers", "credential"))
                else public_config(value)) for key, value in config.items()}
    if isinstance(config, list):
        return [public_config(value) for value in config]
    return config


def config_digest(config: dict) -> str:
    return hashlib.sha256(json.dumps(public_config(config), sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()
