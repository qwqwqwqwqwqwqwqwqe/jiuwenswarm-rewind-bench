# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in Web transport for isolated, single-turn recoverable coding tasks."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any


def enabled(request: Any) -> bool:
    from jiuwenswarm.common.schema.agent import ReqMethod
    from gateway.common.mode_matrix import deprecate_mode, is_single_agent_mode

    return bool(os.environ.get("JIUWENSWARM_REWIND_WEB_MANIFEST")) and (
        request.channel_id == "web" and request.req_method == ReqMethod.CHAT_SEND
        and is_single_agent_mode(deprecate_mode((request.params or {}).get("mode", "agent")))
        and not str((request.params or {}).get("mode", "agent")).endswith(".plan")
    )


async def stream(request: Any):
    """Keep the Web Runtime alive while the supervisor replaces task containers."""
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.common.utils import get_user_workspace_dir
    from jiuwenswarm.rewind.bench import run_task, validate_repositories
    from jiuwenswarm.rewind.contracts import Manifest, Task
    from jiuwenswarm.rewind.storage import atomic_json
    from jiuwenswarm.server.runtime.session.history_io import run_history_io
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    params = request.params or {}
    files = params.get("files") or {}
    if any(files.values()) or params.get("media_items"):
        raise ValueError("Recoverable Web tasks currently accept text and the configured repository only")
    template = Manifest.load(Path(os.environ["JIUWENSWARM_REWIND_WEB_MANIFEST"]).resolve())
    if len(template.tasks) != 1:
        raise ValueError("Web rewind configuration requires exactly one repository template")
    prompt = str(params.get("query") or params.get("content") or "").strip()
    if not prompt:
        raise ValueError("A task message is required")
    task_id = "web-" + uuid.uuid4().hex
    source = template.tasks[0]
    task = Task(task_id, source.repository, source.base_commit, prompt)
    manifest = replace(template, tasks=(task,))
    # Validate before launching a background thread or writing user history.
    await asyncio.to_thread(validate_repositories, manifest)
    output = get_user_workspace_dir() / "rewind" / task_id
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "manifest.json", manifest.to_dict())
    state = output / task_id / "state"
    cancelled = threading.Event()
    offsets: dict[Path, int] = {}
    announced: set[int] = set()
    final_payload = None
    output_sequence = 0
    visible_phase = f"rewind-{request.request_id}"

    async def record(payload: dict, *, role: str = "assistant"):
        nonlocal output_sequence
        if role == "assistant":
            # A process restart remains inside the same visible user turn.
            # Frontend rejects output from a phase other than its current one.
            if "output_phase_id" in payload:
                payload["output_phase_id"] = visible_phase
            if "execution_id" in payload:
                payload["execution_id"] = request.request_id
            if "output_order" in payload:
                output_sequence += 1
                payload["output_order"] = {"request_id": request.request_id, "sequence": output_sequence}
        extra = {k: v for k, v in payload.items() if k not in {"event_type", "content", "request_id", "session_id", "timestamp"}}
        await run_history_io(append_history_record, session_id=request.session_id,
                            request_id=request.request_id, channel_id=request.channel_id,
                            role=role, content=payload.get("content", ""), timestamp=time.time(),
                            event_type=payload.get("event_type"), extra=extra, mode=params.get("mode", "agent"))

    def chunk(payload: dict | None, complete: bool = False):
        if payload is not None:
            payload = {**payload, "session_id": request.session_id, "request_id": request.request_id}
        return AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                  payload=payload, is_complete=complete)

    await record({"content": prompt}, role="user")
    notice = {"event_type": "chat.notice", "notice_type": "rewind-start",
              "content": "已启动可回退代码任务。本次消息独立执行，代码改动保存在隔离工作区。"}
    await record(notice)
    yield chunk(notice)
    yield chunk({"event_type": "runtime.accepted"})
    yield chunk({"event_type": "chat.processing_status", "is_processing": True})
    queue_path = os.environ.get("JIUWENSWARM_REWIND_WEB_QUEUE")
    if queue_path:
        from jiuwenswarm.rewind.web_queue import submit

        operation = asyncio.to_thread(submit, Path(queue_path), task_id, prompt, cancelled=cancelled,
                                      seconds=manifest.budget.seconds)
    else:
        operation = asyncio.to_thread(run_task, manifest, task, mode="on", output=output,
                                      executor="docker", cancel_event=cancelled)
    work = asyncio.create_task(operation)
    try:
        while True:
            finished = work.done()
            for path in sorted(state.glob("attempt-*/events.jsonl"), key=lambda p: int(p.parent.name.split("-")[-1])):
                attempt = int(path.parent.name.split("-")[-1])
                if attempt not in announced:
                    announced.add(attempt)
                    if attempt:
                        notice = {"event_type": "chat.notice", "notice_type": f"rewind-{attempt}",
                                  "content": f"Agent 已发起第 {attempt} 次回退，正在恢复历史并重新执行。"}
                        await record(notice)
                        yield chunk(notice)
                with path.open("rb") as log:
                    log.seek(offsets.get(path, 0))
                    while True:
                        start = log.tell()
                        line = log.readline()
                        if not line or not line.endswith(b"\n"):
                            log.seek(start)
                            break
                        event = json.loads(line)
                        payload = event.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        # Historical tool IDs repeat across attempts. Give UI cards
                        # distinct identities without changing recorded agent input.
                        def correlate(value):
                            if isinstance(value, dict):
                                return {k: (f"a{attempt}:{v}" if k in {"tool_call_id", "call_id"} and isinstance(v, str)
                                            else correlate(v)) for k, v in value.items()}
                            if isinstance(value, list):
                                return [correlate(v) for v in value]
                            return value
                        payload = correlate(payload)
                        if payload.get("event_type") == "chat.output_phase":
                            payload["applied_input_ids"] = [request.request_id]
                        if payload.get("event_type") == "chat.final":
                            final_payload = payload
                            continue
                        if payload.get("event_type") in {"chat.processing_status", "runtime.accepted"}:
                            continue
                        await record(payload)
                        yield chunk(payload)
                    offsets[path] = log.tell()
            if finished:
                report = await work
                if report["state"] != "COMPLETED":
                    error = {"event_type": "chat.error", "error": f"恢复任务结束：{report['state']}；详情保存在 {output}"}
                    await record(error)
                    yield chunk(error, True)
                else:
                    if final_payload is not None:
                        await record(final_payload)
                        yield chunk(final_payload)
                    notice = {"event_type": "chat.notice", "notice_type": "rewind-result",
                              "content": f"任务执行结束，回退 {report['rewinds']} 次。工作区：{output / task_id / 'workspace/repo'}"}
                    await record(notice)
                    yield chunk(notice)
                    yield chunk({"event_type": "chat.processing_status", "is_processing": False,
                                 "is_complete": True}, True)
                return
            await asyncio.sleep(0.2)
    finally:
        cancelled.set()
        if not work.done():
            await asyncio.wait_for(asyncio.shield(work), timeout=30)
