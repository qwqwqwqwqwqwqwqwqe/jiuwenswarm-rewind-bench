from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import UnsupportedTraceSchemaError


class JsonlWriter:
    def __init__(self, path: Path, *, overwrite: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if overwrite else "a"
        self.path = path
        self._file = path.open(mode, encoding="utf-8", newline="\n")

    def write(self, item: dict[str, Any]) -> None:
        stamped = dict(item)
        stamped.setdefault("timestamp", _iso_now())
        line = json.dumps(stamped, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def run_path(log_dir: Path, run_id: str) -> Path:
    return log_dir / f"{run_id}.jsonl"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def backtrack_events_path(log_dir: Path, run_id: str) -> Path:
    return log_dir / f"{run_id}.backtrack_events.jsonl"


def backtrack_summaries_path(log_dir: Path, run_id: str) -> Path:
    return log_dir / f"{run_id}.backtrack_summaries.jsonl"


def backtrack_memory_path(log_dir: Path, run_id: str) -> Path:
    return log_dir / f"{run_id}.backtrack_memory.jsonl"


def make_record_uid(index: int) -> str:
    return f"rec_{index:06d}"


def load_records(
    path: Path,
    *,
    kind: str | None = None,
    kinds: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"Replay log not found: {path}")
    if kind is not None and kinds is not None:
        raise ValueError("Pass either kind or kinds, not both.")
    allowed_kinds = {kind} if kind is not None else set(kinds) if kinds is not None else None

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
            if allowed_kinds is None or item.get("kind") in allowed_kinds:
                records.append(item)
    return records


def load_replay_records(path: Path) -> list[dict[str, Any]]:
    return load_records(path, kinds={"llm", "tool"})


def load_raw_records(path: Path) -> list[dict[str, Any]]:
    return load_records(path)


def load_workspace_records(path: Path) -> list[dict[str, Any]]:
    return load_records(path, kind="workspace")


def load_filesystem_materialization_records(path: Path) -> list[dict[str, Any]]:
    return load_records(path, kind="filesystem_materialization")


def find_record_by_uid(records: Sequence[dict[str, Any]], record_uid: str) -> dict[str, Any]:
    for record in records:
        if record.get("record_uid") == record_uid:
            return record
    raise KeyError(f"Record not found: {record_uid}")


def workspace_metadata_by_id(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    workspaces: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "workspace" or record.get("backend") != "git":
            continue
        workspace_id = record.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            workspaces[workspace_id] = record
    return workspaces


def reject_legacy_filesystem_schema(records: Iterable[dict[str, Any]]) -> None:
    for record in records:
        legacy_effects = record.get("effects")
        if not isinstance(legacy_effects, dict):
            continue
        legacy_payload = legacy_effects.get("file" "system")
        if not isinstance(legacy_payload, dict):
            continue
        if "changes" in legacy_payload:
            raise UnsupportedTraceSchemaError(
                "Legacy filesystem effects schema is not supported for Git workspace replay: "
                f"record_uid={record.get('record_uid')!r}"
            )


def load_llm_records(path: Path) -> list[dict[str, Any]]:
    return load_records(path, kind="llm")


_FORK_SUFFIX_RE = re.compile(r"(?:_fork_\d{3})+$")


def next_fork_run_id(log_dir: Path, base_run: str) -> str:
    fork_stem = _FORK_SUFFIX_RE.sub("", base_run) or base_run
    for index in range(1, 10000):
        fork_run = f"{fork_stem}_fork_{index:03d}"
        if not run_path(log_dir, fork_run).exists():
            return fork_run
    raise RuntimeError(f"Unable to allocate fork run id for {base_run}.")
