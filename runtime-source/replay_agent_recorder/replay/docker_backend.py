from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


DockerStatePolicy = Literal["process_stateless", "fresh_container_per_exec"]
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AUTH|PRIVATE|COOKIE|SESSION)",
    re.I,
)
DEFAULT_DOCKER_METADATA_TIMEOUT = 5.0


@dataclass(frozen=True)
class DockerWorkspaceMount:
    workspace_id: str
    host_root: Path
    container_root: str = "/workspace"
    mode: Literal["rw", "ro"] = "rw"
    logical_root: str | None = None

    @property
    def logical_host_root(self) -> str:
        return self.logical_root or f"replay://workspace/{self.workspace_id}"


@dataclass(frozen=True)
class DockerSandboxSpec:
    image: str
    executable: str = "docker"
    cwd: str = "/workspace"
    run_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    forward_env: tuple[str, ...] = ()
    interpreter: tuple[str, ...] = ("bash", "-lc")
    timeout: int | float | None = None
    state_policy: DockerStatePolicy = "process_stateless"
    workspace_mount: DockerWorkspaceMount | None = None


@dataclass(frozen=True)
class DockerBenchmarkInstance:
    benchmark: str
    instance_id: str
    sandbox: DockerSandboxSpec
    seed_kind: str = "copy_from_image_path"
    seed_path: str = "/workspace"
    submission_kind: str = "host_workspace_tar"


def workspace_mount_args(mount: DockerWorkspaceMount) -> list[str]:
    spec = (
        f"type=bind,source={Path(mount.host_root).resolve()},"
        f"target={mount.container_root}"
    )
    if mount.mode == "ro":
        spec = f"{spec},readonly"
    return ["--mount", spec]


def parse_mount_spec(spec: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_part in str(spec).split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[_normalize_mount_key(key.strip())] = value.strip()
        else:
            parsed[_normalize_mount_key(part)] = "true"
    return parsed


def mount_target_from_spec(spec: str) -> str | None:
    return parse_mount_spec(spec).get("target")


def remove_mount_target_args(run_args: Sequence[Any], target: str) -> list[str]:
    args = [str(arg) for arg in run_args]
    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--mount":
            if index + 1 < len(args) and mount_target_from_spec(args[index + 1]) == target:
                index += 2
                continue
            result.append(value)
            if index + 1 < len(args):
                result.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if value.startswith("--mount="):
            spec = value.split("=", 1)[1]
            if mount_target_from_spec(spec) != target:
                result.append(value)
            index += 1
            continue
        if value in {"-v", "--volume"}:
            if index + 1 < len(args) and _volume_target(args[index + 1]) == target:
                index += 2
                continue
            result.append(value)
            if index + 1 < len(args):
                result.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if value.startswith("--volume=") or value.startswith("-v="):
            spec = value.split("=", 1)[1]
            if _volume_target(spec) != target:
                result.append(value)
            index += 1
            continue
        result.append(value)
        index += 1
    return result


def safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for member in tf.getmembers():
        target = _safe_tar_target(member, dest)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            _chmod_from_tar_member(target, member)
            continue
        if member.issym():
            _safe_link_target(member, dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            os.symlink(member.linkname, target)
            continue
        if member.islnk():
            link_target = _safe_link_target(member, dest)
            if not link_target.exists():
                raise ValueError(f"Hardlink target does not exist: {member.name!r} -> {member.linkname!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            os.link(link_target, target)
            _chmod_from_tar_member(target, member)
            continue
        if not member.isfile():
            raise ValueError(f"Unsupported tar member type for safe workspace extraction: {member.name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tf.extractfile(member)
        if source is None:
            raise ValueError(f"Unable to read tar member: {member.name!r}")
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        _chmod_from_tar_member(target, member)


def make_tree_writable(path: Path) -> None:
    root = Path(path)
    _make_path_writable(root)
    for current_root, dirs, files in os.walk(root):
        for dirname in dirs:
            _make_path_writable(Path(current_root) / dirname)
        for filename in files:
            _make_path_writable(Path(current_root) / filename)


def redact_env(env: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in env.items():
        key_text = str(key)
        if SENSITIVE_ENV_KEY_RE.search(key_text):
            redacted[key_text] = {"redacted": True}
        else:
            redacted[key_text] = _json_like(value)
    return redacted


def redact_run_args(args: Sequence[Any]) -> list[str]:
    values = [str(arg) for arg in args]
    redacted: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value in {"-e", "--env", "--build-arg", "--label"}:
            redacted.append(value)
            if index + 1 < len(values):
                redacted.append(_redact_key_value_arg(values[index + 1]))
                index += 2
            else:
                index += 1
            continue
        if value == "--env-file":
            redacted.append(value)
            if index + 1 < len(values):
                redacted.append("<redacted>")
                index += 2
            else:
                index += 1
            continue
        if value.startswith("-e") and len(value) > 2:
            redacted.append(f"-e{_redact_key_value_arg(value[2:])}")
            index += 1
            continue
        for prefix in ("--env=", "--build-arg=", "--label="):
            if value.startswith(prefix):
                redacted.append(f"{prefix}{_redact_key_value_arg(value[len(prefix):])}")
                break
        else:
            if value.startswith("--env-file="):
                redacted.append("--env-file=<redacted>")
            else:
                redacted.append(value)
        index += 1
    return redacted


def docker_version_metadata(
    executable: str = "docker",
    *,
    timeout: int | float = DEFAULT_DOCKER_METADATA_TIMEOUT,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(executable), "version", "--format", "{{json .}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "available": False,
            "error": f"docker metadata command timed out after {exc.timeout} seconds",
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    if result.returncode != 0:
        return {"available": False, "error": (result.stderr or result.stdout).strip()}
    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        return {"available": False, "error": f"invalid docker version JSON: {exc}"}
    client = payload.get("Client") if isinstance(payload, dict) else None
    server = payload.get("Server") if isinstance(payload, dict) else None
    return {
        "available": True,
        "client": {"version": _mapping_get(client, "Version")},
        "server": {"version": _mapping_get(server, "Version")},
    }


def inspect_image_metadata(
    image: str,
    *,
    executable: str = "docker",
    timeout: int | float = DEFAULT_DOCKER_METADATA_TIMEOUT,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(executable), "image", "inspect", str(image)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "available": False,
            "ref": str(image),
            "error": f"docker metadata command timed out after {exc.timeout} seconds",
        }
    except Exception as exc:
        return {"available": False, "ref": str(image), "error": str(exc)}
    if result.returncode != 0:
        return {
            "available": False,
            "ref": str(image),
            "error": (result.stderr or result.stdout).strip(),
        }
    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        return {"available": False, "ref": str(image), "error": f"invalid image inspect JSON: {exc}"}
    data = payload[0] if isinstance(payload, list) and payload else {}
    if not isinstance(data, dict):
        data = {}
    return {
        "available": True,
        "ref": str(image),
        "id": _mapping_get(data, "Id"),
        "repo_digests": _string_list(_mapping_get(data, "RepoDigests", [])),
        "repo_tags": _string_list(_mapping_get(data, "RepoTags", [])),
        "architecture": _mapping_get(data, "Architecture"),
        "os": _mapping_get(data, "Os"),
    }


def build_tool_docker_metadata_from_env(
    env: Any,
    *,
    command: str | None = None,
    workspace_id: str | None = None,
    container_workspace: str | None = None,
    state_policy: DockerStatePolicy | None = None,
    require_live: bool = False,
) -> dict[str, Any] | None:
    snapshot = getattr(type(env), "docker_metadata_snapshot", None)
    if callable(snapshot):
        return snapshot(
            env,
            require_live=require_live,
            command=command,
            workspace_id=workspace_id,
            container_workspace=container_workspace,
            state_policy=state_policy,
        )

    raw = getattr(env, "__dict__", {})
    if not require_live:
        config = raw.get("config") if isinstance(raw, dict) else None
        container_id = raw.get("container_id") if isinstance(raw, dict) else None
    else:
        config = getattr(env, "config", None)
        container_id = getattr(env, "container_id", None)
    return build_tool_docker_metadata_from_config(
        config,
        container_id=container_id,
        command=command,
        workspace_id=workspace_id,
        container_workspace=container_workspace,
        state_policy=state_policy,
    )


def build_tool_docker_metadata_from_config(
    config: Any,
    *,
    container_id: str | None = None,
    command: str | None = None,
    workspace_id: str | None = None,
    container_workspace: str | None = None,
    state_policy: DockerStatePolicy | None = None,
) -> dict[str, Any] | None:
    image = _config_get(config, "image")
    if not container_id and not image:
        return None

    executable = _config_get(config, "executable", "docker")
    cwd = _config_get(config, "cwd", None) or container_workspace or os.environ.get(
        "REPLAY_CONTAINER_WORKSPACE_ROOT",
        "/workspace",
    )
    run_args = [str(item) for item in (_config_get(config, "run_args", []) or [])]
    env_values = _config_get(config, "env", {}) or {}
    timeout = _config_get(config, "timeout")
    interpreter = _string_list(_config_get(config, "interpreter", ("bash", "-lc"))) or ["bash", "-lc"]
    workspace_id = workspace_id or os.environ.get("REPLAY_WORKSPACE_ID") or "workspace"
    logical_root = os.environ.get("REPLAY_WORKSPACE_LOGICAL_ROOT") or f"replay://workspace/{workspace_id}"
    container_root = container_workspace or os.environ.get("REPLAY_CONTAINER_WORKSPACE_ROOT") or str(cwd)
    run_args = remove_mount_target_args(run_args, str(container_root))
    policy = state_policy or os.environ.get("REPLAY_DOCKER_STATE_POLICY") or "process_stateless"

    docker: dict[str, Any] = {
        "schema_version": 1,
        "state_policy": str(policy),
        "backend": "docker",
        "container": {"id": str(container_id)} if container_id else {},
        "image": {"ref": str(image)} if image else {},
        "workspace_mount": {
            "workspace_id": str(workspace_id),
            "container_root": str(container_root),
            "host_root": str(logical_root),
            "mode": "rw",
        },
        "exec": {
            "cwd": str(cwd),
            "interpreter": interpreter,
            "timeout": _json_like(timeout),
        },
        "run": {
            "executable": str(executable),
            "run_args": redact_run_args(run_args),
            "env": redact_env(env_values) if isinstance(env_values, Mapping) else {},
        },
    }
    return {"docker": docker}


def build_environment_record(
    instance: DockerBenchmarkInstance,
    *,
    env: Any | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    sandbox = instance.sandbox
    docker = docker_version_metadata(sandbox.executable)
    container_root = (
        sandbox.workspace_mount.container_root
        if sandbox.workspace_mount is not None
        else "/workspace"
    )
    record = {
        "kind": "environment",
        "schema_version": 3,
        "backend": "docker",
        "docker_schema_version": 1,
        "created_at": created_at or _iso_now(),
        "environment_id": f"docker:{instance.benchmark}:{instance.instance_id}",
        "benchmark": {
            "name": instance.benchmark,
            "instance_id": instance.instance_id,
        },
        "state_policy": sandbox.state_policy,
        "image": inspect_image_metadata(sandbox.image, executable=sandbox.executable),
        "docker": _docker_record_metadata(docker, executable=sandbox.executable),
        "workspace_mounts": _workspace_mount_records(sandbox.workspace_mount, instance=instance),
        "run": {
            "executable": sandbox.executable,
            "cwd": sandbox.cwd,
            "run_args": redact_run_args(remove_mount_target_args(sandbox.run_args, container_root)),
            "env": redact_env(sandbox.env),
            "forward_env": list(sandbox.forward_env),
            "interpreter": list(sandbox.interpreter),
            "timeout": _json_like(sandbox.timeout),
        },
        "submission": {"kind": instance.submission_kind},
        "notes": {"container_rootfs": "persistent_within_task_not_replayed"},
    }
    if env is not None:
        metadata = build_tool_docker_metadata_from_env(env)
        if metadata:
            record["active_container"] = metadata["docker"].get("container", {})
    return record


def build_docker_environment_record(
    instance: DockerBenchmarkInstance,
    *,
    env: Any | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    return build_environment_record(instance, env=env, created_at=created_at)


def build_container_recreated_event(
    *,
    benchmark: str,
    instance_id: str,
    workspace_mount: DockerWorkspaceMount,
    old_workspace_root: Path | str | None,
    new_workspace_root: Path | str,
    old_container_id: str | None = None,
    new_container_id: str | None = None,
    reason: str = "workspace_root_changed",
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "environment_event",
        "backend": "docker",
        "schema_version": 1,
        "created_at": created_at or _iso_now(),
        "event": "container_recreated",
        "reason": reason,
        "environment_id": f"docker:{benchmark}:{instance_id}",
        "workspace_mount": {
            "workspace_id": workspace_mount.workspace_id,
            "container_root": workspace_mount.container_root,
            "old_host_root": str(old_workspace_root) if old_workspace_root is not None else None,
            "new_host_root": str(new_workspace_root),
            "logical_host_root": workspace_mount.logical_host_root,
        },
        "container": {
            "old_id": str(old_container_id) if old_container_id else None,
            "new_id": str(new_container_id) if new_container_id else None,
        },
    }


def record_docker_environment_if_active(
    instance: DockerBenchmarkInstance,
    *,
    env: Any | None = None,
) -> None:
    from .context import get_current_session

    session = get_current_session()
    if session is not None and session.__class__.__name__ == "RecordSession":
        session.write_record(build_docker_environment_record(instance, env=env))


def record_docker_environment_event_if_active(record: Mapping[str, Any]) -> None:
    from .context import get_current_session

    session = get_current_session()
    if session is not None and session.__class__.__name__ == "RecordSession":
        session.write_record(dict(record))


@contextmanager
def docker_replay_environment(
    *,
    container_workspace: str,
    state_policy: DockerStatePolicy,
) -> Iterator[None]:
    values = {
        "REPLAY_CONTAINER_WORKSPACE_ROOT": str(container_workspace),
        "REPLAY_DOCKER_STATE_POLICY": str(state_policy),
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, old_value in reversed(list(previous.items())):
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _normalize_mount_key(key: str) -> str:
    if key in {"src", "source"}:
        return "source"
    if key in {"dst", "destination", "target"}:
        return "target"
    if key in {"ro", "readonly"}:
        return "readonly"
    return key


def _volume_target(spec: str) -> str | None:
    parts = str(spec).split(":")
    if len(parts) < 2:
        return None
    return parts[1]


def _safe_tar_target(member: tarfile.TarInfo, dest: Path) -> Path:
    name = member.name
    if name.startswith("/") or name == "":
        raise ValueError(f"Unsafe tar member: {name!r}")
    parts = Path(name).parts
    if any(part in {"..", ""} for part in parts):
        raise ValueError(f"Unsafe tar member: {name!r}")
    target = dest.joinpath(*parts).resolve()
    if target != dest and dest not in target.parents:
        raise ValueError(f"Unsafe tar member: {name!r}")
    return target


def _safe_link_target(member: tarfile.TarInfo, dest: Path) -> Path:
    linkname = member.linkname
    if not linkname:
        raise ValueError(f"Empty link target for tar member: {member.name!r}")
    link_path = Path(linkname)
    if link_path.is_absolute():
        raise ValueError(f"Refusing absolute link target in tar member: {member.name!r} -> {linkname!r}")
    base = _safe_tar_target(member, dest).parent
    resolved = (base / link_path).resolve()
    if not _is_relative_to(resolved, dest):
        raise ValueError(f"Refusing link escaping extraction root: {member.name!r} -> {linkname!r}")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _chmod_from_tar_member(path: Path, member: tarfile.TarInfo) -> None:
    if member.mode == 0:
        return
    try:
        if path.is_dir():
            path.chmod(member.mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        else:
            path.chmod(member.mode)
    except OSError:
        pass


def _make_path_writable(path: Path) -> None:
    if path.is_symlink():
        return
    try:
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        else:
            path.chmod(
                mode
                | stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH
                | stat.S_IWOTH
            )
    except OSError:
        pass


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return default


def _redact_key_value_arg(value: str) -> str:
    if "=" not in value:
        return value
    key, _raw_value = value.split("=", 1)
    if SENSITIVE_ENV_KEY_RE.search(key):
        return f"{key}=<redacted>"
    return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        return [str(value)]
    return [str(item) for item in value if item is not None]


def _json_like(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_like(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_like(item) for item in value]
    return str(value)


def _workspace_mount_metadata(mount: DockerWorkspaceMount | None) -> dict[str, Any] | None:
    if mount is None:
        return None
    return {
        "workspace_id": mount.workspace_id,
        "container_root": mount.container_root,
        "host_root": mount.logical_host_root,
        "mode": mount.mode,
    }


def _workspace_mount_records(
    mount: DockerWorkspaceMount | None,
    *,
    instance: DockerBenchmarkInstance,
) -> list[dict[str, Any]]:
    metadata = _workspace_mount_metadata(mount)
    if metadata is None:
        return []
    metadata["seed"] = {"kind": instance.seed_kind, "container_path": instance.seed_path}
    metadata["host_root_runtime_policy"] = "current_git_workspace_root"
    metadata["root_switch_requires_container_recreate"] = True
    return [metadata]


def _docker_record_metadata(metadata: Mapping[str, Any], *, executable: str) -> dict[str, Any]:
    record = {
        "available": bool(metadata.get("available")),
        "executable": str(executable),
        "client_version": _mapping_get(metadata.get("client"), "version"),
        "server_version": _mapping_get(metadata.get("server"), "version"),
    }
    if "error" in metadata:
        record["error"] = metadata.get("error")
    return record


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
