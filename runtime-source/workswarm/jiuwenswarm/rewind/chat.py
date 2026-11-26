"""AgentResponseChunk route owning attempts inside the existing task container."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
import uuid

from jiuwenswarm.rewind.bench_config import BenchOptions, SCHEMA, WORKSPACE_TOOLS, config_digest, single_agent_config, wire_value
from jiuwenswarm.rewind.storage import UsageJournal, atomic_json


def enabled(request) -> bool:
    return "rewind" in (request.params or {})


def validate(request):
    from gateway.common.mode_matrix import canonicalize_mode_text, DEPRECATION_MAP
    from jiuwenswarm.common.schema.agent import ReqMethod

    value = (request.params or {}).get("rewind")
    if not isinstance(value, dict):
        raise ValueError("rewind must be an explicit benchmark options object")
    options = BenchOptions(**value)
    if request.req_method != ReqMethod.CHAT_SEND:
        raise ValueError("Rewind benchmark accepts chat.send only")
    params = request.params or {}
    if params.get("media_items") or any((params.get("files") or {}).values()):
        raise ValueError("Rewind benchmark currently supports text-only tasks")
    if sys.platform != "linux" or sys.version_info < (3, 12):
        raise ValueError("In-container benchmark attempts require Linux Python >= 3.12")
    mode = canonicalize_mode_text((request.params or {}).get("mode", "agent"))
    if DEPRECATION_MAP.get(mode, mode) not in {
        "agent.work.normal", "agent.code.normal"
    }:
        raise ValueError("Rewind benchmark supports normal single-Agent modes only")
    if request.channel_id not in {"tui", "web"}:
        raise ValueError("Rewind benchmark requires a driver web/tui channel")
    workspace = Path((request.params or {}).get("workspace_dir", ""))
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("Rewind benchmark requires an existing absolute workspace_dir")
    return options, workspace.resolve()


async def stop_attempt(process, descendants=()):
    """Fence tracked children and the process group before any restore or new run."""
    import psutil

    tracked = list(descendants)
    try:
        tracked.extend(psutil.Process(process.pid).children(recursive=True))
    except psutil.NoSuchProcess:
        pass
    for child in tracked:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = await asyncio.to_thread(psutil.wait_procs, tracked, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout=3)
    # A surviving reparented child in the attempt group must not outlive its root.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for child in alive:
        try:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                raise RuntimeError("Attempt descendants could not be stopped")
        except psutil.NoSuchProcess:
            pass


async def messages(job):
    """Dedicated inherited pipe; stdout/stderr never enter the event protocol."""
    attempt = Path(job["attempt_dir"])
    attempt.mkdir(parents=True)
    atomic_json(attempt / "job.json", job)
    read_fd, write_fd = os.pipe()
    process = None
    transport = None
    try:
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, os.fdopen(read_fd, "rb"))
        read_fd = -1
        with (attempt / "process.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "jiuwenswarm.rewind.attempt", "--job", str(attempt / "job.json"),
                "--event-fd", str(write_fd), pass_fds=(write_fd,), start_new_session=True,
                cwd=str(attempt), stdout=log, stderr=log, stdin=asyncio.subprocess.PIPE)
            os.close(write_fd)
            write_fd = -1
            result_seen = False
            while True:
                remaining = job["deadline"] - time.time()
                if remaining <= 0:
                    raise TimeoutError("Cumulative benchmark deadline expired")
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                if not line:
                    break
                packet = json.loads(line)
                if packet.get("schema") != SCHEMA or packet.get("kind") not in {"chunk", "result"}:
                    raise ValueError("Invalid attempt IPC message")
                if result_seen:
                    raise ValueError("Attempt sent messages after its terminal result")
                if packet["kind"] == "result":
                    result_seen = True
                    # The child waits for stdin/termination, so descendants remain
                    # discoverable until this process has fenced the entire tree.
                    await stop_attempt(process)
                    yield packet
                    break
                yield packet
            if not result_seen:
                raise RuntimeError("Attempt exited without a terminal result")
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if read_fd >= 0:
            os.close(read_fd)
        try:
            if process is not None:
                await asyncio.shield(stop_attempt(process))
        finally:
            if transport is not None:
                transport.close()
            # Private credentials must not enter the driver's data archive.
            (Path(job["data_dir"]) / "config" / "config.yaml").unlink(missing_ok=True)
        data = Path(job["data_dir"])
        if data.exists():
            # Reuse one logical pathname, never the previous attempt's contents.
            # All processes are stopped before archiving, including SQLite owners.
            data.rename(attempt / "data")


async def stream(request):
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.common.utils import get_user_workspace_dir, get_agent_sessions_dir
    from jiuwenswarm.server.runtime.session.history_io import run_history_io
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    options, workspace = validate(request)
    root = get_user_workspace_dir().resolve()
    if root == workspace or workspace in root.parents:
        raise ValueError("Agent data and recovery state must be outside the task workspace")
    history = get_agent_sessions_dir() / str(request.session_id) / "history.jsonl"
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("role") == "user":
                raise ValueError("Benchmark rewind requires a fresh single-turn session")
    task_id = "bench-" + hashlib.sha256(str(request.request_id).encode()).hexdigest()[:12] + "-" + uuid.uuid4().hex[:8]
    state = root / "rewind-bench" / task_id
    state.mkdir(parents=True, mode=0o700)
    config = single_agent_config(get_config())
    config_file = root / "config" / "rewind-bench" / (task_id + ".json")
    config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # YAML accepts JSON; keep the resolved secret-bearing snapshot under the
    # existing config exclusion rather than the exported recovery directory.
    atomic_json(config_file, config)
    os.chmod(config_file, 0o600)
    original = wire_value(request)
    original["params"].pop("rewind", None)
    original["params"]["supports_user_interaction"] = False
    original["params"]["supports_permission_approval"] = False
    sequence = 0
    started = request.timestamp if request.timestamp > 0 else time.time()
    deadline = min(time.time(), started) + options.remaining_seconds
    journal = UsageJournal(state / "usage.jsonl", token_limit=options.token_limit, deadline=deadline,
                           accounting="uncached_usage")
    from jiuwenswarm.rewind.code_policy import INSTRUCTIONS, VERSION

    report = {"schema": SCHEMA, "state": "FAILED", "rewinds": 0, "attempts": [],
              "options": options.to_dict(), "config_hash": config_digest(config),
              "recovery_policy": VERSION, "recovery_instructions_hash": hashlib.sha256(
                  (INSTRUCTIONS if options.mode == "on" else "").encode()).hexdigest(),
              "token_accounting": "uncached_usage",
              "workspace": str(workspace), "ordinary_tools": list(WORKSPACE_TOOLS)}

    async def emit(payload, attempt, complete=False, role="assistant"):
        nonlocal sequence
        def correlate(value):
            if isinstance(value, dict):
                return {key: (f"a{attempt}:{item}" if key in {"tool_call_id", "call_id"} and isinstance(item, str)
                              else correlate(item)) for key, item in value.items()}
            if isinstance(value, list):
                return [correlate(item) for item in value]
            return value
        payload = correlate(copy.deepcopy(payload))
        sequence += 1
        payload.update(request_id=request.request_id, session_id=request.session_id,
                       rewind_attempt=attempt, output_order={"request_id": request.request_id, "sequence": sequence})
        if "output_phase_id" in payload:
            payload["output_phase_id"] = "rewind-" + str(request.request_id)
        if payload.get("event_type") == "chat.output_phase":
            payload["applied_input_ids"] = [request.request_id]
        await run_history_io(append_history_record, session_id=request.session_id,
                             request_id=request.request_id, channel_id=request.channel_id,
                             role=role, content=payload.get("content", ""), timestamp=time.time(),
                             event_type=payload.get("event_type"), mode=(request.params or {}).get("mode", "agent"),
                             extra={key: value for key, value in payload.items()
                                    if key not in {"event_type", "content", "timestamp", "session_id", "request_id"}})
        return AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                  payload=payload, is_complete=complete)

    # The outer facade is bypassed; it would otherwise own this history write.
    await emit({"content": original["params"].get("query", "")}, 0, role="user")
    recovery = None
    attempt = 0
    try:
        for attempt in range(options.max_rewinds + 1):
            job = {"schema": SCHEMA, "task_id": task_id, "attempt": attempt,
                   "run_id": f"{task_id}-a{attempt}", "mode": options.mode,
                   "options": options.to_dict(), "request": original,
                   "state_dir": str(state), "attempt_dir": str(state / f"attempt-{attempt}"),
                   "data_dir": str(state / "active-data"),
                   "config_file": str(config_file), "workspace": str(workspace),
                   "logical_timestamp": deadline - options.remaining_seconds, "deadline": deadline,
                   "max_output_tokens": 32768, "recovery": recovery}
            final = None
            result = None
            if attempt:
                yield await emit({"event_type": "chat.notice", "notice_type": "rewind",
                                  "content": f"Agent 正在进行第 {attempt} 次回退与重放。"}, attempt)
            from contextlib import aclosing

            async with aclosing(messages(job)) as source:
                async for packet in source:
                    if packet["kind"] == "result":
                        result = packet["value"]
                        continue
                    chunk = packet["value"]
                    payload = chunk.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    event = payload.get("event_type")
                    if event == "chat.final":
                        final = payload
                    elif not chunk.get("is_complete") and event not in {"chat.processing_status", "runtime.accepted"}:
                        yield await emit(payload, attempt)
            if result is None:
                raise RuntimeError("Missing attempt result")
            report["attempts"].append(result)
            atomic_json(state / f"attempt-{attempt}" / "result.json", result)
            if result["state"] == "COMPLETED":
                if final is None:
                    raise RuntimeError("Completed attempt has no final answer")
                report.update(state="COMPLETED", rewinds=attempt)
                yield await emit(final, attempt)
                yield await emit({"event_type": "chat.processing_status", "is_processing": False,
                                  "is_complete": True}, attempt, True)
                return
            if result["state"] != "CONTROL":
                from jiuwenswarm.rewind.model_retry import event_summary

                terminal = event_summary(state / "model-events.jsonl").get("last_terminal")
                if terminal:
                    report["failure_detail"] = terminal
                raise RuntimeError("Attempt failed; inspect its process.log")
            control = result["control"]
            if control["kind"] == "budget_exhausted":
                report["state"] = "BUDGET_EXHAUSTED"
                report["failure_detail"] = {"source": "budget", "reason": control["payload"].get("reason", "Task budget exhausted")}
                raise RuntimeError(report["failure_detail"]["reason"])
            if control["kind"] != "rewind" or options.mode != "on" or attempt >= options.max_rewinds:
                raise RuntimeError("Invalid rewind control or rewind limit reached")
            from jiuwenswarm.rewind.worker import prepare_recovery

            prepared = prepare_recovery(SimpleNamespace(**control), job, state)
            recovery = prepared["recovery"]
            report["rewinds"] = attempt + 1
    except (asyncio.CancelledError, GeneratorExit):
        report["state"] = "CANCELLED"
        raise
    except Exception as error:
        import traceback

        (state / "manager-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if isinstance(error, TimeoutError):
            report["state"] = "BUDGET_EXHAUSTED"
            report["failure_detail"] = {"source": "budget", "reason": "Task wall-time deadline exhausted"}
        report["error"] = type(error).__name__
        yield await emit({"event_type": "chat.error", "error": "Rewind task failed: " + type(error).__name__,
                          "rewind_state": report["state"],
                          **({"failure_detail": report["failure_detail"]} if "failure_detail" in report else {})}, attempt, True)
    finally:
        from jiuwenswarm.rewind.model_retry import event_summary

        report["usage"] = journal.totals()
        report["model_transport"] = event_summary(state / "model-events.jsonl")
        atomic_json(state / "report.json", report)
        config_file.unlink(missing_ok=True)
