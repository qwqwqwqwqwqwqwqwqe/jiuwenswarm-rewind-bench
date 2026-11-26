from __future__ import annotations

from pathlib import Path

from ...git_workspace import GitWorkspace, GitWorkspaceSpec


class FilesDomain:
    """Workspace file state via the existing GitWorkspace (direct mode)."""

    name = "files"

    def __init__(
        self,
        *,
        sandbox_root: Path,
        lineage_id: str,
        session_id: str,
        workspace_dir: Path,
    ) -> None:
        spec = GitWorkspaceSpec(
            work_root=Path(workspace_dir),
            store_dir=Path(sandbox_root) / "_fs",
            initial="work_root",
            direct=True,
            allow_project_root=True,
            allow_nested_git=True,
            restore_policy="quiesce-then-retry",
        )
        self._workspace = GitWorkspace(
            workspace_id=lineage_id,
            spec=spec,
            log_dir=Path(sandbox_root),
            run_id=session_id,
            mode="record",
        )

    @property
    def workspace_dir(self) -> Path:
        return self._workspace.root

    def setup(self) -> str:
        return self._workspace.setup_record().initial_commit

    def setup_fork(self, commit: str) -> str:
        return self._workspace.setup_fork(commit).commit

    def checkpoint(self, parent: str | None, message: str) -> str:
        return self._workspace.commit_current(parent, message).commit

    def restore(self, commit: str) -> None:
        self._workspace.restore_commit(commit)

    def diff_names(self, commit_a: str, commit_b: str) -> list[tuple[str, str]]:
        result = self._workspace._run_git(
            ["diff", "--name-status", commit_a, commit_b]
        )
        entries: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0].strip()[:1]  # R100 -> R
            entries.append((status, parts[-1]))
        return entries
