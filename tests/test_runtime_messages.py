"""Unit tests for request-message repair against orphaned tool calls."""
import pytest

from bench_tools.jiuwenswarm_runtime.runtime_messages import repair_orphan_tool_calls


def assistant(*call_ids):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "x", "arguments": "{}"}} for cid in call_ids]}


def tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestRepairOrphanToolCalls:
    def test_keeps_paired_sequence_unchanged(self):
        messages = [
            {"role": "user", "content": "task"},
            assistant("a", "b"),
            tool("a"), tool("b"),
            {"role": "user", "content": "next"},
        ]
        assert repair_orphan_tool_calls(messages) == messages

    def test_inserts_placeholders_for_missing_responses(self):
        messages = [
            {"role": "user", "content": "task"},
            assistant("a", "b"),
            tool("a"),
            {"role": "user", "content": "next"},
        ]
        repaired = repair_orphan_tool_calls(messages)
        roles = [m["role"] for m in repaired]
        assert roles == ["user", "assistant", "tool", "tool", "user"]
        placeholder = repaired[3]
        assert placeholder["tool_call_id"] == "b"
        assert "unavailable" in placeholder["content"]

    def test_survives_the_real_400_shape(self):
        # The crashed v8.1 request: compacted history ends with the new
        # assistant carrying three tool_calls and zero responses.
        messages = [
            {"role": "system", "content": "id"},
            {"role": "user", "content": "task"},
            assistant("c1", "c2", "c3"),
        ]
        repaired = repair_orphan_tool_calls(messages)
        assert [m["role"] for m in repaired] == ["system", "user", "assistant", "tool", "tool", "tool"]
        assert [m["tool_call_id"] for m in repaired[3:]] == ["c1", "c2", "c3"]

    def test_repairs_each_assistant_independently(self):
        messages = [
            assistant("a"),
            tool("a"),
            assistant("b", "c"),
            tool("c"),
            {"role": "user", "content": "end"},
        ]
        repaired = repair_orphan_tool_calls(messages)
        assert [m["role"] for m in repaired] == [
            "assistant", "tool", "assistant", "tool", "tool", "user"]
        assert repaired[4]["tool_call_id"] == "b"

    def test_placeholder_warns_not_to_assume_success(self):
        messages = [assistant("gone")]
        placeholder = repair_orphan_tool_calls(messages)[1]
        assert "re-run" in placeholder["content"] or "rerun" in placeholder["content"]

    def test_no_tool_calls_means_no_change(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "again"},
        ]
        assert repair_orphan_tool_calls(messages) == messages

    def test_consecutive_tool_groups(self):
        messages = [
            assistant("a", "b"),
            tool("a"), tool("b"),
            assistant("c"),
            tool("c"),
            {"role": "user", "content": "done"},
        ]
        assert repair_orphan_tool_calls(messages) == messages
