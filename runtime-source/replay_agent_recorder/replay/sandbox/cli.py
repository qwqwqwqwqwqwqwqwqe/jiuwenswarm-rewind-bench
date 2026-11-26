# replay/sandbox/cli.py
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from .docker_client import DockerClient
from .session import SandboxSession

DEFAULT_SANDBOX_ROOT = ".replay/sandbox"


def _make_docker() -> DockerClient:
    return DockerClient()


def _session_runtime(sandbox_root: Path, session_id: str) -> str:
    """Peek at the persisted runtime, so the (test-injectable) docker client
    is only forced onto docker sessions; podman sessions build their own
    `sudo -n podman` client inside SandboxSession."""
    path = Path(sandbox_root) / "sessions" / session_id / "session.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "docker"
    if not isinstance(payload, dict):
        return "docker"
    return str(payload.get("runtime", "docker"))


def register_sandbox_parser(subparsers: argparse._SubParsersAction) -> None:
    sandbox = subparsers.add_parser(
        "sandbox", help="Rollback-capable Docker sandbox sessions."
    )
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)

    def _add_common(cmd: argparse.ArgumentParser, with_session: bool = True) -> None:
        cmd.add_argument("--sandbox-root", default=DEFAULT_SANDBOX_ROOT)
        if with_session:
            cmd.add_argument("--session", required=True, help="Session id (sbx_*).")

    up = sandbox_sub.add_parser("up", help="Start a sandbox session.")
    up.add_argument("--image", required=True, help="Base Docker image.")
    up.add_argument("--workspace", required=True, help="Host workspace directory.")
    up.add_argument("--strict-ledger", action="store_true")
    up.add_argument("--async-checkpoints", action="store_true")
    up.add_argument("--runtime", choices=["docker", "podman"], default="docker")
    _add_common(up, with_session=False)

    exec_cmd = sandbox_sub.add_parser("exec", help="Run a command in the sandbox.")
    _add_common(exec_cmd)
    # dest must not be "command": argparse copies nested-subparser values into
    # the top-level namespace, and a "command" dest would clobber the
    # args.command == "sandbox" value that replay.cli.main() dispatches on.
    exec_cmd.add_argument(
        "exec_command",
        metavar="command",
        nargs=argparse.REMAINDER,
        help=(
            "Command to run; pass a single quoted string, or multiple args "
            "(quoting preserved)."
        ),
    )

    checkpoint = sandbox_sub.add_parser("checkpoint", help="Snapshot world state.")
    _add_common(checkpoint)
    checkpoint.add_argument("--label", default=None)
    checkpoint.add_argument("--trigger", default="manual")

    timeline = sandbox_sub.add_parser("timeline", help="List checkpoints.")
    _add_common(timeline)

    diff_cmd = sandbox_sub.add_parser(
        "diff", help="Show what changed between two checkpoints."
    )
    _add_common(diff_cmd)
    diff_cmd.add_argument("from_checkpoint")
    diff_cmd.add_argument("to_checkpoint")

    def _add_goto_arguments(cmd: argparse.ArgumentParser) -> None:
        _add_common(cmd)
        cmd.add_argument("checkpoint_id")
        cmd.add_argument("--reason", default=None)
        cmd.add_argument("--force", action="store_true",
                         help="Accept rolling over irreversible external actions.")

    goto = sandbox_sub.add_parser(
        "goto", help="Move the world to any checkpoint (back, forward or "
        "onto a branch you left).")
    _add_goto_arguments(goto)

    rewind = sandbox_sub.add_parser(
        "rewind", help="Alias of `goto`, kept for compatibility.")
    _add_goto_arguments(rewind)

    fork = sandbox_sub.add_parser("fork", help="Fork a new session from a checkpoint.")
    _add_common(fork)
    fork.add_argument("--from", dest="from_checkpoint", default=None)

    down = sandbox_sub.add_parser("down", help="Stop the sandbox container.")
    _add_common(down)
    down.add_argument("--purge", action="store_true")

    pause_cmd = sandbox_sub.add_parser(
        "pause", help="Freeze every process in the sandbox (no CRIU needed)."
    )
    _add_common(pause_cmd)

    resume_cmd = sandbox_sub.add_parser("resume", help="Unfreeze the sandbox.")
    _add_common(resume_cmd)

    squash_cmd = sandbox_sub.add_parser(
        "squash", help="Flatten the current base image to one layer "
        "(re-platform); restarts recorded services like any goto."
    )
    _add_common(squash_cmd)
    squash_cmd.add_argument("--force", action="store_true",
                            help="Accept rolling over irreversible external actions.")

    mcp = sandbox_sub.add_parser(
        "mcp", help="Run the MCP stdio server for a session."
    )
    _add_common(mcp)

    proxy = sandbox_sub.add_parser(
        "proxy", help="Run the egress-recording proxy daemon for a session."
    )
    _add_common(proxy)
    proxy.add_argument("--port", type=int, default=0)

    commit_worker = sandbox_sub.add_parser(
        "commit-worker", help="Internal: perform one deferred rootfs commit."
    )
    _add_common(commit_worker)
    commit_worker.add_argument("--checkpoint", required=True)

    hook = sandbox_sub.add_parser(
        "hook", help="Claude Code hook adapter (reads hook JSON on stdin)."
    )
    hook.add_argument(
        "hook_event",
        choices=["pre-tool-use", "post-tool-use", "session-start"],
    )

    processes = sandbox_sub.add_parser(
        "processes", help="Show or configure the sandbox process manifest policy."
    )
    _add_common(processes)
    processes.add_argument(
        "--set", dest="policy_assignment", default=None,
        metavar="PATTERN=POLICY",
        help="Force a cmdline substring to restart|lost on rewind.",
    )

    ledger = sandbox_sub.add_parser(
        "ledger", help="Show recorded external side effects."
    )
    _add_common(ledger)
    ledger.add_argument("--since", type=int, default=-1)

    gc_cmd = sandbox_sub.add_parser(
        "gc", help="Reclaim checkpoint images that can no longer be rewound to."
    )
    _add_common(gc_cmd)
    gc_cmd.add_argument("--keep", type=int, default=20)
    gc_cmd.add_argument("--dry-run", action="store_true")

    explore_cmd = sandbox_sub.add_parser(
        "explore", help="Fork the sandbox into parallel branches and run a "
        "command in each."
    )
    _add_common(explore_cmd)
    explore_cmd.add_argument(
        "--command", dest="commands", action="append", default=[], required=True,
        help="Command for one branch; repeat for more branches.",
    )
    explore_cmd.add_argument("--from", dest="from_checkpoint", default=None)
    explore_cmd.add_argument("--max-workers", type=int, default=4)
    explore_cmd.add_argument("--keep", action="store_true")


def _strip_separator(parts: list[str]) -> list[str]:
    return parts[1:] if parts and parts[0] == "--" else parts


def run_sandbox_command(args: argparse.Namespace) -> int:
    if args.sandbox_command == "hook":
        from .hooks import (
            handle_post_tool_use,
            handle_pre_tool_use,
            handle_session_start,
        )

        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except Exception as exc:  # noqa: BLE001 - stdin may be garbage; hooks must not fail
            print(f"[replay-sandbox hook] unreadable stdin: {exc!r}", file=sys.stderr)
            payload = {}
        try:
            if args.hook_event == "pre-tool-use":
                out = handle_pre_tool_use(payload, os.environ)
                if out is not None:
                    print(json.dumps(out, ensure_ascii=False))
            elif args.hook_event == "post-tool-use":
                handle_post_tool_use(payload, os.environ)
            else:
                text = handle_session_start(payload, os.environ)
                if text is not None:
                    print(text)
        except Exception as exc:  # noqa: BLE001 - hooks must never fail the session
            print(f"[replay-sandbox hook] fatal: {exc!r}", file=sys.stderr)
        return 0

    sandbox_root = Path(args.sandbox_root)
    if args.sandbox_command == "up":
        session = SandboxSession.up(
            sandbox_root=sandbox_root,
            workspace_dir=Path(args.workspace),
            image=args.image,
            docker=_make_docker() if args.runtime == "docker" else None,
            strict_ledger=args.strict_ledger,
            async_checkpoints=args.async_checkpoints,
            runtime=args.runtime,
        )
        print(session.session_id)
        return 0

    if args.sandbox_command == "mcp":
        # Dispatched BEFORE the shared session load: a stale/purged session
        # must not crash the server at startup — run_mcp_server's best-effort
        # ensure_up + per-call McpToolError semantics handle it degraded.
        # Local import (vs. top-of-file style): the mcp subcommand should not
        # slow down the other CLI paths with the MCP server machinery.
        from .mcp_server import run_mcp_server
        return run_mcp_server(args.session, sandbox_root)

    if args.sandbox_command == "proxy":
        # Dispatched BEFORE the shared session load (same rationale as mcp):
        # the daemon only writes proxy.json/ledger.jsonl under the session
        # dir and must not require a loadable session.json to start serving.
        from .ledger import run_proxy_daemon
        return run_proxy_daemon(
            sandbox_root / "sessions" / args.session, args.port
        )

    if args.sandbox_command == "commit-worker":
        # Dispatched BEFORE the shared session load: the worker must not
        # construct a SandboxSession (no proxy daemon, no session lock).
        from .session import run_commit_worker

        return run_commit_worker(sandbox_root, args.session, args.checkpoint)

    # Teardown and read-only inspection have no reason to start recording:
    # loading them with the ledger enabled would resurrect the proxy daemon
    # (for `down`, right before killing the sandbox; for post-teardown
    # `ledger`/`timeline`/`processes`, with nothing left to ever stop it),
    # leaking it forever. Neither may `pause`/`resume`: pause would spend up
    # to 15 s starting a daemon immediately before freezing the sandbox,
    # then leave it idling for the whole paused period.
    egress = args.sandbox_command not in {
        "down", "ledger", "timeline", "processes", "gc", "diff",
        "pause", "resume",
    }
    runtime = _session_runtime(sandbox_root, args.session)
    session = SandboxSession.load(
        sandbox_root=sandbox_root, session_id=args.session,
        docker=_make_docker() if runtime == "docker" else None,
        egress_ledger=egress,
    )
    if args.sandbox_command == "explore":
        from .orchestrate import explore

        results = explore(
            session, args.commands, checkpoint_id=args.from_checkpoint,
            max_workers=args.max_workers, keep=args.keep,
        )
        for result in results:
            status = "error" if result.error else f"rc={result.returncode}"
            print(f"[{result.branch}] {status} session={result.session_id} "
                  f"cmd={result.label}")
            if result.error:
                print(f"    error: {result.error}")
            for line in (result.stdout or "").splitlines()[:10]:
                print(f"    {line}")
        if args.keep:
            print("branches kept; tear them down with `replay sandbox down "
                  "--session <id> --purge`")
        return 0 if all(r.error is None and r.returncode == 0 for r in results) else 1
    if args.sandbox_command == "exec":
        parts = _strip_separator(list(args.exec_command))
        command = parts[0] if len(parts) == 1 else shlex.join(parts)
        result = session.exec(command)
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode
    if args.sandbox_command == "checkpoint":
        checkpoint = session.checkpoint(label=args.label, trigger=args.trigger)
        print(checkpoint.checkpoint_id)
        return 0
    if args.sandbox_command == "timeline":
        from .navigation import build_timeline_view, fork_children_in, render_timeline

        nodes = build_timeline_view(
            session.timeline(),
            session.state.head_checkpoint_id,
            fork_children_in(
                sandbox_root, session.state.lineage_id, session.session_id
            ),
        )
        print(render_timeline(nodes, cap=len(nodes)))
        return 0
    if args.sandbox_command == "diff":
        from .navigation import build_diff

        print(
            build_diff(session, args.from_checkpoint, args.to_checkpoint).to_text()
        )
        return 0
    if args.sandbox_command in {"goto", "rewind"}:
        report = session.rewind(
            args.checkpoint_id, reason=args.reason, force=args.force
        )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    if args.sandbox_command == "fork":
        child = session.fork(args.from_checkpoint)
        print(child.session_id)
        return 0
    if args.sandbox_command == "down":
        session.down(purge=args.purge)
        return 0
    if args.sandbox_command == "pause":
        changed = session.pause()
        print("paused" if changed else "already paused (or pause failed)")
        return 0
    if args.sandbox_command == "resume":
        changed = session.resume()
        print("resumed" if changed else "already running (or resume failed)")
        return 0
    if args.sandbox_command == "squash":
        report = session.squash(force=args.force)
        if report is None:
            print("nothing to squash: base image is already flat (<= 2 layers)")
        else:
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    if args.sandbox_command == "processes":
        if args.policy_assignment:
            pattern, _, policy = args.policy_assignment.partition("=")
            if policy not in {"restart", "lost"} or not pattern:
                print("--set expects PATTERN=restart|lost", file=sys.stderr)
                return 2
            path = session.processes_overrides_path
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            current[pattern] = policy
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"{pattern} -> {policy}")
            return 0
        overrides_path = session.processes_overrides_path
        if overrides_path.exists():
            print(f"policy overrides ({overrides_path}):")
            for pattern, policy in json.loads(
                overrides_path.read_text(encoding="utf-8")
            ).items():
                print(f"  {pattern} -> {policy}")
        head = session.state.head_checkpoint_id
        if head:
            handle = session._registry.get(head).handles.get("processes")
            if handle:
                for entry in session._processes.load_manifest(handle):
                    print(
                        f"  pid={entry.get('pid')} policy={entry.get('policy')} "
                        f"cwd={entry.get('cwd')} {entry.get('cmdline')}"
                    )
        return 0
    if args.sandbox_command == "ledger":
        from .ledger import classify_ledger_entry, describe_entry

        for row in session.ledger_store.load():
            if row["seq"] <= args.since:
                continue
            print(f"{describe_entry(row)}  [{classify_ledger_entry(row)}]")
        return 0
    if args.sandbox_command == "gc":
        from .gc import collect, fork_origins_in, layer_depth, plan_gc

        # Plan AND collect under the session lock: hooks/MCP in other
        # processes could otherwise rewind to (or fork from) a checkpoint
        # this plan just marked collectable. Printing stays outside.
        with session._session_lock():
            session._reload_state()
            plan = plan_gc(
                session._registry.load(),
                session.state.head_checkpoint_id,
                fork_origins=fork_origins_in(sandbox_root, session.state.lineage_id),
                keep_recent=args.keep,
            )
            report = collect(session, plan, dry_run=args.dry_run)
        verb = "would remove" if args.dry_run else "removed"
        print(
            f"protected {len(plan.protected)} checkpoint(s); "
            f"{verb} {len(plan.collectable)} image(s)"
        )
        for checkpoint_id in plan.collectable:
            print(f"  {verb}: {checkpoint_id}")
        depth = layer_depth(session)
        if depth:
            print(f"head image layer depth: {depth} (docker's limit is ~125)")
        if not args.dry_run and report.removed_images:
            print(
                "note: registry rows are kept, so the timeline stays complete; "
                "the collected checkpoints can no longer be rewound to"
            )
        return 0
    raise ValueError(f"Unsupported sandbox command: {args.sandbox_command}")


def register_wrap_parser(subparsers: argparse._SubParsersAction) -> None:
    wrap = subparsers.add_parser(
        "wrap", help="Launch Claude Code (or another agent CLI) with a "
        "rollback-capable sandbox wired in."
    )
    wrap.add_argument("--image", default="ubuntu:24.04")
    wrap.add_argument("--workspace", default=".")
    wrap.add_argument("--sandbox-root", default=DEFAULT_SANDBOX_ROOT)
    wrap.add_argument("--strict-ledger", action="store_true")
    wrap.add_argument("--runtime", choices=["docker", "podman"], default="docker")
    # wrap defaults to deferred rootfs commits (the cost hides inside the
    # agent's LLM inference window); opt out per launch.
    wrap.add_argument(
        "--no-async-checkpoints", dest="async_checkpoints",
        action="store_false", default=True,
    )
    wrap.add_argument("--dry-run", action="store_true")
    wrap.add_argument("--no-launch", action="store_true")
    wrap.add_argument("--remove", action="store_true")
    # dest 不可叫 "command":同 exec 的嵌套 subparser 覆写陷阱
    wrap.add_argument(
        "wrap_command", metavar="command", nargs=argparse.REMAINDER,
        help="Agent CLI to exec (default: claude). Prefix with --.",
    )


def run_wrap_command(args: argparse.Namespace) -> int:
    from .wrap import run_wrap

    return run_wrap(
        project_dir=Path(args.workspace),
        workspace_dir=Path(args.workspace),
        image=args.image,
        sandbox_root=Path(args.sandbox_root),
        command=_strip_separator(list(args.wrap_command)),
        dry_run=args.dry_run,
        no_launch=args.no_launch,
        remove=args.remove,
        strict_ledger=args.strict_ledger,
        async_checkpoints=args.async_checkpoints,
        runtime=args.runtime,
    )
