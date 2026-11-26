# replay/sandbox/gc.py
"""Reclaim checkpoint images that can no longer be rewound to.

Every mutating tool call creates an image layer, so a long session grows
without bound and eventually meets docker's layer ceiling. GC keeps what is
still reachable — the head's ancestry, a recent window, fork origins and the
session root — and drops the rest. Registry rows are never deleted: the
timeline stays complete, those checkpoints simply stop being restorable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .types import WorldCheckpoint

DEFAULT_KEEP_RECENT = 20


@dataclass
class GcPlan:
    protected: list[str] = field(default_factory=list)
    collectable: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)


@dataclass
class GcReport:
    removed_images: list[str] = field(default_factory=list)
    removed_manifests: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def plan_gc(
    checkpoints: Sequence[WorldCheckpoint],
    head_checkpoint_id: str | None,
    fork_origins: Iterable[str] = (),
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> GcPlan:
    plan = GcPlan()
    by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in checkpoints}
    order = [checkpoint.checkpoint_id for checkpoint in checkpoints]

    cursor = head_checkpoint_id
    seen: set[str] = set()
    while cursor is not None and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        plan.reasons.setdefault(cursor, "ancestor")
        cursor = by_id[cursor].parent_id
    for checkpoint_id in order[-keep_recent:] if keep_recent > 0 else []:
        plan.reasons.setdefault(checkpoint_id, "recent")
    for checkpoint_id in fork_origins:
        if checkpoint_id in by_id:
            plan.reasons.setdefault(checkpoint_id, "fork-origin")
    if order:
        plan.reasons.setdefault(order[0], "root")

    plan.protected = [cid for cid in order if cid in plan.reasons]
    plan.collectable = [cid for cid in order if cid not in plan.reasons]
    return plan


def fork_origins_in(sandbox_root: Path, lineage_id: str) -> set[str]:
    origins: set[str] = set()
    sessions_dir = Path(sandbox_root) / "sessions"
    if not sessions_dir.exists():
        return origins
    for state_path in sessions_dir.glob("*/session.json"):
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict) or payload.get("lineage_id") != lineage_id:
            continue
        forked_from = payload.get("forked_from")
        if isinstance(forked_from, dict) and forked_from.get("checkpoint_id"):
            origins.add(str(forked_from["checkpoint_id"]))
    return origins


def collect(session, plan: GcPlan, dry_run: bool = False) -> GcReport:
    report = GcReport()
    if dry_run:
        report.skipped = list(plan.collectable)
        return report
    # A squash row aliases its target's processes manifest instead of owning
    # one, so a manifest may be shared: never unlink it while any kept row
    # (registry minus this plan's collected ids) still references it, or a
    # later rewind to that kept checkpoint would restore zero services.
    collected = set(plan.collectable)
    kept_manifests = {
        kept.handles.get("processes")
        for kept in session._registry.load()
        if kept.checkpoint_id not in collected
    }
    kept_manifests.discard(None)
    for checkpoint_id in plan.collectable:
        try:
            checkpoint = session._registry.get(checkpoint_id)
        except Exception:  # noqa: BLE001 - a missing row is nothing to collect
            continue
        tag = checkpoint.handles.get("rootfs")
        if tag:
            session._docker.remove_image(tag)
            report.removed_images.append(tag)
        manifest = checkpoint.handles.get("processes")
        if manifest and manifest not in kept_manifests:
            path = session.session_dir / manifest
            if path.exists():
                path.unlink(missing_ok=True)
                report.removed_manifests.append(manifest)
    return report


def layer_depth(session) -> int:
    """Number of layers in the head checkpoint's image (docker caps at ~125)."""
    head = session.state.head_checkpoint_id
    if head is None:
        return 0
    try:
        tag = session._registry.get(head).handles.get("rootfs", "")
    except Exception:  # noqa: BLE001
        return 0
    if not tag:
        return 0
    result = session._docker._run(
        ["image", "inspect", "-f", "{{len .RootFS.Layers}}", tag], check=False
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0
