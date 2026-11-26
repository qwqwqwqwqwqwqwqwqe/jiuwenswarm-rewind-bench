# replay/sandbox/navigation.py
"""Navigating the checkpoint DAG: layout, direction, and diffs.

The registry is append-only, so a rewind does not delete the checkpoints it
moved away from — it just stops being their descendant. That makes the
history a DAG rather than a list, and an agent deciding where to go needs to
see which branch is live, which was abandoned, and which was forked off.
Everything here is a pure function over data the session already has.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DEEP = "deep"
LIGHT = "light"
FLAT = "flat"


@dataclass(frozen=True)
class TimelineNode:
    checkpoint_id: str
    parent_id: str | None
    on_mainline: bool
    is_head: bool
    grade: str
    trigger: str
    label: str | None
    created_at: str
    files_changed: dict | None
    forked_to: tuple[str, ...]


def _mainline_ids(
    rows: Sequence[Mapping], head_checkpoint_id: str | None
) -> set[str]:
    if head_checkpoint_id is None:
        return set()
    return _ancestry(rows, head_checkpoint_id)


def goto_direction(
    rows: Sequence[Mapping],
    head_checkpoint_id: str | None,
    target_id: str,
) -> str:
    """Which way the world is about to move, computed BEFORE it moves."""
    if head_checkpoint_id == target_id:
        return "same"
    if head_checkpoint_id is None:
        return "branch-switch"
    if target_id in _ancestry(rows, head_checkpoint_id):
        return "back"
    if head_checkpoint_id in _ancestry(rows, target_id):
        return "forward"
    return "branch-switch"


def _ancestry(rows: Sequence[Mapping], start_id: str) -> set[str]:
    by_id = {row["checkpoint_id"]: row for row in rows}
    seen: set[str] = set()
    cursor: str | None = start_id
    while cursor is not None and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        cursor = by_id[cursor].get("parent_id")
    return seen


def build_timeline_view(
    rows: Sequence[Mapping],
    head_checkpoint_id: str | None,
    fork_children: Mapping[str, Sequence[str]] | None = None,
) -> list[TimelineNode]:
    """Newest-first view of the checkpoint DAG."""
    mainline = _mainline_ids(rows, head_checkpoint_id)
    children = fork_children or {}
    nodes = [
        TimelineNode(
            checkpoint_id=row["checkpoint_id"],
            parent_id=row.get("parent_id"),
            on_mainline=row["checkpoint_id"] in mainline,
            is_head=row["checkpoint_id"] == head_checkpoint_id,
            grade=(
                FLAT
                if str(row.get("trigger", "")) == "squash"
                else DEEP if "memory" in (row.get("handles") or {}) else LIGHT
            ),
            trigger=str(row.get("trigger", "")),
            label=row.get("label"),
            created_at=str(row.get("created_at", "")),
            files_changed=row.get("files_changed"),
            forked_to=tuple(children.get(row["checkpoint_id"], ())),
        )
        for row in rows
    ]
    nodes.reverse()
    return nodes


def render_timeline(nodes: Sequence[TimelineNode], cap: int = 30) -> str:
    if not nodes:
        return "(no checkpoints yet)"
    shown = list(nodes[:cap])
    lines: list[str] = []
    if len(nodes) > len(shown):
        lines.append(f"(showing newest {len(shown)} of {len(nodes)} checkpoints)")
    for node in shown:
        marker = "* " if node.on_mainline else "  x "
        head = "  HEAD" if node.is_head else ""
        changed = node.files_changed
        files = (
            f"  files +{changed['added']} ~{changed['modified']} -{changed['deleted']}"
            if changed
            else ""
        )
        label = f"  label={node.label}" if node.label else ""
        forked = (
            "  → forked to " + ", ".join(node.forked_to) if node.forked_to else ""
        )
        abandoned = "" if node.on_mainline else "  (abandoned by a later goto)"
        lines.append(
            f"{marker}{node.checkpoint_id} [{node.grade}]{head}  "
            f"{node.trigger}{files}{label}{forked}{abandoned}"
        )
    return "\n".join(lines)


def fork_children_in(
    sandbox_root: Path, lineage_id: str, session_id: str
) -> dict[str, list[str]]:
    """checkpoint id -> session ids forked from THAT checkpoint of THIS session.

    Checkpoint ids restart at ckpt_000000 in every session, so matching on
    lineage alone would attribute a fork of one session to the same-numbered
    checkpoint of every other session in the lineage (a forked child's own
    timeline would even render "forked to <itself>"). The recorded
    forked_from.session_id keeps the attribution exact."""
    mapping: dict[str, list[str]] = {}
    sessions_dir = Path(sandbox_root) / "sessions"
    if not sessions_dir.exists():
        return mapping
    for state_path in sorted(sessions_dir.glob("*/session.json")):
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict) or payload.get("lineage_id") != lineage_id:
            continue
        forked_from = payload.get("forked_from")
        if not isinstance(forked_from, dict):
            continue
        if forked_from.get("session_id") != session_id:
            continue
        origin = forked_from.get("checkpoint_id")
        child = payload.get("session_id")
        if origin and child:
            mapping.setdefault(str(origin), []).append(str(child))
    return mapping


@dataclass
class DiffReport:
    from_id: str
    to_id: str
    files: list[str]
    processes_added: list[str]
    processes_removed: list[str]
    ledger: list[str]
    notes: list[str]

    def to_text(self) -> str:
        lines = [f"Diff {self.from_id} -> {self.to_id}"]
        if not (
            self.files or self.processes_added or self.processes_removed
            or self.ledger
        ):
            lines.append("No differences recorded between these checkpoints.")
        if self.files:
            lines.append("Workspace files:")
            lines.extend(f"  {entry}" for entry in self.files)
        if self.processes_added:
            lines.append("Processes started:")
            lines.extend(f"  {entry}" for entry in self.processes_added)
        if self.processes_removed:
            lines.append("Processes gone:")
            lines.extend(f"  {entry}" for entry in self.processes_removed)
        if self.ledger:
            lines.append("External actions recorded in between:")
            lines.extend(f"  {entry}" for entry in self.ledger)
        lines.extend(f"NOTE: {note}" for note in self.notes)
        return "\n".join(lines)


def build_diff(session, from_id: str, to_id: str, cap: int = 50) -> DiffReport:
    """What changed between two checkpoints, from data already recorded."""
    from .ledger import classify_ledger_entry, describe_entry

    start = session._registry.get(from_id)
    end = session._registry.get(to_id)
    report = DiffReport(
        from_id=from_id, to_id=to_id, files=[], processes_added=[],
        processes_removed=[], ledger=[], notes=[],
    )

    diff = session._files.diff_names(start.handles["files"], end.handles["files"])
    report.files = [f"{status}\t{path}" for status, path in diff[:cap]]
    if len(diff) > cap:
        report.files.append(f"... and {len(diff) - cap} more")

    before = {
        entry.get("cmdline", "")
        for entry in session._processes.load_manifest(
            start.handles.get("processes", "")
        )
    }
    after = {
        entry.get("cmdline", "")
        for entry in session._processes.load_manifest(
            end.handles.get("processes", "")
        )
    }
    report.processes_added = sorted(c for c in after - before if c)
    report.processes_removed = sorted(c for c in before - after if c)

    low = int(start.handles.get("ledger", "0") or 0)
    high = int(end.handles.get("ledger", "0") or 0)
    if high > low:
        rows = [row for row in session.ledger_store.load() if low <= row["seq"] < high]
        report.ledger = [
            f"{describe_entry(row)}  [{classify_ledger_entry(row)}]"
            for row in rows[:cap]
        ]
        if len(rows) > cap:
            report.ledger.append(f"... and {len(rows) - cap} more")
    if high < low:
        report.notes.append(
            "the second checkpoint predates the first, so no external actions "
            "are listed"
        )
    return report
