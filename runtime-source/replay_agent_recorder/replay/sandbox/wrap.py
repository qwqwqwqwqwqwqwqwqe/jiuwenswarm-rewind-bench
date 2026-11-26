# replay/sandbox/wrap.py
"""`replay wrap` launcher: sandbox session + Claude Code config + exec."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from .cli import _session_runtime
from .docker_client import DockerClient
from .hooks import ENV_BIN, ENV_ROOT, ENV_SESSION
from .mcp_server import SERVER_NAME
from .session import SandboxSession

HOOK_MARKER = " sandbox hook "
HOOK_TIMEOUTS = {"pre-tool-use": 30, "post-tool-use": 300, "session-start": 120}
HOOK_EVENTS = {
    "PreToolUse": ("Bash", "pre-tool-use"),
    "PostToolUse": ("Bash|Edit|Write|MultiEdit|NotebookEdit", "post-tool-use"),
    "PostToolUseFailure": (
        "Bash|Edit|Write|MultiEdit|NotebookEdit", "post-tool-use"
    ),
    "SessionStart": ("startup|resume|clear|compact", "session-start"),
}


def _make_docker() -> DockerClient:
    return DockerClient()


def resolve_replay_bin() -> tuple[list[str], str]:
    found = shutil.which("replay")
    if found:
        return [found], found
    argv = [sys.executable, "-m", "replay.cli"]
    return argv, " ".join(shlex.quote(part) for part in argv)


def ensure_wrap_session(
    *,
    sandbox_root: Path,
    workspace_dir: Path,
    image: str,
    docker: DockerClient | None = None,
    strict_ledger: bool = False,
    async_checkpoints: bool = True,
    runtime: str = "docker",
) -> SandboxSession:
    sandbox_root = Path(sandbox_root)
    workspace_key = str(Path(workspace_dir).resolve())
    mapping_path = sandbox_root / "wrap-sessions.json"
    mapping: dict[str, str] = {}
    if mapping_path.exists():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}
    existing = mapping.get(workspace_key)
    if existing:
        # A reused session's container and images live in ONE runtime's
        # store, so the client must be built from the persisted runtime, not
        # the requested one. For podman, leave docker=None: SandboxSession
        # builds its own `sudo -n podman` client from the persisted state.
        persisted = _session_runtime(sandbox_root, existing)
        reuse_docker = (
            (docker or _make_docker()) if persisted == "docker" else None
        )
        try:
            session = SandboxSession.load(
                sandbox_root=sandbox_root, session_id=existing,
                docker=reuse_docker,
            )
            session.ensure_up()
            if runtime != session.state.runtime:
                print(
                    f"[replay wrap] reused session {session.session_id} stays on "
                    f"{session.state.runtime} (a session cannot move between "
                    f"runtimes); pass --workspace a fresh directory to get a "
                    f"{runtime} session",
                    file=sys.stderr,
                )
            # Upgrade only, never downgrade: a reused strict session stays
            # strict even when this wrap did not ask for it.
            if strict_ledger and not session.state.strict_ledger:
                session.set_strict_ledger(True)
                print(
                    f"[replay wrap] enabled strict ledger mode on reused session "
                    f"{session.session_id}",
                    file=sys.stderr,
                )
            # Async is a preference, not a safety gate: reconcile it in BOTH
            # directions, so a session wrapped before async existed follows
            # the wrap default and --no-async-checkpoints actually disables.
            if session.state.async_checkpoints != async_checkpoints:
                session.set_async_checkpoints(async_checkpoints)
                print(
                    f"[replay wrap] {'enabled' if async_checkpoints else 'disabled'} "
                    f"async checkpoints on reused session {session.session_id}",
                    file=sys.stderr,
                )
            return session
        except Exception as exc:  # noqa: BLE001 - any reuse failure means rebuild
            print(
                f"[replay wrap] cannot reuse session {existing} ({exc}); "
                "creating a fresh one",
                file=sys.stderr,
            )
    # For podman, leave docker=None: SandboxSession builds its own
    # `sudo -n podman` client from the requested runtime.
    docker = docker or (_make_docker() if runtime == "docker" else None)
    session = SandboxSession.up(
        sandbox_root=sandbox_root,
        workspace_dir=workspace_dir,
        image=image,
        docker=docker,
        strict_ledger=strict_ledger,
        async_checkpoints=async_checkpoints,
        runtime=runtime,
    )
    mapping[workspace_key] = session.session_id
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return session


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def apply_wrap_configs(
    *,
    project_dir: Path,
    session_id: str,
    sandbox_root: Path,
    bin_argv: list[str],
    bin_shell: str,
) -> list[Path]:
    changed: list[Path] = []
    abs_root = str(Path(sandbox_root).resolve())

    mcp_path = Path(project_dir) / ".mcp.json"
    mcp = _load_json(mcp_path)
    servers = mcp.setdefault("mcpServers", {})
    entry = {
        "type": "stdio",
        "command": bin_argv[0],
        "args": [
            *bin_argv[1:],
            "sandbox", "mcp", "--session", session_id, "--sandbox-root", abs_root,
        ],
    }
    if servers.get(SERVER_NAME) != entry:
        servers[SERVER_NAME] = entry
        _write_json(mcp_path, mcp)
        changed.append(mcp_path)

    settings_path = Path(project_dir) / ".claude" / "settings.local.json"
    settings = _load_json(settings_path)
    dirty = False
    enabled = settings.setdefault("enabledMcpjsonServers", [])
    if SERVER_NAME not in enabled:
        enabled.append(SERVER_NAME)
        dirty = True
    hooks = settings.setdefault("hooks", {})
    for event, (matcher, hook_event) in HOOK_EVENTS.items():
        command = f"{bin_shell} sandbox hook {hook_event}"
        rules = hooks.setdefault(event, [])
        # Dedup by marker, not exact command: bin_shell may differ between
        # wraps (PATH vs python -m) and must not stack duplicate hook sets.
        already = any(
            HOOK_MARKER in handler.get("command", "")
            for rule in rules
            for handler in rule.get("hooks", [])
        )
        if not already:
            rules.append(
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": HOOK_TIMEOUTS[hook_event],
                        }
                    ],
                }
            )
            dirty = True
    if dirty:
        _write_json(settings_path, settings)
        changed.append(settings_path)
    return changed


def remove_wrap_configs(project_dir: Path) -> list[Path]:
    changed: list[Path] = []
    mcp_path = Path(project_dir) / ".mcp.json"
    mcp = _load_json(mcp_path)
    if SERVER_NAME in mcp.get("mcpServers", {}):
        del mcp["mcpServers"][SERVER_NAME]
        _write_json(mcp_path, mcp)
        changed.append(mcp_path)
    settings_path = Path(project_dir) / ".claude" / "settings.local.json"
    settings = _load_json(settings_path)
    dirty = False
    enabled = settings.get("enabledMcpjsonServers", [])
    if SERVER_NAME in enabled:
        enabled.remove(SERVER_NAME)
        if not enabled:
            del settings["enabledMcpjsonServers"]
        dirty = True
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept_rules = []
        for rule in hooks[event]:
            handlers = [
                handler
                for handler in rule.get("hooks", [])
                if HOOK_MARKER not in handler.get("command", "")
            ]
            if handlers != rule.get("hooks", []):
                dirty = True
            if handlers:
                kept_rules.append({**rule, "hooks": handlers})
            elif rule.get("hooks") is None:
                kept_rules.append(rule)
        if kept_rules != hooks[event]:
            dirty = True
        if kept_rules:
            hooks[event] = kept_rules
        else:
            del hooks[event]
    if dirty:
        _write_json(settings_path, settings)
        changed.append(settings_path)
    return changed


def run_wrap(
    *,
    project_dir: Path,
    workspace_dir: Path,
    image: str,
    sandbox_root: Path,
    command: list[str],
    dry_run: bool = False,
    no_launch: bool = False,
    remove: bool = False,
    strict_ledger: bool = False,
    async_checkpoints: bool = True,
    runtime: str = "docker",
) -> int:
    project_dir = Path(project_dir).resolve()
    if remove:
        if dry_run:
            print(
                "would remove replay-sandbox entries from "
                f"{project_dir / '.mcp.json'} and "
                f"{project_dir / '.claude' / 'settings.local.json'}"
            )
            return 0
        changed = remove_wrap_configs(project_dir)
        print(
            "removed replay-sandbox entries from: "
            + (", ".join(str(p) for p in changed) if changed else "(nothing)")
        )
        return 0
    bin_argv, bin_shell = resolve_replay_bin()
    argv = command or ["claude"]
    if dry_run:
        print(
            "would ensure sandbox session for "
            f"{Path(workspace_dir).resolve()} (image {image}), write "
            f"{project_dir / '.mcp.json'} and "
            f"{project_dir / '.claude' / 'settings.local.json'}, then exec: "
            + " ".join(shlex.quote(part) for part in argv)
        )
        return 0
    session = ensure_wrap_session(
        sandbox_root=sandbox_root, workspace_dir=workspace_dir, image=image,
        strict_ledger=strict_ledger,
        async_checkpoints=async_checkpoints, runtime=runtime,
    )
    apply_wrap_configs(
        project_dir=project_dir,
        session_id=session.session_id,
        sandbox_root=sandbox_root,
        bin_argv=bin_argv,
        bin_shell=bin_shell,
    )
    env_updates = {
        ENV_SESSION: session.session_id,
        ENV_ROOT: str(Path(sandbox_root).resolve()),
        ENV_BIN: bin_shell,
    }
    if no_launch:
        for key, value in env_updates.items():
            print(f"{key}={value}")
        print("launch skipped; run: " + " ".join(shlex.quote(p) for p in argv))
        return 0
    env = dict(os.environ)
    env.update(env_updates)
    os.execvpe(argv[0], argv, env)
    return 0  # unreachable; keeps the type checker honest
