# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One fresh worker process. The parent exclusively owns process restart."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.rewind.contracts import MEMORY_ITEM_CHAR_LIMIT, MEMORY_NOTEBOOK_CHAR_LIMIT, Manifest, SCHEMA, Task
from jiuwenswarm.rewind.storage import BudgetExhausted, UsageJournal, atomic_json


@dataclass
class AttemptContext:
    manifest: Manifest
    task: Task
    mode: str
    repository: Path
    data_dir: Path
    attempt_dir: Path
    logical_timestamp: float
    boundaries: Any


def recorder(job: dict, manifest: Manifest):
    if job["mode"] == "off":
        return nullcontext(None)
    if sys.platform != "linux" or sys.version_info < (3, 12):
        raise RuntimeError("Replay recording requires Linux and Python >= 3.12")
    import replay
    from replay.git_workspace import GitWorkspaceSpec

    state_dir = Path(job["state_dir"])
    spec = GitWorkspaceSpec(
        work_root=job["workspace"], store_dir=state_dir / "snapshot-store",
        initial="work_root", direct=True, allow_nested_git=True, allow_project_root=True,
        restore_process_policy="diagnose-only", keep_worktree=True, allow_symlinks=True,
    )
    common = {"log_dir": state_dir / "traces", "workspaces": {"workspace": spec},
              "backtrack_max_count": manifest.budget.max_rewinds,
              "backtrack_summary_enabled": False}
    recovery = job.get("recovery")
    if recovery is None:
        return replay.record(job["run_id"], overwrite=False, **common)
    override = json.loads(Path(recovery["override_input_file"]).read_text(encoding="utf-8"))
    metadata = json.loads(Path(recovery["backtrack_metadata_file"]).read_text(encoding="utf-8"))
    return replay.replay(base_run=recovery["base_run"],
                         breakpoint_record_uid=recovery["breakpoint_record_uid"],
                         override_input=override, fork_run=job["run_id"],
                         fork_metadata=metadata, fork_log_mode="materialized",
                         fork_reuse_policy="never", semantic_fallback=False, **common)


def version_evidence() -> dict:
    evidence = {"python": sys.version, "executable": sys.executable}
    for distribution in ("workswarm", "openjiuwen", "replay-agent-recorder", "openai"):
        try:
            evidence[distribution] = {"distribution_version": version(distribution)}
        except PackageNotFoundError:
            evidence[distribution] = {"distribution_version": None}
    for name in ("jiuwenswarm", "openjiuwen", "replay", "openjiuwen.core.execution.scope",
                 "openjiuwen.core.foundation.llm.model", "openjiuwen.core.single_agent.ability_manager",
                 "openjiuwen.harness.deep_agent", "openjiuwen.core.single_agent.agents.react_agent",
                 "jiuwenswarm.runtime.service", "jiuwenswarm.server.runtime.agent_adapter.interface_deep",
                  "jiuwenswarm.rewind.native", "jiuwenswarm.rewind.boundaries",
                  "jiuwenswarm.rewind.code_policy", "jiuwenswarm.rewind.model_retry", "replay.context"):
        module = sys.modules.get(name)
        if module is not None and getattr(module, "__file__", None):
            path = Path(module.__file__).resolve()
            evidence[name] = {**evidence.get(name, {}), "file": str(path),
                              "version": getattr(module, "__version__", None),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return evidence


def prepare_recovery(request: Any, job: dict, state_dir: Path) -> dict:
    from replay.backtrack import (
        BacktrackRequested, DEFAULT_MEMORY_PREFIX, is_backtrack_memory_message, load_backtrack_memory,
        prepare_backtrack_replay, render_backtrack_memory_notebook,
    )

    preparation = prepare_backtrack_replay(
        BacktrackRequested(**request.payload), current_run_id=job["run_id"],
        log_dir=state_dir / "traces", fork_run=f"{job['task_id']}-a{job['attempt'] + 1}",
        memory_policy="cumulative", context_policy="rollback", workspace_policy="rollback",
    )
    # Recorder's default 900-char item preview can discard the operational
    # strategy required by its own tool schema. Preserve accepted summaries;
    # show recent items first so the total prompt cap drops oldest text first.
    override_path = Path(preparation["override_input_file"])
    override = json.loads(override_path.read_text(encoding="utf-8"))
    notebook = load_backtrack_memory(state_dir / "traces", preparation["fork_run"])
    code_recovery = "options" in job
    item_limit, notebook_limit = MEMORY_ITEM_CHAR_LIMIT, MEMORY_NOTEBOOK_CHAR_LIMIT
    prefix = DEFAULT_MEMORY_PREFIX + (
        "\nEXECUTION STATE: THE REWIND HAS ALREADY COMPLETED. You are now re-deciding the selected "
        "LLM invocation after restoring its earlier file state. The verifier failure described below "
        "has already happened in the abandoned branch; do not intentionally recreate it. Execute the "
        "corrected_strategy and obey do_not_repeat below instead of repeating abandoned steps from "
        "the original task. Read-only prefix calls shown above have already been replayed.\n"
    )
    if code_recovery:
        from jiuwenswarm.rewind.code_policy import MEMORY_ITEM_LIMIT, MEMORY_NOTEBOOK_LIMIT

        item_limit, notebook_limit = MEMORY_ITEM_LIMIT, MEMORY_NOTEBOOK_LIMIT
        prefix = DEFAULT_MEMORY_PREFIX + (
            "\nRecovery is complete. These are fallible notes, not user requirements or binding bans. "
            "Separate observed facts from hypotheses, especially when a newly authored test failed. "
            "Recheck expectations, contracts and related consumers. New evidence may overturn any "
            "diagnosis or suggested strategy. Keep valid prior work and the full original task scope.\n"
        )
    text = render_backtrack_memory_notebook(
        list(reversed(notebook[-20:])), limit=None,
        memory_prefix=prefix, item_char_limit=item_limit, notebook_char_limit=notebook_limit,
    )
    for message in override["messages"]:
        if is_backtrack_memory_message(message):
            message["content"] = text
    from jiuwenswarm.rewind.memory import NOTICE_KEY, recovery_notice

    override["messages"] = [message for message in override["messages"]
                            if not (message.get("metadata") or {}).get(NOTICE_KEY)]
    context_messages = [message for message in override["messages"] if not is_backtrack_memory_message(message)]
    memory_messages = [message for message in override["messages"] if is_backtrack_memory_message(message)]
    notice = "" if code_recovery else text
    override["messages"] = context_messages + [recovery_notice(notice, position=len(context_messages))] + memory_messages
    atomic_json(override_path, override)
    for key in ("override_input_file", "backtrack_metadata_file"):
        path = Path(preparation[key])
        atomic_json(path, json.loads(path.read_text(encoding="utf-8")))
    return {"state": "REWIND_READY", "recovery": {
        key: str(value) if isinstance(value, Path) else value
        for key, value in preparation.items()
    }, "recovery_id": f"{job['task_id']}-a{job['attempt'] + 1}"}


async def run_scoped(driver, boundaries):
    """Join execution/control explicitly, including DeepAgent's background tasks."""
    from openjiuwen.core.execution import ExecutionControl, ExecutionScope

    with ExecutionScope(model_boundary=boundaries.model, tool_boundary=boundaries.tool,
                        buffer_stream=True, serial_tools=True) as scope:
        work = asyncio.create_task(driver(), name="rewind-attempt")
        control = asyncio.create_task(scope.wait_control(), name="rewind-control")
        try:
            await asyncio.wait((work, control), return_when=asyncio.FIRST_COMPLETED)
            request = scope.control
            if request is not None:
                work.cancel()
                await asyncio.wait_for(asyncio.gather(work, return_exceptions=True), timeout=15)
                boundaries.flush()
                return request, None
            output = await work
            boundaries.check_target_reached()
            boundaries.flush()
            return None, output
        except ExecutionControl:
            raise RuntimeError("Control exception arrived without mailbox handoff") from None
        finally:
            control.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, control, return_exceptions=True)


async def execute(job: dict) -> dict:
    manifest = Manifest.from_dict(job["manifest"])
    task = Task(**job["task"])
    state_dir, attempt_dir = Path(job["state_dir"]), Path(job["attempt_dir"])
    data_dir = Path(job.get("data_dir", attempt_dir / "data"))
    os.environ["JIUWENSWARM_DATA_DIR"] = str(data_dir)
    os.environ["MEMORY_ENGINE"] = "none"
    os.environ["OPENJIUWEN_RUNTIME_LOG_DIR"] = str(data_dir / "runtime-logs")
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise ValueError("Attempt session data must be fresh")
    # Core's default file logger uses ./logs. Configure it before importing
    # Runtime/Replay so runtime output cannot enter task snapshots or patches.
    from openjiuwen.core.common.logging.log_config import configure_log_config, get_log_config_snapshot

    logging_config = get_log_config_snapshot()
    logging_config["log_path"] = str(data_dir / "logs")
    configure_log_config(logging_config)
    # Imports stay inside the worker so parent/off CLI need no recorder SDK.
    from jiuwenswarm.rewind.boundaries import AttemptBoundaries

    journal = UsageJournal(state_dir / "usage.jsonl", token_limit=manifest.budget.tokens,
                           deadline=job["deadline"])
    if time.time() >= job["deadline"]:
        raise BudgetExhausted("Parent deadline expired")
    result: dict = {}
    with recorder(job, manifest) as session:
        boundaries = AttemptBoundaries(session, journal,
                                       max_output_tokens=manifest.budget.max_output_tokens,
                                       rate_limit_retries=manifest.rate_limit_retries,
                                       rewind_enabled=job["mode"] == "on",
                                       rewinds_left=manifest.budget.max_rewinds - job["attempt"],
                                       model_parameters={"temperature": manifest.temperature, "top_p": manifest.top_p,
                                                         **({"seed": manifest.seed} if manifest.seed is not None else {}),
                                                         **(({"reasoning": {"effort": manifest.reasoning_effort}}
                                                             if manifest.api_mode == "responses" else
                                                             {"reasoning_effort": manifest.reasoning_effort})
                                                            if manifest.reasoning_effort is not None else {}),
                                                         **({"send_max_output_tokens": True,
                                                             "send_sampling_params": True}
                                                            if manifest.api_mode == "responses" else {})})
        context = AttemptContext(manifest, task, job["mode"], Path(job["workspace"]) / "repo",
                                 data_dir, attempt_dir, job["logical_timestamp"], boundaries)
        module, function = manifest.driver.split(":", 1)
        driver = getattr(importlib.import_module(module), function)
        request, output = await run_scoped(lambda: driver(context), boundaries)
        if request is None:
            result = {"state": "COMPLETED", "output": output}
        elif request.kind == "budget_exhausted":
            result = {"state": "BUDGET_EXHAUSTED", "error": request.payload["reason"]}
        elif request.kind == "rewind":
            result = prepare_recovery(request, job, state_dir)
        else:
            raise RuntimeError(f"Unsupported execution control: {request.kind}")
    result["versions"] = version_evidence()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    if job.get("schema") != SCHEMA:
        raise ValueError("Unsupported worker job schema")
    result = {"schema": SCHEMA, "task_id": job["task_id"], "attempt": job["attempt"],
              "run_id": job["run_id"]}
    try:
        result.update(asyncio.run(execute(job)))
    except BudgetExhausted as error:
        result.update(state="BUDGET_EXHAUSTED", error=str(error))
    except KeyboardInterrupt:
        result.update(state="CANCELLED")
    except Exception as error:
        # Provider errors may carry credentials/requests. Details stay in local
        # process stderr; the structured report exposes only the exception type.
        import traceback

        traceback.print_exc()
        result.update(state="FAILED", error=type(error).__name__)
    result["versions"] = version_evidence()
    atomic_json(Path(job["attempt_dir"]) / "result.json", result)


if __name__ == "__main__":
    main()
