# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Restricted file queue: Web submits text; a host supervisor owns Docker."""

from __future__ import annotations

import argparse
import json
import re
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from jiuwenswarm.rewind.contracts import Manifest, Task
from jiuwenswarm.rewind.storage import atomic_json

SCHEMA = "jiuwenswarm.rewind.web.v1"


def parse_submission(value: dict, *, directory: str) -> tuple[str, str]:
    if set(value) != {"schema", "task_id", "prompt"} or value.get("schema") != SCHEMA:
        raise ValueError("Queue accepts only a task identity and text prompt")
    identity, prompt = value["task_id"], value["prompt"]
    if not isinstance(identity, str) or not re.fullmatch(r"web-[a-f0-9]{32}", identity) or identity != directory:
        raise ValueError("Invalid queue task identity")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 64000:
        raise ValueError("Task text must contain 1–64000 characters")
    return identity, prompt


def submit(queue: Path, task_id: str, prompt: str, *, cancelled: threading.Event, seconds: float) -> dict:
    packet = {"schema": SCHEMA, "task_id": task_id, "prompt": prompt}
    parse_submission(packet, directory=task_id)
    directory = queue / task_id
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / "request.json", packet)
    deadline = time.monotonic() + seconds
    cancelling_since = None
    while True:
        result = directory / "result.json"
        if result.exists():
            return json.loads(result.read_text(encoding="utf-8"))
        if cancelled.is_set() or time.monotonic() >= deadline:
            if cancelling_since is None:
                atomic_json(directory / "cancel.json", {"cancelled": True})
                cancelling_since = time.monotonic()
            if time.monotonic() - cancelling_since > 25:
                raise TimeoutError("Host supervisor did not acknowledge task cancellation")
        time.sleep(0.2)


def serve(manifest: Manifest, queue: Path, output_root: Path, *, stopped: threading.Event) -> None:
    from jiuwenswarm.rewind.bench import run_task, validate_repositories

    if len(manifest.tasks) != 1 or manifest.driver != "jiuwenswarm.rewind.native:run" or not manifest.image:
        raise ValueError("Host queue requires a fixed native driver, image and single repository")
    validate_repositories(manifest)
    queue.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    while not stopped.is_set():
        for request_path in sorted(queue.glob("web-*/request.json")):
            directory = request_path.parent
            if directory.is_symlink() or request_path.is_symlink() or directory.resolve().parent != queue.resolve():
                continue
            if (directory / "result.json").exists() or (directory / "claimed.json").exists():
                continue
            try:
                identity, prompt = parse_submission(json.loads(request_path.read_text(encoding="utf-8")), directory=directory.name)
                output = output_root / identity
                if not output.resolve().is_relative_to(output_root.resolve()) or output.is_symlink():
                    raise ValueError("Task output escaped the configured root")
                atomic_json(directory / "claimed.json", {"claimed": True})
                if (directory / "cancel.json").exists():
                    atomic_json(directory / "result.json", {"state": "CANCELLED", "rewinds": 0})
                    continue
                source = manifest.tasks[0]
                task = Task(identity, source.repository, source.base_commit, prompt)
                effective = replace(manifest, tasks=(task,))
                output.mkdir(parents=True, exist_ok=True)
                atomic_json(output / "manifest.json", effective.to_dict())
                cancelled, finished = threading.Event(), threading.Event()

                def watch():
                    while not finished.wait(0.1):
                        if stopped.is_set() or (directory / "cancel.json").exists():
                            cancelled.set()

                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
                try:
                    report = run_task(effective, task, mode="on", output=output, executor="docker", cancel_event=cancelled)
                    atomic_json(directory / "result.json", report)
                finally:
                    finished.set()
                    watcher.join(timeout=2)
            except Exception as error:
                # Job input cannot supply executables, paths, environment, image
                # or container flags. Provider details remain in attempt logs.
                atomic_json(directory / "result.json", {"state": "FAILED", "error": type(error).__name__, "rewinds": 0})
            if stopped.is_set():
                break
        stopped.wait(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    serve(Manifest.load(args.manifest.resolve()), args.queue.resolve(), args.output_root.resolve(), stopped=stopped)


if __name__ == "__main__":
    main()
