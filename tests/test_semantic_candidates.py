import asyncio
import importlib.util
import json
import sys

from bench_tools.jiuwenswarm_runtime.semantic_candidates import SemanticCandidates, before_query, comparison_metrics


def llm(number, tools=()):
    return {"kind": "llm", "record_uid": f"rec_{number:06}", "output": {
        "content": f"decision {number}", "tool_calls": [{"name": name} for name in tools],
    }}


def tool(number, *, name="edit", status="M"):
    return {"kind": "tool", "input": {"tool_name": name, "arguments": {"file": "sample"}},
            "output": {"value": "done"}, "metadata": {
                "filesystem": {"workspaces": {"workspace": {
                    "before_tree": str(number - 1), "after_tree": str(number),
                    "diff_summary": [{"status": status, "path": "sample"}],
                }}},
            }}


def write(path, *records):
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def response(decision="same_goal"):
    return json.dumps({"decision": decision, "goal": "migrate test data",
                       "progress": "updated sample", "reason": "same artifacts"}), {"total_tokens": 25}


def selected(index, count):
    candidates = [{"record_uid": llm(number)["record_uid"]} for number in range(1, count + 1)]
    return [int(row["record_uid"].split("_")[1]) for row in index.select(candidates)]


def test_groups_rounds_and_keeps_start_latest_and_delete(tmp_path):
    async def run():
        calls = []

        async def judge(payload):
            calls.append(payload)
            return response("new_goal" if len(calls) == 4 else "same_goal")

        trace = tmp_path / "run.jsonl"
        index = SemanticCandidates(trace, tmp_path, judge)
        write(trace, llm(1), tool(1), tool(2), llm(2), tool(3, status="D"),
              llm(3), tool(4), llm(4), tool(5), *(llm(n) for n in range(5, 105)))
        index.observe()
        await index.wait_for_query()
        assert len(calls) == 4
        assert len(calls[0]["current_round"]["tools"]) == 2
        assert calls[1]["active_segment"]["goal"] == "migrate test data"
        assert selected(index, 104) == [1, 2, 3, 4, *range(97, 105)]
        await index.close()

    asyncio.run(run())


def test_query_does_not_wait_for_unfinished_batch_or_flush_active_tool(tmp_path):
    async def run():
        active = asyncio.Event()

        async def judge(payload):
            active.set()
            await asyncio.Event().wait()

        trace = tmp_path / "run.jsonl"
        index = SemanticCandidates(trace, tmp_path, judge, wait=0.01)
        boundaries = type("Boundaries", (), {"semantic_candidates": index})()
        write(trace, llm(1, ("edit", "edit", "backtrack_candidates")), tool(1))
        await before_query(boundaries)
        assert not active.is_set()
        write(trace, tool(2))
        await before_query(boundaries)
        assert active.is_set()
        assert selected(index, 100) == list(range(21, 101))
        await index.close()

    asyncio.run(run())


def test_invalid_response_keeps_point_and_prefix_cache_reuses_only_active_path(tmp_path):
    async def run():
        async def judge(payload):
            if payload["current_round"]["decision"] == "decision 2":
                return "invalid", {"total_tokens": 5}
            return response()

        trace = tmp_path / "initial.jsonl"
        prefix = (llm(1), tool(1), llm(2), tool(2))
        write(trace, *prefix, llm(3), tool(3), *(llm(n) for n in range(4, 100)))
        index = SemanticCandidates(trace, tmp_path, judge)
        index.observe()
        await index.wait_for_query()
        assert 2 in selected(index, 99)
        await index.close()

        async def unexpected(payload):
            raise AssertionError("prefix should be cached")

        branch_trace = tmp_path / "fork.jsonl"
        write(branch_trace, *prefix, *(llm(n) for n in range(4, 100)))
        branch = SemanticCandidates(branch_trace, tmp_path, unexpected)
        branch.observe()
        await branch.wait_for_query()
        assert branch.successes == 1
        assert [segment["start"] for segment in branch.segments] == [
            llm(1)["record_uid"], llm(2)["record_uid"],
        ]
        assert 3 not in selected(branch, 99)
        await branch.close()

    asyncio.run(run())


def test_budget_leaves_room_for_older_protected_points(tmp_path):
    async def run():
        async def judge(payload):
            return response("new_goal")

        trace = tmp_path / "run.jsonl"
        index = SemanticCandidates(trace, tmp_path, judge)
        rows = [llm(1), tool(1, status="D")]
        for number in range(2, 100):
            rows.extend((llm(number), tool(number)))
        write(trace, *rows, llm(100))
        index.observe()
        await index.wait_for_query()
        assert selected(index, 100) == [1, *range(22, 101)]
        await index.close()

    asyncio.run(run())


def test_comparison_accepts_a_more_precise_target_than_the_historical_one(tmp_path):
    recovery = tmp_path / "ipc" / "recovery"
    recovery.mkdir(parents=True)
    write(recovery / "semantic-candidate-events.jsonl",
          {"event": "query_result", "record_uids": ["rec_000041", "rec_000055"]},
          {"event": "judge_started"},
          {"event": "judge_completed", "usage": {"total_tokens": 20}})
    result = {"attempts": [{"control": {"kind": "rewind", "payload": {"record_uid": "rec_000055"}}}]}
    metrics = comparison_metrics(tmp_path, result, ("rec_000041", "rec_000055"))
    assert metrics["expected_visible_on_first_query"]
    assert not metrics["expected_selected"]
    assert metrics["acceptable_selected"]
    assert metrics["judge_total_tokens_reported"] == 20


def test_protected_target_restores_real_git_snapshot(tmp_path):
    if sys.platform != "linux" or any(importlib.util.find_spec(name) is None for name in ("openai", "replay")):
        import pytest

        pytest.skip("Requires the Linux recorder runtime")
    from openai.types.chat import ChatCompletion
    from replay import GitWorkspaceSpec, record, replay
    from replay.backtrack import deterministic_candidates
    from replay.storage import load_raw_records

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        original = workspace / "original.txt"
        original.write_text("unique bytes")
        traces = tmp_path / "traces"
        spec = GitWorkspaceSpec(work_root=workspace, store_dir=tmp_path / "snapshots",
                                initial="work_root", direct=True, allow_project_root=True,
                                restore_process_policy="diagnose-only")
        args = {"log_dir": traces, "workspaces": {"workspace": spec}}
        inputs = {"messages": [{"role": "user", "content": "preserve original bytes"}]}
        replacement = {"messages": [*inputs["messages"], {"role": "system", "content": "do not delete"}]}
        bare_response = {"id": "model", "created": 1, "model": "fixture", "object": "chat.completion"}

        def completion(content):
            return ChatCompletion.model_validate({**bare_response, "choices": [{
                "index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content},
            }]})

        def delete():
            original.unlink()
            return {"ok": True}

        async def judge(payload):
            return response()

        with record("base", **args) as session:
            await session.handle_async_llm_boundary(name="fixture", record_kwargs=inputs,
                                                    invoke=lambda: completion("delete the original"))
            await session.handle_async_tool_boundary(tool_name="delete", input_record={"name": "delete"},
                                                     invoke=delete)
        assert not original.exists()
        trace = traces / "base.jsonl"
        index = SemanticCandidates(trace, tmp_path, judge)
        index.observe(seal=True)
        await index.wait_for_query()
        candidate, = index.select(deterministic_candidates(load_raw_records(trace)))
        uid = candidate["record_uid"]
        assert index.writes[uid]

        async def regenerate(effective):
            assert effective == replacement
            assert original.read_text() == "unique bytes"
            return completion("keep the original")

        with replay(base_run="base", breakpoint_record_uid=uid, override_input=replacement,
                    fork_run="fork", fork_log_mode="materialized", **args) as session:
            result = await session.handle_async_llm_boundary(name="fixture", record_kwargs=inputs,
                                                             invoke=regenerate)
            assert result.choices[0].message.content == "keep the original"
        await index.close()

    asyncio.run(run())
