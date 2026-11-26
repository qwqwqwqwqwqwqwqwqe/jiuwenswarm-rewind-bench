# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Evidence-aware recovery policy for the single-agent code benchmark profile."""

from __future__ import annotations

VERSION = "code-recovery-v2"
MEMORY_ITEM_LIMIT = 2_000
MEMORY_NOTEBOOK_LIMIT = 6_000

INSTRUCTIONS = """Recovery is optional. Prefer finishing the user's code task over rewinding.
A failed command is an observation, not proof that the production patch is wrong.
Distinguish a repository regression, a newly authored test, an environment/setup failure,
and an unverified interpretation. For a new or uncertain test, independently check its
fixture, units, expected values, and assumptions after the failure before abandoning code.
Inspect existing contracts and other consumers of a shared helper; a narrow reproduction
does not justify narrowing the required fix or silently preserving another broken consumer.
Repeated failures alone do not require a rewind. Repair a wrong test or environment in place.
Use rewind when concrete evidence contradicts the current strategy and replacing an earlier
decision is more useful than a local repair. Querying candidates is advisory: you may inspect,
test, repair, decline to rewind, or finish afterward. Never commit just because you queried.
The target is the LLM invocation whose RESPONSE must change; that response and its later
effects are discarded. Choose the latest returned decision that removes the evidenced mistake,
including the invocation that produced the bad edit. Retain useful inspection and valid work.
Before commit, supply review with evidence_origin, failure_tool_call_id,
independent_check_tool_call_id, alternative_explanation, and why_rewind. Use actual ordinary
tool call IDs from this trajectory. For agent_authored_test or unknown evidence, the independent
check must be a different read/search/command performed AFTER the failure. Evidence summaries
must explain the check; merely repeating the same test is not independent validation.
Environment errors and inference alone do not justify rollback. If a review is rejected or no
rewinds remain, continue normal problem solving within the remaining token/time budget.
Keep memory concise: observed facts and tool IDs; uncertain hypotheses; competing explanations;
what the next attempt should check and try. Do not turn guesses into permanent prohibitions.
The restored agent may reject a memory hypothesis when new evidence contradicts it. Preserve
the user's full acceptance criteria and check related callers; passing your own test is not
proof that the task is complete. Never seek hidden evaluation answers or reference patches.
"""


def tool_definitions() -> list[dict]:
    """Avoid importing the upstream verifier-specific prompt through tool descriptions."""
    review = {
        "type": "object",
        "properties": {
            "evidence_origin": {"type": "string", "enum": [
                "repository_test", "task_reproduction", "agent_authored_test", "environment", "unknown", "inference",
            ]},
            "failure_tool_call_id": {"type": "string", "description": "Ordinary tool observation supporting failure."},
            "independent_check_tool_call_id": {"type": "string", "description": "Later independent check, or empty if not needed."},
            "alternative_explanation": {"type": "string", "description": "Competing cause and what the independent check established."},
            "why_rewind": {"type": "string", "description": "Why replacing this decision beats repairing in place."},
        },
        "required": ["evidence_origin", "failure_tool_call_id", "independent_check_tool_call_id",
                     "alternative_explanation", "why_rewind"],
        "additionalProperties": False,
    }
    return [
        {"name": "backtrack_candidates", "description": "Inspect recovery decisions and recent tool evidence. Querying does not commit to rewind.",
         "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                        "required": ["reason"], "additionalProperties": False}},
        {"name": "backtrack_commit", "description": "Request optional recovery after reviewing evidence. Rejection returns normally; continue solving. The selected LLM response is regenerated.",
         "parameters": {"type": "object", "properties": {
             "record_uid": {"type": "string"}, "memory_summary": {"type": "string", "maxLength": MEMORY_ITEM_LIMIT,
                 "description": "Facts, uncertain hypotheses, alternatives, next checks and strategy. No binding bans based on guesses."},
             "reason": {"type": "string"}, "review": review,
         }, "required": ["record_uid", "memory_summary", "review"], "additionalProperties": False}},
    ]


def review_rejection(review: object, observations: list[dict]) -> str | None:
    if not isinstance(review, dict):
        return "Supply a review grounded in actual tool observations before committing."
    required = ("evidence_origin", "failure_tool_call_id", "independent_check_tool_call_id",
                "alternative_explanation", "why_rewind")
    if any(not isinstance(review.get(key), str) for key in required):
        return "Review fields must be strings; consult the commit tool schema."
    if any(len(review[key]) > 1_000 for key in required):
        return "Keep each review field within 1000 characters."
    if not review["alternative_explanation"].strip() or not review["why_rewind"].strip():
        return "Consider a competing explanation and explain why local repair is insufficient."
    origin = review["evidence_origin"]
    if origin not in {"repository_test", "task_reproduction", "agent_authored_test", "unknown"}:
        return "Environment/setup failures or inference alone do not justify rewind; investigate or repair in place."
    by_id = {item["id"]: item for item in observations}
    failure = by_id.get(review["failure_tool_call_id"])
    if failure is None:
        return "Failure evidence must reference a real ordinary tool call in this trajectory."
    if origin in {"repository_test", "agent_authored_test"} and failure["success"]:
        return "A test-failure review must reference an actually failed tool observation; otherwise investigate or use unknown evidence with an independent check."
    if origin in {"agent_authored_test", "unknown"}:
        check = by_id.get(review["independent_check_tool_call_id"])
        if (check is None or check["index"] <= failure["index"]
                or check["name"] not in {"read_file", "grep", "glob", "list_files", "bash"}
                or not check["success"]):
            return "Validate the new/uncertain test independently AFTER its failure (fixture, units, expectations or contract); then reconsider local repair."
        if check["name"] == failure["name"] and check["arguments"] == failure["arguments"]:
            return "Repeating the identical failing command is not an independent check."
    return None
