from __future__ import annotations

import copy
import asyncio
import contextvars
import inspect
import threading
import time
from collections.abc import Callable, Mapping
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from .edges import make_edge_records, provenance_to_json
from .errors import InvalidBreakpointError, ReplayMissError, UnsupportedOverrideInputError
from .git_workspace import GitCheckpoint, GitWorkspace, GitWorkspaceSpec
from .ids import build_input_record, compute_input_id
from .response import (
    build_override_message_response,
    build_override_response,
    response_from_record,
    response_to_record,
)
from .semantic_runtime import RUNTIME, Source, seed_response
from .storage import (
    JsonlWriter,
    load_raw_records,
    load_records,
    load_replay_records,
    load_workspace_records,
    make_record_uid,
    next_fork_run_id,
    reject_legacy_filesystem_schema,
    run_path,
)
from .backtrack_summary import BacktrackSummaryManager, DEFAULT_SUMMARY_MAX_INPUT_CHARS
from .tools import (
    replay_tool_record,
    tool_error_to_record,
    tool_output_to_record,
)
from .workspace_manager import FilesystemNodeContext, GitWorkspaceManager


DEFAULT_BRANCH_ID = "root"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "runs"
ForkReusePolicy = Literal["never", "safe"]
FORK_LOG_MODES = {"delta", "materialized"}

current_branch_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "replay_current_branch_id",
    default=DEFAULT_BRANCH_ID,
)
_current_session: contextvars.ContextVar["BaseSession | None"] = contextvars.ContextVar(
    "replay_current_session",
    default=None,
)
_current_replay_record: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "replay_current_replay_record",
    default=None,
)
branch_allocation_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "replay_branch_allocation_suppressed",
    default=False,
)


@dataclass(frozen=True)
class PreparedCall:
    input_record: dict[str, Any]
    input_id: str
    path_id: str
    provenance: dict[str, set[Source]]
    semantic: dict[str, Any] | None


@dataclass
class PendingRecord:
    fs_ctx: FilesystemNodeContext
    record: dict[str, Any]
    owner: tuple[str, int]
    edge_records: list[dict[str, Any]] | None = None
    extra_records_factory: Any = None
    decision: dict[str, Any] | None = None
    source: Source | None = None
    branch_id: str | None = None
    previous_post_fork_live_mode: bool | None = None
    previous_branch_mode: str | None = None
    previous_branch_dirty: bool | None = None


def current_pending_owner() -> tuple[str, int]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return ("task", id(task))
    return ("thread", threading.get_ident())


def get_current_session() -> "BaseSession | None":
    return _current_session.get()


def current_replay_record() -> dict[str, Any] | None:
    """Return the matched base record currently being live-prefix replayed."""

    return _current_replay_record.get()


def resolve_metadata(
    metadata: Mapping[str, Any] | None,
    metadata_factory: Callable[[], Mapping[str, Any] | None] | None,
) -> dict[str, Any] | None:
    resolved = dict(metadata or {})
    if metadata_factory is not None:
        produced = metadata_factory()
        if produced:
            resolved.update(dict(produced))
    return resolved or None


def with_replay_live_prefix_metadata(
    metadata: dict[str, Any] | None,
    replay_live_prefix: bool,
) -> dict[str, Any] | None:
    if not replay_live_prefix:
        return metadata
    resolved = dict(metadata or {})
    resolved["replay_live_prefix"] = True
    return resolved


class BaseSession:
    def __init__(
        self,
        *,
        log_dir: str | Path | None = None,
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
    ) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.workspace_manager = GitWorkspaceManager(
            specs=workspaces or {},
            log_dir=self.log_dir,
            symlink_policy_override=workspace_symlink_policy_override,
        )
        self.lock = threading.RLock()
        self.branch_child_indexes: dict[str, int] = defaultdict(int)
        self.branch_llm_indexes: dict[str, int] = defaultdict(int)
        self.branch_tool_indexes: dict[str, int] = defaultdict(int)
        self.branch_parents: dict[str, str] = {}
        self._session_token: contextvars.Token | None = None
        self._branch_token: contextvars.Token | None = None
        self._runtime_token: Any = None
        self.backtrack_history_mode = backtrack_history_mode
        self.backtrack_selection_policy = backtrack_selection_policy
        self.backtrack_selection_top_k = backtrack_selection_top_k
        self.backtrack_selection_delta = backtrack_selection_delta
        self.backtrack_max_candidates = backtrack_max_candidates
        self.backtrack_max_count = backtrack_max_count
        self.backtrack_memory_policy = backtrack_memory_policy
        self.backtrack_context_policy = backtrack_context_policy
        self.backtrack_workspace_policy = backtrack_workspace_policy
        self.last_backtrack_candidates: dict[str, Any] | None = None
        self.next_tool_names: list[str] | None = None
        self._applied_override_messages: list[dict[str, Any]] = []

    def __enter__(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_token = RUNTIME.enter_context(enabled=RUNTIME.enabled)
        self._session_token = _current_session.set(self)
        self._branch_token = current_branch_id.set(DEFAULT_BRANCH_ID)
        try:
            self.on_enter()
        except Exception:
            self._cleanup_enter_failure()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.on_exit()
        finally:
            if self._branch_token is not None:
                current_branch_id.reset(self._branch_token)
            if self._session_token is not None:
                _current_session.reset(self._session_token)
            if self._runtime_token is not None:
                RUNTIME.exit_context(self._runtime_token)

    def on_enter(self) -> None:
        pass

    def on_exit(self) -> None:
        pass

    def remember_applied_override_messages(self, messages: Any) -> None:
        from .backtrack import is_backtrack_memory_message

        if not isinstance(messages, list):
            self._applied_override_messages = []
            return
        self._applied_override_messages = [
            copy.deepcopy(message)
            for message in messages
            if is_backtrack_memory_message(message)
        ]

    def consume_applied_override_messages(self) -> list[dict[str, Any]]:
        messages = self._applied_override_messages
        self._applied_override_messages = []
        return messages

    def restrict_next_tools(self, tool_names: list[str]) -> None:
        self.next_tool_names = list(tool_names)

    def peek_next_tool_names(self) -> list[str] | None:
        tool_names = self.next_tool_names
        return list(tool_names) if isinstance(tool_names, list) else None

    def clear_next_tool_names(self) -> None:
        self.next_tool_names = None

    def _cleanup_enter_failure(self) -> None:
        try:
            self.on_exit()
        finally:
            if self._branch_token is not None:
                current_branch_id.reset(self._branch_token)
                self._branch_token = None
            if self._session_token is not None:
                _current_session.reset(self._session_token)
                self._session_token = None
            if self._runtime_token is not None:
                RUNTIME.exit_context(self._runtime_token)
                self._runtime_token = None

    def workspace(self, workspace_id: str = "workspace") -> GitWorkspace:
        return self.workspace_manager.workspace(workspace_id)

    def allocate_child_branch(self, parent_branch_id: str) -> str:
        with self.lock:
            child_index = self.branch_child_indexes[parent_branch_id]
            self.branch_child_indexes[parent_branch_id] += 1
            child_branch_id = f"{parent_branch_id}.{child_index}"
            self.branch_parents[child_branch_id] = parent_branch_id
            self.on_child_branch_allocated(parent_branch_id, child_branch_id)
            return child_branch_id

    def on_child_branch_allocated(self, parent_branch_id: str, child_branch_id: str) -> None:
        pass

    def finish_branch(self, branch_id: str) -> None:
        pass

    def allocate_path_id(self) -> str:
        branch_id = current_branch_id.get()
        with self.lock:
            local_index = self.branch_llm_indexes[branch_id]
            self.branch_llm_indexes[branch_id] += 1
        return f"{branch_id}/{local_index}"

    def release_path_id(self, path_id: str) -> None:
        """Give a branch's last LLM path index back after a boundary wrote no record.

        A boundary whose underlying call raises has already consumed a path
        index but persists nothing, leaving a hole in the recorded path space.
        The caller (e.g. the model client retrying a transient API error)
        then records the retry one index further along. A later backtrack
        replay walking that prefix asks for the hole's path_id: content
        matching can miss it when the retry rewrote the request (the client
        trims messages after a context-limit error), and the path_id-based
        materialized-prefix fallback finds nothing there either, so replay
        raises ReplayMissError. Releasing the index keeps recorded indexes
        contiguous so the retry lands on the same path_id.

        Only the branch's most recently allocated index is released, so a
        concurrent allocation on the same branch leaves the counter untouched.
        """
        parsed = parse_llm_path_index(path_id)
        if parsed is None:
            return
        branch_id, index = parsed
        with self.lock:
            if self.branch_llm_indexes.get(branch_id) == index + 1:
                self.branch_llm_indexes[branch_id] = index

    def allocate_tool_path_id(self) -> str:
        branch_id = current_branch_id.get()
        with self.lock:
            local_index = self.branch_tool_indexes[branch_id]
            self.branch_tool_indexes[branch_id] += 1
        return f"{branch_id}/tool/{local_index}"

    def next_record_uid(self) -> str:
        raise NotImplementedError

    def _merged_metadata(
        self,
        metadata: dict[str, Any] | None = None,
        *,
        semantic_hint: str | None = None,
    ) -> dict[str, Any] | None:
        merged = RUNTIME.merge_metadata(metadata, semantic_hint=semantic_hint)
        return merged or None

    def response_from_record_with_denormalized_paths(self, record: dict[str, Any]) -> Any:
        patched = copy.deepcopy(record)
        if "output" in patched:
            patched["output"] = self.workspace_manager.denormalize_paths(patched["output"])
        return response_from_record(patched)

    def build_call_record(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        path_id: str,
        response: Any,
        started_at: float,
        matched_by: str | None = None,
        override: bool = False,
        provenance: dict[str, set[Source]] | None = None,
        semantic: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        record_uid: str | None = None,
        output_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if output_record is None:
            output_record = response_to_record(response)
        output_record = self.workspace_manager.normalize_paths(output_record)
        record_metadata = {
            "created_at": iso_now(),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "usage": output_record.get("usage"),
            "matched_by": matched_by,
            "override": override,
        }
        if metadata:
            record_metadata.update(metadata)
        record = {
            "schema_version": 3,
            "record_uid": record_uid if record_uid is not None else self.next_record_uid(),
            "kind": "llm",
            "input_id": input_id,
            "path_id": path_id,
            "input": input_record,
            "output": output_record,
            "callsite": capture_callsite(),
            "metadata": record_metadata,
        }
        self.attach_semantic_metadata(record, provenance=provenance, semantic=semantic)
        return record

    def build_tool_record(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        path_id: str,
        output_record: dict[str, Any] | None,
        error_record: dict[str, Any] | None,
        started_at: float,
        matched_by: str | None = None,
        provenance: dict[str, set[Source]] | None = None,
        semantic: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        record_uid: str | None = None,
    ) -> dict[str, Any]:
        record_metadata = {
            "created_at": iso_now(),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "matched_by": matched_by,
        }
        if metadata:
            record_metadata.update(metadata)
        if output_record is not None:
            output_record = self.workspace_manager.normalize_paths(output_record)
        record = {
            "schema_version": 3,
            "record_uid": record_uid if record_uid is not None else self.next_record_uid(),
            "kind": "tool",
            "input_id": input_id,
            "path_id": path_id,
            "input": input_record,
            "output": output_record,
            "error": error_record,
            "callsite": capture_callsite(),
            "metadata": record_metadata,
        }
        self.attach_semantic_metadata(record, provenance=provenance, semantic=semantic)
        return record

    def attach_semantic_metadata(
        self,
        record: dict[str, Any],
        *,
        provenance: dict[str, set[Source]] | None = None,
        semantic: dict[str, Any] | None = None,
    ) -> None:
        if provenance is None and semantic is None:
            return
        record["schema_version"] = 3
        if provenance is not None:
            record["metadata"]["provenance"] = provenance_to_json(provenance)
        if semantic is not None:
            existing = record["metadata"].get("semantic")
            if isinstance(existing, dict):
                merged = dict(existing)
                merged.update(semantic)
                record["metadata"]["semantic"] = merged
            else:
                record["metadata"]["semantic"] = semantic


class RecordSession(BaseSession):
    def __init__(
        self,
        *,
        run_id: str,
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
        backtrack_summary_max_input_chars: int = DEFAULT_SUMMARY_MAX_INPUT_CHARS,
    ) -> None:
        super().__init__(
            log_dir=log_dir,
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
        )
        self.run_id = run_id
        self.overwrite = overwrite
        self.writer: JsonlWriter | None = None
        self._record_counter = 0
        self.backtrack_summary_manager = BacktrackSummaryManager(
            run_id=self.run_id,
            log_dir=self.log_dir,
            enabled=backtrack_summary_enabled,
            max_input_chars=backtrack_summary_max_input_chars,
        )
        self._pending_records: list[PendingRecord] = []
        self._pending_flush_lock = threading.RLock()

    def on_enter(self) -> None:
        self.writer = JsonlWriter(run_path(self.log_dir, self.run_id), overwrite=self.overwrite)
        self.workspace_manager.on_record_enter(run_id=self.run_id, writer=self.writer)
        if self.overwrite:
            self.backtrack_summary_manager.reset_sidecar()

    def on_exit(self) -> None:
        try:
            try:
                self.flush_pending_records()
            finally:
                self.backtrack_summary_manager.close()
        finally:
            if self.writer is not None:
                self.writer.close()

    def next_record_uid(self) -> str:
        with self.lock:
            self._record_counter += 1
            return make_record_uid(self._record_counter)

    def queue_pending_record(
        self,
        fs_ctx: FilesystemNodeContext,
        record: dict[str, Any],
        edge_records: list[dict[str, Any]] | None = None,
        *,
        extra_records_factory=None,
    ) -> None:
        with self._pending_flush_lock:
            with self.lock:
                self._pending_records.append(
                    PendingRecord(
                        fs_ctx=fs_ctx,
                        record=record,
                        owner=current_pending_owner(),
                        edge_records=edge_records,
                        extra_records_factory=extra_records_factory,
                    )
                )
            self.workspace_manager.defer_node(fs_ctx)

    def flush_pending_records(
        self,
        *,
        owner: tuple[str, int] | None = None,
    ) -> dict[str, GitCheckpoint] | None:
        boundary_before: dict[str, GitCheckpoint] | None = None
        with self._pending_flush_lock:
            while True:
                with self.lock:
                    if not self._pending_records:
                        return boundary_before
                    if owner is not None and self._pending_records[0].owner != owner:
                        return boundary_before
                    pending = self._pending_records.pop(0)
                settled = self.workspace_manager.settle_deferred_node_with_checkpoints(pending.fs_ctx)
                fs_metadata = settled.metadata
                pending.record["metadata"]["filesystem"] = fs_metadata
                try:
                    self.write_record(
                        pending.record,
                        pending.edge_records,
                        extra_records_factory=pending.extra_records_factory,
                    )
                except Exception:
                    self.workspace_manager.rollback_settled_node(fs_metadata)
                    raise
                boundary_before = settled.after

    def has_pending_records(self) -> bool:
        with self.lock:
            return bool(self._pending_records)

    async def handle_async_llm_boundary(
        self,
        *,
        name: str,
        record_kwargs: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        provider: str = "openai",
        api: str = "chat.completions.create",
        extra_records_factory=None,
    ):
        boundary_before = self.flush_pending_records(owner=current_pending_owner())
        prepared = prepare_llm_call(
            self,
            record_kwargs,
            semantic_hint=semantic_hint,
            provider=provider,
            api=api,
        )
        record_uid = self.next_record_uid()
        fs_ctx = await self.workspace_manager.begin_deferred_node_async(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("llm")
        try:
            try:
                response = invoke()
                response = await response if inspect.isawaitable(response) else response
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                self.release_path_id(prepared.path_id)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        combined_metadata = {
            **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
        }
        try:
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
                output_record=output_record,
            )
            source = source_for_record(self.run_id, record)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        try:
            self.queue_pending_record(
                fs_ctx,
                record,
                edge_records_for(prepared, source),
                extra_records_factory=extra_records_factory,
            )
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            raise
        seed_response(response, source)
        return response

    def handle_sync_llm_boundary(
        self,
        *,
        name: str,
        record_kwargs: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        provider: str = "openai",
        api: str = "chat.completions.create",
        extra_records_factory=None,
    ):
        boundary_before = self.flush_pending_records(owner=current_pending_owner())
        prepared = prepare_llm_call(
            self,
            record_kwargs,
            semantic_hint=semantic_hint,
            provider=provider,
            api=api,
        )
        record_uid = self.next_record_uid()
        fs_ctx = self.workspace_manager.begin_deferred_node(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("llm")
        try:
            try:
                response = invoke()
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                self.release_path_id(prepared.path_id)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        combined_metadata = {
            **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
        }
        try:
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
                output_record=output_record,
            )
            source = source_for_record(self.run_id, record)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        try:
            self.queue_pending_record(
                fs_ctx,
                record,
                edge_records_for(prepared, source),
                extra_records_factory=extra_records_factory,
            )
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            raise
        seed_response(response, source)
        return response

    async def handle_async_llm_call(self, original_create, resource, args, kwargs):
        def invoke():
            call_args, call_kwargs = RUNTIME.plain_call_args(args, kwargs)
            return original_create(resource, *call_args, **call_kwargs)

        return await self.handle_async_llm_boundary(
            name="chat.completions.create",
            record_kwargs=kwargs,
            invoke=invoke,
            provider="openai",
            api="chat.completions.create",
        )

    def handle_sync_llm_call(self, original_create, resource, args, kwargs):
        def invoke():
            call_args, call_kwargs = RUNTIME.plain_call_args(args, kwargs)
            return original_create(resource, *call_args, **call_kwargs)

        return self.handle_sync_llm_boundary(
            name="chat.completions.create",
            record_kwargs=kwargs,
            invoke=invoke,
            provider="openai",
            api="chat.completions.create",
        )

    async def handle_async_tool_event(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        tool_name: str,
        invoke,
        path_id: str | None = None,
        input_arguments: Any = None,
        metadata: dict[str, Any] | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        semantic_hint: str | None = None,
        replay_live_prefix: bool = False,
    ):
        return await self.handle_async_tool_boundary(
            tool_name=tool_name,
            input_record=input_record,
            invoke=invoke,
            path_id=path_id,
            input_arguments=input_arguments,
            input_id=input_id,
            metadata=metadata,
            metadata_factory=metadata_factory,
            semantic_hint=semantic_hint,
            replay_live_prefix=replay_live_prefix,
        )

    async def handle_async_tool_boundary(
        self,
        *,
        tool_name: str,
        input_record: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        path_id: str | None = None,
        input_arguments: Any = None,
        input_id: str | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        replay_live_prefix: bool = False,
    ):
        boundary_before = self.flush_pending_records(owner=current_pending_owner())
        input_record = RUNTIME.plain_snapshot(input_record)
        input_record = self.workspace_manager.normalize_paths(input_record)
        input_id = compute_input_id(input_record)
        if path_id is None:
            path_id = self.allocate_tool_path_id()
        prepared = prepare_tool_event(
            input_record=input_record,
            input_id=input_id,
            path_id=path_id,
            input_arguments=input_arguments,
            semantic_hint=semantic_hint,
        )
        record_uid = self.next_record_uid()
        fs_ctx = await self.workspace_manager.begin_deferred_node_async(
            kind="tool",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("tool")
        try:
            try:
                result = invoke()
                output = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                resolved_metadata = resolve_metadata(metadata, metadata_factory)
                resolved_metadata = with_replay_live_prefix_metadata(
                    resolved_metadata,
                    replay_live_prefix,
                )
                combined_metadata = {
                    **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
                }
                try:
                    record = self.build_tool_record(
                        input_record=prepared.input_record,
                        input_id=prepared.input_id,
                        path_id=prepared.path_id,
                        output_record=None,
                        error_record=tool_error_to_record(exc),
                        started_at=started_at,
                        provenance=prepared.provenance if prepared.semantic is not None else None,
                        semantic=prepared.semantic,
                        metadata=combined_metadata,
                        record_uid=record_uid,
                    )
                    source = source_for_record(self.run_id, record)
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    raise
                try:
                    self.queue_pending_record(
                        fs_ctx,
                        record,
                        edge_records_for(prepared, source),
                    )
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    raise
                raise
            try:
                output_record = tool_output_to_record(
                    self.workspace_manager.normalize_paths(output),
                    tool_name=tool_name,
                )
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)

        resolved_metadata = resolve_metadata(metadata, metadata_factory)
        resolved_metadata = with_replay_live_prefix_metadata(
            resolved_metadata,
            replay_live_prefix,
        )
        combined_metadata = {
            **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
        }
        try:
            record = self.build_tool_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                output_record=output_record,
                error_record=None,
                started_at=started_at,
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
            )
            source = source_for_record(self.run_id, record)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        try:
            self.queue_pending_record(fs_ctx, record, edge_records_for(prepared, source))
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            raise
        return seed_response(output, source)

    def handle_sync_tool_event(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        tool_name: str,
        invoke,
        path_id: str | None = None,
        input_arguments: Any = None,
        metadata: dict[str, Any] | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        semantic_hint: str | None = None,
        replay_live_prefix: bool = False,
    ):
        return self.handle_sync_tool_boundary(
            tool_name=tool_name,
            input_record=input_record,
            invoke=invoke,
            path_id=path_id,
            input_arguments=input_arguments,
            input_id=input_id,
            metadata=metadata,
            metadata_factory=metadata_factory,
            semantic_hint=semantic_hint,
            replay_live_prefix=replay_live_prefix,
        )

    def handle_sync_tool_boundary(
        self,
        *,
        tool_name: str,
        input_record: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        path_id: str | None = None,
        input_arguments: Any = None,
        input_id: str | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        replay_live_prefix: bool = False,
    ):
        boundary_before = self.flush_pending_records(owner=current_pending_owner())
        input_record = RUNTIME.plain_snapshot(input_record)
        input_record = self.workspace_manager.normalize_paths(input_record)
        input_id = compute_input_id(input_record)
        if path_id is None:
            path_id = self.allocate_tool_path_id()
        prepared = prepare_tool_event(
            input_record=input_record,
            input_id=input_id,
            path_id=path_id,
            input_arguments=input_arguments,
            semantic_hint=semantic_hint,
        )
        record_uid = self.next_record_uid()
        fs_ctx = self.workspace_manager.begin_deferred_node(
            kind="tool",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("tool")
        try:
            try:
                output = invoke()
            except Exception as exc:
                resolved_metadata = resolve_metadata(metadata, metadata_factory)
                resolved_metadata = with_replay_live_prefix_metadata(
                    resolved_metadata,
                    replay_live_prefix,
                )
                combined_metadata = {
                    **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
                }
                try:
                    record = self.build_tool_record(
                        input_record=prepared.input_record,
                        input_id=prepared.input_id,
                        path_id=prepared.path_id,
                        output_record=None,
                        error_record=tool_error_to_record(exc),
                        started_at=started_at,
                        provenance=prepared.provenance if prepared.semantic is not None else None,
                        semantic=prepared.semantic,
                        metadata=combined_metadata,
                        record_uid=record_uid,
                    )
                    source = source_for_record(self.run_id, record)
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    raise
                try:
                    self.queue_pending_record(
                        fs_ctx,
                        record,
                        edge_records_for(prepared, source),
                    )
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    raise
                raise
            try:
                output_record = tool_output_to_record(
                    self.workspace_manager.normalize_paths(output),
                    tool_name=tool_name,
                )
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)

        resolved_metadata = resolve_metadata(metadata, metadata_factory)
        resolved_metadata = with_replay_live_prefix_metadata(
            resolved_metadata,
            replay_live_prefix,
        )
        combined_metadata = {
            **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
        }
        try:
            record = self.build_tool_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                output_record=output_record,
                error_record=None,
                started_at=started_at,
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
            )
            source = source_for_record(self.run_id, record)
        except Exception:
            self.workspace_manager.abort_node(fs_ctx)
            raise
        try:
            self.queue_pending_record(fs_ctx, record, edge_records_for(prepared, source))
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            raise
        return seed_response(output, source)

    def write_record(
        self,
        record: dict[str, Any],
        edge_records: list[dict[str, Any]] | None = None,
        *,
        extra_records_factory=None,
    ) -> None:
        if self.writer is None:
            raise RuntimeError("Record session writer is not open.")
        written_records: list[dict[str, Any]] = []
        with self.lock:
            self.writer.write(record)
            written_records.append(record)
            extra_records = (
                extra_records_factory(record)
                if extra_records_factory is not None
                else []
            )
            for extra_record in extra_records:
                self.writer.write(extra_record)
                written_records.append(extra_record)
            for edge_record in edge_records or []:
                self.writer.write(edge_record)
                written_records.append(edge_record)
        self.backtrack_summary_manager.observe_records(written_records)


class ReplaySession(BaseSession):
    def __init__(
        self,
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
        backtrack_summary_max_input_chars: int = DEFAULT_SUMMARY_MAX_INPUT_CHARS,
    ) -> None:
        super().__init__(
            log_dir=log_dir,
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
        )
        override_count = sum(
            item is not None
            for item in (override_output, override_input, override_message)
        )
        if override_count > 1:
            raise ValueError(
                "override_output, override_input, and override_message are mutually exclusive."
            )
        if fork_log_mode not in FORK_LOG_MODES:
            raise ValueError(
                "fork_log_mode must be one of "
                f"{', '.join(sorted(FORK_LOG_MODES))}, got {fork_log_mode!r}."
            )
        if fork_reuse_policy not in {"never", "safe"}:
            raise ValueError("fork_reuse_policy must be 'never' or 'safe'.")
        self.base_run = base_run
        self.breakpoint_record_uid = breakpoint_record_uid
        self.override_output = override_output
        self.override_input = override_input
        self.override_message = override_message
        self.fork_run = fork_run
        self.fork_log_mode = fork_log_mode
        self.fork_metadata = dict(fork_metadata or {})
        self.semantic_fallback = semantic_fallback
        self.fork_reuse_policy: ForkReusePolicy = fork_reuse_policy
        self.post_fork_live_mode = False
        self.branch_modes: dict[str, str] = defaultdict(lambda: "replay")
        self.dirty_branches: set[str] = set()
        self.fork_sources: set[Source] = set()
        self.history: list[dict[str, Any]] = []
        self.workspace_records: list[dict[str, Any]] = []
        self.consumed_record_uids: set[str] = set()
        self.fork_writer: JsonlWriter | None = None
        self._fork_record_counter = 0
        self.raw_history: list[dict[str, Any]] = []
        self.backtrack_summary_enabled = backtrack_summary_enabled
        self.backtrack_summary_max_input_chars = backtrack_summary_max_input_chars
        self.backtrack_summary_manager: BacktrackSummaryManager | None = None
        self._pending_fork_records: list[PendingRecord] = []
        self._pending_flush_lock = threading.RLock()

    def on_enter(self) -> None:
        base_path = run_path(self.log_dir, self.base_run)
        self.raw_history = load_raw_records(base_path)
        self.history = load_replay_records(base_path)
        reject_legacy_filesystem_schema(self.history)
        self.workspace_records = load_workspace_records(base_path)
        replay_run = self.fork_run or f"{self.base_run}_replay"
        self.workspace_manager.on_replay_enter(
            base_run=self.base_run,
            replay_run=replay_run,
            workspace_records=self.workspace_records,
            preserve_current=self.should_preserve_backtrack_workspace(),
        )
        self.validate_breakpoint()
        if self.fork_log_mode == "materialized":
            self.seed_path_allocators_from_materialized_prefix()
            self._fork_record_counter = max(
                (record_uid_index(record.get("record_uid")) for record in self.materialized_prefix_records()),
                default=0,
            )
        self.branch_modes[DEFAULT_BRANCH_ID] = "replay"

    def should_preserve_backtrack_workspace(self) -> bool:
        return (
            self.backtrack_workspace_policy == "preserve"
            and self.breakpoint_record_uid is not None
            and self.fork_run is not None
        )

    def on_exit(self) -> None:
        try:
            try:
                self.flush_pending_fork_records()
            finally:
                if self.backtrack_summary_manager is not None:
                    self.backtrack_summary_manager.close()
        finally:
            if self.fork_writer is not None:
                self.fork_writer.close()

    def next_record_uid(self) -> str:
        with self.lock:
            self._fork_record_counter += 1
            return make_record_uid(self._fork_record_counter)

    def queue_pending_fork_record_with_edges(
        self,
        fs_ctx: FilesystemNodeContext,
        record: dict[str, Any],
        prepared: PreparedCall,
        decision: dict[str, Any] | None = None,
        *,
        extra_records_factory=None,
    ) -> None:
        self.ensure_fork_writer()
        source = source_for_record(self.fork_run or self.base_run, record)
        with self._pending_flush_lock:
            branch_id = current_branch_id.get()
            with self.lock:
                previous_post_fork_live_mode = self.post_fork_live_mode
                previous_branch_mode = self.branch_modes[branch_id]
                previous_branch_dirty = branch_id in self.dirty_branches
                self._pending_fork_records.append(
                    PendingRecord(
                        fs_ctx=fs_ctx,
                        record=record,
                        owner=current_pending_owner(),
                        edge_records=edge_records_for(prepared, source),
                        extra_records_factory=extra_records_factory,
                        decision=decision,
                        source=source,
                        branch_id=branch_id,
                        previous_post_fork_live_mode=previous_post_fork_live_mode,
                        previous_branch_mode=previous_branch_mode,
                        previous_branch_dirty=previous_branch_dirty,
                    )
                )
            self.workspace_manager.defer_node(fs_ctx)
            self.fork_sources.add(source)
            self.mark_branch_dirty(branch_id)
            if self.fork_reuse_policy == "never":
                self.post_fork_live_mode = True
                self.mark_branch_live(branch_id)

    def flush_pending_fork_records(
        self,
        *,
        owner: tuple[str, int] | None = None,
    ) -> dict[str, GitCheckpoint] | None:
        boundary_before: dict[str, GitCheckpoint] | None = None
        with self._pending_flush_lock:
            while True:
                with self.lock:
                    if not self._pending_fork_records:
                        return boundary_before
                    if owner is not None and self._pending_fork_records[0].owner != owner:
                        return boundary_before
                    pending = self._pending_fork_records.pop(0)
                settled = self.workspace_manager.settle_deferred_node_with_checkpoints(pending.fs_ctx)
                fs_metadata = settled.metadata
                pending.record["metadata"]["filesystem"] = fs_metadata
                try:
                    self.write_fork_record(
                        pending.record,
                        pending.edge_records,
                        extra_records_factory=pending.extra_records_factory,
                    )
                except Exception:
                    self.workspace_manager.rollback_settled_node(fs_metadata)
                    if pending.decision is not None:
                        self.discard_consumed_record_for_decision(pending.decision)
                    if pending.source is not None:
                        self.fork_sources.discard(pending.source)
                    if pending.previous_post_fork_live_mode is not None:
                        self.post_fork_live_mode = pending.previous_post_fork_live_mode
                    if pending.branch_id is not None:
                        if pending.previous_branch_mode is not None:
                            self.branch_modes[pending.branch_id] = pending.previous_branch_mode
                        if pending.previous_branch_dirty:
                            self.dirty_branches.add(pending.branch_id)
                        else:
                            self.dirty_branches.discard(pending.branch_id)
                    raise
                boundary_before = settled.after

    def has_pending_fork_records(self) -> bool:
        with self.lock:
            return bool(self._pending_fork_records)

    def on_child_branch_allocated(self, parent_branch_id: str, child_branch_id: str) -> None:
        self.branch_modes[child_branch_id] = self.branch_modes[parent_branch_id]

    def finish_branch(self, branch_id: str) -> None:
        with self.lock:
            parent_branch_id = self.branch_parents.get(branch_id)
            if parent_branch_id is None or branch_id not in self.dirty_branches:
                return
            if self.branch_modes[branch_id] == "live":
                self.mark_branch_live(parent_branch_id)
            else:
                self.mark_branch_dirty(parent_branch_id)

    def validate_breakpoint(self) -> None:
        if self.breakpoint_record_uid is None:
            return
        matches = [
            record
            for record in self.history
            if record.get("record_uid") == self.breakpoint_record_uid
        ]
        if not matches:
            raise InvalidBreakpointError(
                f"breakpoint_record_uid not found in base run: {self.breakpoint_record_uid}"
            )
        record = matches[0]
        if record.get("kind") != "llm":
            raise InvalidBreakpointError(
                "breakpoint_record_uid must refer to an LLM record, "
                f"but got kind={record.get('kind')!r}."
            )

    async def handle_async_llm_boundary(
        self,
        *,
        name: str,
        record_kwargs: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        provider: str = "openai",
        api: str = "chat.completions.create",
        extra_records_factory=None,
    ):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        prepared = prepare_llm_call(
            self,
            record_kwargs,
            semantic_hint=semantic_hint,
            provider=provider,
            api=api,
        )
        decision = self.decide_replay(
            kind="llm",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            response = self.response_from_record_with_denormalized_paths(record)
            return seed_response(response, source_for_record(self.base_run, record))

        if decision["action"] == "override":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_response(decision["record"], self.override_output or "")
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_message":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_message_response(
                    decision["record"],
                    self.override_message or {},
                )
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_input":
            self.start_fork_for_decision(decision)
            patched_kwargs = self.apply_override_input(record_kwargs)
            patched_kwargs_for_record = self.workspace_manager.normalize_paths(patched_kwargs)
            input_record, input_id = prepare_replayed_llm_input(patched_kwargs_for_record)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            token = branch_allocation_suppressed.set(True)
            RUNTIME.push_kind("llm")
            try:
                try:
                    response = invoke(patched_kwargs)
                    response = await response if inspect.isawaitable(response) else response
                    self.remember_applied_override_messages(patched_kwargs.get("messages"))
                except Exception:
                    self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                    raise
            finally:
                RUNTIME.pop_kind()
                branch_allocation_suppressed.reset(token)
            try:
                output_record = response_to_record(response)
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                    "input_override": True,
                    "base_record_uid": decision["record"]["record_uid"],
                }
                record = self.build_call_record(
                    input_record=input_record,
                    input_id=input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                    output_record=output_record,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(
                response,
                source_for_record(self.fork_run or self.base_run, record),
            )

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = await self.workspace_manager.begin_deferred_node_async(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("llm")
        try:
            try:
                response = invoke()
                response = await response if inspect.isawaitable(response) else response
            except Exception:
                self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
            record_metadata = {
                **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
            }
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=record_metadata,
                record_uid=record_uid,
                output_record=output_record,
            )
            self.queue_pending_fork_record_with_edges(
                fs_ctx,
                record,
                prepared,
                decision,
                extra_records_factory=extra_records_factory,
            )
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

    def handle_sync_llm_boundary(
        self,
        *,
        name: str,
        record_kwargs: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        provider: str = "openai",
        api: str = "chat.completions.create",
        extra_records_factory=None,
    ):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        prepared = prepare_llm_call(
            self,
            record_kwargs,
            semantic_hint=semantic_hint,
            provider=provider,
            api=api,
        )
        decision = self.decide_replay(
            kind="llm",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            response = self.response_from_record_with_denormalized_paths(record)
            return seed_response(response, source_for_record(self.base_run, record))

        if decision["action"] == "override":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_response(decision["record"], self.override_output or "")
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_message":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_message_response(
                    decision["record"],
                    self.override_message or {},
                )
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_input":
            self.start_fork_for_decision(decision)
            patched_kwargs = self.apply_override_input(record_kwargs)
            patched_kwargs_for_record = self.workspace_manager.normalize_paths(patched_kwargs)
            input_record, input_id = prepare_replayed_llm_input(patched_kwargs_for_record)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            token = branch_allocation_suppressed.set(True)
            RUNTIME.push_kind("llm")
            try:
                try:
                    response = invoke(patched_kwargs)
                    self.remember_applied_override_messages(patched_kwargs.get("messages"))
                except Exception:
                    self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                    raise
            finally:
                RUNTIME.pop_kind()
                branch_allocation_suppressed.reset(token)
            try:
                output_record = response_to_record(response)
                record_metadata = {
                    **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
                    "input_override": True,
                    "base_record_uid": decision["record"]["record_uid"],
                }
                record = self.build_call_record(
                    input_record=input_record,
                    input_id=input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=record_metadata,
                    record_uid=record_uid,
                    output_record=output_record,
                )
                self.queue_pending_fork_record_with_edges(
                    fs_ctx,
                    record,
                    prepared,
                    decision,
                    extra_records_factory=extra_records_factory,
                )
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(
                response,
                source_for_record(self.fork_run or self.base_run, record),
            )

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = self.workspace_manager.begin_deferred_node(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("llm")
        try:
            try:
                response = invoke()
            except Exception:
                self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
            record_metadata = {
                **(self._merged_metadata(metadata, semantic_hint=semantic_hint) or {}),
            }
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=record_metadata,
                record_uid=record_uid,
                output_record=output_record,
            )
            self.queue_pending_fork_record_with_edges(
                fs_ctx,
                record,
                prepared,
                decision,
                extra_records_factory=extra_records_factory,
            )
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

    async def handle_async_llm_call(self, original_create, resource, args, kwargs):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        prepared = prepare_llm_call(self, kwargs)
        decision = self.decide_replay(
            kind="llm",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            response = self.response_from_record_with_denormalized_paths(record)
            return seed_response(response, source_for_record(self.base_run, record))

        if decision["action"] == "override":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_response(decision["record"], self.override_output or "")
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_message":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_message_response(
                    decision["record"],
                    self.override_message or {},
                )
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_input":
            self.start_fork_for_decision(decision)
            patched_kwargs = self.apply_override_input(kwargs)
            patched_kwargs_for_record = self.workspace_manager.normalize_paths(patched_kwargs)
            input_record, input_id = prepare_replayed_llm_input(patched_kwargs_for_record)
            record_uid = self.next_record_uid()
            fs_ctx = await self.workspace_manager.begin_deferred_node_async(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            token = branch_allocation_suppressed.set(True)
            try:
                try:
                    call_args, call_kwargs = RUNTIME.plain_call_args(args, patched_kwargs)
                    response = await original_create(resource, *call_args, **call_kwargs)
                    self.remember_applied_override_messages(patched_kwargs.get("messages"))
                except Exception:
                    self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                    raise
            finally:
                branch_allocation_suppressed.reset(token)
            try:
                output_record = response_to_record(response)
                record = self.build_call_record(
                    input_record=input_record,
                    input_id=input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata={
                        "input_override": True,
                        "base_record_uid": decision["record"]["record_uid"],
                    },
                    record_uid=record_uid,
                    output_record=output_record,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = await self.workspace_manager.begin_deferred_node_async(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        try:
            try:
                call_args, call_kwargs = RUNTIME.plain_call_args(args, kwargs)
                response = await original_create(resource, *call_args, **call_kwargs)
            except Exception:
                self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                raise
        finally:
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                record_uid=record_uid,
                output_record=output_record,
            )
            self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

    def handle_sync_llm_call(self, original_create, resource, args, kwargs):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        prepared = prepare_llm_call(self, kwargs)
        decision = self.decide_replay(
            kind="llm",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            response = self.response_from_record_with_denormalized_paths(record)
            return seed_response(response, source_for_record(self.base_run, record))

        if decision["action"] == "override":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_response(decision["record"], self.override_output or "")
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_message":
            self.start_fork_for_decision(decision)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            try:
                response = build_override_message_response(
                    decision["record"],
                    self.override_message or {},
                )
                record = self.build_call_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    override=True,
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    record_uid=record_uid,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        if decision["action"] == "override_input":
            self.start_fork_for_decision(decision)
            patched_kwargs = self.apply_override_input(kwargs)
            patched_kwargs_for_record = self.workspace_manager.normalize_paths(patched_kwargs)
            input_record, input_id = prepare_replayed_llm_input(patched_kwargs_for_record)
            record_uid = self.next_record_uid()
            fs_ctx = self.workspace_manager.begin_deferred_node(
                kind="llm",
                path_id=prepared.path_id,
                record_uid=record_uid,
                defer_ambient=self.has_pending_fork_records(),
                precomputed_before=boundary_before,
            )
            started_at = time.perf_counter()
            token = branch_allocation_suppressed.set(True)
            try:
                try:
                    call_args, call_kwargs = RUNTIME.plain_call_args(args, patched_kwargs)
                    response = original_create(resource, *call_args, **call_kwargs)
                    self.remember_applied_override_messages(patched_kwargs.get("messages"))
                except Exception:
                    self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                    raise
            finally:
                branch_allocation_suppressed.reset(token)
            try:
                output_record = response_to_record(response)
                record = self.build_call_record(
                    input_record=input_record,
                    input_id=input_id,
                    path_id=prepared.path_id,
                    response=response,
                    started_at=started_at,
                    matched_by=decision["matched_by"],
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata={
                        "input_override": True,
                        "base_record_uid": decision["record"]["record_uid"],
                    },
                    record_uid=record_uid,
                    output_record=output_record,
                )
                self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
            except Exception:
                self.workspace_manager.finalize_failed_node(fs_ctx)
                self.discard_consumed_record_for_decision(decision)
                raise
            return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = self.workspace_manager.begin_deferred_node(
            kind="llm",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        try:
            try:
                call_args, call_kwargs = RUNTIME.plain_call_args(args, kwargs)
                response = original_create(resource, *call_args, **call_kwargs)
            except Exception:
                self.abort_failed_llm_attempt(fs_ctx, prepared, decision)
                raise
        finally:
            branch_allocation_suppressed.reset(token)
        try:
            output_record = response_to_record(response)
            record = self.build_call_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                response=response,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                record_uid=record_uid,
                output_record=output_record,
            )
            self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(response, source_for_record(self.fork_run or self.base_run, record))

    async def handle_async_tool_event(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        tool_name: str,
        invoke,
        path_id: str | None = None,
        input_arguments: Any = None,
        metadata: dict[str, Any] | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        semantic_hint: str | None = None,
        replay_live_prefix: bool = False,
    ):
        return await self.handle_async_tool_boundary(
            tool_name=tool_name,
            input_record=input_record,
            invoke=invoke,
            path_id=path_id,
            input_arguments=input_arguments,
            input_id=input_id,
            metadata=metadata,
            metadata_factory=metadata_factory,
            semantic_hint=semantic_hint,
            replay_live_prefix=replay_live_prefix,
        )

    async def handle_async_tool_boundary(
        self,
        *,
        tool_name: str,
        input_record: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        path_id: str | None = None,
        input_arguments: Any = None,
        input_id: str | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        replay_live_prefix: bool = False,
    ):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        input_record = RUNTIME.plain_snapshot(input_record)
        input_record = self.workspace_manager.normalize_paths(input_record)
        input_id = compute_input_id(input_record)
        if path_id is None:
            path_id = self.allocate_tool_path_id()
        prepared = prepare_tool_event(
            input_record=input_record,
            input_id=input_id,
            path_id=path_id,
            input_arguments=input_arguments,
            semantic_hint=semantic_hint,
        )
        decision = self.decide_replay(
            kind="tool",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            if replay_live_prefix:
                token = branch_allocation_suppressed.set(True)
                record_token = _current_replay_record.set(record)
                RUNTIME.push_kind("tool")
                try:
                    result = invoke()
                    output = await result if inspect.isawaitable(result) else result
                finally:
                    RUNTIME.pop_kind()
                    _current_replay_record.reset(record_token)
                    branch_allocation_suppressed.reset(token)
                return seed_response(output, source_for_record(self.base_run, record))
            output = replay_tool_record(record)
            output = self.workspace_manager.denormalize_paths(output)
            return seed_response(output, source_for_record(self.base_run, record))

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = await self.workspace_manager.begin_deferred_node_async(
            kind="tool",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("tool")
        try:
            try:
                result = invoke()
                output = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                resolved_metadata = resolve_metadata(metadata, metadata_factory)
                resolved_metadata = with_replay_live_prefix_metadata(
                    resolved_metadata,
                    replay_live_prefix,
                )
                combined_metadata = {
                    **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_tool_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    output_record=None,
                    error_record=tool_error_to_record(exc),
                    started_at=started_at,
                    matched_by=decision.get("matched_by"),
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=combined_metadata,
                    record_uid=record_uid,
                )
                try:
                    self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    self.discard_consumed_record_for_decision(decision)
                    raise
                raise
            try:
                output_record = tool_output_to_record(output, tool_name=tool_name)
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)

        try:
            resolved_metadata = resolve_metadata(metadata, metadata_factory)
            resolved_metadata = with_replay_live_prefix_metadata(
                resolved_metadata,
                replay_live_prefix,
            )
            combined_metadata = {
                **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
            }
            record = self.build_tool_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                output_record=output_record,
                error_record=None,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
            )
            self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(output, source_for_record(self.fork_run or self.base_run, record))

    def handle_sync_tool_event(
        self,
        *,
        input_record: dict[str, Any],
        input_id: str,
        tool_name: str,
        invoke,
        path_id: str | None = None,
        input_arguments: Any = None,
        metadata: dict[str, Any] | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        semantic_hint: str | None = None,
        replay_live_prefix: bool = False,
    ):
        return self.handle_sync_tool_boundary(
            tool_name=tool_name,
            input_record=input_record,
            invoke=invoke,
            path_id=path_id,
            input_arguments=input_arguments,
            input_id=input_id,
            metadata=metadata,
            metadata_factory=metadata_factory,
            semantic_hint=semantic_hint,
            replay_live_prefix=replay_live_prefix,
        )

    def handle_sync_tool_boundary(
        self,
        *,
        tool_name: str,
        input_record: dict[str, Any],
        invoke,
        metadata: dict[str, Any] | None = None,
        semantic_hint: str | None = None,
        path_id: str | None = None,
        input_arguments: Any = None,
        input_id: str | None = None,
        metadata_factory: Callable[[], Mapping[str, Any] | None] | None = None,
        replay_live_prefix: bool = False,
    ):
        boundary_before = self.flush_pending_fork_records(owner=current_pending_owner())
        input_record = RUNTIME.plain_snapshot(input_record)
        input_record = self.workspace_manager.normalize_paths(input_record)
        input_id = compute_input_id(input_record)
        if path_id is None:
            path_id = self.allocate_tool_path_id()
        prepared = prepare_tool_event(
            input_record=input_record,
            input_id=input_id,
            path_id=path_id,
            input_arguments=input_arguments,
            semantic_hint=semantic_hint,
        )
        decision = self.decide_replay(
            kind="tool",
            input_id=prepared.input_id,
            path_id=prepared.path_id,
            semantic=prepared.semantic,
            provenance=prepared.provenance,
        )
        decision = self.apply_filesystem_replay_policy_with_consumption_rollback(decision)

        if decision["action"] == "replay":
            record = decision["record"]
            fs_action = decision["filesystem_action"]
            self.write_materialization_records(fs_action)
            if replay_live_prefix:
                token = branch_allocation_suppressed.set(True)
                record_token = _current_replay_record.set(record)
                RUNTIME.push_kind("tool")
                try:
                    output = invoke()
                finally:
                    RUNTIME.pop_kind()
                    _current_replay_record.reset(record_token)
                    branch_allocation_suppressed.reset(token)
                return seed_response(output, source_for_record(self.base_run, record))
            output = replay_tool_record(record)
            output = self.workspace_manager.denormalize_paths(output)
            return seed_response(output, source_for_record(self.base_run, record))

        self.start_fork_for_decision(decision)
        record_uid = self.next_record_uid()
        fs_ctx = self.workspace_manager.begin_deferred_node(
            kind="tool",
            path_id=prepared.path_id,
            record_uid=record_uid,
            defer_ambient=self.has_pending_fork_records(),
            precomputed_before=boundary_before,
        )
        started_at = time.perf_counter()
        token = branch_allocation_suppressed.set(True)
        RUNTIME.push_kind("tool")
        try:
            try:
                output = invoke()
            except Exception as exc:
                resolved_metadata = resolve_metadata(metadata, metadata_factory)
                resolved_metadata = with_replay_live_prefix_metadata(
                    resolved_metadata,
                    replay_live_prefix,
                )
                combined_metadata = {
                    **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
                }
                record = self.build_tool_record(
                    input_record=prepared.input_record,
                    input_id=prepared.input_id,
                    path_id=prepared.path_id,
                    output_record=None,
                    error_record=tool_error_to_record(exc),
                    started_at=started_at,
                    matched_by=decision.get("matched_by"),
                    provenance=prepared.provenance if prepared.semantic is not None else None,
                    semantic=prepared.semantic,
                    metadata=combined_metadata,
                    record_uid=record_uid,
                )
                try:
                    self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
                except Exception:
                    self.workspace_manager.finalize_failed_node(fs_ctx)
                    self.discard_consumed_record_for_decision(decision)
                    raise
                raise
            try:
                output_record = tool_output_to_record(output, tool_name=tool_name)
            except Exception:
                self.workspace_manager.abort_node(fs_ctx)
                raise
        finally:
            RUNTIME.pop_kind()
            branch_allocation_suppressed.reset(token)

        try:
            resolved_metadata = resolve_metadata(metadata, metadata_factory)
            resolved_metadata = with_replay_live_prefix_metadata(
                resolved_metadata,
                replay_live_prefix,
            )
            combined_metadata = {
                **(self._merged_metadata(resolved_metadata, semantic_hint=semantic_hint) or {}),
            }
            record = self.build_tool_record(
                input_record=prepared.input_record,
                input_id=prepared.input_id,
                path_id=prepared.path_id,
                output_record=output_record,
                error_record=None,
                started_at=started_at,
                matched_by=decision.get("matched_by"),
                provenance=prepared.provenance if prepared.semantic is not None else None,
                semantic=prepared.semantic,
                metadata=combined_metadata,
                record_uid=record_uid,
            )
            self.queue_pending_fork_record_with_edges(fs_ctx, record, prepared, decision)
        except Exception:
            self.workspace_manager.finalize_failed_node(fs_ctx)
            self.discard_consumed_record_for_decision(decision)
            raise
        return seed_response(output, source_for_record(self.fork_run or self.base_run, record))

    def decide_replay(
        self,
        *,
        kind: str,
        input_id: str,
        path_id: str,
        semantic: dict[str, Any] | None = None,
        provenance: dict[str, set[Source]] | None = None,
    ) -> dict[str, Any]:
        branch_id = current_branch_id.get()
        with self.lock:
            if self.post_fork_live_mode and self.fork_reuse_policy == "never":
                self.mark_branch_live(branch_id)
                return {"action": "live", "matched_by": "post_fork_live"}

            if self.branch_modes[branch_id] == "live":
                return {"action": "live", "matched_by": "branch_live"}

            if self.has_fork_dependency(provenance):
                self.mark_branch_dirty(branch_id)
                matched_by = self.consume_matching_record_if_available(
                    kind=kind,
                    input_id=input_id,
                    path_id=path_id,
                )
                return {"action": "live", "matched_by": matched_by or "provenance"}

            prefix_match = self.match_materialized_prefix_record_by_path_id(
                kind=kind,
                input_id=input_id,
                path_id=path_id,
            )
            if prefix_match is not None:
                record, matched_by = prefix_match
                if record.get("input_id") == input_id:
                    matched_by = "materialized_prefix_input_id+path_id"
            else:
                try:
                    record, matched_by = self.match_record(
                        kind=kind,
                        input_id=input_id,
                        path_id=path_id,
                        semantic=semantic,
                    )
                except ReplayMissError:
                    prefix_match = self.match_materialized_prefix_record_by_path_id(
                        kind=kind,
                        input_id=input_id,
                        path_id=path_id,
                    )
                    if prefix_match is not None:
                        record, matched_by = prefix_match
                    elif branch_id in self.dirty_branches:
                        self.consume_matching_record_if_available(
                            kind=kind,
                            input_id=input_id,
                            path_id=path_id,
                        )
                        return {"action": "live", "matched_by": "dirty_branch"}
                    else:
                        raise

            record_uid = record["record_uid"]
            self.consumed_record_uids.add(record_uid)

            if (
                kind == "llm"
                and self.breakpoint_record_uid is not None
                and record_uid == self.breakpoint_record_uid
            ):
                self.mark_branch_dirty(branch_id)
                if self.override_output is not None:
                    return {"action": "override", "record": record, "matched_by": matched_by}
                if self.override_message is not None:
                    return {
                        "action": "override_message",
                        "record": record,
                        "matched_by": matched_by,
                    }
                if self.override_input is not None:
                    return {"action": "override_input", "record": record, "matched_by": matched_by}
                return {"action": "live", "record": record, "matched_by": matched_by}

            return {"action": "replay", "record": record, "matched_by": matched_by}

    def apply_filesystem_replay_policy(self, decision: dict[str, Any]) -> dict[str, Any]:
        if decision["action"] != "replay":
            return decision
        record = decision["record"]
        if self.workspace_manager.can_replay(record):
            return decision
        if self.should_force_materialized_backtrack_prefix_restore(record):
            patched = dict(decision)
            patched["filesystem_mismatch_policy"] = "materialized_backtrack_prefix_restore"
            return patched
        branch_id = current_branch_id.get()
        can_live_on_fs_mismatch = (
            self.workspace_manager.fork_active
            or branch_id in self.dirty_branches
            or (
                self.fork_run is not None
                and self.breakpoint_record_uid is None
            )
        )
        if can_live_on_fs_mismatch:
            self.mark_branch_dirty(branch_id)
            return {
                "action": "live",
                "record": record,
                "matched_by": "filesystem_tree_mismatch",
            }
        self.workspace_manager.assert_compatible(record)
        return decision

    def apply_filesystem_replay_policy_with_consumption_rollback(
        self,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with self.workspace_manager.replay_lock():
                if self.should_preserve_backtrack_workspace() and decision["action"] == "replay":
                    decision["filesystem_action"] = {
                        "backend": "git",
                        "preserved": True,
                        "policy": "preserve",
                    }
                    return decision
                decision = self.apply_filesystem_replay_policy(decision)
                if decision["action"] == "replay":
                    record = decision["record"]
                    if (
                        decision.get("filesystem_mismatch_policy")
                        != "materialized_backtrack_prefix_restore"
                    ):
                        self.workspace_manager.assert_compatible(record)
                    decision["filesystem_action"] = self.workspace_manager.restore_after_replayed_record(record)
                return decision
        except Exception:
            self.discard_consumed_record_for_decision(decision)
            raise

    def should_force_materialized_backtrack_prefix_restore(self, record: dict[str, Any]) -> bool:
        if (
            self.fork_log_mode != "materialized"
            or self.fork_run is None
            or self.breakpoint_record_uid is None
        ):
            return False
        fork_metadata = self.fork_metadata
        if not fork_metadata and self.raw_history:
            fork_metadata = self.raw_history[0].get("fork_metadata")
        if not isinstance(fork_metadata, dict) or fork_metadata.get("mode") != "backtrack":
            return False
        record_uid = record.get("record_uid")
        path_id = record.get("path_id")
        return any(
            prefix_record.get("record_uid") == record_uid
            and prefix_record.get("path_id") == path_id
            for prefix_record in self.materialized_prefix_records(include_breakpoint=True)
        )

    def discard_consumed_record_for_decision(self, decision: dict[str, Any]) -> None:
        record = decision.get("record")
        if isinstance(record, dict):
            record_uid = record.get("record_uid")
            if isinstance(record_uid, str):
                self.consumed_record_uids.discard(record_uid)

    def abort_failed_llm_attempt(
        self,
        fs_ctx: FilesystemNodeContext,
        prepared: PreparedCall,
        decision: dict[str, Any],
    ) -> None:
        self.workspace_manager.abort_node(fs_ctx)
        self.release_path_id(prepared.path_id)
        self.discard_consumed_record_for_decision(decision)

    def mark_branch_dirty(self, branch_id: str) -> None:
        self.dirty_branches.add(branch_id)

    def mark_branch_live(self, branch_id: str) -> None:
        self.branch_modes[branch_id] = "live"
        self.mark_branch_dirty(branch_id)

    def has_fork_dependency(self, provenance: dict[str, set[Source]] | None) -> bool:
        if not provenance:
            return False
        return any(self.is_fork_source(source) for source in provenance_sources(provenance))

    def is_fork_source(self, source: Source) -> bool:
        if source in self.fork_sources:
            return True
        return self.fork_run is not None and source.run_id == self.fork_run

    def start_fork_for_decision(self, decision: dict[str, Any]) -> None:
        record = decision.get("record")
        if not isinstance(record, dict):
            return
        self.ensure_fork_writer()
        if self.fork_run is None:
            raise RuntimeError("Replay fork run was not allocated.")
        try:
            if self.should_preserve_backtrack_workspace():
                self.workspace_manager.preserve_fork_from_current(
                    record=record,
                    fork_run=self.fork_run,
                )
            else:
                self.workspace_manager.fork_from_record_before(
                    record=record,
                    fork_run=self.fork_run,
                )
            self.write_workspace_fork_records()
        except Exception:
            self.discard_consumed_record_for_decision(decision)
            self.workspace_manager.rollback_fork_start(record)
            raise

    def consume_matching_record_if_available(
        self,
        *,
        kind: str,
        input_id: str,
        path_id: str,
    ) -> str | None:
        candidates = [
            record
            for record in self.history
            if record.get("kind") == kind
            and record.get("input_id") == input_id
            and record.get("record_uid") not in self.consumed_record_uids
        ]
        path_matches = [record for record in candidates if record.get("path_id") == path_id]
        if len(path_matches) == 1:
            self.consumed_record_uids.add(path_matches[0]["record_uid"])
            return "provenance+input_id+path_id"
        if len(candidates) == 1:
            self.consumed_record_uids.add(candidates[0]["record_uid"])
            return "provenance+input_id"
        path_only_matches = [
            record
            for record in self.history
            if record.get("kind") == kind
            and record.get("path_id") == path_id
            and record.get("record_uid") not in self.consumed_record_uids
        ]
        if len(path_only_matches) == 1:
            self.consumed_record_uids.add(path_only_matches[0]["record_uid"])
            return "provenance+path_id"
        return None

    def apply_override_input(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        patched_kwargs = dict(kwargs)
        if self.override_input is not None:
            patched_kwargs.update(self.override_input)
        return patched_kwargs

    def match_record(
        self,
        *,
        kind: str,
        input_id: str,
        path_id: str,
        semantic: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        candidates = [
            record
            for record in self.history
            if record.get("kind") == kind
            and record.get("input_id") == input_id
            and record.get("record_uid") not in self.consumed_record_uids
        ]
        if not candidates:
            return self.match_semantic_record(
                kind=kind,
                input_id=input_id,
                path_id=path_id,
                semantic=semantic,
            )

        if len(candidates) == 1:
            return candidates[0], "input_id"

        path_matches = [record for record in candidates if record.get("path_id") == path_id]
        if len(path_matches) == 1:
            return path_matches[0], "input_id+path_id"

        from .errors import AmbiguousReplayError

        raise AmbiguousReplayError(
            "Multiple records have the same input_id, and path_id cannot disambiguate: "
            f"input_id={input_id}, path_id={path_id}, candidates="
            f"{[record.get('record_uid') for record in candidates]}"
        )

    def match_semantic_record(
        self,
        *,
        kind: str,
        input_id: str,
        path_id: str,
        semantic: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        if not self.semantic_fallback:
            raise_replay_miss(kind=kind, input_id=input_id, path_id=path_id)
        fingerprint = semantic_callsite_fingerprint(semantic)
        if fingerprint is None:
            raise_replay_miss(kind=kind, input_id=input_id, path_id=path_id)

        candidates = [
            record
            for record in self.history
            if record.get("kind") == kind
            and record.get("record_uid") not in self.consumed_record_uids
            and record_callsite_fingerprint(record) == fingerprint
        ]
        if not candidates:
            raise_replay_miss(kind=kind, input_id=input_id, path_id=path_id)

        if len(candidates) == 1:
            return candidates[0], "semantic"

        path_matches = [record for record in candidates if record.get("path_id") == path_id]
        if len(path_matches) == 1:
            return path_matches[0], "semantic+path_id"

        from .errors import AmbiguousReplayError

        raise AmbiguousReplayError(
            "Multiple semantic replay records have the same callsite_fingerprint, "
            "and path_id cannot disambiguate: "
            f"kind={kind}, callsite_fingerprint={fingerprint}, input_id={input_id}, path_id={path_id}, candidates="
            f"{[record.get('record_uid') for record in candidates]}"
        )

    def write_fork_record(
        self,
        record: dict[str, Any],
        edge_records: list[dict[str, Any]] | None = None,
        *,
        extra_records_factory=None,
    ) -> None:
        self.ensure_fork_writer()
        written_records: list[dict[str, Any]] = []
        with self.lock:
            if self.fork_writer is None:
                raise RuntimeError("Replay fork writer is not open.")
            if self.fork_log_mode == "materialized" and self.fork_run is not None:
                retarget_record_sources(record, from_run=self.base_run, to_run=self.fork_run)
                for edge_record in edge_records or []:
                    retarget_record_sources(edge_record, from_run=self.base_run, to_run=self.fork_run)
            self.fork_writer.write(record)
            written_records.append(record)
            extra_records = (
                extra_records_factory(record)
                if extra_records_factory is not None
                else []
            )
            for extra_record in extra_records:
                if self.fork_log_mode == "materialized" and self.fork_run is not None:
                    retarget_record_sources(extra_record, from_run=self.base_run, to_run=self.fork_run)
                self.fork_writer.write(extra_record)
                written_records.append(extra_record)
            for edge_record in edge_records or []:
                self.fork_writer.write(edge_record)
                written_records.append(edge_record)
        if self.backtrack_summary_manager is not None:
            self.backtrack_summary_manager.observe_records(written_records)

    def write_fork_record_with_edges(
        self,
        record: dict[str, Any],
        prepared: PreparedCall,
        *,
        extra_records_factory=None,
    ) -> None:
        self.ensure_fork_writer()
        source = source_for_record(self.fork_run or self.base_run, record)
        self.write_fork_record(
            record,
            edge_records_for(prepared, source),
            extra_records_factory=extra_records_factory,
        )
        self.fork_sources.add(source)
        branch_id = current_branch_id.get()
        self.mark_branch_dirty(branch_id)
        if self.fork_reuse_policy == "never":
            self.post_fork_live_mode = True
            self.mark_branch_live(branch_id)

    def ensure_fork_writer(self) -> None:
        with self.lock:
            if self.fork_writer is not None:
                return
            if self.fork_run is None:
                self.fork_run = next_fork_run_id(self.log_dir, self.base_run)
            self.fork_writer = JsonlWriter(run_path(self.log_dir, self.fork_run), overwrite=True)
            self.backtrack_summary_manager = BacktrackSummaryManager(
                run_id=self.fork_run,
                log_dir=self.log_dir,
                enabled=self.backtrack_summary_enabled,
                max_input_chars=self.backtrack_summary_max_input_chars,
            )
            fork_metadata = {
                "base_run": self.base_run,
                "breakpoint_record_uid": self.breakpoint_record_uid,
                "created_at": iso_now(),
                "fork_log_mode": self.fork_log_mode,
                "fork_reuse_policy": self.fork_reuse_policy,
                "mode": "fork",
                "post_breakpoint_default": (
                    "live" if self.fork_reuse_policy == "never" else "safe_reuse"
                ),
            }
            fork_metadata.update(self.fork_metadata)
            self.fork_writer.write({"fork_metadata": fork_metadata})
            if self.fork_log_mode == "materialized":
                self.write_materialized_prefix()

    def write_materialized_prefix(self) -> None:
        if self.fork_writer is None:
            raise RuntimeError("Replay fork writer is not open.")
        for record in self.materialized_prefix_records():
            copied = self.materialized_prefix_record(record)
            self.fork_writer.write(copied)
            self._fork_record_counter = max(
                self._fork_record_counter,
                record_uid_index(record.get("record_uid")),
            )

    def materialized_prefix_records(
        self,
        *,
        include_breakpoint: bool = False,
    ) -> list[dict[str, Any]]:
        if self.breakpoint_record_uid is None:
            return []
        prefix: list[dict[str, Any]] = []
        for record in self.raw_history:
            if record.get("fork_metadata") is not None and record.get("kind") is None:
                continue
            if record.get("kind") == "llm" and record.get("record_uid") == self.breakpoint_record_uid:
                if include_breakpoint:
                    prefix.append(record)
                break
            prefix.append(record)
        return prefix

    def match_materialized_prefix_record_by_path_id(
        self,
        *,
        kind: str,
        input_id: str,
        path_id: str,
    ) -> tuple[dict[str, Any], str] | None:
        if self.fork_log_mode != "materialized" or self.breakpoint_record_uid is None:
            return None
        candidates = [
            record
            for record in self.materialized_prefix_records(include_breakpoint=True)
            if record.get("kind") == kind
            and record.get("path_id") == path_id
            and record.get("record_uid") not in self.consumed_record_uids
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0], "materialized_prefix_path_id"
        input_matches = [record for record in candidates if record.get("input_id") == input_id]
        if len(input_matches) == 1:
            return input_matches[0], "materialized_prefix_input_id+path_id"

        from .errors import AmbiguousReplayError

        raise AmbiguousReplayError(
            "Multiple materialized prefix records match by path_id: "
            f"kind={kind}, path_id={path_id}, candidates="
            f"{[record.get('record_uid') for record in candidates]}"
        )

    def materialized_prefix_record(self, record: dict[str, Any]) -> dict[str, Any]:
        copied = copy.deepcopy(record)
        if self.fork_run is not None:
            retarget_record_sources(copied, from_run=self.base_run, to_run=self.fork_run)
        return copied

    def seed_path_allocators_from_materialized_prefix(self) -> None:
        llm_indexes: dict[str, int] = {}
        tool_indexes: dict[str, int] = {}
        for record in self.materialized_prefix_records(include_breakpoint=True):
            kind = record.get("kind")
            path_id = record.get("path_id")
            if not isinstance(path_id, str):
                continue
            if kind == "llm":
                parsed_llm = parse_llm_path_index(path_id)
                if parsed_llm is None:
                    continue
                branch_id, index = parsed_llm
                llm_indexes[branch_id] = min(index, llm_indexes.get(branch_id, index))
            elif kind == "tool":
                parsed_tool = parse_tool_path_index(path_id)
                if parsed_tool is None:
                    continue
                branch_id, index = parsed_tool
                tool_indexes[branch_id] = min(index, tool_indexes.get(branch_id, index))
        for branch_id, index in llm_indexes.items():
            self.branch_llm_indexes[branch_id] = index
        for branch_id, index in tool_indexes.items():
            self.branch_tool_indexes[branch_id] = index

    def write_workspace_fork_records(self) -> None:
        records = self.workspace_manager.fork_metadata_records()
        if not records:
            return
        self.ensure_fork_writer()
        with self.lock:
            if self.fork_writer is None:
                raise RuntimeError("Replay fork writer is not open.")
            for record in records:
                self.fork_writer.write(record)

    def write_materialization_records(self, fs_action: dict[str, Any]) -> None:
        records = self.workspace_manager.materialization_records(
            fs_action,
            base_run=self.base_run,
            fork_run=self.fork_run,
        )
        if not records:
            return
        self.ensure_fork_writer()
        with self.lock:
            if self.fork_writer is None:
                raise RuntimeError("Replay fork writer is not open.")
            for record in records:
                self.fork_writer.write(record)


def prepare_llm_call(
    session: BaseSession,
    kwargs: dict[str, Any],
    *,
    semantic_hint: str | None = None,
    provider: str = "openai",
    api: str = "chat.completions.create",
) -> PreparedCall:
    kwargs = session.workspace_manager.normalize_paths(kwargs)
    semantic = None
    provenance: dict[str, set[Source]] = {"data_sources": set(), "control_sources": set()}
    if RUNTIME.enabled:
        provenance = RUNTIME.capture_input_provenance(kwargs=kwargs)
        callsite = capture_callsite()
        semantic = {
            "callsite_fingerprint": semantic_hint
            or RUNTIME.current_record_semantic_hint()
            or callsite_fingerprint(callsite)
        }

    input_record, input_id = prepare_replayed_llm_input(
        RUNTIME.plain_snapshot(kwargs),
        provider=provider,
        api=api,
    )
    path_id = session.allocate_path_id()
    return PreparedCall(
        input_record=input_record,
        input_id=input_id,
        path_id=path_id,
        provenance=provenance,
        semantic=semantic,
    )


def prepare_replayed_llm_input(
    kwargs: dict[str, Any],
    *,
    provider: str = "openai",
    api: str = "chat.completions.create",
) -> tuple[dict[str, Any], str]:
    input_record = build_input_record(RUNTIME.plain_snapshot(kwargs), provider=provider, api=api)
    input_id = compute_input_id(input_record)
    return input_record, input_id


def prepare_tool_event(
    *,
    input_record: dict[str, Any],
    input_id: str,
    path_id: str,
    input_arguments: Any = None,
    semantic_hint: str | None = None,
) -> PreparedCall:
    semantic = None
    provenance: dict[str, set[Source]] = {"data_sources": set(), "control_sources": set()}
    if RUNTIME.enabled:
        provenance = RUNTIME.capture_input_provenance(input_arguments)
        callsite = capture_callsite()
        semantic = {
            "callsite_fingerprint": semantic_hint
            or RUNTIME.current_record_semantic_hint()
            or callsite_fingerprint(callsite)
        }
    return PreparedCall(
        input_record=RUNTIME.plain_snapshot(input_record),
        input_id=input_id,
        path_id=path_id,
        provenance=provenance,
        semantic=semantic,
    )


def semantic_callsite_fingerprint(semantic: dict[str, Any] | None) -> str | None:
    if not isinstance(semantic, dict):
        return None
    fingerprint = semantic.get("callsite_fingerprint")
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def record_callsite_fingerprint(record: dict[str, Any]) -> str | None:
    metadata = record.get("metadata")
    semantic = metadata.get("semantic") if isinstance(metadata, dict) else None
    return semantic_callsite_fingerprint(semantic)


def raise_replay_miss(*, kind: str, input_id: str, path_id: str) -> None:
    raise ReplayMissError(f"No replay record found for kind={kind}, input_id={input_id}, path_id={path_id}.")


def parse_llm_path_index(path_id: str) -> tuple[str, int] | None:
    if "/tool/" in path_id:
        return None
    branch_id, separator, index_text = path_id.rpartition("/")
    if not separator or not branch_id:
        return None
    try:
        return branch_id, int(index_text)
    except ValueError:
        return None


def parse_tool_path_index(path_id: str) -> tuple[str, int] | None:
    branch_id, separator, index_text = path_id.rpartition("/tool/")
    if not separator or not branch_id:
        return None
    try:
        return branch_id, int(index_text)
    except ValueError:
        return None


def source_for_record(run_id: str, record: dict[str, Any]) -> Source:
    return Source(
        run_id=run_id,
        record_uid=record["record_uid"],
        kind=record["kind"],
        path_id=record.get("path_id"),
    )


def edge_records_for(prepared: PreparedCall, to_source: Source) -> list[dict[str, Any]]:
    if prepared.semantic is None:
        return []
    return [
        *make_edge_records(prepared.provenance.get("data_sources"), to_source, "data"),
        *make_edge_records(prepared.provenance.get("control_sources"), to_source, "control"),
    ]


def provenance_sources(provenance: dict[str, set[Source]]) -> set[Source]:
    return set(provenance.get("data_sources") or set()) | set(provenance.get("control_sources") or set())


def record_uid_index(record_uid: Any) -> int:
    if not isinstance(record_uid, str) or not record_uid.startswith("rec_"):
        return 0
    try:
        return int(record_uid.removeprefix("rec_"))
    except ValueError:
        return 0


def retarget_record_sources(record: dict[str, Any], *, from_run: str, to_run: str) -> None:
    if record.get("run_id") == from_run:
        record["run_id"] = to_run

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        provenance = metadata.get("provenance")
        if isinstance(provenance, dict):
            for key in ("data_sources", "control_sources"):
                sources = provenance.get(key)
                if isinstance(sources, list):
                    for source in sources:
                        retarget_source(source, from_run=from_run, to_run=to_run)

    retarget_source(record.get("from"), from_run=from_run, to_run=to_run)
    retarget_source(record.get("to"), from_run=from_run, to_run=to_run)


def retarget_source(source: Any, *, from_run: str, to_run: str) -> None:
    if isinstance(source, dict) and source.get("run_id") == from_run:
        source["run_id"] = to_run


def callsite_fingerprint(callsite: dict[str, Any]) -> str | None:
    if callsite.get("file") is None:
        return None
    return f"{callsite.get('file')}:{callsite.get('function')}:{callsite.get('line')}"


def capture_callsite() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    for frame in inspect.stack()[2:]:
        filename = Path(frame.filename).resolve()
        if package_root not in filename.parents:
            return {
                "file": str(filename),
                "line": frame.lineno,
                "function": frame.function,
            }
    return {"file": None, "line": None, "function": None}


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
