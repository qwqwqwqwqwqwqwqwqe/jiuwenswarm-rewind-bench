from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator, Literal, TypeVar

from .api import install, record, replay
from .backtrack import BacktrackRequested, prepare_backtrack_replay
from .context import DEFAULT_LOG_DIR, ForkReusePolicy
from .git_workspace import DEFAULT_WORKSPACE_EXCLUDES, GitWorkspace, GitWorkspaceSpec
from .git_workspace import WorkspaceRestorePolicy


ReplayMode = Literal["record", "replay", "none"]
ForkLogMode = Literal["delta", "materialized"]
BacktrackSelectionPolicy = Literal["agent-direct", "probabilistic-suffix"]
BacktrackMemoryPolicy = Literal["cumulative", "none"]
BacktrackContextPolicy = Literal["rollback", "preserve"]
BacktrackWorkspacePolicy = Literal["rollback", "preserve"]
T = TypeVar("T")
REPLAY_ORIGINAL_PYTHON_ARGV_ENV = "REPLAY_ORIGINAL_PYTHON_ARGV"
REPLAY_REEXEC_CONFIG_ENV = "REPLAY_REEXEC_CONFIG"
DEFAULT_WORKSPACE_ALLOW_SYMLINKS = True
DEFAULT_WORKSPACE_RESTORE_POLICY: WorkspaceRestorePolicy = "quiesce-then-retry"


@dataclass(frozen=True)
class ReplayRunConfig:
    """Normalized replay runtime options for integration wrapper scripts."""

    mode: ReplayMode = "record"
    run_id: str | None = None
    base_run: str | None = None
    base_run_explicit: bool = False
    fork_run: str | None = None
    fork_log_mode: ForkLogMode = "delta"
    log_dir: Path | None = None
    overwrite: bool = True
    breakpoint_record_uid: str | None = None
    override_output: str | None = None
    override_input: dict[str, Any] | None = None
    override_message: dict[str, Any] | None = None
    fork_reuse_policy: ForkReusePolicy = "never"
    semantic_fallback: bool = False
    semantic: bool = True
    project_root: Path | None = None
    include: tuple[str, ...] | None = None
    exclude: tuple[str, ...] | None = None
    workspace_id: str = "workspace"
    workspace_root: Path | None = None
    workspace_volatile_exclude: tuple[str, ...] | None = None
    workspace_allow_symlinks: bool | None = DEFAULT_WORKSPACE_ALLOW_SYMLINKS
    workspace_restore_policy: WorkspaceRestorePolicy = DEFAULT_WORKSPACE_RESTORE_POLICY
    workspace_path_aliases: tuple[str, ...] | None = None
    workspace_denormalize_root: str | None = None
    workspaces: Mapping[str, GitWorkspaceSpec] | None = None
    enable_backtrack: bool = False
    backtrack_history_mode: Literal["deterministic", "llm-summary"] = "deterministic"
    backtrack_selection_policy: BacktrackSelectionPolicy = "agent-direct"
    backtrack_selection_top_k: int = 5
    backtrack_selection_delta: float = 0.7
    backtrack_max_candidates: int = 80
    backtrack_max_count: int = 0
    backtrack_memory_policy: BacktrackMemoryPolicy = "cumulative"
    backtrack_context_policy: BacktrackContextPolicy = "rollback"
    backtrack_workspace_policy: BacktrackWorkspacePolicy = "rollback"
    backtrack_memory_limit: int = 20
    backtrack_summary_max_input_chars: int = 80_000
    backtrack_metadata: dict[str, Any] | None = None


def add_replay_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_mode: ReplayMode = "record",
    default_run_id: str | None = None,
    default_log_dir: Path | str | None = None,
    include_install_options: bool = True,
    include_workspace_options: bool = False,
    default_workspace_root: Path | str | None = None,
    default_workspace_id: str = "workspace",
) -> None:
    """Add the standard replay integration flags to an existing parser."""

    parser.add_argument(
        "--replay-mode",
        choices=("record", "replay", "none"),
        default=default_mode,
        help="Replay mode for the wrapped agent.",
    )
    parser.add_argument(
        "--run-id",
        default=default_run_id,
        help="Run id used in record mode. Also used as the replay base if --base-run is omitted.",
    )
    parser.add_argument(
        "--base-run",
        default=None,
        help="Base run id used in replay mode. Defaults to --run-id.",
    )
    parser.add_argument("--fork-run", default=None, help="Optional fork run id for replay mode.")
    parser.add_argument(
        "--fork-log-mode",
        choices=("delta", "materialized"),
        default="delta",
        help="How replay fork logs are written when a live branch is created.",
    )
    parser.add_argument(
        "--fork-reuse-policy",
        choices=("never", "safe"),
        default="never",
        help="Post-breakpoint fork reuse policy.",
    )
    parser.add_argument(
        "--breakpoint-record-uid",
        default=None,
        help="LLM record uid where replay should branch or apply an override.",
    )
    parser.add_argument("--override-output", default=None, help="Replacement assistant content at the breakpoint.")
    parser.add_argument("--override-input-json", default=None, help="JSON object merged into the breakpoint request.")
    parser.add_argument("--override-input-file", type=Path, default=None, help="File containing a JSON object for --override-input-json.")
    parser.add_argument("--override-message-json", default=None, help="JSON assistant message object at the breakpoint.")
    parser.add_argument(
        "--override-message-file",
        type=Path,
        default=None,
        help="File containing a JSON object for --override-message-json.",
    )
    parser.add_argument(
        "--replay-log-dir",
        type=Path,
        default=Path(default_log_dir) if default_log_dir is not None else None,
        help="Directory for Replay JSONL runs.",
    )
    if include_workspace_options:
        parser.add_argument(
            "--workspace-root",
            type=Path,
            default=Path(default_workspace_root) if default_workspace_root is not None else None,
            help=(
                "Workspace directory used directly for Replay filesystem checkpoints. "
                "Record, replay, and fork mutate this directory in place. "
                "Generated wrappers default this to --target-root."
            ),
        )
        parser.add_argument(
            "--workspace-id",
            default=default_workspace_id,
            help="Logical Git workspace id used in filesystem checkpoint metadata.",
        )
        parser.add_argument(
            "--workspace-include",
            action="append",
            default=None,
            help="Include glob for checkpointed workspace files; repeatable. Defaults to '**'.",
        )
        parser.add_argument(
            "--workspace-exclude",
            action="append",
            default=None,
            help="Additional exclude glob for checkpointed workspace files; repeatable.",
        )
        parser.add_argument(
            "--workspace-volatile-exclude",
            action="append",
            default=None,
            help=(
                "Additional volatile glob for workspace files. Volatile paths are not checkpointed "
                "and are removed when restoring direct workspaces; repeatable."
            ),
        )
        parser.add_argument(
            "--workspace-path-alias",
            action="append",
            default=None,
            help="Additional runtime path root that maps to this workspace, e.g. /workspace in Docker.",
        )
        parser.add_argument(
            "--workspace-denormalize-root",
            default=None,
            help="Preferred path root when replaying logical workspace paths, e.g. /workspace in Docker.",
        )
        parser.add_argument(
            "--workspace-allow-symlinks",
            action=argparse.BooleanOptionalAction,
            default=DEFAULT_WORKSPACE_ALLOW_SYMLINKS,
            help=(
                "Allow safe internal symlink objects in workspace checkpoints. "
                "Enabled by default; pass --no-workspace-allow-symlinks to disable."
            ),
        )
        parser.add_argument(
            "--workspace-restore-policy",
            choices=("generation-fallback", "quiesce-then-retry", "strict"),
            default=DEFAULT_WORKSPACE_RESTORE_POLICY,
            help="Policy for handling workspace restore failures. Defaults to quiesce-then-retry.",
        )
        parser.add_argument(
            "--no-workspace",
            action="store_true",
            help="Disable automatic Git workspace management in generated wrappers.",
        )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite an existing record-mode run file.",
    )
    parser.add_argument(
        "--semantic-fallback",
        action="store_true",
        help="Enable semantic fallback matching in replay mode.",
    )
    parser.add_argument("--enable-backtrack", action="store_true", help="Allow agent-triggered backtrack re-exec.")
    parser.add_argument(
        "--backtrack-history-mode",
        choices=("deterministic", "llm-summary"),
        default="deterministic",
        help="History mode used by backtrack_candidates.",
    )
    parser.add_argument(
        "--backtrack-selection-policy",
        choices=("agent-direct", "probabilistic-suffix"),
        default="agent-direct",
        help="How the agent commits a backtrack target after seeing candidates.",
    )
    parser.add_argument(
        "--backtrack-selection-top-k",
        type=int,
        default=5,
        help="Maximum number of candidate probability estimates accepted by probabilistic suffix selection.",
    )
    parser.add_argument(
        "--backtrack-selection-delta",
        type=float,
        default=0.7,
        help="Suffix-confidence threshold used by probabilistic suffix backtrack selection.",
    )
    parser.add_argument(
        "--backtrack-max-candidates",
        type=int,
        default=80,
        help="Default maximum candidate count for backtrack tools.",
    )
    parser.add_argument(
        "--backtrack-max-count",
        type=int,
        default=0,
        help="Maximum backtrack re-exec count for one runner chain.",
    )
    parser.add_argument(
        "--backtrack-memory-limit",
        type=int,
        default=20,
        help="Maximum number of recent backtrack memory entries injected at the breakpoint.",
    )
    parser.add_argument(
        "--backtrack-memory-policy",
        choices=("cumulative", "none"),
        default="cumulative",
        help="Whether backtrack forks receive the cumulative memory notebook.",
    )
    parser.add_argument(
        "--backtrack-context-policy",
        choices=("rollback", "preserve"),
        default="rollback",
        help="Whether backtrack forks rewind the agent LLM context to the selected breakpoint.",
    )
    parser.add_argument(
        "--backtrack-workspace-policy",
        choices=("rollback", "preserve"),
        default="rollback",
        help="Whether backtrack forks restore the Git workspace to the selected breakpoint.",
    )
    parser.add_argument(
        "--backtrack-summary-max-input-chars",
        type=int,
        default=80_000,
        help="Maximum raw segment JSON size sent to the backtrack summary model.",
    )
    parser.add_argument("--backtrack-metadata-file", type=Path, default=None, help=argparse.SUPPRESS)

    if include_install_options:
        parser.add_argument("--no-semantic", action="store_true", help="Disable AST provenance instrumentation.")
        parser.add_argument("--project-root", type=Path, default=None, help="Project root for AST provenance instrumentation.")
        parser.add_argument("--include", action="append", default=None, help="Include glob for AST instrumentation; repeatable.")
        parser.add_argument("--exclude", action="append", default=None, help="Exclude glob for AST instrumentation; repeatable.")


def config_from_args(
    args: argparse.Namespace,
    *,
    default_run_id: str | None = None,
    default_log_dir: Path | str | None = None,
    default_project_root: Path | str | None = None,
    default_workspace_root: Path | str | None = None,
) -> ReplayRunConfig:
    """Build and validate a ReplayRunConfig from argparse output."""

    ensure_original_python_argv()

    mode = getattr(args, "replay_mode", "record")
    if mode not in {"record", "replay", "none"}:
        raise ValueError(f"Unsupported replay mode: {mode!r}")

    run_id = getattr(args, "run_id", None) or default_run_id
    base_run_arg = getattr(args, "base_run", None)
    base_run = base_run_arg or run_id
    base_run_explicit = base_run_arg is not None
    log_dir = getattr(args, "replay_log_dir", None)
    if log_dir is None and default_log_dir is not None:
        log_dir = Path(default_log_dir)
    log_dir = Path(log_dir).resolve() if log_dir is not None else None

    override_input = load_json_object_option(
        getattr(args, "override_input_json", None),
        getattr(args, "override_input_file", None),
        label="override-input",
    )
    override_message = load_json_object_option(
        getattr(args, "override_message_json", None),
        getattr(args, "override_message_file", None),
        label="override-message",
    )
    backtrack_metadata = load_json_object_option(
        None,
        getattr(args, "backtrack_metadata_file", None),
        label="backtrack-metadata",
    )
    override_output = getattr(args, "override_output", None)
    _validate_run_options(
        mode=mode,
        run_id=run_id,
        base_run=base_run,
        breakpoint_record_uid=getattr(args, "breakpoint_record_uid", None),
        override_output=override_output,
        override_input=override_input,
        override_message=override_message,
    )

    project_root = getattr(args, "project_root", None)
    if project_root is None and default_project_root is not None:
        project_root = Path(default_project_root)

    workspace_id = str(getattr(args, "workspace_id", "workspace") or "workspace")
    if not workspace_id:
        raise ValueError("--workspace-id must not be empty.")
    workspace_root_value = getattr(args, "workspace_root", None)
    if workspace_root_value is None and default_workspace_root is not None:
        workspace_root_value = Path(default_workspace_root)
    workspace_root = Path(workspace_root_value).resolve() if workspace_root_value is not None else None
    workspace_allow_symlinks = getattr(args, "workspace_allow_symlinks", DEFAULT_WORKSPACE_ALLOW_SYMLINKS)
    workspace_restore_policy = getattr(args, "workspace_restore_policy", DEFAULT_WORKSPACE_RESTORE_POLICY)
    workspace_path_aliases = _tuple_or_none(getattr(args, "workspace_path_alias", None))
    workspace_denormalize_root = getattr(args, "workspace_denormalize_root", None)
    workspaces = _build_workspace_specs(
        mode=mode,
        run_id=run_id,
        base_run=base_run,
        fork_run=getattr(args, "fork_run", None),
        log_dir=log_dir if log_dir is not None else DEFAULT_LOG_DIR,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        include=_tuple_or_none(getattr(args, "workspace_include", None)),
        extra_exclude=_tuple_or_none(getattr(args, "workspace_exclude", None)),
        volatile_exclude=_tuple_or_none(getattr(args, "workspace_volatile_exclude", None)),
        allow_symlinks=workspace_allow_symlinks,
        restore_policy=workspace_restore_policy,
        path_aliases=workspace_path_aliases,
        denormalize_root=workspace_denormalize_root,
        disabled=bool(getattr(args, "no_workspace", False)),
    )
    workspace_volatile_exclude = _tuple_or_none(getattr(args, "workspace_volatile_exclude", None))

    config = ReplayRunConfig(
        mode=mode,
        run_id=run_id,
        base_run=base_run,
        base_run_explicit=base_run_explicit,
        fork_run=getattr(args, "fork_run", None),
        fork_log_mode=getattr(args, "fork_log_mode", "delta"),
        log_dir=log_dir,
        overwrite=not bool(getattr(args, "no_overwrite", False)),
        breakpoint_record_uid=getattr(args, "breakpoint_record_uid", None),
        override_output=override_output,
        override_input=override_input,
        override_message=override_message,
        fork_reuse_policy=getattr(args, "fork_reuse_policy", "never"),
        semantic_fallback=bool(getattr(args, "semantic_fallback", False)),
        semantic=not bool(getattr(args, "no_semantic", False)),
        project_root=Path(project_root) if project_root is not None else None,
        include=_tuple_or_none(getattr(args, "include", None)),
        exclude=_tuple_or_none(getattr(args, "exclude", None)),
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        workspace_volatile_exclude=workspace_volatile_exclude,
        workspace_allow_symlinks=workspace_allow_symlinks,
        workspace_restore_policy=workspace_restore_policy,
        workspace_path_aliases=workspace_path_aliases,
        workspace_denormalize_root=workspace_denormalize_root,
        workspaces=workspaces,
        enable_backtrack=bool(getattr(args, "enable_backtrack", False)),
        backtrack_history_mode=getattr(args, "backtrack_history_mode", "deterministic"),
        backtrack_selection_policy=getattr(args, "backtrack_selection_policy", "agent-direct"),
        backtrack_selection_top_k=int(getattr(args, "backtrack_selection_top_k", 5)),
        backtrack_selection_delta=float(getattr(args, "backtrack_selection_delta", 0.7)),
        backtrack_max_candidates=int(getattr(args, "backtrack_max_candidates", 80)),
        backtrack_max_count=int(getattr(args, "backtrack_max_count", 0)),
        backtrack_memory_policy=getattr(args, "backtrack_memory_policy", "cumulative"),
        backtrack_context_policy=getattr(args, "backtrack_context_policy", "rollback"),
        backtrack_workspace_policy=getattr(args, "backtrack_workspace_policy", "rollback"),
        backtrack_memory_limit=int(getattr(args, "backtrack_memory_limit", 20)),
        backtrack_summary_max_input_chars=int(getattr(args, "backtrack_summary_max_input_chars", 80_000)),
        backtrack_metadata=backtrack_metadata,
    )
    return apply_reexec_config_overlay(config)


def load_json_object_option(
    raw: str | None,
    file_path: Path | str | None,
    *,
    label: str,
) -> dict[str, Any] | None:
    """Load one JSON object from either a direct string option or a file option."""

    if raw and file_path:
        raise ValueError(f"--{label}-json and --{label}-file are mutually exclusive.")
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Failed to read --{label}-file {file_path!r}: {exc}") from exc
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--{label} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"--{label} must decode to a JSON object.")
    return value


def ensure_original_python_argv() -> list[str]:
    """Capture the original Python launch vector for opaque re-exec."""

    existing = os.environ.get(REPLAY_ORIGINAL_PYTHON_ARGV_ENV)
    if existing:
        return _load_argv_json(existing, REPLAY_ORIGINAL_PYTHON_ARGV_ENV)

    original_argv = getattr(sys, "orig_argv", None)
    if isinstance(original_argv, list) and original_argv:
        argv = [sys.executable, *(str(value) for value in original_argv[1:])]
    else:
        argv = [sys.executable, *(str(value) for value in sys.argv)]
    os.environ[REPLAY_ORIGINAL_PYTHON_ARGV_ENV] = json.dumps(argv, ensure_ascii=False)
    return argv


def original_python_argv() -> list[str]:
    existing = os.environ.get(REPLAY_ORIGINAL_PYTHON_ARGV_ENV)
    if not existing:
        return ensure_original_python_argv()
    return _load_argv_json(existing, REPLAY_ORIGINAL_PYTHON_ARGV_ENV)


def build_backtrack_reexec_config(replay_args: dict[str, Any]) -> dict[str, Any]:
    """Return an env-serializable replay overlay for a backtrack fork."""

    override_input_file = Path(replay_args["override_input_file"])
    return {
        "mode": "replay",
        "base_run": str(replay_args["base_run"]),
        "base_run_explicit": True,
        "log_dir": str(override_input_file.parent),
        "breakpoint_record_uid": str(replay_args["breakpoint_record_uid"]),
        "override_output": None,
        "override_input_file": str(override_input_file),
        "override_message": None,
        "fork_run": str(replay_args["fork_run"]),
        "fork_log_mode": "materialized",
        "enable_backtrack": True,
        "backtrack_memory_policy": replay_args.get("backtrack_memory_policy", "cumulative"),
        "backtrack_context_policy": replay_args.get("backtrack_context_policy", "rollback"),
        "backtrack_workspace_policy": replay_args.get("backtrack_workspace_policy", "rollback"),
        "backtrack_memory_limit": replay_args.get("backtrack_memory_limit", 20),
        "backtrack_metadata_file": str(replay_args["backtrack_metadata_file"]),
    }


def load_reexec_config_from_env() -> dict[str, Any]:
    """Load and normalize the pending re-exec overlay from the environment."""

    raw = os.environ.get(REPLAY_REEXEC_CONFIG_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{REPLAY_REEXEC_CONFIG_ENV} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{REPLAY_REEXEC_CONFIG_ENV} must decode to a JSON object.")

    overlay: dict[str, Any] = {}
    config_fields = set(ReplayRunConfig.__dataclass_fields__)
    file_keys = {
        "override_input_file": ("override_input", "override-input"),
        "override_message_file": ("override_message", "override-message"),
        "backtrack_metadata_file": ("backtrack_metadata", "backtrack-metadata"),
    }
    for key, value in parsed.items():
        if key in file_keys:
            target_key, label = file_keys[key]
            overlay[target_key] = load_json_object_option(None, value, label=label)
            continue
        if key not in config_fields:
            raise ValueError(f"Unsupported {REPLAY_REEXEC_CONFIG_ENV} key: {key}")
        if key in {"log_dir", "project_root", "workspace_root"}:
            overlay[key] = Path(value) if value is not None else None
        elif key in {"include", "exclude", "workspace_volatile_exclude", "workspace_path_aliases"}:
            overlay[key] = tuple(value) if value is not None else None
        else:
            overlay[key] = value
    return overlay


def apply_reexec_config_overlay(config: ReplayRunConfig) -> ReplayRunConfig:
    overlay = load_reexec_config_from_env()
    if not overlay:
        return config
    merged = replace(config, **overlay)
    if (
        (
            "workspace_volatile_exclude" in overlay
            or "workspace_allow_symlinks" in overlay
            or "workspace_restore_policy" in overlay
            or "workspace_path_aliases" in overlay
            or "workspace_denormalize_root" in overlay
        )
        and merged.workspaces is not None
    ):
        volatile_exclude = merged.workspace_volatile_exclude or ()
        allow_symlinks = merged.workspace_allow_symlinks
        restore_policy = merged.workspace_restore_policy
        path_aliases = merged.workspace_path_aliases or ()
        denormalize_root = merged.workspace_denormalize_root
        new_workspaces: dict[str, GitWorkspaceSpec] = {}
        for workspace_id, spec in merged.workspaces.items():
            updates: dict[str, Any] = {}
            if "workspace_volatile_exclude" in overlay:
                updates["volatile_exclude"] = volatile_exclude
            if "workspace_allow_symlinks" in overlay and allow_symlinks is not None:
                updates["allow_symlinks"] = allow_symlinks
            if "workspace_restore_policy" in overlay:
                updates["restore_policy"] = restore_policy
            if "workspace_path_aliases" in overlay:
                updates["path_aliases"] = path_aliases
            if "workspace_denormalize_root" in overlay:
                updates["path_denormalize_root"] = denormalize_root
            new_workspaces[workspace_id] = replace(spec, **updates) if updates else spec
        merged = replace(
            merged,
            workspaces=new_workspaces,
        )
    _validate_run_options(
        mode=merged.mode,
        run_id=merged.run_id,
        base_run=merged.base_run,
        breakpoint_record_uid=merged.breakpoint_record_uid,
        override_output=merged.override_output,
        override_input=merged.override_input,
        override_message=merged.override_message,
    )
    return merged


def _load_argv_json(raw: str, label: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON list: {exc}") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must decode to a non-empty JSON list of strings.")
    return value


def install_from_config(config: ReplayRunConfig) -> None:
    """Install replay instrumentation using normalized integration options."""

    if config.mode == "none":
        return
    install(
        semantic=config.semantic,
        project_root=config.project_root,
        include=config.include,
        exclude=config.exclude,
    )


@contextmanager
def replay_session(config: ReplayRunConfig) -> Iterator[Any]:
    """Open a record/replay/no-op session for a wrapper script."""

    if config.mode == "none":
        with nullcontext():
            yield None
        return

    if config.mode == "record":
        if not config.run_id:
            raise ValueError("run_id is required in record mode.")
        with record(
            config.run_id,
            log_dir=config.log_dir,
            overwrite=config.overwrite,
            workspaces=config.workspaces,
            backtrack_history_mode=config.backtrack_history_mode,
            backtrack_selection_policy=config.backtrack_selection_policy,
            backtrack_selection_top_k=config.backtrack_selection_top_k,
            backtrack_selection_delta=config.backtrack_selection_delta,
            backtrack_max_candidates=config.backtrack_max_candidates,
            backtrack_max_count=config.backtrack_max_count,
            backtrack_memory_policy=config.backtrack_memory_policy,
            backtrack_context_policy=config.backtrack_context_policy,
            backtrack_workspace_policy=config.backtrack_workspace_policy,
            backtrack_summary_enabled=_backtrack_summary_enabled(config),
            backtrack_summary_max_input_chars=config.backtrack_summary_max_input_chars,
        ) as session:
            yield session
        return

    if config.mode == "replay":
        if not config.base_run:
            raise ValueError("base_run is required in replay mode.")
        with replay(
            base_run=config.base_run,
            breakpoint_record_uid=config.breakpoint_record_uid,
            override_output=config.override_output,
            override_input=config.override_input,
            override_message=config.override_message,
            log_dir=config.log_dir,
            fork_run=config.fork_run,
            fork_log_mode=config.fork_log_mode,
            fork_metadata=_fork_metadata_from_config(config),
            semantic_fallback=config.semantic_fallback,
            fork_reuse_policy=config.fork_reuse_policy,
            workspaces=config.workspaces,
            workspace_symlink_policy_override=config.workspace_allow_symlinks,
            backtrack_history_mode=config.backtrack_history_mode,
            backtrack_selection_policy=config.backtrack_selection_policy,
            backtrack_selection_top_k=config.backtrack_selection_top_k,
            backtrack_selection_delta=config.backtrack_selection_delta,
            backtrack_max_candidates=config.backtrack_max_candidates,
            backtrack_max_count=config.backtrack_max_count,
            backtrack_memory_policy=config.backtrack_memory_policy,
            backtrack_context_policy=config.backtrack_context_policy,
            backtrack_workspace_policy=config.backtrack_workspace_policy,
            backtrack_summary_enabled=_backtrack_summary_enabled(config),
            backtrack_summary_max_input_chars=config.backtrack_summary_max_input_chars,
        ) as session:
            yield session
        return

    raise ValueError(f"Unsupported replay mode: {config.mode!r}")


def handle_backtrack_request(
    request: BacktrackRequested,
    *,
    config: ReplayRunConfig,
    argv: list[str] | None = None,
    reexec: bool = True,
) -> list[str]:
    """Prepare a materialized fork replay overlay and optionally replace the runner process."""

    if not config.enable_backtrack:
        raise request
    original_argv = ensure_original_python_argv()
    count = int(os.environ.get("REPLAY_BACKTRACK_COUNT", "0"))
    if config.backtrack_max_count > 0 and count >= config.backtrack_max_count:
        raise RuntimeError(
            f"Backtrack limit exceeded: {count} >= {config.backtrack_max_count}."
        ) from request

    current_run_id = request.run_id or current_timeline_run_id(config)
    replay_args = prepare_backtrack_replay(
        request,
        current_run_id=current_run_id,
        log_dir=request.log_dir or config.log_dir,
        memory_limit=config.backtrack_memory_limit,
        memory_policy=config.backtrack_memory_policy,
        context_policy=config.backtrack_context_policy,
        workspace_policy=config.backtrack_workspace_policy,
    )
    reexec_config = build_backtrack_reexec_config(replay_args)
    reexec_config["backtrack_history_mode"] = config.backtrack_history_mode
    reexec_config["backtrack_selection_policy"] = config.backtrack_selection_policy
    reexec_config["backtrack_selection_top_k"] = config.backtrack_selection_top_k
    reexec_config["backtrack_selection_delta"] = config.backtrack_selection_delta
    reexec_config["backtrack_memory_policy"] = config.backtrack_memory_policy
    reexec_config["backtrack_context_policy"] = config.backtrack_context_policy
    reexec_config["backtrack_workspace_policy"] = config.backtrack_workspace_policy
    reexec_config["backtrack_memory_limit"] = config.backtrack_memory_limit
    reexec_config["backtrack_summary_max_input_chars"] = config.backtrack_summary_max_input_chars
    reexec_config["backtrack_max_count"] = config.backtrack_max_count
    reexec_config["workspace_restore_policy"] = config.workspace_restore_policy
    os.environ[REPLAY_REEXEC_CONFIG_ENV] = json.dumps(reexec_config, ensure_ascii=False)
    if reexec:
        os.environ["REPLAY_BACKTRACK_COUNT"] = str(count + 1)
        exec_argv = argv or original_argv
        os.execv(exec_argv[0], exec_argv)
    return original_argv


def current_timeline_run_id(config: ReplayRunConfig) -> str:
    if config.mode == "record" and config.run_id:
        return config.run_id
    if config.mode == "replay" and config.fork_run:
        return config.fork_run
    if config.mode == "replay" and config.base_run:
        return config.base_run
    if config.run_id:
        return config.run_id
    raise ValueError("Cannot determine current replay timeline run id.")


def build_backtrack_reexec_argv(
    argv: list[str],
    replay_args: dict[str, Any],
) -> list[str]:
    if not argv:
        argv = [sys.argv[0]]
    runner_values, target_values = split_target_remainder(argv[1:])
    replay_values = [
        "--replay-mode",
        "replay",
        "--base-run",
        str(replay_args["base_run"]),
        "--breakpoint-record-uid",
        str(replay_args["breakpoint_record_uid"]),
        "--override-input-file",
        str(replay_args["override_input_file"]),
        "--fork-run",
        str(replay_args["fork_run"]),
        "--fork-log-mode",
        "materialized",
        "--enable-backtrack",
        "--backtrack-memory-policy",
        str(replay_args.get("backtrack_memory_policy", "cumulative")),
        "--backtrack-context-policy",
        str(replay_args.get("backtrack_context_policy", "rollback")),
        "--backtrack-workspace-policy",
        str(replay_args.get("backtrack_workspace_policy", "rollback")),
        "--backtrack-memory-limit",
        str(replay_args.get("backtrack_memory_limit", 20)),
        "--backtrack-metadata-file",
        str(replay_args["backtrack_metadata_file"]),
    ]
    return [argv[0], *strip_replay_option_values(runner_values), *replay_values, *target_values]


def split_target_remainder(values: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in values:
        return values, []
    separator_index = values.index("--")
    return values[:separator_index], values[separator_index:]


def strip_replay_option_values(values: list[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    remove_with_values = {
        "--replay-mode",
        "--run-id",
        "--base-run",
        "--fork-run",
        "--fork-log-mode",
        "--breakpoint-record-uid",
        "--override-output",
        "--override-input-json",
        "--override-input-file",
        "--override-message-json",
        "--override-message-file",
        "--backtrack-history-mode",
        "--backtrack-selection-policy",
        "--backtrack-selection-top-k",
        "--backtrack-selection-delta",
        "--backtrack-max-candidates",
        "--backtrack-max-count",
        "--backtrack-memory-limit",
        "--backtrack-memory-policy",
        "--backtrack-context-policy",
        "--backtrack-workspace-policy",
        "--backtrack-summary-max-input-chars",
        "--backtrack-metadata-file",
    }
    remove_flags = {"--enable-backtrack"}
    for value in values:
        if skip_next:
            skip_next = False
            continue
        if value == "--":
            stripped.append(value)
            continue
        option = value.split("=", 1)[0]
        if option in remove_with_values:
            skip_next = "=" not in value
            continue
        if option in remove_flags:
            continue
        stripped.append(value)
    return stripped


def _fork_metadata_from_config(config: ReplayRunConfig) -> dict[str, Any] | None:
    if config.backtrack_metadata is not None:
        return dict(config.backtrack_metadata)
    return None


def _backtrack_summary_enabled(config: ReplayRunConfig) -> bool:
    return config.enable_backtrack and config.backtrack_history_mode == "llm-summary"


def runtime_entry_for_workspace(
    entry: Any,
    config: ReplayRunConfig,
    session: Any,
) -> tuple[Any, Path | None]:
    """Return an entry adjusted to run inside the configured workspace root.

    In direct workspace mode, ``config.workspace_root`` is the runtime work
    tree for filesystem checkpoints. The target cwd still defaults to the
    target root so wrapped agents run from their own checkout by default.
    The path mapping is kept for callers that still provide a custom
    GitWorkspaceSpec with a separate work_root.
    """

    workspace = workspace_from_session(config, session)
    if workspace is None:
        return entry, None

    workspace_root = workspace.root
    source_root = config.workspace_root.resolve() if config.workspace_root is not None else None
    original_target_root = Path(getattr(entry, "target_root")).resolve()
    runtime_target_root = Path(getattr(entry, "target_root"))
    if source_root is not None:
        runtime_target_root = _map_path_from_source(
            original_target_root,
            source_root=source_root,
            workspace_root=workspace_root,
        ) or runtime_target_root
    runtime_entry_ref = _runtime_entry_ref(
        getattr(entry, "entry"),
        source_root=source_root,
        workspace_root=workspace_root,
    )

    runtime_target_cwd = _runtime_target_cwd(
        getattr(entry, "target_cwd", None),
        target_root=runtime_target_root,
        workspace_root=workspace_root,
        source_root=source_root,
    )
    return replace(
        entry,
        entry=runtime_entry_ref,
        target_root=runtime_target_root,
        target_cwd=runtime_target_cwd,
    ), workspace_root


def runtime_config_for_workspace(
    config: ReplayRunConfig,
    original_entry: Any,
    runtime_entry: Any,
) -> ReplayRunConfig:
    """Adjust instrumentation roots when a custom workspace remaps target roots."""

    if config.project_root is None:
        return config
    original_target_root = Path(getattr(original_entry, "target_root")).resolve()
    runtime_target_root = Path(getattr(runtime_entry, "target_root")).resolve()
    if _same_path(config.project_root, original_target_root) and not _same_path(
        runtime_target_root,
        original_target_root,
    ):
        return replace(config, project_root=runtime_target_root)
    return config


def workspace_from_session(config: ReplayRunConfig, session: Any) -> GitWorkspace | None:
    """Return the configured workspace for an active session, if any."""

    if session is None or not config.workspaces:
        return None
    return session.workspace(config.workspace_id)


@contextmanager
def workspace_runtime_environment(
    config: ReplayRunConfig,
    session: Any,
) -> Iterator[GitWorkspace | None]:
    """Expose the active workspace path to unmodified target agents."""

    workspace = workspace_from_session(config, session)
    if workspace is None:
        yield None
        return

    values = {
        "REPLAY_WORKSPACE_ID": workspace.workspace_id,
        "REPLAY_WORKSPACE_ROOT": str(workspace.root),
        "REPLAY_WORKSPACE_LOGICAL_ROOT": workspace.logical_root,
    }
    if workspace.spec.path_denormalize_root:
        values["REPLAY_CONTAINER_WORKSPACE_ROOT"] = workspace.spec.path_denormalize_root
    if not workspace.spec.direct or workspace.runtime_git_mode == "replay-managed":
        values.update(
            {
                "GIT_DIR": str(workspace.repo_dir),
                "GIT_WORK_TREE": str(workspace.root),
                "GIT_INDEX_FILE": str(workspace.index_file),
            }
        )
    restore_keys = set(values) | {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    previous = {key: os.environ.get(key) for key in restore_keys}
    try:
        os.environ.update(values)
        yield workspace
    finally:
        for key, old_value in reversed(list(previous.items())):
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _runtime_target_cwd(
    target_cwd: Path | str | None,
    *,
    target_root: Path,
    workspace_root: Path,
    source_root: Path | None,
) -> Path:
    if target_cwd is None:
        return target_root.resolve()
    cwd = Path(target_cwd)
    if not cwd.is_absolute():
        return (workspace_root / cwd).resolve()
    resolved = cwd.resolve()
    if source_root is not None:
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            return resolved
        return (workspace_root / relative).resolve()
    return resolved


def _runtime_entry_ref(
    entry_ref: Any,
    *,
    source_root: Path | None,
    workspace_root: Path,
) -> str:
    text = str(entry_ref)
    if source_root is None:
        return text
    path = Path(text)
    if not path.is_absolute():
        return text
    mapped = _map_path_from_source(path, source_root=source_root, workspace_root=workspace_root)
    return str(mapped) if mapped is not None else text


def _map_path_from_source(
    path: Path | str,
    *,
    source_root: Path,
    workspace_root: Path,
) -> Path | None:
    resolved = Path(path).resolve()
    source = source_root.resolve()
    try:
        relative = resolved.relative_to(source)
    except ValueError:
        return None
    return (workspace_root / relative).resolve()


def _same_path(left: Path | str, right: Path | str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def _build_workspace_specs(
    *,
    mode: str,
    run_id: str | None,
    base_run: str | None,
    fork_run: str | None,
    log_dir: Path,
    workspace_root: Path | None,
    workspace_id: str,
    include: tuple[str, ...] | None,
    extra_exclude: tuple[str, ...] | None,
    volatile_exclude: tuple[str, ...] | None,
    allow_symlinks: bool | None,
    restore_policy: WorkspaceRestorePolicy,
    path_aliases: tuple[str, ...] | None,
    denormalize_root: str | None,
    disabled: bool,
) -> Mapping[str, GitWorkspaceSpec] | None:
    if disabled or mode == "none" or workspace_root is None:
        return None
    log_root = Path(log_dir).resolve()
    return {
        workspace_id: GitWorkspaceSpec(
            base_root=None,
            work_root=workspace_root,
            include=include or ("**",),
            exclude=_workspace_excludes(
                workspace_root=workspace_root,
                log_dir=log_root,
                extra_exclude=extra_exclude,
            ),
            volatile_exclude=volatile_exclude or (),
            initial="work_root",
            allow_symlinks=bool(allow_symlinks),
            allow_project_root=True,
            allow_nested_git=True,
            direct=True,
            restore_policy=restore_policy,
            path_aliases=path_aliases or (),
            path_denormalize_root=denormalize_root,
        )
    }


def _workspace_excludes(
    *,
    workspace_root: Path,
    log_dir: Path,
    extra_exclude: tuple[str, ...] | None,
) -> tuple[str, ...]:
    patterns = list(DEFAULT_WORKSPACE_EXCLUDES)
    if _contains_or_equals(workspace_root, log_dir):
        rel = log_dir.relative_to(workspace_root).as_posix()
        patterns.append(rel)
        patterns.append(f"{rel}/**")
    if extra_exclude:
        patterns.extend(extra_exclude)
    return tuple(dict.fromkeys(patterns))


def _contains_or_equals(parent: Path, child: Path) -> bool:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    return child_resolved == parent_resolved or parent_resolved in child_resolved.parents


def run_with_replay(
    target: Callable[[], T],
    config: ReplayRunConfig,
    *,
    install_instrumentation: bool = True,
) -> T:
    """Run a synchronous target inside the configured replay session."""

    if install_instrumentation:
        install_from_config(config)
    with replay_session(config):
        return target()


async def run_async_with_replay(
    target: Callable[[], Awaitable[T] | T],
    config: ReplayRunConfig,
    *,
    install_instrumentation: bool = True,
) -> T:
    """Run a synchronous or asynchronous target inside the configured replay session."""

    if install_instrumentation:
        install_from_config(config)
    with replay_session(config):
        result = target()
        if inspect.isawaitable(result):
            return await result
        return result


def _validate_run_options(
    *,
    mode: str,
    run_id: str | None,
    base_run: str | None,
    breakpoint_record_uid: str | None,
    override_output: str | None,
    override_input: dict[str, Any] | None,
    override_message: dict[str, Any] | None,
) -> None:
    if mode == "record" and not run_id:
        raise ValueError("--run-id is required in record mode unless a wrapper default is provided.")
    if mode == "replay" and not base_run:
        raise ValueError("--base-run or --run-id is required in replay mode.")

    override_count = sum(
        value is not None
        for value in (override_output, override_input, override_message)
    )
    if override_count > 1:
        raise ValueError(
            "--override-output, --override-input-json/--override-input-file, and "
            "--override-message-json/--override-message-file are mutually exclusive."
        )
    if override_count and not breakpoint_record_uid:
        raise ValueError("--breakpoint-record-uid is required when using an override.")


def _tuple_or_none(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    return tuple(values)
