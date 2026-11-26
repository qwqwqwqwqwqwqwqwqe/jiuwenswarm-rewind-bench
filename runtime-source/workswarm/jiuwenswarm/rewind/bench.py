# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline task supervisor: fresh processes, cumulative budgets, final patches."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jiuwenswarm.rewind.contracts import Manifest, SCHEMA, TERMINAL_STATES, Task
from jiuwenswarm.rewind.storage import UsageJournal, atomic_json


def git(repository: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(repository), *args], check=True,
                          text=True, encoding="utf-8", capture_output=True, env=env).stdout


def validate_repositories(manifest: Manifest) -> list[dict]:
    evidence = []
    for task in manifest.tasks:
        repository = Path(task.repository).resolve(strict=True)
        commit = git(repository, "rev-parse", "--verify", f"{task.base_commit}^{{commit}}").strip()
        if git(repository, "rev-parse", "HEAD").strip() != commit:
            raise ValueError(f"{task.instance_id}: HEAD must equal base_commit")
        if git(repository, "status", "--porcelain", "--untracked-files=normal").strip():
            raise ValueError(f"{task.instance_id}: source repository must be clean")
        if (repository / ".gitmodules").exists():
            raise ValueError("v1 requires self-contained repositories without submodules")
        evidence.append({"instance_id": task.instance_id, "repository": str(repository), "base_commit": commit})
    return evidence


def export_patch(repository: Path, base_commit: str, output: Path) -> str:
    """Include added files without changing the Agent's real Git index."""
    fd, name = tempfile.mkstemp(prefix="rewind-index-", dir=output.parent)
    os.close(fd)
    os.unlink(name)
    env = {**os.environ, "GIT_INDEX_FILE": name}
    try:
        git(repository, "read-tree", base_commit, env=env)
        git(repository, "add", "-A", "--", ".", env=env)
        patch = git(repository, "diff", "--cached", "--binary", "--no-ext-diff", base_commit, env=env)
        output.write_text(patch, encoding="utf-8", newline="\n")
        return patch
    finally:
        for path in (Path(name), Path(name + ".lock")):
            path.unlink(missing_ok=True)


def _stop(process: subprocess.Popen, *, container: str | None) -> None:
    """Stop the whole attempt before allowing the next one to start."""
    if container:
        try:
            removed = subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
            if removed.returncode != 0:
                raise RuntimeError("Could not confirm removal of the previous attempt container")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
    else:
        # Local mode is for trusted smoke drivers. Benchmark tools may spawn
        # detached process groups; use a container for the real coding Agent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def run_worker(job: dict, *, executor: str, image: str | None, environment: tuple[str, ...], cancel_event: Any = None) -> dict:
    attempt_dir = Path(job["attempt_dir"])
    job_path = attempt_dir / "job.json"
    container = None
    if executor == "docker":
        session_dir = attempt_dir / "data"
        session_dir.mkdir(exist_ok=False)
        container_job = {**job, "workspace": "/workspace/task", "state_dir": "/recovery",
                         "attempt_dir": f"/recovery/attempt-{job['attempt']}", "data_dir": "/session-data"}
        if job.get("recovery"):
            container_job["recovery"] = {
                key: value.replace(job["state_dir"], "/recovery", 1)
                if isinstance(value, str) and value.startswith(job["state_dir"] + "/") else value
                for key, value in job["recovery"].items()
            }
        atomic_json(job_path, container_job)
        container = f"rewind-{job['task_id']}-{job['mode']}-a{job['attempt']}"
        command = ["docker", "run", "--name", container, "--init", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--pids-limit", "512",
                   "--mount", f"type=bind,src={job['state_dir']},dst=/recovery",
                   "--mount", f"type=bind,src={job['workspace']},dst=/workspace/task",
                   "--mount", f"type=bind,src={session_dir},dst=/session-data",
                   "--workdir", "/workspace/task/repo"]
        for name in environment:
            command.extend(["--env", name])
        command.extend([image, "python", "-m", "jiuwenswarm.rewind.worker", "--job",
                        f"/recovery/attempt-{job['attempt']}/job.json"])
    else:
        atomic_json(job_path, job)
        command = [sys.executable, "-m", "jiuwenswarm.rewind.worker", "--job", str(job_path)]
    timeout = max(0.01, job["deadline"] - time.time())
    with (attempt_dir / "stdout.log").open("wb") as out, (attempt_dir / "stderr.log").open("wb") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True,
                                   cwd=Path(job["workspace"]) / "repo")
        timed_out = False
        cancelled = False
        cleanup_error = None
        try:
            if cancel_event is None:
                process.wait(timeout=timeout)
            else:
                while process.poll() is None:
                    if cancel_event.is_set():
                        cancelled = True
                        break
                    remaining = job["deadline"] - time.time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(0.25, remaining))
                    except subprocess.TimeoutExpired:
                        continue
        except subprocess.TimeoutExpired:
            timed_out = True
        except KeyboardInterrupt:
            cancelled = True
        finally:
            try:
                _stop(process, container=container)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                cleanup_error = type(error).__name__
    # The result is consumed only after all processes in the container stopped.
    result_path = attempt_dir / "result.json"
    if cleanup_error:
        return {"state": "FAILED", "error": "Attempt cleanup could not be confirmed", "cleanup_error": cleanup_error}
    if cancelled:
        return {"state": "CANCELLED"}
    if timed_out:
        return {"state": "BUDGET_EXHAUSTED", "error": "Parent wall-time deadline"}
    if process.returncode != 0 or not result_path.exists():
        return {"state": "FAILED", "error": "Worker exited without a successful handoff"}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if container and result.get("recovery"):
        result["recovery"] = {
            key: str(Path(job["state_dir"]) / value.removeprefix("/recovery/"))
            if isinstance(value, str) and value.startswith("/recovery/") else value
            for key, value in result["recovery"].items()
        }
    if (result.get("schema") != SCHEMA or result.get("task_id") != job["task_id"] or
            result.get("attempt") != job["attempt"] or result.get("run_id") != job["run_id"] or
            result.get("state") not in TERMINAL_STATES):
        raise ValueError("Invalid or stale worker handoff")
    if result["state"] == "REWIND_READY":
        expected = f"{job['task_id']}-a{job['attempt'] + 1}"
        if result.get("recovery_id") != expected or result["recovery"]["fork_run"] != expected:
            raise ValueError("Recovery identity does not match the next attempt")
        for key in ("override_input_file", "backtrack_metadata_file"):
            path = Path(result["recovery"][key]).resolve(strict=True)
            if not path.is_relative_to(Path(job["state_dir"]).resolve() / "traces"):
                raise ValueError("Recovery path escaped the task trace directory")
    return result


def run_task(manifest: Manifest, task: Task, *, mode: str, output: Path, executor: str, cancel_event: Any = None) -> dict:
    task_dir = output / task.instance_id
    task_dir.mkdir(parents=True, exist_ok=False)
    state_dir, workspace = task_dir / "state", task_dir / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    repository = workspace / "repo"
    # Materialize the source commit independently of its index, ignored files,
    # Git alternates and worktree link. Dependencies belong in the image.
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", task.repository,
                    str(repository)], check=True, capture_output=True)
    git(repository, "checkout", "--quiet", "--detach", task.base_commit)
    base_commit = git(repository, "rev-parse", "HEAD").strip()
    started = time.time()
    deadline = started + manifest.budget.seconds
    journal = UsageJournal(state_dir / "usage.jsonl", token_limit=manifest.budget.tokens, deadline=deadline)
    recovery = None
    attempts: list[dict] = []
    for attempt in range(manifest.budget.max_rewinds + 1):
        attempt_dir = state_dir / f"attempt-{attempt}"
        attempt_dir.mkdir()
        run_id = f"{task.instance_id}-a{attempt}"
        job = {"schema": SCHEMA, "manifest": manifest.to_dict(), "task": asdict(task),
               "mode": mode, "task_id": task.instance_id, "attempt": attempt, "run_id": run_id,
               "workspace": str(workspace), "state_dir": str(state_dir), "attempt_dir": str(attempt_dir),
               "deadline": deadline, "logical_timestamp": started, "recovery": recovery}
        worker_options = {"cancel_event": cancel_event} if cancel_event is not None else {}
        result = run_worker(job, executor=executor, image=manifest.image, environment=manifest.environment, **worker_options)
        attempts.append({"attempt": attempt, **result})
        atomic_json(state_dir / "attempts.json", attempts)
        if result["state"] != "REWIND_READY":
            break
        if mode != "on" or attempt >= manifest.budget.max_rewinds:
            result = {"state": "BUDGET_EXHAUSTED", "error": "Rewind limit reached"}
            break
        if time.time() >= deadline or journal.totals()["tokens"] >= manifest.budget.tokens:
            result = {"state": "BUDGET_EXHAUSTED", "error": "Cumulative budget exhausted before restart"}
            break
        recovery = result["recovery"]
    # Failed/timed-out partial edits are retained for diagnosis but never
    # submitted as a successful final branch to the official evaluator.
    try:
        patch = export_patch(repository, base_commit, task_dir / "diagnostic.patch")
    except subprocess.CalledProcessError:
        patch = ""
        result = {"state": "FAILED", "error": "Could not export a patch relative to the initial commit"}
    prediction = {"instance_id": task.instance_id, "model_name_or_path": manifest.model,
                  "model_patch": patch if result["state"] == "COMPLETED" else ""}
    atomic_json(task_dir / "prediction.json", prediction)
    report = {"schema": SCHEMA, "instance_id": task.instance_id, "mode": mode,
              "state": result["state"], "elapsed_seconds": time.time() - started,
              "rewinds": max(0, len(attempts) - 1), "usage": journal.totals(),
              "base_commit": base_commit, "attempts": attempts,
              "error": result.get("error"),
              "resolved": None, "prediction": prediction}
    atomic_json(task_dir / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rewind", choices=("off", "record-only", "on"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--executor", choices=("docker", "local"), default="docker")
    parser.add_argument("--check", action="store_true", help="Validate frozen tasks without running an Agent")
    args = parser.parse_args()
    manifest = Manifest.load(args.manifest.resolve())
    evidence = validate_repositories(manifest)
    if args.check:
        print(json.dumps({"schema": SCHEMA, "tasks": evidence, "mode": args.rewind}, ensure_ascii=False, indent=2))
        return
    if sys.platform != "linux" or sys.version_info < (3, 12):
        parser.error("Execution requires Linux and Python >= 3.12; use WSL or a Linux host")
    if args.executor == "docker" and (not manifest.image or not shutil.which("docker")):
        parser.error("Docker execution requires Docker and a manifest image pinned by digest")
    if args.executor == "local" and manifest.driver == "jiuwenswarm.rewind.native:run":
        parser.error("The coding Agent must run in a container; local mode is for trusted smoke drivers")
    output = (args.output or Path("rewind-results") / f"{manifest.experiment_id}-{args.rewind}").resolve()
    # Never place recovery state or result files inside an input repository.
    if any(output.is_relative_to(Path(task.repository)) for task in manifest.tasks):
        parser.error("Output must be outside every task repository")
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "manifest.json", manifest.to_dict())
    atomic_json(output / "source-revisions.json", evidence)
    with (output / "predictions.jsonl").open("w", encoding="utf-8") as predictions:
        for task in manifest.tasks:
            report = run_task(manifest, task, mode=args.rewind, output=output, executor=args.executor)
            predictions.write(json.dumps(report["prediction"], ensure_ascii=False) + "\n")
            predictions.flush()
            print(json.dumps({"instance_id": task.instance_id, "state": report["state"],
                              "usage": report["usage"], "rewinds": report["rewinds"]}), flush=True)
            if report["state"] == "CANCELLED":
                raise SystemExit(130)


if __name__ == "__main__":
    main()
