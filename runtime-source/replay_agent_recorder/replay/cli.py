from __future__ import annotations

import argparse
import json
import runpy
import shutil
import sys
from pathlib import Path
from typing import Any

from .api import install, record, replay
from .git_workspace import GitWorkspace, GitWorkspaceSpec
from .graph_ir import build_graph_ir, filter_graph_ir, load_trace_records, summarize_graph_ir
from .sandbox.cli import register_sandbox_parser, register_wrap_parser, run_sandbox_command, run_wrap_command
from .scaffold import SUPPORTED_TOOL_STYLES, scaffold_integration
from .storage import find_record_by_uid, load_records, run_path, workspace_metadata_by_id
from .visualize import graph_ir_to_mermaid, write_html_graph, write_mermaid_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="replay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_cmd = subparsers.add_parser("record", help="Record a Python script run.")
    record_cmd.add_argument("run_id", help="Run id to write.")
    _add_common_script_arguments(record_cmd)

    replay_cmd = subparsers.add_parser("replay", help="Replay a Python script from a recorded run.")
    replay_cmd.add_argument("base_run", help="Run id to replay from.")
    replay_cmd.add_argument("--semantic-fallback", action="store_true")
    _add_common_script_arguments(replay_cmd)

    fork_cmd = subparsers.add_parser("fork", help="Replay a script from a breakpoint with an override.")
    fork_cmd.add_argument("base_run", help="Run id to replay from.")
    fork_cmd.add_argument("--breakpoint-record-uid", required=True)
    fork_override_group = fork_cmd.add_mutually_exclusive_group(required=True)
    fork_override_group.add_argument("--override-output", default=None)
    fork_override_group.add_argument("--override-message-json", default=None)
    fork_override_group.add_argument("--override-input-json", default=None)
    fork_cmd.add_argument("--fork-run", default=None)
    fork_cmd.add_argument(
        "--fork-reuse-policy",
        choices=("never", "safe"),
        default="never",
        help=(
            "Post-breakpoint reuse policy: 'never' runs all actual post-fork nodes live; "
            "'safe' allows matching base records as a cache when provenance and Git tree are compatible."
        ),
    )
    fork_cmd.add_argument("--semantic-fallback", action="store_true")
    _add_common_script_arguments(fork_cmd)

    python_cmd = subparsers.add_parser("python", help="Run a Python script under replay instrumentation.")
    mode_group = python_cmd.add_mutually_exclusive_group()
    mode_group.add_argument("--run-id", default=None, help="Record the script into this run id.")
    mode_group.add_argument("--base-run", default=None, help="Replay the script from this base run id.")
    python_cmd.add_argument("--log-dir", type=Path, default=None)
    python_cmd.add_argument("--breakpoint-record-uid", default=None)
    python_cmd.add_argument("--override-output", default=None)
    python_cmd.add_argument("--override-message-json", default=None)
    python_cmd.add_argument("--override-input-json", default=None)
    python_cmd.add_argument("--fork-run", default=None)
    python_cmd.add_argument(
        "--fork-reuse-policy",
        choices=("never", "safe"),
        default="never",
        help="Post-breakpoint fork reuse policy.",
    )
    python_cmd.add_argument("--semantic-fallback", action="store_true")
    python_cmd.add_argument("--no-semantic", action="store_true", help="Disable AST provenance instrumentation.")
    python_cmd.add_argument("--project-root", type=Path, default=None)
    python_cmd.add_argument("--include", action="append", default=None)
    python_cmd.add_argument("--exclude", action="append", default=None)
    python_cmd.add_argument("script", type=Path)
    python_cmd.add_argument("script_args", nargs=argparse.REMAINDER)

    graph = subparsers.add_parser("graph", help="Build and export graph views from replay traces.")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)

    for name in ("summary", "export-ir", "mermaid", "html"):
        graph_cmd = graph_sub.add_parser(name)
        graph_cmd.add_argument("paths", nargs="+", type=Path)
        graph_cmd.add_argument("--fork", action="append", default=[], type=Path)
        graph_cmd.add_argument("--focus", default=None)
        graph_cmd.add_argument("--direction", choices=("upstream", "downstream", "both"), default="both")
        graph_cmd.add_argument("--max-depth", type=int, default=None)
        graph_cmd.add_argument("--title", default=None)
        graph_cmd.add_argument("--output", type=Path, default=None)
        graph_cmd.add_argument("--group-by", choices=("none", "path", "span", "run"), default="path")
        if name == "html":
            graph_cmd.add_argument("--asset-mode", choices=("inline", "vendored"), default="inline")
            graph_cmd.add_argument("--renderer", choices=("svg", "xyflow"), default="svg")

    scaffold = subparsers.add_parser("scaffold", help="Generate Replay integration wrapper skeletons.")
    scaffold_sub = scaffold.add_subparsers(dest="scaffold_command", required=True)
    integration = scaffold_sub.add_parser("integration", help="Generate a generic agent integration wrapper.")
    integration.add_argument("--name", required=True, help="Integration package name.")
    integration.add_argument(
        "--output-dir",
        type=Path,
        default=Path("integrations"),
        help="Directory where the integration package should be created.",
    )
    integration.add_argument(
        "--tool-style",
        choices=SUPPORTED_TOOL_STYLES,
        default="none",
        help="Initial adapter template style.",
    )
    integration.add_argument(
        "--framework",
        choices=("auto", "none", "langchain", "langgraph", "both"),
        default="auto",
        help="Framework patches installed by generated runner.py.",
    )
    integration.add_argument("--target-root", type=Path, default=None, help="Target project root used for detection/defaults.")
    integration.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Default workspace directory used directly by the generated wrapper. "
            "Record, replay, and fork mutate this directory in place. "
            "If omitted, generated runners default it to --target-root."
        ),
    )
    integration.add_argument("--detect", action="store_true", help="Detect likely target entries and write replay_target.json.")
    integration.add_argument("--entry", default=None, help="Default target entry to write into replay_target.json.")
    integration.add_argument(
        "--entry-kind",
        choices=("auto", "script", "module", "import", "factory", "runnable", "langgraph-json", "asgi"),
        default="auto",
    )
    integration.add_argument(
        "--method",
        choices=("auto", "call", "invoke", "ainvoke", "stream", "astream", "serve"),
        default="auto",
    )
    integration.add_argument("--graph", default=None, help="Default LangGraph graph name.")
    integration.add_argument("--force", action="store_true", help="Overwrite existing generated files.")

    fs = subparsers.add_parser("fs", help="Inspect and restore Git-backed replay filesystems.")
    fs_sub = fs.add_subparsers(dest="fs_command", required=True)

    fs_list = fs_sub.add_parser("list", help="List filesystem checkpoints for a run.")
    fs_list.add_argument("--run", required=True, help="Run id to inspect.")
    fs_list.add_argument("--workspace", default=None, help="Workspace id to filter.")
    fs_list.add_argument("--log-dir", type=Path, default=None)

    fs_checkout = fs_sub.add_parser("checkout", help="Restore a checkpoint into a destination directory.")
    fs_checkout.add_argument("--run", required=True, help="Run id to inspect.")
    fs_checkout.add_argument("--record-uid", required=True)
    fs_checkout.add_argument("--position", choices=("before", "after"), required=True)
    fs_checkout.add_argument("--workspace", required=True)
    fs_checkout.add_argument("--to", type=Path, required=True)
    fs_checkout.add_argument("--force", action="store_true")
    fs_checkout.add_argument("--log-dir", type=Path, default=None)

    fs_diff = fs_sub.add_parser("diff", help="Diff filesystem trees between two records.")
    fs_diff.add_argument("--run", required=True, help="Run id to inspect.")
    fs_diff.add_argument("--from", dest="from_uid", required=True)
    fs_diff.add_argument("--to", dest="to_uid", required=True)
    fs_diff.add_argument("--workspace", required=True)
    fs_diff.add_argument("--position", choices=("before", "after"), default="after")
    fs_diff.add_argument("--log-dir", type=Path, default=None)

    fs_log = fs_sub.add_parser("log", help="Show Git checkpoint history for a workspace.")
    fs_log.add_argument("--run", required=True, help="Run id to inspect.")
    fs_log.add_argument("--workspace", required=True)
    fs_log.add_argument("--log-dir", type=Path, default=None)

    register_sandbox_parser(subparsers)
    register_wrap_parser(subparsers)

    return parser


def _add_common_script_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--no-semantic", action="store_true", help="Disable AST provenance instrumentation.")
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--include", action="append", default=None)
    parser.add_argument("--exclude", action="append", default=None)
    parser.add_argument("script", type=Path)
    parser.add_argument("script_args", nargs=argparse.REMAINDER)


def main() -> None:
    args = build_parser().parse_args()
    if args.command in {"record", "replay", "fork"}:
        _run_script_command(args)
        return
    if args.command == "python":
        _run_python_command(args)
        return
    if args.command == "graph":
        _run_graph_command(args)
        return
    if args.command == "scaffold":
        _run_scaffold_command(args)
        return
    if args.command == "fs":
        _run_fs_command(args)
        return
    if args.command == "sandbox":
        raise SystemExit(run_sandbox_command(args))
    if args.command == "wrap":
        raise SystemExit(run_wrap_command(args))
    raise ValueError(f"Unsupported command: {args.command}")


def _run_script_command(args: argparse.Namespace) -> None:
    python_args = argparse.Namespace(
        run_id=args.run_id if args.command == "record" else None,
        base_run=args.base_run if args.command in {"replay", "fork"} else None,
        log_dir=args.log_dir,
        breakpoint_record_uid=getattr(args, "breakpoint_record_uid", None),
        override_output=getattr(args, "override_output", None),
        override_message_json=getattr(args, "override_message_json", None),
        override_input_json=getattr(args, "override_input_json", None),
        fork_run=getattr(args, "fork_run", None),
        fork_reuse_policy=getattr(args, "fork_reuse_policy", "never"),
        semantic_fallback=getattr(args, "semantic_fallback", False),
        no_semantic=args.no_semantic,
        project_root=args.project_root,
        include=args.include,
        exclude=args.exclude,
        script=args.script,
        script_args=_strip_remainder_separator(args.script_args),
    )
    _run_python_command(python_args)


def _strip_remainder_separator(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _run_python_command(args: argparse.Namespace) -> None:
    override_message = json.loads(args.override_message_json) if args.override_message_json else None
    override_input = json.loads(args.override_input_json) if args.override_input_json else None
    install(
        semantic=not args.no_semantic,
        project_root=args.project_root,
        include=args.include,
        exclude=args.exclude,
    )

    script_path = args.script
    script_argv = [str(script_path), *args.script_args]
    old_argv = sys.argv
    try:
        sys.argv = script_argv
        if args.base_run:
            with replay(
                base_run=args.base_run,
                breakpoint_record_uid=args.breakpoint_record_uid,
                override_output=args.override_output,
                override_input=override_input,
                override_message=override_message,
                log_dir=args.log_dir,
                fork_run=args.fork_run,
                semantic_fallback=args.semantic_fallback,
                fork_reuse_policy=getattr(args, "fork_reuse_policy", "never"),
            ):
                runpy.run_path(str(script_path), run_name="__main__")
            return
        run_id = args.run_id or script_path.stem
        with record(run_id, log_dir=args.log_dir):
            runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = old_argv


def _run_graph_command(args: argparse.Namespace) -> None:
    ir = _build_cli_graph_ir(args)

    if args.graph_command == "summary":
        print(json.dumps(summarize_graph_ir(ir), indent=2, sort_keys=True))
        return
    if args.graph_command == "export-ir":
        if args.output is None:
            raise ValueError("--output is required for export-ir")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(ir, indent=2, sort_keys=True), encoding="utf-8")
        return
    if args.graph_command == "mermaid":
        if args.output is None:
            print(graph_ir_to_mermaid(ir, group_by=args.group_by), end="")
            return
        write_mermaid_markdown(ir, args.output, title=args.title, group_by=args.group_by)
        return
    if args.graph_command == "html":
        if args.output is None:
            raise ValueError("--output is required for html")
        write_html_graph(ir, args.output, title=args.title, asset_mode=args.asset_mode, renderer=args.renderer)
        return
    raise ValueError(f"Unsupported graph command: {args.graph_command}")


def _build_cli_graph_ir(args: argparse.Namespace) -> dict[str, Any]:
    all_paths = [*args.paths, *args.fork]
    records = load_trace_records(all_paths)
    ir = build_graph_ir(records, title=args.title)
    if args.focus:
        ir = filter_graph_ir(
            ir,
            focus=args.focus,
            direction=args.direction,
            max_depth=args.max_depth,
        )
    return ir


def _run_scaffold_command(args: argparse.Namespace) -> None:
    if args.scaffold_command != "integration":
        raise ValueError(f"Unsupported scaffold command: {args.scaffold_command}")
    written = scaffold_integration(
        name=args.name,
        output_dir=args.output_dir,
        tool_style=args.tool_style,
        framework=args.framework,
        target_root=args.target_root,
        workspace_root=args.workspace_root,
        entry=args.entry,
        entry_kind=args.entry_kind,
        method=args.method,
        detect=args.detect,
        graph=args.graph,
        force=args.force,
    )
    for path in written:
        print(path)


def _run_fs_command(args: argparse.Namespace) -> None:
    log_dir = _cli_log_dir(args)
    records = load_records(run_path(log_dir, args.run))
    workspaces = workspace_metadata_by_id(records)

    if args.fs_command == "list":
        _run_fs_list(args, records)
        return
    if args.fs_command == "checkout":
        _run_fs_checkout(args, records, workspaces, log_dir)
        return
    if args.fs_command == "diff":
        _run_fs_diff(args, records, workspaces, log_dir)
        return
    if args.fs_command == "log":
        _run_fs_log(args, workspaces, log_dir)
        return
    raise ValueError(f"Unsupported fs command: {args.fs_command}")


def _cli_log_dir(args: argparse.Namespace) -> Path:
    return Path(args.log_dir) if args.log_dir is not None else Path(__file__).resolve().parent / "runs"


def _run_fs_list(args: argparse.Namespace, records: list[dict[str, Any]]) -> None:
    print("workspace\trecord_uid\tkind\tchanged\tbefore_commit\tafter_commit\tbefore_tree\tafter_tree\tdiff")
    for record in records:
        if record.get("kind") not in {"llm", "tool"}:
            continue
        filesystem = _record_filesystem(record)
        if not filesystem:
            continue
        for workspace_id, workspace in sorted(filesystem.get("workspaces", {}).items()):
            if args.workspace is not None and workspace_id != args.workspace:
                continue
            if not isinstance(workspace, dict):
                continue
            print(
                "\t".join(
                    [
                        workspace_id,
                        str(record.get("record_uid") or ""),
                        str(record.get("kind") or ""),
                        str(workspace.get("changed")),
                        _short_commit(workspace.get("before_commit")),
                        _short_commit(workspace.get("after_commit")),
                        _short_commit(workspace.get("before_tree")),
                        _short_commit(workspace.get("after_tree")),
                        _format_diff_summary(workspace.get("diff_summary") or []),
                    ]
                )
            )


def _run_fs_checkout(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    workspaces: dict[str, dict[str, Any]],
    log_dir: Path,
) -> None:
    workspace_metadata = _workspace_metadata(workspaces, args.workspace)
    record = find_record_by_uid(records, args.record_uid)
    checkpoint = _record_workspace_checkpoint(record, args.workspace)
    commit = checkpoint.get(f"{args.position}_commit")
    if not isinstance(commit, str) or not commit:
        raise ValueError(
            f"Record {args.record_uid!r} does not have {args.position}_commit for workspace {args.workspace!r}"
        )
    workspace = _workspace_for_cli(
        workspace_id=args.workspace,
        workspace_metadata=workspace_metadata,
        log_dir=log_dir,
        run_id=f"{args.run}_fs_checkout",
        work_root=args.to,
    )
    workspace.validate_for_replay()
    _prepare_checkout_destination(args.to, force=args.force)
    workspace.setup_replay(
        initial_commit=commit,
        branch_ref=f"refs/replay/cli/{args.run}/checkout/{args.workspace}",
    )
    print(f"Checked out {args.workspace} {args.record_uid} {args.position} {commit} to {workspace.root}")


def _run_fs_diff(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    workspaces: dict[str, dict[str, Any]],
    log_dir: Path,
) -> None:
    workspace_metadata = _workspace_metadata(workspaces, args.workspace)
    from_record = find_record_by_uid(records, args.from_uid)
    to_record = find_record_by_uid(records, args.to_uid)
    from_checkpoint = _record_workspace_checkpoint(from_record, args.workspace)
    to_checkpoint = _record_workspace_checkpoint(to_record, args.workspace)
    from_tree = from_checkpoint.get(f"{args.position}_tree")
    to_tree = to_checkpoint.get(f"{args.position}_tree")
    if not isinstance(from_tree, str) or not isinstance(to_tree, str):
        raise ValueError(f"Selected records do not have {args.position}_tree for workspace {args.workspace!r}")
    workspace = _workspace_for_cli(
        workspace_id=args.workspace,
        workspace_metadata=workspace_metadata,
        log_dir=log_dir,
        run_id=f"{args.run}_fs_diff",
        work_root=log_dir / "_cli" / "diff" / args.run / args.workspace,
    )
    workspace.root.mkdir(parents=True, exist_ok=True)
    workspace.ensure_bare_repo()
    result = workspace._run_git(["diff", "--name-status", from_tree, to_tree])
    print(result.stdout, end="")


def _run_fs_log(
    args: argparse.Namespace,
    workspaces: dict[str, dict[str, Any]],
    log_dir: Path,
) -> None:
    workspace_metadata = _workspace_metadata(workspaces, args.workspace)
    branch_ref = workspace_metadata.get("branch_ref")
    if not isinstance(branch_ref, str) or not branch_ref:
        branch_ref = f"refs/replay/runs/{args.run}/workspaces/{args.workspace}"
    workspace = _workspace_for_cli(
        workspace_id=args.workspace,
        workspace_metadata=workspace_metadata,
        log_dir=log_dir,
        run_id=f"{args.run}_fs_log",
        work_root=log_dir / "_cli" / "log" / args.run / args.workspace,
    )
    workspace.root.mkdir(parents=True, exist_ok=True)
    workspace.ensure_bare_repo()
    result = workspace._run_git(["log", "--oneline", branch_ref])
    print(result.stdout, end="")


def _record_filesystem(record: dict[str, Any]) -> dict[str, Any] | None:
    metadata = record.get("metadata")
    filesystem = metadata.get("filesystem") if isinstance(metadata, dict) else None
    if isinstance(filesystem, dict) and filesystem.get("backend") == "git":
        return filesystem
    return None


def _record_workspace_checkpoint(record: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    filesystem = _record_filesystem(record)
    if filesystem is None:
        raise ValueError(f"Record {record.get('record_uid')!r} has no Git filesystem metadata")
    workspaces = filesystem.get("workspaces")
    if not isinstance(workspaces, dict):
        raise ValueError(f"Record {record.get('record_uid')!r} has invalid workspace metadata")
    workspace = workspaces.get(workspace_id)
    if not isinstance(workspace, dict):
        raise ValueError(f"Record {record.get('record_uid')!r} has no workspace {workspace_id!r}")
    return workspace


def _workspace_metadata(workspaces: dict[str, dict[str, Any]], workspace_id: str) -> dict[str, Any]:
    workspace = workspaces.get(workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace metadata not found: {workspace_id}")
    return workspace


def _workspace_for_cli(
    *,
    workspace_id: str,
    workspace_metadata: dict[str, Any],
    log_dir: Path,
    run_id: str,
    work_root: Path,
) -> GitWorkspace:
    repo_dir = workspace_metadata.get("repo_dir")
    if not isinstance(repo_dir, str) or not repo_dir:
        raise ValueError(f"Workspace metadata is missing repo_dir: {workspace_id}")
    return GitWorkspace(
        workspace_id=workspace_id,
        spec=GitWorkspaceSpec(
            work_root=work_root,
            store_dir=Path(repo_dir).parent,
            initial="empty",
        ),
        log_dir=log_dir,
        run_id=run_id,
        mode="replay",
    )


def _prepare_checkout_destination(path: Path, *, force: bool) -> None:
    if path.exists() and any(path.iterdir()):
        if not force:
            raise ValueError(f"Checkout destination exists and is nonempty: {path}")
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    path.mkdir(parents=True, exist_ok=True)


def _short_commit(value: Any) -> str:
    return str(value)[:12] if value else "-"


def _format_diff_summary(diff_summary: Any) -> str:
    if not isinstance(diff_summary, list) or not diff_summary:
        return "-"
    parts = []
    for item in diff_summary:
        if isinstance(item, dict):
            status = item.get("status")
            path = item.get("path")
            if status and path:
                parts.append(f"{status} {path}")
    return ", ".join(parts) if parts else "-"


if __name__ == "__main__":
    main()
