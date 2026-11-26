# replay/sandbox/hooks.py
"""Claude Code hook handlers (fail-open, never break the user's session).

Contract facts baked in from the official hooks reference (verified):
- PreToolUse stdout JSON: hookSpecificOutput.updatedInput REPLACES the whole
  tool_input, so unchanged fields must be echoed back; permissionDecision
  "allow" is set explicitly alongside.
- A failing Bash command fires PostToolUseFailure (no tool_response), so the
  checkpoint trigger handles both PostToolUse and PostToolUseFailure.
- Exit 0 with empty stdout is a safe no-op; hooks must never raise.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Mapping

from .docker_client import DockerClient
from .ledger import IRREVERSIBLE, OPAQUE, classify_ledger_entry, is_read_only_command
from .session import SandboxSession

ENV_SESSION = "REPLAY_SANDBOX_SESSION"
ENV_ROOT = "REPLAY_SANDBOX_ROOT"
ENV_BIN = "REPLAY_SANDBOX_BIN"
EXEC_MARKER = " sandbox exec --session "

MUTATING_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})
POST_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})


def _log(text: str) -> None:
    print(f"[replay-sandbox hook] {text}", file=sys.stderr, flush=True)


def unwrap_sandbox_command(command: str) -> str:
    """Recover the agent's original command from our PreToolUse rewrite.

    Splits at the FIRST ` -- ` after the marker: the original command may
    itself contain ` -- ` (e.g. `git checkout HEAD -- path`), and taking the
    last one would hand back a fragment that looks harmless.
    """
    marker_at = command.find(EXEC_MARKER)
    if marker_at < 0:
        return command
    separator_at = command.find(" -- ", marker_at)
    if separator_at < 0:
        return command
    tail = command[separator_at + 4:].strip()
    try:
        parts = shlex.split(tail)
    except ValueError:
        return tail
    return parts[0] if len(parts) == 1 else tail


def _load_session(
    env: Mapping[str, str], docker: DockerClient | None
) -> SandboxSession | None:
    session_id = env.get(ENV_SESSION)
    root = env.get(ENV_ROOT)
    if not session_id or not root:
        return None
    return SandboxSession.load(
        sandbox_root=Path(root), session_id=session_id, docker=docker
    )


def handle_pre_tool_use(payload: dict, env: Mapping[str, str]) -> dict | None:
    try:
        session_id = env.get(ENV_SESSION)
        root = env.get(ENV_ROOT)
        if not session_id or not root:
            _log(
                "REPLAY_SANDBOX_* env not set; sandbox inactive, "
                "command runs on host (fail-open)"
            )
            return None
        if payload.get("tool_name") != "Bash":
            return None
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command")
        if not command or not isinstance(command, str):
            return None
        bin_shell = env.get(ENV_BIN, "replay")
        if EXEC_MARKER in command:
            return None  # already wrapped; never double-wrap
        new_command = (
            f"{bin_shell} sandbox exec --session {session_id} "
            f"--sandbox-root {shlex.quote(root)} -- {shlex.quote(command)}"
        )
        updated = dict(tool_input)
        updated["command"] = new_command
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": (
                    "replay sandbox: command redirected into the "
                    "rollback-capable sandbox"
                ),
                "updatedInput": updated,
            }
        }
    except Exception as exc:  # noqa: BLE001 - fail open, never break the session
        _log(f"pre-tool-use failed open: {exc!r}")
        return None


def _checkpoint_label(payload: dict) -> str:
    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input") or {}
    if tool_name == "Bash":
        command = unwrap_sandbox_command(str(tool_input.get("command", "")))
        excerpt = command[:60]
    else:
        excerpt = str(tool_input.get("file_path", ""))[-60:]
    return f"{tool_name}: {excerpt}" if excerpt else tool_name


def handle_post_tool_use(
    payload: dict, env: Mapping[str, str], docker: DockerClient | None = None
) -> None:
    try:
        if payload.get("hook_event_name") not in POST_EVENTS:
            return None
        tool_name = payload.get("tool_name")
        if tool_name not in MUTATING_TOOLS:
            return None
        if tool_name == "Bash":
            command = unwrap_sandbox_command(
                str((payload.get("tool_input") or {}).get("command", ""))
            )
            if is_read_only_command(command):
                # Nothing could have changed; skipping keeps the hot path cheap.
                return None
        session = _load_session(env, docker)
        if session is None:
            _log("checkpoint skipped: sandbox env not set")
            return None
        session.checkpoint(
            label=_checkpoint_label(payload), trigger=f"tool:{tool_name}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        _log(f"post-tool-use checkpoint skipped: {exc!r}")
        return None


def handle_session_start(
    payload: dict, env: Mapping[str, str], docker: DockerClient | None = None
) -> str | None:
    try:
        if not env.get(ENV_SESSION) or not env.get(ENV_ROOT):
            return (
                "replay sandbox WARNING: this project has sandbox hooks/MCP "
                "configured, but REPLAY_SANDBOX_* env is not set (you were not "
                "launched via `replay wrap`). Commands run on the HOST and NO "
                "automatic checkpoints are being taken. Do not rely on "
                "sandbox_goto to undo new work; relaunch via `replay wrap` to "
                "re-enable world rollback."
            )
        session = _load_session(env, docker)
        if session is None:
            return None
        status = session.ensure_up()
        count = session.checkpoint_count()
        rows = session.ledger_store.load()
        # Reversible `action` rows (pip install, ...) are sandbox-contained,
        # not external: report the total honestly and split out what a rewind
        # could NOT undo.
        irreversible = sum(
            1 for row in rows
            if classify_ledger_entry(row) in (IRREVERSIBLE, OPAQUE)
        )
        return (
            f"replay sandbox active: session {session.session_id} "
            f"(container {status}, {count} checkpoint(s), "
            f"HEAD={session.state.head_checkpoint_id}, "
            f"{len(rows)} recorded action(s), "
            f"{irreversible} irreversible/opaque).\n"
            "Your Bash commands run inside a rollback-capable sandbox. After "
            "mutating tools a world checkpoint is taken automatically. If the "
            "environment gets into a bad state, use the MCP tools "
            "sandbox_timeline then sandbox_goto(checkpoint_id, reason) to "
            "roll back files AND container state together."
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"session-start context skipped: {exc!r}")
        return None
