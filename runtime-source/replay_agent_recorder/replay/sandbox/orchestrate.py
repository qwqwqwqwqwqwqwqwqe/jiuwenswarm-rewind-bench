# replay/sandbox/orchestrate.py
"""Fork a checkpoint into N isolated sandboxes and explore them in parallel.

Each branch is a full session — its own container, workspace directory and
checkpoint registry — so branches cannot interfere with each other or with
the parent. This is the scaling half of the sandbox: try several approaches
at once, keep the one that worked.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Sequence

from .session import SandboxSession


@dataclass
class BranchResult:
    branch: int
    label: str
    session_id: str
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


def fork_branches(
    session: SandboxSession, count: int, checkpoint_id: str | None = None
) -> list[SandboxSession]:
    if count < 1:
        raise ValueError("count must be >= 1")
    branches: list[SandboxSession] = []
    try:
        for _ in range(count):
            branches.append(session.fork(checkpoint_id))
    except BaseException:
        # never strand half-created branches: they are running containers
        # (BaseException: Ctrl-C during a slow multi-fork must clean up too;
        # re-raised unconditionally below)
        close_branches(branches)
        raise
    return branches


def close_branches(
    branches: Sequence[SandboxSession], purge: bool = True
) -> None:
    for branch in branches:
        try:
            branch.down(purge=purge)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            print(
                f"[replay-sandbox] failed to tear down branch session "
                f"{branch.session_id}: {exc}",
                file=sys.stderr,
            )
            continue


def run_branches(
    branches: Sequence[SandboxSession],
    runner: Callable[[SandboxSession], BranchResult | None],
    max_workers: int = 4,
) -> list[BranchResult]:
    results: list[BranchResult | None] = [None] * len(branches)

    def work(index: int) -> None:
        branch = branches[index]
        try:
            outcome = runner(branch)
        except Exception as exc:  # noqa: BLE001 - one branch must not kill the rest
            results[index] = BranchResult(
                branch=index, label="", session_id=branch.session_id,
                returncode=None, stdout="", stderr="", error=repr(exc),
            )
            return
        if outcome is None:
            outcome = BranchResult(
                branch=index, label="", session_id=branch.session_id,
                returncode=None, stdout="", stderr="", error=None,
            )
        outcome.branch = index
        outcome.session_id = branch.session_id
        results[index] = outcome

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(branches)))) as pool:
        list(pool.map(work, range(len(branches))))
    return [result for result in results if result is not None]


def explore(
    session: SandboxSession,
    commands: Sequence[str],
    checkpoint_id: str | None = None,
    max_workers: int = 4,
    keep: bool = False,
) -> list[BranchResult]:
    """Run one shell command per branch, forked from the same checkpoint."""
    if not commands:
        return []
    branches = fork_branches(session, len(commands), checkpoint_id)
    try:
        def runner(branch: SandboxSession) -> BranchResult:
            index = branches.index(branch)
            command = commands[index]
            result = branch.exec(command)
            return BranchResult(
                branch=index, label=command, session_id=branch.session_id,
                returncode=result.returncode, stdout=result.stdout,
                stderr=result.stderr, error=None,
            )

        return run_branches(branches, runner, max_workers=max_workers)
    finally:
        if not keep:
            close_branches(branches)
