"""Shared safety and filesystem helpers for the standalone JiuwenSwarm runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_ROOT = Path.home() / "jws_swe_runner"
DEFAULT_SHARED_NAME = "shared_test"
_PRODUCTION_PATHS = (Path("/opt/miniconda3"),)
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Layout:
    root: Path
    runtime: Path
    home: Path
    shared: Path
    package: Path
    results: Path
    logs: Path
    tmp: Path
    state: Path


def _absolute(path: Path | str) -> Path:
    return Path(path).expanduser().absolute()


def _resolved_existing_or_absolute(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _is_same_or_below(path: Path, parent: Path) -> bool:
    try:
        _resolved_existing_or_absolute(path).relative_to(
            _resolved_existing_or_absolute(parent)
        )
        return True
    except ValueError:
        return False


def assert_safe_test_path(
    path: Path | str,
    root: Path | str,
    *,
    allow_create: bool = False,
) -> Path:
    """Reject production paths and paths that could erase a user's home.

    ``allow_create`` only controls whether a not-yet-existing path is accepted;
    it never relaxes the production-path checks.
    """
    candidate = _absolute(path)
    root_path = _absolute(root)
    candidate_real = _resolved_existing_or_absolute(candidate)
    root_real = _resolved_existing_or_absolute(root_path)
    home_real = _resolved_existing_or_absolute(Path.home())
    protected = [
        *(_resolved_existing_or_absolute(item) for item in _PRODUCTION_PATHS),
        home_real,
        home_real / ".jiuwenswarm",
        home_real / "shared",
        Path("/").resolve(),
    ]
    for protected_path in protected:
        if candidate_real == protected_path:
            raise ValueError(f"拒绝使用受保护路径: {candidate}")
    if not allow_create and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def resolve_layout(root: Path | str = DEFAULT_ROOT, shared_dir: Path | str | None = None) -> Layout:
    root_path = _absolute(root)
    shared_path = _absolute(shared_dir) if shared_dir is not None else root_path / DEFAULT_SHARED_NAME
    assert_safe_test_path(root_path, root_path, allow_create=True)
    if shared_dir is not None and not _is_same_or_below(shared_path, root_path):
        raise ValueError(f"shared 目录必须位于测试 root 内: {shared_path}")
    assert_safe_test_path(shared_path, root_path, allow_create=True)
    return Layout(
        root=root_path,
        runtime=root_path / "runtime",
        home=root_path / "home",
        shared=shared_path,
        package=root_path / "package",
        results=root_path / "results",
        logs=root_path / "logs",
        tmp=root_path / "tmp",
        state=root_path / "state",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path | str, payload: Mapping[str, object]) -> None:
    target = _absolute(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path | str) -> dict:
    with _absolute(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 状态必须是对象: {path}")
    return value


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with _absolute(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(value: object, secrets: Sequence[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def run_command(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured UTF-8 output and no shell interpolation."""
    return subprocess.run(
        [os.fspath(item) for item in args],
        cwd=os.fspath(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def shell_quote(value: object) -> str:
    return shlex.quote(str(value))


def docker_name(prefix: str, run_id: str, case_id: str | None = None) -> str:
    parts = [prefix, run_id]
    if case_id:
        parts.append(case_id)
    normalized = _NAME_RE.sub("-", "-".join(parts)).strip("-.")
    return normalized[:120] or "jws-swe-test"
