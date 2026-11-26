# replay/sandbox/domains/processes.py
"""Process state inside the sandbox: manifest capture + managed restart.

M3 scope is deliberately a *logical* rollback: container recreation kills
every process, then services recorded in the checkpoint's manifest are
restarted from their recorded cmdline/cwd. Live memory is not preserved
(a CRIU tier would be a later, opt-in backend).

Collection reads /proc inside the container (busybox `ps` options are not
portable). The collector script carries a marker so its own process tree
can be excluded from the manifest.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

COLLECTOR_MARKER = "replay-proc-collector"

# ppid comes from /proc/<pid>/stat AFTER ") " because comm may contain
# spaces and parentheses; readlink gives the cwd to restart services in.
COLLECTOR_SCRIPT = f"""# {COLLECTOR_MARKER}
for p in /proc/[0-9]*; do
  pid=${{p#/proc/}}
  cmd=$(tr '\\0' ' ' < "$p/cmdline" 2>/dev/null)
  [ -n "$cmd" ] || continue
  st=$(cat "$p/stat" 2>/dev/null)
  ppid=$(echo "${{st##*) }}" | cut -d' ' -f2)
  cwd=$(readlink "$p/cwd" 2>/dev/null)
  printf '%s\\t%s\\t%s\\t%s\\n' "$pid" "$ppid" "$cwd" "$cmd"
done
"""

RESTART = "restart"
LOST = "lost"

_SHELLS = {"sh", "bash", "zsh", "dash", "ash", "fish"}
_BARE_REPLS = {"python", "python3", "node", "irb", "R", "ipython"}
_INTERACTIVE_PROGRAMS = {
    "vim", "vi", "nano", "emacs", "less", "more", "top", "htop", "man",
    "ipython", "psql", "mysql", "sqlite3", "gdb", "watch",
}
_INTERACTIVE_FLAGS = {"-i", "--interactive"}


class _Docker(Protocol):
    def exec_(self, name: str, command: str, workdir: str = ...): ...
    def exec_detached(self, name: str, command: str, workdir: str = ...) -> bool: ...


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    ppid: int
    cwd: str
    cmdline: str


@dataclass
class ProcessRestoreOutcome:
    restarted: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def parse_process_table(text: str) -> list[ProcessEntry]:
    rows: list[ProcessEntry] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pid_raw, ppid_raw, cwd, cmdline = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        try:
            pid, ppid = int(pid_raw), int(ppid_raw)
        except ValueError:
            continue
        rows.append(ProcessEntry(pid=pid, ppid=ppid, cwd=cwd, cmdline=cmdline.strip()))
    excluded = {row.pid for row in rows if COLLECTOR_MARKER in row.cmdline}
    # transitive descendants of the collector shell
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.ppid in excluded and row.pid not in excluded:
                excluded.add(row.pid)
                changed = True
    excluded.add(1)
    return [row for row in rows if row.pid not in excluded]


def classify_process(
    cmdline: str, overrides: Mapping[str, str] | None = None
) -> str:
    text = cmdline.strip()
    if overrides:
        for pattern in sorted(overrides, key=len, reverse=True):
            if pattern and pattern in text:
                return overrides[pattern]
    if not text:
        return LOST
    parts = text.split()
    program = parts[0].rsplit("/", 1)[-1]
    if program in _INTERACTIVE_PROGRAMS:
        return LOST
    if program in _SHELLS and len(parts) == 1:
        return LOST
    if program in _BARE_REPLS and len(parts) == 1:
        return LOST
    if program in _BARE_REPLS and _INTERACTIVE_FLAGS.intersection(parts[1:]):
        return LOST
    return RESTART


class ProcessesDomain:
    """Container process state via /proc manifest + managed restart."""

    name = "processes"

    def __init__(
        self,
        docker: _Docker,
        container_name: str,
        store_dir: Path,
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self._docker = docker
        self._container = container_name
        self._store = Path(store_dir)
        self._overrides = dict(overrides or {})

    def snapshot(self) -> list[ProcessEntry]:
        try:
            result = self._docker.exec_(self._container, COLLECTOR_SCRIPT)
        except Exception:  # noqa: BLE001 - never break a checkpoint
            return []
        if result.returncode != 0:
            return []
        return parse_process_table(result.stdout)

    def checkpoint(self, checkpoint_id: str) -> str:
        handle = f"processes/{checkpoint_id}.json"
        entries = [
            {**asdict(entry), "policy": classify_process(entry.cmdline, self._overrides)}
            for entry in self.snapshot()
        ]
        payload = {
            "checkpoint_id": checkpoint_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        }
        path = self._store / handle
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # A manifest we cannot persist must not fail the world checkpoint;
            # load_manifest() already treats a missing file as an empty manifest.
            pass
        return handle

    def load_manifest(self, handle: str) -> list[dict]:
        path = self._store / handle
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("entries", [])
        except (ValueError, OSError):
            return []

    def restore(self, handle: str) -> ProcessRestoreOutcome:
        outcome = ProcessRestoreOutcome()
        for entry in self.load_manifest(handle):
            cmdline = entry.get("cmdline", "")
            # Overrides configured AFTER the checkpoint must still apply: the
            # documented workflow is see `lost` -> `processes --set ...` ->
            # rewind. Without overrides the manifest policy is used verbatim.
            policy = (
                classify_process(cmdline, self._overrides)
                if self._overrides
                else entry.get("policy")
            )
            if policy != RESTART:
                outcome.lost.append(cmdline)
                continue
            started = False
            try:
                started = self._docker.exec_detached(
                    self._container, cmdline, entry.get("cwd") or "/workspace"
                )
            except Exception:  # noqa: BLE001 - restart is best-effort
                started = False
            (outcome.restarted if started else outcome.failed).append(cmdline)
        return outcome
