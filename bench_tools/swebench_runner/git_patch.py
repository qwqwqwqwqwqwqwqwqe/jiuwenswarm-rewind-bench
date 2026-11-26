"""Git patch collection for disposable benchmark containers."""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class GitPatchCapture:
    patch: str = ""
    status: str = ""
    error: str | None = None
    excluded_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitWorkspaceBaseline:
    tree: str = ""
    error: str | None = None


def _temporary_index_path() -> str:
    return f"/tmp/benchrunner-index-{uuid.uuid4().hex}"


# awk 剥离器: 删除 marker 锚定块 (marker 注释行 + 其后连续的非注释非空行), 再裁掉
# 尾部空行 (agent 追加块前通常垫了一个空行)。未命中任何 marker 时 exit 42, 调用方
# 据此跳过改写, 保证未受污染的 .gitignore 字节不动 (含末尾无换行的文件)。
_GITIGNORE_STRIP_AWK = (
    "BEGIN { n = split(markers, M, \"\\034\"); changed = 0; len = 0 } "
    "{ "
    "if (skip) { "
    "if ($0 ~ /^[ \\t]*#/ || $0 ~ /^[ \\t]*$/) { skip = 0 } "
    "else { next } } "
    "for (i = 1; i <= n; i++) { "
    "if (length(M[i]) && index($0, M[i]) == 1) { skip = 1; changed = 1; next } } "
    "buf[++len] = $0 } "
    "END { "
    "if (!changed) { exit 42 } "
    "while (len > 0 && buf[len] ~ /^[ \\t]*$/) { len-- } "
    "for (j = 1; j <= len; j++) { print buf[j] } }"
)


def _gitignore_strip_command(
    workspace: str,
    markers: Sequence[str],
) -> str:
    """Build the in-container command that strips runtime marker blocks.

    Agents (JiuwenSwarm) hide their own runtime state by appending marker
    comment blocks to ``.gitignore`` files. That edit lives in a *tracked*
    file, so the path-based ``exclude_paths`` mechanism cannot remove it.
    This command rewrites every git-visible ``.gitignore`` (tracked or
    untracked) with the marker blocks removed; a file that becomes empty and
    is absent from ``HEAD`` is deleted. Twin implementation:
    ``workbuddy_bench/agents/jiuwenswarm_agent.py``.
    """
    workdir = shlex.quote(workspace)
    joined = "\x1c".join(m for m in markers if m)
    return (
        f"cd {workdir} && "
        "{ git ls-files -z -- '*.gitignore'; "
        "git ls-files --others --exclude-standard -z -- '*.gitignore'; } | "
        "sort -zu | while IFS= read -r -d '' f; do "
        "[ -f \"$f\" ] || continue; "
        f"awk -v markers={shlex.quote(joined)} '{_GITIGNORE_STRIP_AWK}' "
        "< \"$f\" > \"$f.benchrunner-strip\"; rc=$?; "
        "if [ \"$rc\" -eq 42 ]; then rm -f -- \"$f.benchrunner-strip\"; continue; fi; "
        "if [ \"$rc\" -ne 0 ]; then exit 1; fi; "
        "if [ -s \"$f.benchrunner-strip\" ]; then "
        "if cmp -s -- \"$f\" \"$f.benchrunner-strip\"; "
        "then rm -f -- \"$f.benchrunner-strip\"; "
        "else mv -f -- \"$f.benchrunner-strip\" \"$f\"; fi; "
        "elif git cat-file -e \"HEAD:$f\" 2>/dev/null; then "
        "mv -f -- \"$f.benchrunner-strip\" \"$f\"; "
        "else rm -f -- \"$f\" \"$f.benchrunner-strip\"; fi; "
        "done"
    )


def _remove_container_file(container_name: str, path: str, timeout: int) -> None:
    try:
        subprocess.run(
            ["docker", "exec", container_name, "rm", "-f", "--", path, f"{path}.lock"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        pass


def snapshot_git_workspace(
    container_name: str,
    workspace: str,
    timeout: int = 60,
) -> GitWorkspaceBaseline:
    """Create a Git tree for HEAD plus pre-existing, non-ignored untracked files."""
    workdir = shlex.quote(workspace)
    index_path = _temporary_index_path()
    index_env = f"GIT_INDEX_FILE={shlex.quote(index_path)}"

    def run_git(command: str):
        return subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        init_result = run_git(f"cd {workdir} && {index_env} git read-tree HEAD")
        if init_result.returncode != 0:
            message = (init_result.stderr or init_result.stdout or "git read-tree failed").strip()
            return GitWorkspaceBaseline(error=message)

        add_result = run_git(
            f"cd {workdir} && set -o pipefail && "
            f"{index_env} git ls-files --others --exclude-standard -z -- . | "
            f"{index_env} git update-index --add -z --stdin"
        )
        if add_result.returncode != 0:
            message = (
                add_result.stderr
                or add_result.stdout
                or "failed to snapshot untracked files"
            ).strip()
            return GitWorkspaceBaseline(error=message)

        tree_result = run_git(f"cd {workdir} && {index_env} git write-tree")
        if tree_result.returncode != 0:
            message = (tree_result.stderr or tree_result.stdout or "git write-tree failed").strip()
            return GitWorkspaceBaseline(error=message)
        tree = tree_result.stdout.strip()
        if not tree:
            return GitWorkspaceBaseline(error="git write-tree returned an empty tree id")
        return GitWorkspaceBaseline(tree=tree)
    except Exception as exc:
        return GitWorkspaceBaseline(error=f"workspace baseline failed: {exc}")
    finally:
        _remove_container_file(container_name, index_path, timeout)


def _parse_name_status(output: str) -> list[tuple[str, tuple[str, ...]]]:
    fields = output.split("\0")
    if fields and not fields[-1]:
        fields.pop()

    changes = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise ValueError("malformed git diff --name-status output")
        paths = tuple(fields[index:index + path_count])
        index += path_count
        changes.append((status, paths))
    return changes


def _patch_header_paths(line: str) -> tuple[str, ...]:
    if not line.startswith("diff --git "):
        return ()
    try:
        fields = shlex.split(line)
    except ValueError:
        return ()
    paths = []
    for field, prefix in zip(fields[2:4], ("a/", "b/")):
        if field.startswith(prefix):
            paths.append(field[len(prefix):])
    return tuple(paths)


def capture_git_patch(
    container_name: str,
    workspace: str,
    timeout: int = 60,
    exclude_paths: Sequence[str] = (),
    baseline_tree: str | None = None,
    gitignore_strip_markers: Sequence[str] = (),
) -> GitPatchCapture:
    """Collect tracked and untracked workspace changes from a Docker container.

    ``git diff`` alone omits untracked files. A private temporary index marks them
    intent-to-add without touching the repository index. When ``baseline_tree`` is
    provided, files that already existed in the initial workspace are compared with
    that baseline instead of being reported as agent-created files. Matching paths
    are excluded only when they don't exist in ``HEAD``; tracked files with the same
    names remain.

    ``gitignore_strip_markers`` removes marker-anchored blocks agents append to
    tracked ``.gitignore`` files (path-based exclusion cannot touch tracked
    content) before any diff is taken.
    """
    workdir = shlex.quote(workspace)
    comparison_tree = shlex.quote(baseline_tree or "HEAD")
    index_path = _temporary_index_path()
    index_env = f"GIT_INDEX_FILE={shlex.quote(index_path)}"
    status_cmd = f"cd {workdir} && GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1"
    effective_markers = tuple(m for m in gitignore_strip_markers if m)

    def run_git(command: str):
        # encoding/errors 显式指定: git diff/status 输出带仓库内容 (可含多字节字符),
        # strict decode 遇到异常字节会把整个取 patch 炸掉 → 用例白白丢分; replace 降级。
        return subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    try:
        if effective_markers:
            strip_result = run_git(
                _gitignore_strip_command(workspace, effective_markers)
            )
            if strip_result.returncode != 0:
                message = (
                    strip_result.stderr
                    or strip_result.stdout
                    or "gitignore marker strip failed"
                ).strip()
                return GitPatchCapture(error=message)
        status_result = run_git(status_cmd)
        if status_result.returncode != 0:
            message = (status_result.stderr or status_result.stdout or "git status failed").strip()
            return GitPatchCapture(error=message)

        init_result = run_git(
            f"cd {workdir} && {index_env} git read-tree {comparison_tree}"
        )
        if init_result.returncode != 0:
            message = (init_result.stderr or init_result.stdout or "git read-tree failed").strip()
            return GitPatchCapture(status=status_result.stdout, error=message)

        add_result = run_git(f"cd {workdir} && {index_env} git add -N -- .")
        if add_result.returncode != 0:
            message = (add_result.stderr or add_result.stdout or "git add -N failed").strip()
            return GitPatchCapture(status=status_result.stdout, error=message)

        excluded = set()
        if exclude_paths:
            head_result = run_git(
                f"cd {workdir} && git ls-tree -r --name-only -z HEAD -- ."
            )
            if head_result.returncode != 0:
                message = (
                    head_result.stderr
                    or head_result.stdout
                    or "git ls-tree failed"
                ).strip()
                return GitPatchCapture(status=status_result.stdout, error=message)
            head_paths = {
                path for path in head_result.stdout.split("\0") if path
            }

            changes_result = run_git(
                f"cd {workdir} && "
                f"{index_env} git diff --name-status -z --find-renames "
                f"{comparison_tree} -- ."
            )
            if changes_result.returncode != 0:
                message = (
                    changes_result.stderr
                    or changes_result.stdout
                    or "git diff --name-status failed"
                ).strip()
                return GitPatchCapture(status=status_result.stdout, error=message)

            try:
                changes = _parse_name_status(changes_result.stdout)
            except ValueError as exc:
                return GitPatchCapture(status=status_result.stdout, error=str(exc))

            for status, paths in changes:
                path_is_excluded = tuple(
                    path not in head_paths
                    and any(
                        fnmatch.fnmatchcase(path, pattern)
                        for pattern in exclude_paths
                    )
                    for path in paths
                )
                excluded.update(
                    path for path, should_exclude in zip(paths, path_is_excluded)
                    if should_exclude
                )
                if status.startswith(("R", "C")) and any(path_is_excluded):
                    if not all(path_is_excluded):
                        return GitPatchCapture(
                            status=status_result.stdout,
                            error=(
                                "rename/copy crosses excluded-path boundary: "
                                + " -> ".join(paths)
                            ),
                            excluded_paths=tuple(sorted(excluded)),
                        )

        excluded_paths = tuple(sorted(excluded))
        exact_excludes = " ".join(
            shlex.quote(f":(top,exclude,literal){path}")
            for path in excluded_paths
        )
        diff_scope = f". {exact_excludes}".rstrip()
        patch_result = run_git(
            f"cd {workdir} && "
            f"{index_env} git diff --binary --no-ext-diff "
            f"{comparison_tree} -- {diff_scope}"
        )
        if patch_result.returncode != 0:
            message = (patch_result.stderr or patch_result.stdout or "git diff failed").strip()
            return GitPatchCapture(
                status=status_result.stdout,
                error=message,
                excluded_paths=excluded_paths,
            )

        patch = patch_result.stdout
        leaked_paths = set()
        for line in patch.splitlines():
            leaked_paths.update(set(_patch_header_paths(line)) & excluded)
        if leaked_paths:
            return GitPatchCapture(
                status=status_result.stdout,
                error=(
                    "excluded runtime artifacts leaked into patch: "
                    + ", ".join(sorted(set(leaked_paths)))
                ),
                excluded_paths=excluded_paths,
            )
        leaked_markers = sorted(
            {
                marker
                for marker in effective_markers
                for line in patch.splitlines()
                if line.startswith("+" + marker)
            }
        )
        if leaked_markers:
            return GitPatchCapture(
                status=status_result.stdout,
                error=(
                    "runtime gitignore markers leaked into patch: "
                    + ", ".join(leaked_markers)
                ),
                excluded_paths=excluded_paths,
            )
        if patch and not patch.endswith("\n"):
            patch += "\n"
        return GitPatchCapture(
            patch=patch,
            status=status_result.stdout,
            excluded_paths=excluded_paths,
        )
    except Exception as exc:
        return GitPatchCapture(error=f"patch collection failed: {exc}")
    finally:
        _remove_container_file(container_name, index_path, timeout)
