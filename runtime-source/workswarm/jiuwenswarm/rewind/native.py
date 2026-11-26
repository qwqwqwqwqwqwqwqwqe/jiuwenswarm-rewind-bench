# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native driver using the existing Process CLI / AgentRuntime execution chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_TARGET_INSTRUCTIONS = (
    "In this integration a rewind target is the LLM invocation whose input will be reused and whose "
    "response will be generated again. Its old response is discarded. Select the earliest decision "
    "you need to replace, including the decision that emitted the bad tool call; earlier read-only "
    "inspection decisions remain a replayed prefix. Do not move to an earlier predecessor merely "
    "because the selected invocation itself made the bad decision. Only returned top-level candidate "
    "record_uid values are valid targets."
)


def prepare_config(data_dir: Path, *, api_mode: str = "chat_completions") -> None:
    """Create fresh attempt-local config before importing the Runtime.

    Model credentials continue to use the resource template's environment
    substitutions. No home configuration or failed-branch session is copied.
    """
    import yaml
    import jiuwenswarm

    source = Path(jiuwenswarm.__file__).parent / "resources" / "config.yaml"
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    for entry in config["models"]["defaults"]:
        entry["model_client_config"]["api_mode"] = api_mode
        entry["model_client_config"]["supports_vision"] = False
    # Code-only tasks have no image inputs. A background capability probe can
    # race the decision stream and time out without leaving a replay record.
    config["execution_guard"]["model_anomaly_detection_rail"]["enabled"] = False
    config["memory"]["engine"] = "none"
    config["progressive_tool_enabled"] = False
    config["task_memory"]["enabled"] = False
    config["proactive_recommendation"]["enabled"] = False
    react = config["react"]
    react["enable_read_image_multimodal"] = False
    react["enable_task_loop"] = False
    react["evolution"]["skill_evolution"] = False
    react["ttse"]["enabled"] = False
    for mode in ("agent", "code"):
        config["modes"][mode]["memory"]["enabled"] = False
        config["modes"][mode]["rails"] = []
        config["modes"][mode]["tools"] = []
    # No hidden provider retry may reset or bypass the attempt budget.
    def disable_retries(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "max_retries":
                    value[key] = 0
                else:
                    disable_retries(item)
        elif isinstance(value, list):
            for item in value:
                disable_retries(item)
    disable_retries(config)
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=False)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")


def recovery_tools(boundaries: Any) -> tuple[Any, ...]:
    from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
    from openjiuwen.core.foundation.tool.schema import ToolOutput
    from replay.backtrack import backtrack_tool_definitions
    from jiuwenswarm.rewind.contracts import MEMORY_ITEM_CHAR_LIMIT

    class RecoveryTool(Tool):
        def __init__(self, definition: dict):
            card = ToolCard(name=definition["name"], description=definition["description"],
                            input_params=definition["parameters"], parallel_safe=False)
            card.exposure = ToolExposure.DIRECT
            card.set_exposure_declared(True)
            super().__init__(card)

        async def invoke(self, inputs: dict, **kwargs: Any) -> Any:
            if self.card.name == "backtrack_candidates":
                result = boundaries.candidates(**inputs)
                if boundaries.code_recovery:
                    result = {"candidates": result, "remaining_rewinds": boundaries.rewinds_left,
                              "recent_tools": [{**item, "arguments": json.dumps(item["arguments"], ensure_ascii=False)[:240]}
                                               for item in boundaries.observations[-12:]],
                              "guidance": "Query is advisory. Check uncertain tests before commit; no rewinds means continue solving."}
                return ToolOutput(success=True, data={"content": json.dumps(result, ensure_ascii=False)})
            result = boundaries.commit(**inputs)
            if result is not None:
                return ToolOutput(success=True, data={"content": json.dumps(result, ensure_ascii=False)})

        async def stream(self, inputs: dict, **kwargs: Any):
            yield await self.invoke(inputs, **kwargs)

    if boundaries.code_recovery:
        from jiuwenswarm.rewind.code_policy import tool_definitions

        return tuple(RecoveryTool(definition) for definition in tool_definitions())
    definitions = backtrack_tool_definitions()
    for definition in definitions:
        if definition["name"] == "backtrack_commit":
            definition["description"] = "Commit a rewind; this transfers control to the host and does not return. " + _TARGET_INSTRUCTIONS
            summary = definition["parameters"]["properties"]["memory_summary"]
            summary["maxLength"] = MEMORY_ITEM_CHAR_LIMIT
            summary["description"] = "Put corrected_strategy and do_not_repeat first. Keep this operational failure memory within 8000 characters. " + summary["description"]
    return tuple(RecoveryTool(definition) for definition in definitions)


async def run(context: Any) -> dict:
    """Reconstruct input time without freezing Runtime event/usage clocks."""
    prepare_config(context.data_dir, api_mode=context.manifest.api_mode)
    from jiuwenswarm.server.runtime.agent_adapter.user_turn import user_turn_time_scope

    with user_turn_time_scope(context.logical_timestamp):
        return await _run(context)


async def _run(context: Any) -> dict:
    """Drive a single root code Agent; completion is execution, not benchmark success."""
    from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
    from jiuwenswarm.common.schema.agent import AgentRequest, ReqMethod
    from jiuwenswarm.runtime.agent_definition import RuntimeAgentDefinition
    from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES

    tools = recovery_tools(context.boundaries) if context.mode == "on" else ()
    instructions = context.manifest.instructions
    if tools:
        from replay.backtrack import BACKTRACK_TOOL_INSTRUCTIONS

        instructions += "\n\n" + BACKTRACK_TOOL_INSTRUCTIONS + "\n\n" + _TARGET_INSTRUCTIONS
    definition = RuntimeAgentDefinition(
        name="rewind-coding-agent", instructions=instructions,
        model=context.manifest.model,
        tools=tuple(context.manifest.tools) + tuple(tool.card.name for tool in tools),
        max_iterations=context.manifest.max_iterations,
    )
    request = AgentRequest(
        request_id=f"request-{context.task.instance_id}", channel_id="process_cli",
        session_id=f"rewind-{context.task.instance_id}", req_method=ReqMethod.CHAT_SEND,
        is_stream=True, timestamp=context.logical_timestamp,
        params={"query": context.task.prompt, "content": context.task.prompt,
                "mode": "agent", "work_mode": "code", "model_name": context.manifest.model,
                "cwd": str(context.repository), "project_dir": str(context.repository),
                "trusted_dirs": [str(context.repository)],
                "supports_user_interaction": False, "supports_permission_approval": False},
    )

    async def ready(agent: Any) -> None:
        await agent.configure_process_cli_run(max_turns=context.manifest.max_iterations, host_tools=tools)

    client = InProcessRuntimeClient()
    completed = False
    final = ""
    try:
        await client.start()
        session_id = await client.create_or_resume_session(
            channel_id="process_cli", session_id=request.session_id,
        )
        if session_id != request.session_id:
            raise RuntimeError("Runtime did not preserve the task's stable session identity")
        with (context.attempt_dir / "events.jsonl").open("w", encoding="utf-8") as stream:
            async for event in client.stream_agent(request, definition, on_agent_ready=ready):
                stream.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
                stream.flush()
                if not event.ok or event.event_type in TERMINAL_ERROR_EVENT_TYPES:
                    raise RuntimeError("AgentRuntime emitted a failed event; see attempt events")
                if event.event_type == "chat.final":
                    final = str((event.payload or {}).get("content", ""))
                if event.is_complete:
                    completed = True
    finally:
        await client.close()
    if not completed:
        raise RuntimeError("AgentRuntime stream ended without completion")
    return {"final_response": final, "runtime_completed": True}
