# replay/sandbox/session.py
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .docker_client import DockerClient, DockerResult
from .domains.files import FilesDomain
from .domains.processes import ProcessesDomain
from .domains.rootfs import ContainerSpec, RootfsDomain
from .ledger import (
    IRREVERSIBLE,
    OPAQUE,
    LedgerStore,
    classify_command,
    classify_ledger_entry,
    classify_sandbox_action,
    describe_entry,
)
from .registry import CheckpointRegistry
from .types import RestoreReport, SandboxError, WorldCheckpoint

# docker/overlay2 refuses commits past ~125 layers; every rewind -> real
# work -> checkpoint cycle adds one. Re-platform during goto once the
# restore target's base is deeper than this (45 layers of headroom for
# fork children, which inherit their parent's depth).
SQUASH_LAYER_THRESHOLD = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return f"sbx_{uuid.uuid4().hex[:8]}"


def _host_user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def _default_commit_spawner(
    sandbox_root: Path, session_id: str
) -> Callable[[str, str], None]:
    """Detach a one-shot worker that performs the deferred `docker commit`."""

    def spawn(checkpoint_id: str, image_tag: str) -> None:
        # Worker output goes to a per-session log, not DEVNULL: when the
        # barrier later declares a checkpoint degraded, the root cause of the
        # failed `docker commit` must be recoverable. The child keeps its own
        # descriptor, so closing the parent's handle right away is safe.
        log_path = (
            Path(sandbox_root) / "sessions" / session_id / "commit-worker.log"
        )
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable, "-m", "replay.cli", "sandbox", "commit-worker",
                    "--session", session_id,
                    "--sandbox-root", str(sandbox_root),
                    "--checkpoint", checkpoint_id,
                ],
                start_new_session=True,
                stdout=log,
                stderr=log,
            )

    return spawn


def run_commit_worker(
    sandbox_root: Path,
    session_id: str,
    checkpoint_id: str,
    docker: DockerClient | None = None,
) -> int:
    """Perform one deferred rootfs commit and clear its pending marker.

    Deliberately does NOT construct a SandboxSession: this runs in a
    short-lived detached process and must not start a proxy daemon or take
    the session lock.
    """
    session_dir = Path(sandbox_root) / "sessions" / session_id
    marker = session_dir / "pending" / f"{checkpoint_id}.json"
    if not marker.exists():
        return 0
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("degraded"):
        # The barrier already declared this commit lost; the container may
        # have changed since, so committing now would capture a WRONG world.
        # The degraded verdict is final — leave the marker in place.
        print(
            f"[replay-sandbox commit-worker] {checkpoint_id} is already "
            "degraded; refusing a late commit",
            file=sys.stderr,
        )
        return 1
    if docker is None:
        # The detached worker only gets root+session id; a podman session's
        # deferred commit must not be driven through the docker binary.
        # SessionState.save is not atomic, and this worker races the parent:
        # a torn/unreadable session.json must fall back like a missing one,
        # not crash the worker (matching cli.py's runtime peek).
        try:
            runtime = SessionState.load(session_dir / "session.json").runtime
        except (SandboxError, ValueError, OSError):
            runtime = "docker"
        docker = DockerClient(binary=runtime, sudo=runtime == "podman")
    client = docker
    try:
        client.commit(payload["container"], payload["tag"])
    except Exception as exc:  # noqa: BLE001 - the barrier reconciles failures
        print(f"[replay-sandbox commit-worker] {exc}", file=sys.stderr)
        return 1
    # Re-read before cleanup: if the barrier timed out meanwhile and marked
    # this checkpoint degraded, the container may already have moved on and
    # the image we just committed is untrustworthy. Never erase that verdict.
    try:
        current = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        current = None
    if isinstance(current, dict) and current.get("degraded"):
        return 1
    marker.unlink(missing_ok=True)
    return 0


@dataclass
class SessionState:
    session_id: str
    lineage_id: str
    image: str
    workspace_dir: str
    container_name: str
    head_checkpoint_id: str | None
    forked_from: dict[str, str] | None
    strict_ledger: bool = False
    async_checkpoints: bool = False
    paused: bool = False
    runtime: str = "docker"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        if not path.exists():
            raise SandboxError(f"Session state not found: {path}")
        # Tolerant load: ignore unknown keys, default missing ones, so state
        # files written by older/newer versions never raise TypeError.
        payload = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


class SandboxSession:
    def __init__(
        self,
        *,
        sandbox_root: Path,
        state: SessionState,
        docker: DockerClient | None = None,
        egress_ledger: bool = True,
        commit_spawner: Callable[[str, str], None] | None = None,
    ) -> None:
        self._sandbox_root = Path(sandbox_root).resolve()
        self._state = state
        self._docker = docker or DockerClient(
            binary=state.runtime, sudo=state.runtime == "podman"
        )
        self._registry = CheckpointRegistry(self.session_dir / "checkpoints.jsonl")
        self._files = FilesDomain(
            sandbox_root=self._sandbox_root,
            lineage_id=state.lineage_id,
            session_id=state.session_id,
            workspace_dir=Path(state.workspace_dir),
        )
        self._egress_ledger = egress_ledger
        # Keep the raw injection (None when defaulted) so fork() can hand a
        # child either the same test injection or a child-bound default; the
        # resolved default is bound to THIS session's id and must not leak.
        self._injected_commit_spawner = commit_spawner
        self._commit_spawner = commit_spawner or _default_commit_spawner(
            self._sandbox_root, state.session_id
        )
        self._ledger = LedgerStore(self.session_dir / "ledger.jsonl")
        self._processes = ProcessesDomain(
            self._docker,
            container_name=state.container_name,
            store_dir=self.session_dir,
            overrides=self._load_process_overrides(),
        )
        self._sync_container_spec()  # sets self._rootfs

    # ---- construction ----

    @classmethod
    def up(
        cls,
        *,
        sandbox_root: Path,
        workspace_dir: Path,
        image: str,
        docker: DockerClient | None = None,
        egress_ledger: bool = True,
        strict_ledger: bool = False,
        async_checkpoints: bool = False,
        commit_spawner: Callable[[str, str], None] | None = None,
        runtime: str = "docker",
    ) -> "SandboxSession":
        session_id = _new_session_id()
        state = SessionState(
            session_id=session_id,
            lineage_id=session_id,
            image=image,
            workspace_dir=str(Path(workspace_dir).resolve()),
            container_name=f"replay-sbx-{session_id}",
            head_checkpoint_id=None,
            forked_from=None,
            strict_ledger=strict_ledger,
            async_checkpoints=async_checkpoints,
            runtime=runtime,
        )
        session = cls(
            sandbox_root=sandbox_root, state=state, docker=docker,
            egress_ledger=egress_ledger, commit_spawner=commit_spawner,
        )
        session._files.setup()
        session._rootfs.start()
        session._record_checkpoint(trigger="up", label="initial")
        return session

    @classmethod
    def load(
        cls,
        *,
        sandbox_root: Path,
        session_id: str,
        docker: DockerClient | None = None,
        egress_ledger: bool = True,
        commit_spawner: Callable[[str, str], None] | None = None,
    ) -> "SandboxSession":
        state_path = (
            Path(sandbox_root) / "sessions" / session_id / "session.json"
        )
        state = SessionState.load(state_path)
        return cls(
            sandbox_root=sandbox_root, state=state, docker=docker,
            egress_ledger=egress_ledger, commit_spawner=commit_spawner,
        )

    # ---- properties ----

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session_dir(self) -> Path:
        return self._sandbox_root / "sessions" / self._state.session_id

    @property
    def ledger_store(self) -> LedgerStore:
        return self._ledger

    @property
    def processes_overrides_path(self) -> Path:
        return self.session_dir / "process-policy.json"

    # ---- egress proxy lifecycle ----

    def _load_process_overrides(self) -> dict[str, str]:
        path = self.processes_overrides_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}

    def _proxy_info(self) -> dict | None:
        path = self.session_dir / "proxy.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        # A torn write can leave valid-but-wrong JSON (e.g. `5`); treating it
        # as info would crash every session operation with AttributeError.
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _proxy_alive(info: dict | None) -> bool:
        if not info or not info.get("port") or not info.get("pid"):
            return False
        try:
            os.kill(int(info["pid"]), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # EPERM: the process EXISTS (another user's); trust the port probe
        except (OSError, ValueError):
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2):
                return True
        except OSError:
            return False

    def ensure_proxy(self) -> int | None:
        """Start (or reuse) the host-side egress proxy daemon for this session."""
        if not self._egress_ledger:
            return None
        info = self._proxy_info()
        if self._proxy_alive(info):
            return int(info["port"])  # type: ignore[index]
        preferred = int(info["port"]) if info and info.get("port") else 0
        self.session_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable, "-m", "replay.cli", "sandbox", "proxy",
                "--session", self._state.session_id,
                "--sandbox-root", str(self._sandbox_root),
                "--port", str(preferred),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = self._proxy_info()
            if self._proxy_alive(info):
                return int(info["port"])  # type: ignore[index]
            time.sleep(0.1)
        print(
            "[replay-sandbox] egress proxy failed to start; "
            "network actions will not be recorded",
            file=sys.stderr,
        )
        return None

    # ---- deferred rootfs commits (async checkpoints) ----

    @property
    def pending_dir(self) -> Path:
        return self.session_dir / "pending"

    def pending_rootfs(self) -> list[str]:
        if not self.pending_dir.exists():
            return []
        return sorted(path.stem for path in self.pending_dir.glob("*.json"))

    def _marker_payload(self, checkpoint_id: str) -> dict:
        path = self.pending_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def is_degraded(self, checkpoint_id: str) -> bool:
        return bool(self._marker_payload(checkpoint_id).get("degraded"))

    def _settle_pending(self) -> list[str]:
        """Drop markers whose image already exists; return ids still in flight.

        An image present while the marker is still pending is trustworthy:
        every container-changing operation crosses the barrier first, so
        nothing can have changed the container since the marker was written.
        Already-degraded markers are final — never waited on, never revived.
        """
        unsettled: list[str] = []
        for checkpoint_id in self.pending_rootfs():
            payload = self._marker_payload(checkpoint_id)
            if payload.get("degraded"):
                continue
            tag = payload.get("tag", "")
            if tag and self._docker.image_exists(tag):
                # worker finished (or died after committing) without cleanup
                (self.pending_dir / f"{checkpoint_id}.json").unlink(missing_ok=True)
                continue
            unsettled.append(checkpoint_id)
        return unsettled

    def await_pending_rootfs(self, timeout: float = 60.0) -> list[str]:
        """Block until deferred commits settle; return degraded checkpoint ids.

        This is the barrier that makes deferral safe: `docker commit` captures
        the container's CURRENT state, so nothing may change the container
        while a commit is still outstanding.
        """
        deadline = time.monotonic() + timeout
        while self._settle_pending():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        degraded: list[str] = []
        for checkpoint_id in self.pending_rootfs():
            payload = self._marker_payload(checkpoint_id)
            marker = self.pending_dir / f"{checkpoint_id}.json"
            if payload.get("degraded"):
                # Final verdict: an image that shows up after degradation was
                # committed from a container that may have changed — even if
                # `image_exists(tag)` now says true, never trust or clear it.
                degraded.append(checkpoint_id)
                continue
            tag = payload.get("tag", "")
            if tag and self._docker.image_exists(tag):
                marker.unlink(missing_ok=True)   # worker finished, never cleaned up
                continue
            marker.write_text(
                json.dumps({**payload, "degraded": True}, sort_keys=True),
                encoding="utf-8",
            )
            print(
                f"[replay-sandbox] rootfs commit for {checkpoint_id} did not "
                "complete; that checkpoint can no longer be rewound to",
                file=sys.stderr,
            )
            degraded.append(checkpoint_id)
        return degraded

    def stop_proxy(self) -> bool:
        """Terminate the recorded egress-proxy daemon and drop proxy.json.

        Tolerates an already-dead or foreign-owned pid. Returns True only if
        a live pid was actually signalled."""
        path = self.session_dir / "proxy.json"
        info = self._proxy_info()
        signalled = False
        if info and info.get("pid"):
            try:
                os.kill(int(info["pid"]), signal.SIGTERM)
                signalled = True
            except (ProcessLookupError, PermissionError, ValueError):
                pass
        if path.exists():
            path.unlink(missing_ok=True)
        return signalled

    def _sync_container_spec(self) -> None:
        """Rebuild the container spec with the current proxy port (env is
        baked in at every container start, so all start paths agree)."""
        port = self.ensure_proxy()
        extra_env: tuple[tuple[str, str], ...] = ()
        add_hosts: tuple[str, ...] = ()
        if port:
            url = f"http://host.docker.internal:{port}"
            extra_env = (
                ("HTTP_PROXY", url), ("HTTPS_PROXY", url),
                ("http_proxy", url), ("https_proxy", url),
                ("NO_PROXY", "localhost,127.0.0.1"),
                ("no_proxy", "localhost,127.0.0.1"),
            )
            add_hosts = ("host.docker.internal:host-gateway",)
        self._rootfs = RootfsDomain(
            self._docker,
            ContainerSpec(
                image=self._state.image,
                name=self._state.container_name,
                workspace_dir=self._state.workspace_dir,
                session_id=self._state.session_id,
                user=_host_user(),
                extra_env=extra_env,
                add_hosts=add_hosts,
            ),
        )

    # ---- concurrency ----

    @contextmanager
    def _session_lock(self) -> Iterator[None]:
        """Cross-process mutual exclusion for state-mutating operations.

        Hooks and the MCP server run as separate processes against the same
        session; the lock plus a state reload serializes head/registry moves.
        """
        lock_path = self.session_dir / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _reload_state(self) -> None:
        path = self.session_dir / "session.json"
        if path.exists():
            self._state = SessionState.load(path)

    # ---- operations ----

    def exec(self, command: str) -> DockerResult:
        if self._state.paused:
            raise SandboxError(
                "sandbox is paused; call resume before running commands"
            )
        # Barrier: the command may change the container; a still-outstanding
        # `docker commit` would otherwise capture the post-exec world.
        self.await_pending_rootfs()
        pattern = classify_command(command)
        if pattern:
            self._ledger.append(
                {"kind": "command", "command": command, "pattern": pattern}
            )
        action = classify_sandbox_action(command)
        if action:
            category, matched = action
            self._ledger.append(
                {
                    "kind": "action", "command": command,
                    "category": category, "pattern": matched,
                }
            )
        return self._docker.exec_(self._state.container_name, command)

    def set_strict_ledger(self, enabled: bool) -> None:
        """Persist the strict-ledger setting (used when wrap reuses a session)."""
        with self._session_lock():
            self._reload_state()
            if self._state.strict_ledger != enabled:
                self._state.strict_ledger = enabled
                self._save_state()

    def set_async_checkpoints(self, enabled: bool) -> None:
        """Persist the async-checkpoint preference (used when wrap reuses a session)."""
        with self._session_lock():
            self._reload_state()
            if self._state.async_checkpoints != enabled:
                self._state.async_checkpoints = enabled
                self._save_state()

    def checkpoint(
        self, label: str | None = None, trigger: str = "manual"
    ) -> WorldCheckpoint:
        with self._session_lock():
            self._reload_state()
            if self._state.paused:
                # docker exec always fails on a frozen container, so the
                # process snapshot would silently come back empty and the
                # checkpoint would claim "no processes" — a lie that rewind
                # and sandbox_diff would later repeat.
                raise SandboxError(
                    "sandbox is paused; a checkpoint taken now would record "
                    "an empty process manifest. Call resume before "
                    "checkpointing."
                )
            return self._record_checkpoint(trigger=trigger, label=label)

    def rewind(
        self,
        checkpoint_id: str,
        reason: str | None = None,
        force: bool = False,
        force_squash: bool = False,
    ) -> RestoreReport:
        # Barrier before the lock: the restore replaces the container, and an
        # outstanding commit must land (or degrade loudly) before that.
        self.await_pending_rootfs()
        with self._session_lock():
            self._reload_state()
            # Validate the target first: an unknown checkpoint_id must have
            # zero side effects (no safety checkpoint, no restore).
            checkpoint = self._registry.get(checkpoint_id)
            if self.is_degraded(checkpoint_id):
                raise SandboxError(
                    f"checkpoint {checkpoint_id} is degraded: its "
                    "rootfs image was never committed, so the world cannot be "
                    "restored from it"
                )
            # `replay sandbox gc` removes checkpoint images but keeps the
            # registry rows, so the timeline still lists them. Restoring one
            # would `rm -f` the container and then fail the `docker run` from
            # the removed image — leaving no world at all. Refuse up front
            # (zero side effects); the barrier above already settled pending
            # commits, so a missing image really means collected.
            rootfs_tag = checkpoint.handles.get("rootfs", "")
            if rootfs_tag and not self._docker.image_exists(rootfs_tag):
                raise SandboxError(
                    f"checkpoint {checkpoint_id} can no longer be restored: its "
                    f"rootfs image {rootfs_tag} was reclaimed by `replay sandbox "
                    "gc`. It stays in the timeline for history, but it is no "
                    "longer a destination."
                )
            old_head = self._state.head_checkpoint_id
            # Ledger watermark: entries with seq >= the target's recorded
            # count happened after that checkpoint; irreversible/opaque ones
            # are rolled over, not undone. Old checkpoints without a "ledger"
            # handle count as watermark 0.
            watermark = int(checkpoint.handles.get("ledger", "0") or 0)
            crossed = [
                row for row in self._ledger.load() if row["seq"] >= watermark
            ]
            uncovered = [
                row for row in crossed
                if classify_ledger_entry(row) in (IRREVERSIBLE, OPAQUE)
            ]
            # The strict gate must fire BEFORE the safety checkpoint: a
            # refused rewind is a pure refusal (no docker commit, no registry
            # row, no HEAD move).
            if uncovered and self._state.strict_ledger and not force:
                raise SandboxError(
                    "strict ledger mode: this rewind would roll over "
                    f"{len(uncovered)} external action(s) that cannot be undone: "
                    + "; ".join(describe_entry(row) for row in uncovered[:10])
                    + ". Re-run with force=True to accept this."
                )
            # A paused (frozen) container cannot be exec'd into, so the
            # safety checkpoint below would record a lying empty process
            # manifest. Unpause first — best-effort: if it fails, continue,
            # since the container is about to be destroyed anyway and the
            # paused flag is cleared after the restore below.
            if self._state.paused:
                self._docker.unpause_container(self._state.container_name)
            # Auto-save the CURRENT world before restoring, so a rewind never
            # destroys uncheckpointed work and is itself undoable.
            # (_record_checkpoint is lock-free; we already hold the lock.)
            safety = self._record_checkpoint(
                trigger="pre-rewind", label=f"auto-save before rewind to {checkpoint_id}"
            )
            from .navigation import goto_direction

            report = RestoreReport(
                checkpoint_id=checkpoint_id,
                reason=reason,
                direction=goto_direction(
                    [json.loads(c.to_json()) for c in self._registry.load()],
                    old_head,
                    checkpoint_id,
                ),
            )
            report.rewound_over = self._chain_between(old_head, checkpoint_id)
            report.ledger_uncovered = [describe_entry(row) for row in uncovered[:20]]
            if len(uncovered) > 20:
                report.ledger_uncovered.append(f"... and {len(uncovered) - 20} more")
            # Exact diff: target vs the just-taken safety checkpoint, so
            # uncheckpointed edits are included.
            diff = self._files.diff_names(
                checkpoint.handles["files"], safety.handles["files"]
            )
            report.files_diff = [
                f"{status}\t{path}" for status, path in diff[:50]
            ]
            if len(diff) > 50:
                report.files_diff.append(f"... and {len(diff) - 50} more")
            # In async mode the safety checkpoint above deferred its commit;
            # it must capture the PRE-rewind container, so drain it before
            # the restore below destroys and recreates that container. If the
            # drain degrades that very checkpoint, the pre-rewind world is
            # unreachable and "rewind is undoable" would be a lie — abort
            # while nothing has been restored yet (only an additive registry
            # row and a head move happened).
            degraded_now = self.await_pending_rootfs()
            if safety.checkpoint_id in degraded_now:
                raise SandboxError(
                    f"aborting rewind: the pre-rewind safety checkpoint "
                    f"{safety.checkpoint_id} could not be committed, so this "
                    "rewind would not be undoable; the world was left untouched"
                )
            # Re-sync right before recreating the container: a long-lived
            # session object must not bake a stale proxy port into it.
            self._sync_container_spec()
            # Re-platform a deep base while the old container still exists:
            # the flat image is produced BEFORE `rm -f`, so a squash failure
            # can never leave the session without a world. image_layers None
            # (missing image, daemon hiccup, or a fake runner) skips the
            # optimisation instead of guessing.
            restore_tag = checkpoint.handles["rootfs"]
            layers = (
                self._docker.image_layers(restore_tag) if restore_tag else None
            )
            squash_note: str | None = None
            flat_row: WorldCheckpoint | None = None
            if layers is not None and (
                force_squash or layers > SQUASH_LAYER_THRESHOLD
            ):
                flat_id = self._registry.next_checkpoint_id()
                flat_tag = self._rootfs.image_tag(flat_id)
                if self._docker.squash_image(restore_tag, flat_tag):
                    restore_tag = flat_tag
                    flat_row = WorldCheckpoint(
                        checkpoint_id=flat_id,
                        parent_id=checkpoint_id,
                        handles={**checkpoint.handles, "rootfs": flat_tag},
                        trigger="squash",
                        label=f"squash of {checkpoint_id}",
                        created_at=_now(),
                    )
                    # Register the flat image the moment it exists: the row is
                    # content-identical to the target whatever happens next,
                    # and a restore failure below must not leave a
                    # successfully-created image invisible to gc/--purge.
                    self._registry.append(flat_row)
                else:
                    squash_note = (
                        f"squash failed; restored from the original "
                        f"{layers}-layer image"
                    )
            elif force_squash and layers is None:
                squash_note = "could not inspect the base image; squash skipped"
            self._rootfs.restore(restore_tag)
            # The restore replaced the (possibly frozen) container with a
            # fresh, RUNNING one; a stale paused flag would refuse every exec
            # while resume() has nothing left to unpause. Persisted by the
            # _save_state() below.
            self._state.paused = False
            report.domain_results["rootfs"] = (
                f"container recreated from {restore_tag}"
            )
            self._files.restore(checkpoint.handles["files"])
            if self._files.workspace_dir != Path(self._state.workspace_dir):
                raise SandboxError(
                    "files restore relocated the workspace "
                    f"({self._state.workspace_dir} -> {self._files.workspace_dir}); "
                    "sandbox mount is no longer consistent"
                )
            report.domain_results["files"] = (
                f"workspace restored to {checkpoint.handles['files'][:12]}"
            )
            report.domain_results["safety"] = (
                f"current state saved as {safety.checkpoint_id} "
                "(rewind is undoable)"
            )
            # Restart services recorded in the target's process manifest
            # (missing handle on legacy checkpoints => empty manifest).
            outcome = self._processes.restore(checkpoint.handles.get("processes", ""))
            report.processes_restarted = outcome.restarted
            report.processes_lost = outcome.lost + outcome.failed
            report.domain_results["processes"] = (
                f"{len(outcome.restarted)} service(s) restarted, "
                f"{len(outcome.lost)} not restartable, {len(outcome.failed)} failed"
            )
            report.domain_results["ledger"] = (
                f"{len(uncovered)} external action(s) crossed by this rewind "
                "cannot be undone"
            )
            report.notes = [
                "Live process memory is not preserved: services were restarted "
                "from their recorded command line, so in-memory state is gone.",
                "External side effects listed above were recorded, not undone; "
                "verify remote state yourself before continuing.",
            ]
            if flat_row is not None:
                report.replatform_layers = layers
                report.replatform_checkpoint_id = flat_row.checkpoint_id
            if squash_note:
                report.notes.append(squash_note)
            self._state.head_checkpoint_id = (
                flat_row.checkpoint_id if flat_row is not None else checkpoint_id
            )
            self._save_state()
            return report

    def _chain_between(
        self, from_id: str | None, target_id: str
    ) -> list[str]:
        """Checkpoint ids from from_id (inclusive) down to target_id
        (exclusive), following parent links; [] if unreachable, identical,
        or the registry's parent links are corrupt (cycle)."""
        if from_id is None or from_id == target_id:
            return []
        by_id = {c.checkpoint_id: c for c in self._registry.load()}
        chain: list[str] = []
        seen: set[str] = set()
        cursor: str | None = from_id
        while cursor is not None and cursor != target_id:
            if cursor in seen:
                return []  # corrupt registry: cycle in parent links
            seen.add(cursor)
            node = by_id.get(cursor)
            if node is None:
                return []
            chain.append(cursor)
            cursor = node.parent_id
        return chain if cursor == target_id else []

    def fork(self, checkpoint_id: str | None = None) -> "SandboxSession":
        # Barrier before the lock: `docker tag` below needs the source image
        # to really exist (or the target to be refused as degraded).
        self.await_pending_rootfs()
        with self._session_lock():
            self._reload_state()
            target_id = (
                checkpoint_id if checkpoint_id is not None else self._state.head_checkpoint_id
            )
            if target_id is None:
                raise SandboxError("Session has no checkpoints to fork from")
            checkpoint = self._registry.get(target_id)
            if self.is_degraded(target_id):
                raise SandboxError(
                    f"checkpoint {target_id} is degraded: its "
                    "rootfs image was never committed, so the world cannot be "
                    "restored from it"
                )

            child_id = _new_session_id()
            child_dir = self._sandbox_root / "sessions" / child_id
            child_workspace = child_dir / "workspace"
            child_workspace.mkdir(parents=True, exist_ok=True)
            child_state = SessionState(
                session_id=child_id,
                lineage_id=self._state.lineage_id,
                image=self._state.image,
                workspace_dir=str(child_workspace),
                container_name=f"replay-sbx-{child_id}",
                head_checkpoint_id=None,
                forked_from={
                    "session_id": self._state.session_id,
                    "checkpoint_id": target_id,
                },
                # The child must inherit strict mode: sandbox_fork is
                # agent-invocable, so dropping it here would be a one-call
                # escape hatch from the safety gate.
                strict_ledger=self._state.strict_ledger,
                async_checkpoints=self._state.async_checkpoints,
                runtime=self._state.runtime,
            )
            # Pass the raw injection (tests stay detach-free), not the
            # resolved spawner: the parent's default is bound to the PARENT
            # session id and would drive workers against the wrong session.
            child = SandboxSession(
                sandbox_root=self._sandbox_root, state=child_state,
                docker=self._docker, egress_ledger=self._egress_ledger,
                commit_spawner=self._injected_commit_spawner,
            )
            files_sha = child._files.setup_fork(checkpoint.handles["files"])
            child_tag = child._rootfs.image_tag("ckpt_000000")
            self._docker.tag(checkpoint.handles["rootfs"], child_tag)
            child._rootfs.start(from_image=child_tag)
            child_checkpoint = WorldCheckpoint(
                checkpoint_id="ckpt_000000",
                parent_id=None,
                handles={"rootfs": child_tag, "files": files_sha},
                trigger=f"fork:{self._state.session_id}:{target_id}",
                label=None,
                created_at=_now(),
            )
            child._registry.append(child_checkpoint)
            child._state.head_checkpoint_id = "ckpt_000000"
            child._save_state()
            return child

    def timeline(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        checkpoints = self._registry.load()
        by_id = {c.checkpoint_id: c for c in checkpoints}
        for checkpoint in checkpoints:
            row = json.loads(checkpoint.to_json())
            row["is_head"] = (
                checkpoint.checkpoint_id == self._state.head_checkpoint_id
            )
            parent = (
                by_id.get(checkpoint.parent_id) if checkpoint.parent_id else None
            )
            if parent is None:
                row["files_changed"] = None
            else:
                diff = self._files.diff_names(
                    parent.handles["files"], checkpoint.handles["files"]
                )
                row["files_changed"] = {
                    "added": sum(1 for s, _ in diff if s == "A"),
                    "modified": sum(1 for s, _ in diff if s in {"M", "R"}),
                    "deleted": sum(1 for s, _ in diff if s == "D"),
                }
            rows.append(row)
        return rows

    def checkpoint_count(self) -> int:
        return len(self._registry.load())

    def ensure_up(self) -> str:
        """Make sure the sandbox container exists and runs; return how."""
        # Barrier: a start/recreate changes the container an outstanding
        # commit would capture.
        self.await_pending_rootfs()
        # A recreate must bake in the CURRENT proxy port, not the one from
        # construction time.
        self._sync_container_spec()
        name = self._state.container_name
        if self._docker.container_running(name):
            return "running"
        if self._docker.start_container(name):
            return "started"
        with self._session_lock():
            self._reload_state()
            head = self._state.head_checkpoint_id
            if head is None:
                raise SandboxError(
                    "Session has no checkpoints; cannot recreate container"
                )
            if self.is_degraded(head):
                raise SandboxError(
                    f"cannot recreate the container: head checkpoint {head} is "
                    "degraded (its rootfs image was never committed at the right "
                    "moment), so restoring from it could produce a wrong world"
                )
            self._rootfs.restore(self._registry.get(head).handles["rootfs"])
        return "recreated"

    def pause(self) -> bool:
        """Freeze every process in the sandbox. Returns True if state changed."""
        # A frozen container cannot be committed, so any deferred rootfs
        # commit must land before we freeze it.
        self.await_pending_rootfs()
        with self._session_lock():
            self._reload_state()
            if self._state.paused:
                return False
            if not self._docker.pause_container(self._state.container_name):
                return False
            self._state.paused = True
            self._save_state()
            return True

    def resume(self) -> bool:
        """Unfreeze the sandbox. Returns True if state changed."""
        with self._session_lock():
            self._reload_state()
            if not self._state.paused:
                return False
            if not self._docker.unpause_container(self._state.container_name):
                if self._docker.container_paused(self._state.container_name) is not False:
                    # Still frozen (True) — or the probe itself failed (None).
                    # Only a positively-verified "not paused" may heal the
                    # flag: persisting paused=False on a daemon hiccup would
                    # claim a running world while it is genuinely frozen.
                    return False
                # unpause failed because there was nothing to unpause (e.g.
                # the frozen container was replaced by a running one): the
                # stale flag is the only thing wrong — heal it. The world is
                # running, which is what the caller asked for.
            self._state.paused = False
            self._save_state()
            return True

    def squash(self, force: bool = False) -> RestoreReport | None:
        """Re-platform the CURRENT world onto a single-layer base.

        checkpoint-then-goto composition: the checkpoint captures any
        uncheckpointed work first (a bare goto-HEAD would silently roll it
        back) and crosses zero ledger entries. Returns None when the base
        is already flat — no container churn for nothing. The probe runs
        outside the session lock (checkpoint and rewind take it
        themselves; the fcntl lock is not re-entrant); the worst TOCTOU
        outcome is squashing a base that just became flat, which is
        harmless and manual-only.

        `force` reaches the strict-ledger gate: the async proxy can append
        an entry between the pre-squash checkpoint's watermark read and
        the rewind's ledger load, and this surface has no other way to
        accept the roll-over — which is safe here, because the restore
        target is content-identical to the world that produced the entry.
        """
        # Fresh read before the probe: a long-lived session object must not
        # no-op on a stale flat head after another process moved HEAD.
        self._reload_state()
        head_id = self._state.head_checkpoint_id
        if head_id is not None:
            head_tag = self._registry.get(head_id).handles.get("rootfs", "")
            layers = self._docker.image_layers(head_tag) if head_tag else None
            if layers is not None and layers <= 2:
                return None
        checkpoint = self.checkpoint(label="pre-squash", trigger="squash-request")
        return self.rewind(
            checkpoint.checkpoint_id, reason="manual squash",
            force=force, force_squash=True,
        )

    def down(self, purge: bool = False) -> None:
        # Barrier (bounded): removing the container kills any in-flight
        # commit; give it a short window to land, then degrade loudly.
        self.await_pending_rootfs(timeout=10.0)
        # Stop the host-side proxy daemon FIRST: once teardown (or a purge)
        # discards session artifacts the recorded pid would be untrackable
        # and the daemon would leak forever.
        self.stop_proxy()
        self._rootfs.down()
        if purge:
            for checkpoint in self._registry.load():
                self._docker.remove_image(checkpoint.handles["rootfs"])

    # ---- internals ----

    def _record_checkpoint(
        self, *, trigger: str, label: str | None
    ) -> WorldCheckpoint:
        # Barrier: only one rootfs commit may be outstanding at a time, and
        # it must capture the container as it was when ITS checkpoint was cut.
        self.await_pending_rootfs()
        checkpoint_id = self._registry.next_checkpoint_id()
        parent_id = self._state.head_checkpoint_id
        parent_files = (
            self._registry.get(parent_id).handles["files"] if parent_id else None
        )
        files_sha = self._files.checkpoint(
            parent_files, f"sandbox checkpoint {self._state.session_id}:{checkpoint_id}"
        )
        if self._state.async_checkpoints:
            # Deterministic tag: the registry row can be written before the
            # commit lands (append-only semantics unchanged). The marker is
            # the source of truth for "not landed yet".
            rootfs_tag = self._rootfs.image_tag(checkpoint_id)
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            (self.pending_dir / f"{checkpoint_id}.json").write_text(
                json.dumps(
                    {
                        "checkpoint_id": checkpoint_id,
                        "tag": rootfs_tag,
                        "container": self._state.container_name,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            self._commit_spawner(checkpoint_id, rootfs_tag)
        else:
            rootfs_tag = self._rootfs.checkpoint(checkpoint_id)
        handles = {
            "rootfs": rootfs_tag,
            "files": files_sha,
            "processes": self._processes.checkpoint(checkpoint_id),
            "ledger": str(self._ledger.count()),
        }
        checkpoint = WorldCheckpoint(
            checkpoint_id=checkpoint_id,
            parent_id=parent_id,
            handles=handles,
            trigger=trigger,
            label=label,
            created_at=_now(),
        )
        self._registry.append(checkpoint)
        self._state.head_checkpoint_id = checkpoint_id
        self._save_state()
        return checkpoint

    def _save_state(self) -> None:
        self._state.save(self.session_dir / "session.json")
