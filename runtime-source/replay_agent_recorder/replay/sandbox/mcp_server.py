# replay/sandbox/mcp_server.py
"""Sandbox tools exposed over MCP stdio for agent-driven rewind."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from .docker_client import DockerClient
from .mcp_protocol import McpStdioServer, McpTool, McpToolError
from .session import SandboxSession
from .types import SandboxError

SERVER_NAME = "replay-sandbox"
SERVER_VERSION = "1.0.0-alpha.1"
TIMELINE_ROW_CAP = 30

INSTRUCTIONS = (
    "This server controls a rollback-capable Docker sandbox that holds the "
    "world your shell commands run in (files, installed packages, container "
    "state). Checkpoints are taken automatically after mutating tools. If "
    "you realize you went down a wrong path (broke the environment, installed "
    "the wrong packages, corrupted files), call sandbox_timeline to inspect "
    "checkpoints, then sandbox_goto with a checkpoint_id and a concrete "
    "reason. Rewinding restarts recorded services but cannot undo external "
    "side effects; the rewind report lists what it could not undo. "
    "You can also move forward again with sandbox_goto: the history is a "
    "DAG. One caveat: `replay sandbox gc` can reclaim older checkpoints' "
    "images to free disk — those checkpoints stay in the timeline for "
    "history but can no longer be moved to."
    " Deep base-image chains are re-platformed automatically during goto: "
    "a timeline row marked [flat] is such a squash point — content identical "
    "to its parent, flattened to one layer."
)


def build_sandbox_tools(
    load_session: Callable[[], SandboxSession],
) -> list[McpTool]:
    def _session() -> SandboxSession:
        try:
            return load_session()
        except SandboxError as exc:
            raise McpToolError(f"sandbox session unavailable: {exc}") from exc

    def timeline_handler(args: dict) -> str:
        from .navigation import build_timeline_view, fork_children_in, render_timeline

        session = _session()
        nodes = build_timeline_view(
            session.timeline(),
            session.state.head_checkpoint_id,
            fork_children_in(
                session._sandbox_root,
                session.state.lineage_id,
                session.session_id,
            ),
        )
        return "\n".join(
            [
                f"Session {session.session_id}: {len(nodes)} checkpoint(s), "
                f"HEAD={session.state.head_checkpoint_id}",
                render_timeline(nodes, cap=TIMELINE_ROW_CAP),
                "`*` = current line of history, `x` = branch you left behind. "
                "To move the world to any of them: "
                "sandbox_goto(checkpoint_id, reason).",
            ]
        )

    def diff_handler(args: dict) -> str:
        from .navigation import build_diff

        from_id = args.get("from_checkpoint")
        to_id = args.get("to_checkpoint")
        if not from_id or not to_id:
            raise McpToolError(
                "from_checkpoint and to_checkpoint are both required"
            )
        session = _session()
        try:
            return build_diff(session, str(from_id), str(to_id)).to_text()
        except SandboxError as exc:
            raise McpToolError(str(exc)) from exc

    def checkpoint_handler(args: dict) -> str:
        session = _session()
        label = args.get("label")
        checkpoint = session.checkpoint(label=label, trigger="mcp")
        return (
            f"Created {checkpoint.checkpoint_id} (now HEAD)."
            + (f" label={label}" if label else "")
        )

    def goto_handler(args: dict) -> str:
        checkpoint_id = args.get("checkpoint_id")
        reason = args.get("reason")
        force = bool(args.get("force", False))
        if not checkpoint_id or not isinstance(checkpoint_id, str):
            raise McpToolError("checkpoint_id is required (e.g. ckpt_000003)")
        if not reason or not isinstance(reason, str) or not reason.strip():
            raise McpToolError(
                "reason is required: state concretely why you are rewinding "
                "so the decision is auditable and you avoid repeating it"
            )
        session = _session()
        try:
            report = session.rewind(checkpoint_id, reason=reason.strip(), force=force)
        except SandboxError as exc:
            raise McpToolError(str(exc)) from exc
        return report.to_text()

    def fork_handler(args: dict) -> str:
        session = _session()
        checkpoint_id = args.get("checkpoint_id")
        try:
            child = session.fork(checkpoint_id)
        except SandboxError as exc:
            raise McpToolError(str(exc)) from exc
        return (
            f"Forked new independent session {child.session_id} from "
            f"{checkpoint_id or session.state.head_checkpoint_id}. Drive it "
            f"via: replay sandbox exec --session {child.session_id} -- <cmd>"
        )

    def pause_handler(args: dict) -> str:
        session = _session()
        changed = session.pause()
        return (
            f"Sandbox {session.session_id} is now paused; every process is "
            "frozen in place. Call sandbox_resume to continue."
            if changed
            else "Sandbox was already paused (or the runtime refused to pause it)."
        )

    def resume_handler(args: dict) -> str:
        session = _session()
        changed = session.resume()
        return (
            f"Sandbox {session.session_id} resumed; processes continue from "
            "exactly where they were."
            if changed
            else "Sandbox was already running (or the runtime refused to resume it)."
        )

    return [
        McpTool(
            name="sandbox_timeline",
            description=(
                "List world checkpoints (files+container state) with per-step "
                "file-change summaries; use before rewinding."
            ),
            input_schema={"type": "object", "additionalProperties": False},
            handler=timeline_handler,
        ),
        McpTool(
            name="sandbox_diff",
            description=(
                "Show what changed between two checkpoints: workspace files, "
                "processes started or gone, and external actions recorded in "
                "between. Use it to decide which checkpoint to go to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_checkpoint": {"type": "string"},
                    "to_checkpoint": {"type": "string"},
                },
                "required": ["from_checkpoint", "to_checkpoint"],
                "additionalProperties": False,
            },
            handler=diff_handler,
        ),
        McpTool(
            name="sandbox_checkpoint",
            description="Snapshot the current world state as a new checkpoint.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short label."}
                },
                "additionalProperties": False,
            },
            handler=checkpoint_handler,
        ),
        McpTool(
            name="sandbox_goto",
            description=(
                "Move the WHOLE world (workspace files, installed packages, "
                "container state) to any checkpoint — backward to undo, "
                "forward to redo, or onto a branch you previously left. "
                "Requires a concrete reason. Services recorded at that "
                "checkpoint are restarted; external actions already sent "
                "(HTTP writes, pushes) are reported but CANNOT be undone. "
                "If the session is in strict mode the call fails until you "
                "retry with force=true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkpoint_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Accept rolling over irreversible external actions."
                        ),
                    },
                },
                "required": ["checkpoint_id", "reason"],
                "additionalProperties": False,
            },
            handler=goto_handler,
        ),
        McpTool(
            name="sandbox_rewind",
            description=(
                "Deprecated alias of sandbox_goto — identical behaviour. "
                "Prefer sandbox_goto, which also moves forward."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checkpoint_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["checkpoint_id", "reason"],
                "additionalProperties": False,
            },
            handler=goto_handler,
        ),
        McpTool(
            name="sandbox_fork",
            description=(
                "Fork an independent sandbox session from a checkpoint "
                "(defaults to HEAD) for parallel exploration."
            ),
            input_schema={
                "type": "object",
                "properties": {"checkpoint_id": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=fork_handler,
        ),
        McpTool(
            name="sandbox_pause",
            description=(
                "Freeze every process in the sandbox in place (a long task can "
                "be set aside and continued later). Commands are refused while "
                "paused; call sandbox_resume to continue."
            ),
            input_schema={"type": "object", "additionalProperties": False},
            handler=pause_handler,
        ),
        McpTool(
            name="sandbox_resume",
            description="Unfreeze a paused sandbox; processes continue where they were.",
            input_schema={"type": "object", "additionalProperties": False},
            handler=resume_handler,
        ),
    ]


def build_server(
    session_id: str,
    sandbox_root: Path,
    docker: DockerClient | None = None,
) -> McpStdioServer:
    def load_session() -> SandboxSession:
        return SandboxSession.load(
            sandbox_root=sandbox_root, session_id=session_id, docker=docker
        )

    return McpStdioServer(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=build_sandbox_tools(load_session),
        instructions=INSTRUCTIONS,
    )


def run_mcp_server(session_id: str, sandbox_root: Path) -> int:
    try:
        SandboxSession.load(
            sandbox_root=sandbox_root, session_id=session_id
        ).ensure_up()
    except Exception as exc:  # noqa: BLE001 - startup is best-effort
        print(f"[replay-sandbox mcp] ensure_up failed: {exc}", file=sys.stderr)
    build_server(session_id, sandbox_root).serve()
    return 0
