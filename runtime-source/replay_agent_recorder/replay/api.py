from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from .asyncio_patch import install_asyncio_patch, uninstall_asyncio_patch
from .context import ForkReusePolicy, RecordSession, ReplaySession
from .git_workspace import GitWorkspace, GitWorkspaceSpec
from .import_hook import install_import_hook, uninstall_import_hook
from .langchain_patch import install_langchain_patch
from .langgraph_patch import install_langgraph_patch
from .litellm_patch import install_litellm_patch, uninstall_litellm_patch
from .openai_patch import install_openai_patch, uninstall_openai_patch
from .tool_adapters import (
    BaseToolAdapter,
    ClassMethodToolAdapter,
    MappingToolAdapter,
    MethodToolAdapter,
    ToolAdapter,
)
from .tools import invoke_tool, invoke_tool_sync


_semantic_token = None
_patch_restorers: list = []


def install(
    *,
    semantic: bool = True,
    project_root: str | Path | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    langchain: bool = False,
    langgraph: bool = False,
) -> None:
    """Patch supported LLM providers, asyncio branches, and optional AST provenance."""

    global _semantic_token
    global _patch_restorers
    if semantic and _semantic_token is None:
        _semantic_token = install_import_hook(project_root or Path.cwd(), include=include, exclude=exclude)
    install_openai_patch()
    install_litellm_patch()
    install_asyncio_patch()
    if langchain:
        _patch_restorers.append(install_langchain_patch())
    if langgraph:
        _patch_restorers.append(install_langgraph_patch())


def wrap_runnable(obj: Any, *, framework: str = "auto") -> Any:
    """Best-effort wrapper for runnable-like objects imported by generated runners."""

    if framework in {"auto", "langgraph", "both"}:
        try:
            from .langgraph_patch import wrap_compiled_graph

            obj = wrap_compiled_graph(obj)
        except Exception:
            pass
    return obj


def uninstall() -> None:
    """Restore patched functions."""

    global _semantic_token
    global _patch_restorers
    for restore in reversed(_patch_restorers):
        restore()
    _patch_restorers = []
    uninstall_asyncio_patch()
    uninstall_litellm_patch()
    uninstall_openai_patch()
    if _semantic_token is not None:
        uninstall_import_hook(_semantic_token)
        _semantic_token = None


def record(
    run_id: str,
    *,
    log_dir: str | Path | None = None,
    overwrite: bool = True,
    workspaces: Mapping[str, GitWorkspaceSpec] | None = None,
    backtrack_history_mode: str = "deterministic",
    backtrack_selection_policy: str = "agent-direct",
    backtrack_selection_top_k: int = 5,
    backtrack_selection_delta: float = 0.7,
    backtrack_max_candidates: int = 80,
    backtrack_max_count: int = 0,
    backtrack_memory_policy: str = "cumulative",
    backtrack_context_policy: str = "rollback",
    backtrack_workspace_policy: str = "rollback",
    backtrack_summary_enabled: bool = False,
    backtrack_summary_max_input_chars: int = 80_000,
) -> RecordSession:
    return RecordSession(
        run_id=run_id,
        log_dir=log_dir,
        overwrite=overwrite,
        workspaces=workspaces,
        backtrack_history_mode=backtrack_history_mode,
        backtrack_selection_policy=backtrack_selection_policy,
        backtrack_selection_top_k=backtrack_selection_top_k,
        backtrack_selection_delta=backtrack_selection_delta,
        backtrack_max_candidates=backtrack_max_candidates,
        backtrack_max_count=backtrack_max_count,
        backtrack_memory_policy=backtrack_memory_policy,
        backtrack_context_policy=backtrack_context_policy,
        backtrack_workspace_policy=backtrack_workspace_policy,
        backtrack_summary_enabled=backtrack_summary_enabled,
        backtrack_summary_max_input_chars=backtrack_summary_max_input_chars,
    )


def replay(
    *,
    base_run: str,
    breakpoint_record_uid: str | None = None,
    override_output: str | None = None,
    override_input: dict[str, Any] | None = None,
    override_message: dict[str, Any] | None = None,
    log_dir: str | Path | None = None,
    fork_run: str | None = None,
    fork_log_mode: str = "delta",
    fork_metadata: dict[str, Any] | None = None,
    semantic_fallback: bool = False,
    fork_reuse_policy: ForkReusePolicy = "never",
    workspaces: Mapping[str, GitWorkspaceSpec] | None = None,
    workspace_symlink_policy_override: bool | None = None,
    backtrack_history_mode: str = "deterministic",
    backtrack_selection_policy: str = "agent-direct",
    backtrack_selection_top_k: int = 5,
    backtrack_selection_delta: float = 0.7,
    backtrack_max_candidates: int = 80,
    backtrack_max_count: int = 0,
    backtrack_memory_policy: str = "cumulative",
    backtrack_context_policy: str = "rollback",
    backtrack_workspace_policy: str = "rollback",
    backtrack_summary_enabled: bool = False,
    backtrack_summary_max_input_chars: int = 80_000,
) -> ReplaySession:
    return ReplaySession(
        base_run=base_run,
        breakpoint_record_uid=breakpoint_record_uid,
        override_output=override_output,
        override_input=override_input,
        override_message=override_message,
        log_dir=log_dir,
        fork_run=fork_run,
        fork_log_mode=fork_log_mode,
        fork_metadata=fork_metadata,
        semantic_fallback=semantic_fallback,
        fork_reuse_policy=fork_reuse_policy,
        workspaces=workspaces,
        workspace_symlink_policy_override=workspace_symlink_policy_override,
        backtrack_history_mode=backtrack_history_mode,
        backtrack_selection_policy=backtrack_selection_policy,
        backtrack_selection_top_k=backtrack_selection_top_k,
        backtrack_selection_delta=backtrack_selection_delta,
        backtrack_max_candidates=backtrack_max_candidates,
        backtrack_max_count=backtrack_max_count,
        backtrack_memory_policy=backtrack_memory_policy,
        backtrack_context_policy=backtrack_context_policy,
        backtrack_workspace_policy=backtrack_workspace_policy,
        backtrack_summary_enabled=backtrack_summary_enabled,
        backtrack_summary_max_input_chars=backtrack_summary_max_input_chars,
    )
