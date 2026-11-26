"""Repair request message sequences against orphaned assistant tool calls.

The real paired Django-14034 v8.1 run crashed at ReAct iteration 62: after the
context compressor rebuilt the history, one assistant message kept its three
tool_calls while the recorded tool responses were absent from the rebuilt
list, and the provider rejected the request with HTTP 400 ("An assistant
message with 'tool_calls' must be followed by tool messages"). The recorder
retains the real responses, so this repair never hides evidence; it keeps the
protocol valid and tells the model the outcome is unknown so it re-runs any
check whose result matters.
"""
from __future__ import annotations

PLACEHOLDER_NOTICE = (
    "[Tool response unavailable: this response was lost by the context pipeline "
    "before the request was sent; the call itself is recorded in the trace. Do "
    "not assume it succeeded; re-run the check if its outcome matters.]"
)


def replay_prefix_llm_uids(fork_records: list) -> list:
    """Record uids of the replayed LLM boundaries inside a fork's own trace.

    A fork replays the source records BEFORE the chosen target; the target
    itself is regenerated as the first live request (``matched_by`` set, e.g.
    ``materialized_prefix...`` or ``post_fork_live``), and every request after
    it is live. Replayed records carry no ``matched_by``. So the replay segment
    is every llm record before the first one that has ``matched_by``; if no
    record has one, the whole run is a replayed prefix.
    """
    uids: list = []
    for record in fork_records:
        if record.get("kind") != "llm" or not record.get("record_uid"):
            continue
        if (record.get("metadata") or {}).get("matched_by"):
            break
        uids.append(record["record_uid"])
    return uids


def filter_replay_candidates(candidates: list, prefix_uids: list) -> list:
    """Drop replay-segment records from a backtrack candidate list.

    Replay-segment records are copied common history, not decisions this arm
    made; rewinding there silently discards all repair-phase work. The
    regenerated target request right after the prefix stays selectable as the
    full-restart boundary because it is live.
    """
    blocked = set(prefix_uids)
    return [item for item in candidates
            if not (isinstance(item, dict) and item.get("record_uid") in blocked)]


def repair_orphan_tool_calls(messages: list) -> list:
    """Return a new list where every assistant tool_call has a tool response.

    For each assistant message carrying tool_calls, consumes the consecutive
    tool responses that follow it and appends explicit placeholders for any
    tool_call_id without a response. Everything else passes through in order.
    """
    repaired: list = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        repaired.append(message)
        if message.get("role") == "assistant" and message.get("tool_calls"):
            responded: set = set()
            scan = index + 1
            while scan < total and messages[scan].get("role") == "tool":
                responded.add(messages[scan].get("tool_call_id"))
                repaired.append(messages[scan])
                scan += 1
            for call in message["tool_calls"]:
                call_id = call.get("id") if isinstance(call, dict) else None
                if call_id is not None and call_id not in responded:
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": PLACEHOLDER_NOTICE,
                    })
            index = scan
        else:
            index += 1
    return repaired
