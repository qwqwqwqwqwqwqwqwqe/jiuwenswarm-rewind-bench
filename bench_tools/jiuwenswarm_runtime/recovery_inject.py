"""Post-process regenerated rewind inputs after worker.prepare_recovery.

Fixes the three v6/v7 recovery-input defects inside the override file, before
the fork's first model call reads it:

1. Truncation/duplication: the v7 paper-profile branch renders the notebook
   both in a recovery notice and in the backtrack memory message. The notice
   is cut back to its persistent-marker intro; the memory message stays the
   single notebook channel, so its content keeps the ``MEMORY_PREFIX`` start
   that later rewinds rely on when stripping and re-rendering it.
2. Official evidence: rendered once inside the notebook message, above the
   numbered items (v6 appended it to every cumulative memory item last, where
   the old 2,000-char item cap always cut it off; the first v8 design used a
   dedicated anchored message, which boundaries rejects once history
   compaction shortens the conversation below the recorded index). The
   notebook message is re-appended to every request of the fork, so the
   evidence persists; the next rewind re-renders the notebook and the current
   evidence is injected fresh.
3. Operative plan: the most recent rewind's action sections are extracted and
   rendered as a header directly after the evidence, still above the numbered
   items, so "experience" arrives as a verifiable plan instead of prose that
   only appears after everything else.

This module is pure stdlib on purpose: it must be importable and testable
without the experimental runtime.
"""
from __future__ import annotations
import json
import re

# Matches replay.backtrack.DEFAULT_MEMORY_PREFIX and memory.NOTICE_KEY.
MEMORY_PREFIX = "Backtrack memory notebook from abandoned future branches"
NOTICE_KEY = "jiuwenswarm_recovery_notice"
NOTICE_POSITION_KEY = "jiuwenswarm_recovery_position"
EVIDENCE_LEAD = "Official evaluator feedback (observed, not a hypothesis): "

NOTES_CHAR_LIMIT = 7_700  # leaves room inside the 8,000-char notebook item schema
_FIELD_CHAR_LIMIT = 700

# Header priority: prohibitions and pre-mutation gates first, then the new
# verifiable plan fields, then supporting evidence sections.
_PLAN_FIELDS = (
    "do_not_repeat",
    "do_not_call_before",
    "must_satisfy_before_mutation",
    "mutation_preflight",
    "verify_first",
    "next_plan",
    "corrected_strategy",
    "first_3_tool_calls",
    "success_invariant",
    "success_criteria",
    "observed_fact",
    "hypothesis",
)
# Labels recognized as section boundaries inside memory notes.
_KNOWN_LABELS = frozenset(_PLAN_FIELDS) | frozenset({
    "failed_check",
    "observed_loop",
    "why_local_repair_failed",
    "root_cause",
    "evidence_observed",
    "assumptions_to_recheck",
    "rewind_target_reason",
    "selected_candidate_safety_check",
})
_LABEL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(_KNOWN_LABELS, key=len, reverse=True)) + r")\s*:"
)


def cap_notes(text: str, *, limit: int = NOTES_CHAR_LIMIT) -> str:
    """Cap model recovery notes at a paragraph boundary and mark the cut."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", limit // 2, limit)
    if cut < 0:
        cut = limit
    return text[:cut].rstrip() + "\n...[recovery notes truncated]"


def extract_operative_plan(notes: str, *, per_field_limit: int = _FIELD_CHAR_LIMIT) -> dict:
    """Extract labeled action sections from recovery notes.

    Labels appear as ``label: text`` either inline in flowing prose or as
    newline-separated sections (both occurred in real v6 memories). Each
    field's text runs until the next recognized label.
    """
    sections: dict[str, str] = {}
    matches = list(_LABEL_PATTERN.finditer(notes))
    for index, match in enumerate(matches):
        label = match.group(1)
        if label in sections:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(notes)
        text = re.sub(r"\s+", " ", notes[start:end]).strip()
        if len(text) > per_field_limit:
            text = text[: per_field_limit - 3].rstrip() + "..."
        sections[label] = text
    return {field: sections[field] for field in _PLAN_FIELDS if field in sections}


def operative_header(plan: dict) -> str:
    """Render the extracted plan as a compact header, in priority order."""
    if not plan:
        return ""
    lines = [
        "OPERATIVE RECOVERY PLAN (from the most recent rewind memory; execute these before and "
        "during new work; if a listed check cannot be performed as written, record a substitute "
        "check and continue instead of looping):"
    ]
    lines.extend(f"- {field}: {plan[field]}" for field in _PLAN_FIELDS if field in plan)
    return "\n".join(lines)


def evidence_text(history_tail: list) -> str:
    """Render the latest official feedback entries as the evidence message body."""
    return EVIDENCE_LEAD + json.dumps(history_tail, ensure_ascii=False)


def _is_memory(message) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and message["content"].strip().startswith(MEMORY_PREFIX)
    )


def _is_evidence(message) -> bool:
    return (
        isinstance(message, dict)
        and bool((message.get("metadata") or {}).get(NOTICE_KEY))
        and isinstance(message.get("content"), str)
        and message["content"].startswith(EVIDENCE_LEAD)
    )


def _insert_before_items(content: str, block: str) -> str:
    match = re.search(r"(?m)^\s*1\. ", content)
    if match:
        return content[: match.start()] + block + "\n\n" + content[match.start():]
    return content.rstrip() + "\n\n" + block


def apply_recovery_injection(override: dict, evidence: str, newest_notes: str = "") -> dict:
    """Mutate a regenerated rewind input in place; returns the same dict.

    Idempotent: applying it to an already-injected override changes nothing,
    so a replayed or re-prepared recovery converges to the same input.
    """
    messages = override.get("messages") or []
    # 1. Drop stale standalone evidence left by the anchored-message design.
    messages = [message for message in messages if not _is_evidence(message)]
    # 2. Notices must stay persistent markers, not a second notebook copy.
    for message in messages:
        metadata = message.get("metadata") or {}
        if metadata.get(NOTICE_KEY) and isinstance(message.get("content"), str) \
                and MEMORY_PREFIX in message["content"]:
            message["content"] = message["content"].split(MEMORY_PREFIX, 1)[0].rstrip()
    # 3. Notebook preamble: official evidence first, then the operative plan,
    #    both above the numbered items. They travel inside the memory message,
    #    which boundaries re-appends to every request of the fork without any
    #    positional anchor (anchors hard-fail once history compaction shrinks
    #    the message list below the recorded index).
    blocks = []
    if evidence:
        blocks.append(evidence)
    if newest_notes:
        blocks.append(operative_header(extract_operative_plan(newest_notes)))
    for message in messages:
        if not _is_memory(message):
            continue
        pending = [block for block in blocks
                   if block.split("\n", 1)[0] not in message["content"]]
        if pending:
            message["content"] = _insert_before_items(message["content"], "\n\n".join(pending))
    override["messages"] = messages
    return override
