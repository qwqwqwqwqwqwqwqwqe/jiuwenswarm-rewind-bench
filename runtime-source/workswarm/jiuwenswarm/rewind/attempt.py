"""One in-container attempt; no Docker client, server restart, or workspace clone."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def send(fd: int, kind: str, value: dict) -> None:
    from jiuwenswarm.rewind.bench_config import SCHEMA, wire_value

    data = (json.dumps({"schema": SCHEMA, "kind": kind, "value": wire_value(value)},
                       ensure_ascii=False, allow_nan=False) + "\n").encode()
    while data:
        data = data[os.write(fd, data):]


async def agent_run(job: dict, boundaries, fd: int) -> dict:
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk, PermissionContext, ReqMethod
    from jiuwenswarm.runtime import AgentRuntime
    from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES
    from jiuwenswarm.rewind.native import recovery_tools
    from jiuwenswarm.server.runtime.agent_adapter.user_turn import user_turn_time_scope

    values = dict(job["request"])
    values["req_method"] = ReqMethod(values["req_method"])
    if isinstance(values.get("permission_context"), dict):
        values["permission_context"] = PermissionContext(**values["permission_context"])
    request = AgentRequest(**values)
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import get_config_file

    initial_config = get_config()
    print("BENCH_CONFIG", get_config_file(), "task_loop=", initial_config.get("react", {}).get("enable_task_loop"),
          "anomaly=", initial_config.get("execution_guard", {}).get("model_anomaly_detection_rail", {}).get("enabled"),
          flush=True)
    tools = recovery_tools(boundaries) if job["mode"] == "on" else ()

    async def ready(agent):
        await agent.configure_rewind_run(host_tools=tools)

    runtime = AgentRuntime()
    completed = False
    try:
        await runtime.start()
        await runtime.create_or_resume_session(channel_id=request.channel_id,
                                              session_id=request.session_id, request=request)
        with user_turn_time_scope(job["logical_timestamp"]):
            async for event in runtime.stream(request, on_agent_ready=ready):
                if not event.ok or event.event_type in TERMINAL_ERROR_EVENT_TYPES:
                    raise RuntimeError("Agent emitted an error; see private attempt log")
                if event.event_type in {"chat.ask_user", "chat.ask_user_question", "chat.permission_request"}:
                    raise RuntimeError("Interactive requests are unsupported in benchmark attempts")
                chunk = AgentResponseChunk(request_id=event.request_id, channel_id=event.channel_id,
                                          payload=event.payload, metadata=event.metadata or {},
                                          is_complete=event.is_complete)
                send(fd, "chunk", asdict(chunk))
                completed = completed or event.is_complete
    finally:
        await runtime.close()
    if not completed:
        raise RuntimeError("Agent stream ended without completion")
    return {"runtime_completed": True}


async def execute(job: dict, fd: int) -> dict:
    from openjiuwen.core.common.logging.log_config import configure_log_config, get_log_config_snapshot
    from jiuwenswarm.rewind.boundaries import AttemptBoundaries
    from jiuwenswarm.rewind.storage import UsageJournal
    from jiuwenswarm.rewind.worker import recorder, run_scoped

    logging = get_log_config_snapshot()
    logging["log_path"] = str(Path(job["data_dir"]) / "logs")
    configure_log_config(logging)
    journal = UsageJournal(Path(job["state_dir"]) / "usage.jsonl",
                           token_limit=job["options"]["token_limit"], deadline=job["deadline"],
                           accounting="uncached_usage", run_id=job["run_id"])
    from jiuwenswarm.rewind.bench_config import BenchOptions

    retry_policy = BenchOptions(**job["options"]).retry_policy()
    manifest = SimpleNamespace(budget=SimpleNamespace(max_rewinds=job["options"]["max_rewinds"]))
    instructions = ""
    if job["mode"] == "on":
        from jiuwenswarm.rewind.code_policy import INSTRUCTIONS

        instructions = INSTRUCTIONS
    with recorder(job, manifest) as session:
        boundaries = AttemptBoundaries(session, journal, max_output_tokens=job["max_output_tokens"],
                                       rewind_enabled=job["mode"] == "on",
                                       rewinds_left=job["options"]["max_rewinds"] - job["attempt"],
                                        model_instructions=instructions, preserve_output_limit=True,
                                        code_recovery=True, retry_policy=retry_policy)
        control, output = await run_scoped(lambda: agent_run(job, boundaries, fd), boundaries)
        if control is not None:
            return {"state": "CONTROL", "control": {"kind": control.kind, "payload": control.payload}}
    return {"state": "COMPLETED", "output": output}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--event-fd", type=int, required=True)
    args = parser.parse_args()
    os.set_inheritable(args.event_fd, False)
    job = json.loads(args.job.read_text(encoding="utf-8"))
    from jiuwenswarm.rewind.bench_config import SCHEMA

    if job.get("schema") != SCHEMA:
        raise ValueError("Unsupported attempt schema")
    # These must be set before importing common.utils/config or the Agent factory.
    data = Path(job["data_dir"])
    if data.exists():
        raise ValueError("Attempt data must be fresh")
    (data / "config").mkdir(parents=True)
    import shutil

    shutil.copyfile(job["config_file"], data / "config" / "config.yaml")
    os.chmod(data / "config" / "config.yaml", 0o600)
    os.environ.update(JIUWENSWARM_DATA_DIR=str(data), MEMORY_ENGINE="none",
                      JIUWENSWARM_CONFIG_DIR=str(data / "config"),
                      OPENJIUWEN_RUNTIME_LOG_DIR=str(data / "runtime-logs"))
    os.environ.pop("JIUWENSWARM_REWIND_WEB_MANIFEST", None)
    try:
        result = asyncio.run(execute(job, args.event_fd))
    except Exception as error:
        import traceback

        traceback.print_exc()
        result = {"state": "FAILED", "error": type(error).__name__}
    from jiuwenswarm.rewind.worker import version_evidence

    result["versions"] = version_evidence()
    send(args.event_fd, "result", result)
    # Parent discovers and stops detached descendants before releasing this root.
    sys.stdin.buffer.readline()
    os.close(args.event_fd)


if __name__ == "__main__":
    main()
