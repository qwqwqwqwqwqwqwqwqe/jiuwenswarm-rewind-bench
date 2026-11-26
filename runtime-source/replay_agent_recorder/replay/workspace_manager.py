from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import time
import threading
from typing import Any, Iterator, Literal, Sequence
import weakref

from .errors import (
    FilesystemReplayConflictError,
    UnsupportedTraceSchemaError,
    WorkspaceNotFoundError,
)
from .git_workspace import (
    GitCheckpoint,
    GitWorkspace,
    GitWorkspaceSpec,
    WorkspaceInitMetadata,
    WorkspaceRestorePolicy,
)
from .path_normalization import (
    PathMapping,
    denormalize_workspace_paths,
    normalize_workspace_paths,
)
from .storage import JsonlWriter


@dataclass
class FilesystemNodeContext:
    mode: Literal["record", "replay", "fork"]
    kind: str
    path_id: str
    record_uid: str
    before: dict[str, GitCheckpoint]
    locked: bool = False
    async_locked: bool = False


@dataclass
class SettledFilesystemNode:
    metadata: dict[str, Any]
    after: dict[str, GitCheckpoint]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _recorded_filter_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    items = tuple(item for item in value if isinstance(item, str) and item)
    return items or default


def _recorded_path_aliases(record: dict[str, Any], default: tuple[str, ...]) -> tuple[str, ...]:
    value = record.get("path_aliases")
    if isinstance(value, list):
        items = tuple(item for item in value if isinstance(item, str) and item)
        if items:
            return items
    paths = record.get("paths")
    if isinstance(paths, dict):
        aliases = paths.get("aliases")
        if isinstance(aliases, list):
            items = tuple(item for item in aliases if isinstance(item, str) and item)
            if items:
                return items
    return default


def _recorded_path_denormalize_root(record: dict[str, Any], default: str | None) -> str | None:
    value = record.get("path_denormalize_root")
    if isinstance(value, str) and value:
        return value
    paths = record.get("paths")
    if isinstance(paths, dict):
        value = paths.get("denormalize_root")
        if isinstance(value, str) and value:
            return value
    return default


def _symlink_policy_from_spec(spec: GitWorkspaceSpec) -> str:
    return "safe-internal" if spec.allow_symlinks else "deny"


def _allow_symlinks_from_record(record: dict[str, Any]) -> bool:
    policy = record.get("symlink_policy", "deny")
    if policy == "safe-internal":
        return True
    if policy == "deny":
        return False
    raise UnsupportedTraceSchemaError(f"Unsupported workspace symlink_policy: {policy!r}")


def _recorded_restore_policy(record: dict[str, Any]) -> WorkspaceRestorePolicy:
    policy = record.get("restore_policy", GitWorkspaceSpec().restore_policy)
    if policy in {"generation-fallback", "quiesce-then-retry", "strict"}:
        return policy
    raise UnsupportedTraceSchemaError(f"Unsupported workspace restore_policy: {policy!r}")


class GitWorkspaceManager:
    def __init__(
        self,
        *,
        specs: Mapping[str, GitWorkspaceSpec],
        log_dir: Path,
        symlink_policy_override: bool | None = None,
    ) -> None:
        self.specs = dict(specs)
        self.log_dir = log_dir
        self.symlink_policy_override = symlink_policy_override
        self.workspaces: dict[str, GitWorkspace] = {}
        self.mode: Literal["record", "replay", "fork"] | None = None
        self.base_run: str | None = None
        self.run_id: str | None = None
        self.fork_active = False
        self.fork_started_from_record_uid: str | None = None
        self._fork_metadata_records: list[dict[str, Any]] = []
        self._fork_start_snapshot: dict[str, Any] | None = None
        self._node_lock = threading.RLock()
        self._async_node_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    def _has_workspaces(self) -> bool:
        return bool(self.workspaces)

    def _async_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._async_node_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._async_node_locks[loop] = lock
        return lock

    async def begin_node_async(
        self,
        kind: str,
        path_id: str,
        record_uid: str,
    ) -> FilesystemNodeContext:
        if self._has_workspaces():
            async_lock = self._async_lock()
            await async_lock.acquire()
        else:
            async_lock = None
        try:
            ctx = self.begin_node(kind=kind, path_id=path_id, record_uid=record_uid)
        finally:
            if async_lock is not None:
                async_lock.release()
        return ctx

    def on_record_enter(self, run_id: str, writer: JsonlWriter) -> None:
        self.mode = "record"
        self.run_id = run_id
        for workspace_id, spec in self.specs.items():
            workspace = GitWorkspace(
                workspace_id=workspace_id,
                spec=spec,
                log_dir=self.log_dir,
                run_id=run_id,
                mode="record",
            )
            metadata = workspace.setup_record()
            self.workspaces[workspace_id] = workspace
            writer.write(self.workspace_metadata_record(metadata, workspace))

    def on_replay_enter(
        self,
        *,
        base_run: str,
        replay_run: str,
        workspace_records: Sequence[dict[str, Any]],
        preserve_current: bool = False,
    ) -> None:
        self.mode = "replay"
        self.base_run = base_run
        self.run_id = replay_run
        records_by_id = {}
        for record in workspace_records:
            if record.get("backend") != "git":
                raise UnsupportedTraceSchemaError(
                    "Unsupported workspace backend for replay: "
                    f"workspace_id={record.get('workspace_id')!r} backend={record.get('backend')!r}"
                )
            workspace_id = record.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id:
                raise UnsupportedTraceSchemaError("Workspace metadata record is missing workspace_id.")
            records_by_id[workspace_id] = record

        for workspace_id in self.specs:
            if workspace_id not in records_by_id:
                raise WorkspaceNotFoundError(
                    f"Workspace declared for replay but not found in base run: {workspace_id}"
                )

        for workspace_id, record in records_by_id.items():
            repo_dir_value = record.get("repo_dir")
            initial_commit = record.get("initial_commit")
            if not isinstance(repo_dir_value, str) or not isinstance(initial_commit, str):
                raise UnsupportedTraceSchemaError(
                    f"Workspace metadata is incomplete: workspace_id={workspace_id!r}"
                )
            recorded_allow_symlinks = _allow_symlinks_from_record(record)
            base_spec = self.specs.get(workspace_id)
            if base_spec is None:
                recorded_filters = record.get("filters") if isinstance(record.get("filters"), dict) else {}
                include = _recorded_filter_tuple(recorded_filters.get("include"), GitWorkspaceSpec().include)
                exclude = _recorded_filter_tuple(recorded_filters.get("exclude"), GitWorkspaceSpec().exclude)
                volatile_exclude = _recorded_filter_tuple(
                    recorded_filters.get("volatile_exclude"),
                    GitWorkspaceSpec().volatile_exclude,
                )
                base_spec = GitWorkspaceSpec(
                    include=include,
                    exclude=exclude,
                    volatile_exclude=volatile_exclude,
                    allow_symlinks=recorded_allow_symlinks,
                    allow_project_root=bool(record.get("direct")),
                    allow_nested_git=bool(record.get("direct")),
                    direct=bool(record.get("direct")),
                    restore_policy=_recorded_restore_policy(record),
                    transient_patterns=_recorded_filter_tuple(
                        record.get("transient_patterns"),
                        GitWorkspaceSpec().transient_patterns,
                    ),
                    path_aliases=_recorded_path_aliases(record, GitWorkspaceSpec().path_aliases),
                    path_denormalize_root=_recorded_path_denormalize_root(
                        record,
                        GitWorkspaceSpec().path_denormalize_root,
                    ),
                )
            if self.symlink_policy_override is None:
                allow_symlinks = base_spec.allow_symlinks or recorded_allow_symlinks
            else:
                allow_symlinks = self.symlink_policy_override
            record_work_root = record.get("work_root")
            record_direct = bool(record.get("direct"))
            default_work_root_run = (
                replay_run if replay_run != f"{base_run}_replay" else f"{base_run}__replay"
            )
            if base_spec.work_root is not None:
                replay_work_root = Path(base_spec.work_root)
            elif record_direct and isinstance(record_work_root, str):
                replay_work_root = Path(record_work_root)
            else:
                replay_work_root = self.log_dir / "_workspaces" / default_work_root_run / workspace_id
            replay_work_root.mkdir(parents=True, exist_ok=True)
            spec = GitWorkspaceSpec(
                base_root=None,
                work_root=replay_work_root,
                store_dir=Path(repo_dir_value).parent,
                include=base_spec.include,
                exclude=base_spec.exclude,
                volatile_exclude=base_spec.volatile_exclude,
                initial="empty",
                allow_symlinks=allow_symlinks,
                allow_project_root=base_spec.allow_project_root or record_direct,
                allow_nested_git=base_spec.allow_nested_git or record_direct,
                keep_worktree=base_spec.keep_worktree,
                chdir=base_spec.chdir,
                direct=base_spec.direct or record_direct,
                restore_policy=base_spec.restore_policy,
                transient_patterns=base_spec.transient_patterns,
                restore_process_policy=base_spec.restore_process_policy,
                path_aliases=base_spec.path_aliases
                or _recorded_path_aliases(record, GitWorkspaceSpec().path_aliases),
                path_denormalize_root=(
                    base_spec.path_denormalize_root
                    if base_spec.path_denormalize_root is not None
                    else _recorded_path_denormalize_root(record, GitWorkspaceSpec().path_denormalize_root)
                ),
            )
            workspace = GitWorkspace(
                workspace_id=workspace_id,
                spec=spec,
                log_dir=self.log_dir,
                run_id=replay_run,
                mode="replay",
                base_run=base_run,
            )
            if preserve_current:
                workspace.setup_replay_preserve_current(initial_commit=initial_commit)
            else:
                workspace.setup_replay(initial_commit=initial_commit)
            self.workspaces[workspace_id] = workspace

    def workspace_metadata_record(
        self,
        metadata: WorkspaceInitMetadata,
        workspace: GitWorkspace,
    ) -> dict[str, Any]:
        return {
            "kind": "workspace",
            "schema_version": 3,
            "backend": "git",
            "created_at": _iso_now(),
            "workspace_id": metadata.workspace_id,
            "logical_root": metadata.logical_root,
            "repo": {
                "type": "bare",
                "path": str(metadata.repo_dir),
            },
            "repo_dir": str(metadata.repo_dir),
            "work_root": str(metadata.work_root),
            "index_file": str(metadata.index_file),
            "branch_ref": metadata.branch_ref,
            "initial_commit": metadata.initial_commit,
            "initial_tree": metadata.initial_tree,
            "direct": workspace.spec.direct,
            "symlink_policy": _symlink_policy_from_spec(workspace.spec),
            "restore_policy": workspace.spec.restore_policy,
            "restore_policy_source": "spec",
            "transient_patterns": list(workspace.spec.transient_patterns),
            "generation_id": metadata.generation_id,
            "runtime_git_mode": metadata.runtime_git_mode,
            "path_aliases": list(workspace.spec.path_aliases),
            "path_denormalize_root": workspace.spec.path_denormalize_root,
            "filters": {
                "include": list(workspace.spec.include),
                "exclude": list(workspace.spec.exclude),
                "volatile_exclude": list(workspace.spec.volatile_exclude),
            },
        }

    def fork_from_record_before(
        self,
        *,
        record: dict[str, Any],
        fork_run: str,
    ) -> None:
        record_uid = record.get("record_uid")
        if self.fork_active:
            if self.fork_started_from_record_uid == record_uid:
                return
            return
        filesystem = self.filesystem_metadata_from_record(record)
        self._fork_start_snapshot = {
            "mode": self.mode,
            "run_id": self.run_id,
            "workspaces": {
                workspace_id: {
                    "run_id": workspace.run_id,
                    "branch_ref": workspace.branch_ref,
                    "commit": workspace.current_commit(),
                }
                for workspace_id, workspace in self.workspaces.items()
            },
        }
        self.mode = "fork"
        self.run_id = fork_run
        self.fork_active = True
        self.fork_started_from_record_uid = record_uid if isinstance(record_uid, str) else None
        self._fork_metadata_records = []
        for workspace_id, workspace_metadata in filesystem["workspaces"].items():
            if workspace_id not in self.workspaces:
                raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")
            if not isinstance(workspace_metadata, dict):
                raise UnsupportedTraceSchemaError(
                    "Invalid filesystem workspace metadata: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            before_commit = workspace_metadata.get("before_commit")
            before_tree = workspace_metadata.get("before_tree")
            if not isinstance(before_commit, str) or not isinstance(before_tree, str):
                raise UnsupportedTraceSchemaError(
                    "Filesystem metadata missing before commit/tree: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            workspace = self.workspaces[workspace_id]
            workspace._run_id = fork_run
            checkpoint = workspace.setup_fork(fork_from_commit=before_commit)
            restore_report = workspace.consume_last_restore_report()
            self._fork_metadata_records.append(
                {
                    "kind": "workspace_fork",
                    "schema_version": 3,
                    "backend": "git",
                    "base_run": self.base_run,
                    "fork_run": fork_run,
                    "breakpoint_record_uid": record_uid,
                    "workspace_id": workspace_id,
                    "forked_from_commit": checkpoint.commit,
                    "forked_from_tree": checkpoint.tree,
                    "branch_ref": workspace.branch_ref,
                    "work_root": str(workspace.root),
                    "generation_id": workspace.generation_id,
                    "runtime_git_mode": workspace.runtime_git_mode,
                    "restore": restore_report,
                }
            )

    def preserve_fork_from_current(
        self,
        *,
        record: dict[str, Any],
        fork_run: str,
    ) -> None:
        record_uid = record.get("record_uid")
        if self.fork_active:
            if self.fork_started_from_record_uid == record_uid:
                return
            return
        self._fork_start_snapshot = {
            "mode": self.mode,
            "run_id": self.run_id,
            "workspaces": {
                workspace_id: {
                    "run_id": workspace.run_id,
                    "mode": workspace.mode,
                    "branch_ref": workspace.branch_ref,
                    "commit": workspace.current_commit(),
                }
                for workspace_id, workspace in self.workspaces.items()
            },
        }
        self.mode = "fork"
        self.run_id = fork_run
        self.fork_active = True
        self.fork_started_from_record_uid = record_uid if isinstance(record_uid, str) else None
        self._fork_metadata_records = []
        for workspace_id, workspace in self.workspaces.items():
            before_commit = workspace.current_commit()
            before_tree = workspace.commit_tree(before_commit)
            workspace._run_id = fork_run
            workspace._mode = "fork"
            workspace._branch_ref = f"refs/replay/runs/{fork_run}/workspaces/{workspace_id}"
            checkpoint = workspace.commit_current(
                before_commit,
                f"preserve current workspace for {fork_run}:{record_uid}",
                allow_empty=True,
                update_ref=True,
            )
            self._fork_metadata_records.append(
                {
                    "kind": "workspace_fork",
                    "schema_version": 3,
                    "backend": "git",
                    "base_run": self.base_run,
                    "fork_run": fork_run,
                    "breakpoint_record_uid": record_uid,
                    "workspace_id": workspace_id,
                    "workspace_policy": "preserve",
                    "forked_from_commit": checkpoint.commit,
                    "forked_from_tree": checkpoint.tree,
                    "preserved_from_commit": before_commit,
                    "preserved_from_tree": before_tree,
                    "branch_ref": workspace.branch_ref,
                    "work_root": str(workspace.root),
                    "generation_id": workspace.generation_id,
                    "runtime_git_mode": workspace.runtime_git_mode,
                    "restore": {"strategy": "preserve-current-worktree"},
                }
            )

    def fork_metadata_records(self) -> list[dict[str, Any]]:
        records = list(self._fork_metadata_records)
        self._fork_metadata_records = []
        return records

    def rollback_fork_start(self, record: dict[str, Any]) -> None:
        record_uid = record.get("record_uid")
        if not self.fork_active or self.fork_started_from_record_uid != record_uid:
            return
        snapshot = self._fork_start_snapshot or {}
        for workspace_id, workspace_snapshot in snapshot.get("workspaces", {}).items():
            if workspace_id not in self.workspaces:
                continue
            workspace = self.workspaces[workspace_id]
            workspace._run_id = workspace_snapshot["run_id"]
            workspace._mode = workspace_snapshot.get("mode", workspace.mode)
            workspace._branch_ref = workspace_snapshot["branch_ref"]
            workspace.restore_commit(workspace_snapshot["commit"])
            workspace._run_git(["update-ref", workspace.branch_ref, workspace_snapshot["commit"]])
        self.mode = snapshot.get("mode", "replay")
        self.run_id = snapshot.get("run_id", self.run_id)
        self.fork_active = False
        self.fork_started_from_record_uid = None
        self._fork_metadata_records = []
        self._fork_start_snapshot = None

    def workspace(self, workspace_id: str) -> GitWorkspace:
        try:
            return self.workspaces[workspace_id]
        except KeyError as exc:
            raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}") from exc

    def begin_node(self, kind: str, path_id: str, record_uid: str) -> FilesystemNodeContext:
        return self._begin_node(
            kind=kind,
            path_id=path_id,
            record_uid=record_uid,
            defer_ambient=False,
        )

    def begin_deferred_node(
        self,
        kind: str,
        path_id: str,
        record_uid: str,
        *,
        defer_ambient: bool = False,
        precomputed_before: dict[str, GitCheckpoint] | None = None,
    ) -> FilesystemNodeContext:
        return self._begin_node(
            kind=kind,
            path_id=path_id,
            record_uid=record_uid,
            defer_ambient=defer_ambient,
            precomputed_before=precomputed_before,
        )

    async def begin_deferred_node_async(
        self,
        kind: str,
        path_id: str,
        record_uid: str,
        *,
        defer_ambient: bool = False,
        precomputed_before: dict[str, GitCheckpoint] | None = None,
    ) -> FilesystemNodeContext:
        if self._has_workspaces():
            async_lock = self._async_lock()
            await async_lock.acquire()
        else:
            async_lock = None
        try:
            return self._begin_node(
                kind=kind,
                path_id=path_id,
                record_uid=record_uid,
                defer_ambient=defer_ambient,
                precomputed_before=precomputed_before,
            )
        finally:
            if async_lock is not None:
                async_lock.release()

    def _begin_node(
        self,
        *,
        kind: str,
        path_id: str,
        record_uid: str,
        defer_ambient: bool,
        precomputed_before: dict[str, GitCheckpoint] | None = None,
    ) -> FilesystemNodeContext:
        if self._has_workspaces():
            self._node_lock.acquire()
        before: dict[str, GitCheckpoint] = {}
        try:
            if self.mode is None:
                mode: Literal["record", "replay", "fork"] = "record"
            else:
                mode = self.mode
            if precomputed_before is not None:
                missing = set(self.workspaces) - set(precomputed_before)
                unknown = set(precomputed_before) - set(self.workspaces)
                if missing or unknown:
                    raise WorkspaceNotFoundError(
                        "Precomputed checkpoint workspace mismatch: "
                        f"missing={sorted(missing)!r} unknown={sorted(unknown)!r}"
                    )
                before = dict(precomputed_before)
                return FilesystemNodeContext(
                    mode=mode,
                    kind=kind,
                    path_id=path_id,
                    record_uid=record_uid,
                    before=before,
                )
            for workspace_id, workspace in self.workspaces.items():
                head_commit = workspace.current_commit()
                head_tree = workspace.commit_tree(head_commit)
                current_tree = workspace.current_tree()
                if current_tree != head_tree and not defer_ambient:
                    checkpoint = workspace.commit_current(
                        head_commit,
                        f"ambient changes before {self.run_id}:{record_uid} {kind} {path_id}",
                        allow_empty=False,
                        update_ref=True,
                    )
                else:
                    checkpoint = GitCheckpoint(
                        commit=head_commit,
                        tree=head_tree,
                        parent=None,
                        changed_from_parent=False,
                    )
                before[workspace_id] = checkpoint
            return FilesystemNodeContext(
                mode=mode,
                kind=kind,
                path_id=path_id,
                record_uid=record_uid,
                before=before,
            )
        finally:
            if self._has_workspaces():
                self._node_lock.release()

    def _release_node_lock(self, ctx: FilesystemNodeContext) -> None:
        if ctx.locked:
            ctx.locked = False
            self._node_lock.release()
        if ctx.async_locked:
            ctx.async_locked = False
            self._async_lock().release()

    def restore_before(self, ctx: FilesystemNodeContext) -> None:
        for workspace_id, workspace in self.workspaces.items():
            before = ctx.before[workspace_id]
            workspace.restore_commit(before.commit)
            workspace._run_git(["update-ref", workspace.branch_ref, before.commit])

    def finalize_failed_node(self, ctx: FilesystemNodeContext) -> None:
        try:
            with self._node_lock:
                self.restore_before(ctx)
        finally:
            self._release_node_lock(ctx)

    def defer_node(self, ctx: FilesystemNodeContext) -> None:
        self._release_node_lock(ctx)

    async def settle_deferred_node_async(self, ctx: FilesystemNodeContext) -> dict[str, Any]:
        async_locked = False
        if self._has_workspaces():
            async_lock = self._async_lock()
            await async_lock.acquire()
            async_locked = True
        try:
            return self.settle_deferred_node(ctx)
        finally:
            if async_locked:
                async_lock.release()

    def settle_deferred_node(self, ctx: FilesystemNodeContext) -> dict[str, Any]:
        return self.settle_deferred_node_with_checkpoints(ctx).metadata

    def settle_deferred_node_with_checkpoints(self, ctx: FilesystemNodeContext) -> SettledFilesystemNode:
        metadata: dict[str, Any] = {"backend": "git", "workspaces": {}}
        after_checkpoints: dict[str, GitCheckpoint] = {}
        with self._node_lock:
            for workspace_id, workspace in self.workspaces.items():
                before_commit = workspace.current_commit()
                before_tree = workspace.commit_tree(before_commit)
                after = workspace.commit_current(
                    before_commit,
                    f"record {self.run_id}:{ctx.record_uid} {ctx.kind} {ctx.path_id}",
                    allow_empty=True,
                    update_ref=True,
                )
                after_checkpoints[workspace_id] = after
                metadata["workspaces"][workspace_id] = {
                    "before_commit": before_commit,
                    "before_tree": before_tree,
                    "after_commit": after.commit,
                    "after_tree": after.tree,
                    "changed": before_tree != after.tree,
                    "branch_ref": workspace.branch_ref,
                    "diff_summary": workspace.diff_summary(before_tree, after.tree),
                }
        return SettledFilesystemNode(metadata=metadata, after=after_checkpoints)

    def rollback_settled_node(self, filesystem: dict[str, Any]) -> None:
        with self._node_lock:
            workspaces = filesystem.get("workspaces")
            if not isinstance(workspaces, dict):
                return
            for workspace_id, workspace_metadata in workspaces.items():
                if workspace_id not in self.workspaces or not isinstance(workspace_metadata, dict):
                    continue
                before_commit = workspace_metadata.get("before_commit")
                if not isinstance(before_commit, str):
                    continue
                workspace = self.workspaces[workspace_id]
                workspace.restore_commit(before_commit)
                workspace._run_git(["update-ref", workspace.branch_ref, before_commit])

    def abort_node(self, ctx: FilesystemNodeContext) -> None:
        try:
            with self._node_lock:
                self.restore_before(ctx)
        finally:
            self._release_node_lock(ctx)

    def filesystem_metadata_from_record(self, record: dict[str, Any]) -> dict[str, Any]:
        legacy_effects = record.get("effects")
        if isinstance(legacy_effects, dict):
            legacy_payload = legacy_effects.get("file" "system")
            if isinstance(legacy_payload, dict) and "changes" in legacy_payload:
                raise UnsupportedTraceSchemaError(
                    "Legacy filesystem effects schema is not supported for Git workspace replay: "
                    f"record_uid={record.get('record_uid')!r}"
                )
        metadata = record.get("metadata")
        filesystem = metadata.get("filesystem") if isinstance(metadata, dict) else None
        if filesystem is None:
            if self._has_workspaces():
                raise UnsupportedTraceSchemaError(
                    "Replay record is missing Git filesystem metadata: "
                    f"record_uid={record.get('record_uid')!r}"
                )
            return {"backend": "git", "workspaces": {}}
        if not isinstance(filesystem, dict):
            raise UnsupportedTraceSchemaError(
                f"Invalid filesystem metadata: record_uid={record.get('record_uid')!r}"
            )
        if filesystem.get("backend") != "git":
            raise UnsupportedTraceSchemaError(
                "Unsupported filesystem backend for replay: "
                f"record_uid={record.get('record_uid')!r} backend={filesystem.get('backend')!r}"
            )
        workspaces = filesystem.get("workspaces")
        if not isinstance(workspaces, dict):
            raise UnsupportedTraceSchemaError(
                f"Invalid filesystem workspace metadata: record_uid={record.get('record_uid')!r}"
            )
        if self._has_workspaces():
            active_workspace_ids = set(self.workspaces)
            record_workspace_ids = set(workspaces)
            missing_workspace_ids = active_workspace_ids - record_workspace_ids
            if missing_workspace_ids:
                raise UnsupportedTraceSchemaError(
                    "Replay record is missing active workspace filesystem metadata: "
                    f"record_uid={record.get('record_uid')!r} "
                    f"workspace_ids={sorted(missing_workspace_ids)!r}"
                )
            unknown_workspace_ids = record_workspace_ids - active_workspace_ids
            if unknown_workspace_ids:
                raise UnsupportedTraceSchemaError(
                    "Replay record has unknown workspace filesystem metadata: "
                    f"record_uid={record.get('record_uid')!r} "
                    f"workspace_ids={sorted(unknown_workspace_ids)!r}"
                )
        return filesystem

    def can_replay(self, record: dict[str, Any]) -> bool:
        filesystem = self.filesystem_metadata_from_record(record)
        workspaces = filesystem["workspaces"]
        for workspace_id, workspace_metadata in workspaces.items():
            if workspace_id not in self.workspaces:
                raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")
            if not isinstance(workspace_metadata, dict):
                raise UnsupportedTraceSchemaError(
                    "Invalid filesystem workspace metadata: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            before_tree = workspace_metadata.get("before_tree")
            if not isinstance(before_tree, str):
                raise UnsupportedTraceSchemaError(
                    "Filesystem metadata missing before_tree: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            if self.workspaces[workspace_id].current_tree() != before_tree:
                return False
        return True

    def assert_compatible(self, record: dict[str, Any]) -> None:
        filesystem = self.filesystem_metadata_from_record(record)
        workspaces = filesystem["workspaces"]
        for workspace_id, workspace_metadata in workspaces.items():
            if workspace_id not in self.workspaces:
                raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")
            if not isinstance(workspace_metadata, dict):
                raise UnsupportedTraceSchemaError(
                    "Invalid filesystem workspace metadata: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            expected_before_tree = workspace_metadata.get("before_tree")
            if not isinstance(expected_before_tree, str):
                raise UnsupportedTraceSchemaError(
                    "Filesystem metadata missing before_tree: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            current_tree = self.workspaces[workspace_id].current_tree()
            if current_tree != expected_before_tree:
                raise FilesystemReplayConflictError(
                    "Filesystem replay conflict: "
                    f"record_uid={record.get('record_uid')!r} "
                    f"workspace_id={workspace_id!r} "
                    f"current_tree={current_tree} "
                    f"expected_before_tree={expected_before_tree}"
                )

    # In fork sessions this path is only expected when ReplaySession is using
    # fork_reuse_policy="safe" and an actual post-fork boundary matched a base
    # record that is safe to reuse. The default fork policy is "never", which
    # returns live decisions before any base record is matched after the first
    # fork node has been written.
    def restore_after_replayed_record(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.fork_active:
            return self.materialize_replayed_record_on_fork(record)
        return self.restore_after_record(record)

    @contextmanager
    def replay_lock(self) -> Iterator[None]:
        with self._node_lock:
            yield

    def restore_after_record(self, record: dict[str, Any]) -> dict[str, Any]:
        filesystem = self.filesystem_metadata_from_record(record)
        restored: dict[str, Any] = {"backend": "git", "workspaces": {}}
        for workspace_id, workspace_metadata in filesystem["workspaces"].items():
            if workspace_id not in self.workspaces:
                raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")
            if not isinstance(workspace_metadata, dict):
                raise UnsupportedTraceSchemaError(
                    "Invalid filesystem workspace metadata: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            after_commit = workspace_metadata.get("after_commit")
            if not isinstance(after_commit, str):
                raise UnsupportedTraceSchemaError(
                    "Filesystem metadata missing after_commit: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            checkpoint = self.workspaces[workspace_id].restore_commit(after_commit)
            self.workspaces[workspace_id]._run_git(
                ["update-ref", self.workspaces[workspace_id].branch_ref, checkpoint.commit]
            )
            restored["workspaces"][workspace_id] = {
                "commit": checkpoint.commit,
                "tree": checkpoint.tree,
                "work_root": str(self.workspaces[workspace_id].root),
                "generation_id": self.workspaces[workspace_id].generation_id,
                "runtime_git_mode": self.workspaces[workspace_id].runtime_git_mode,
                "restore": self.workspaces[workspace_id].consume_last_restore_report(),
            }
        return restored

    def materialize_replayed_record_on_fork(self, record: dict[str, Any]) -> dict[str, Any]:
        """Materialize a matched base record only for fork_reuse_policy="safe" forks.

        This helper does not imply the default fork flow reuses or names forks after
        the base replay suffix; default forks still follow the live fork policy path.
        """
        filesystem = self.filesystem_metadata_from_record(record)
        materialized: dict[str, Any] = {
            "backend": "git",
            "materialized": True,
            "base_record_uid": record.get("record_uid"),
            "workspaces": {},
        }
        for workspace_id, workspace_metadata in filesystem["workspaces"].items():
            if workspace_id not in self.workspaces:
                raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")
            if not isinstance(workspace_metadata, dict):
                raise UnsupportedTraceSchemaError(
                    "Invalid filesystem workspace metadata: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            after_tree = workspace_metadata.get("after_tree")
            if not isinstance(after_tree, str):
                raise UnsupportedTraceSchemaError(
                    "Filesystem metadata missing after_tree: "
                    f"record_uid={record.get('record_uid')!r} workspace_id={workspace_id!r}"
                )
            workspace = self.workspaces[workspace_id]
            before_commit = workspace.current_commit()
            before_tree = workspace.commit_tree(before_commit)
            checkpoint = workspace.materialize_tree_as_commit(
                tree=after_tree,
                parent=before_commit,
                message=f"materialize {self.run_id}:{record.get('record_uid')} on fork",
            )
            materialized["workspaces"][workspace_id] = {
                "before_commit": before_commit,
                "before_tree": before_tree,
                "after_commit": checkpoint.commit,
                "after_tree": checkpoint.tree,
                "changed": before_tree != checkpoint.tree,
                "branch_ref": workspace.branch_ref,
                "diff_summary": workspace.diff_summary(before_tree, checkpoint.tree),
            }
        return materialized

    def materialization_records(
        self,
        fs_action: dict[str, Any],
        *,
        base_run: str,
        fork_run: str | None,
    ) -> list[dict[str, Any]]:
        if not fs_action.get("materialized"):
            return []
        return [
            {
                "kind": "filesystem_materialization",
                "schema_version": 3,
                "backend": "git",
                "base_run": base_run,
                "fork_run": fork_run,
                "base_record_uid": fs_action.get("base_record_uid"),
                "workspaces": fs_action.get("workspaces", {}),
            }
        ]

    def path_mappings(self) -> list[PathMapping]:
        return [
            PathMapping(
                workspace_id=workspace.workspace_id,
                real_root=workspace.root,
                logical_root=workspace.logical_root,
                aliases=workspace.spec.path_aliases,
                denormalize_root=workspace.spec.path_denormalize_root,
            )
            for workspace in self.workspaces.values()
        ]

    def normalize_paths(self, value: Any) -> Any:
        mappings = self.path_mappings()
        if not mappings:
            return value
        return normalize_workspace_paths(value, mappings)

    def denormalize_paths(self, value: Any) -> Any:
        mappings = self.path_mappings()
        if not mappings:
            return value
        return denormalize_workspace_paths(value, mappings)
