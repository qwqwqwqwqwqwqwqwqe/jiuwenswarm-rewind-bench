"""Git future-history isolation for disposable benchmark containers."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GitHistoryPrune:
    output: str = ""
    skipped: bool = False
    error: str | None = None


def prune_git_future_history(
    container_name: str,
    workspace: str = "/testbed",
    timeout: int = 300,
) -> GitHistoryPrune:
    """Remove future Git history from a disposable agent workspace."""
    workdir = shlex.quote(workspace)
    script = f"""
set -e
cd {workdir}
git rev-parse --git-dir >/dev/null 2>&1 || {{
    echo not_a_git_repo
    exit 0
}}
head_before=$(git rev-parse HEAD)
git checkout --detach >/dev/null 2>&1 || true
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' | while read -r ref; do
    [ -n "$ref" ] && git update-ref -d "$ref" || true
done
git reflog expire --expire=now --all
git gc --prune=now --quiet
refs_left=$(git for-each-ref | wc -l)
head_after=$(git rev-parse HEAD)
[ "$refs_left" -eq 0 ] || {{ echo "refs_remaining=$refs_left"; exit 3; }}
[ "$head_before" = "$head_after" ] || {{ echo "head_moved:$head_before->$head_after"; exit 4; }}
echo pruned_ok
"""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GitHistoryPrune(error=f"git future history prune timed out (>{timeout}s)")
    except Exception as exc:
        return GitHistoryPrune(error=f"git future history prune failed: {exc}")

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return GitHistoryPrune(
            output=output,
            error=f"git future history prune failed (rc={result.returncode}): {output}",
        )
    if "not_a_git_repo" in output:
        return GitHistoryPrune(output=output, skipped=True)
    if "pruned_ok" not in output:
        return GitHistoryPrune(
            output=output,
            error=f"git future history prune self-check failed: {output}",
        )
    return GitHistoryPrune(output=output)
