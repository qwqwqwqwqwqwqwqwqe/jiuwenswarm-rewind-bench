"""Unit tests for rewind recovery injection: dedup, evidence, operative plan header."""
import json

import pytest

from bench_tools.jiuwenswarm_runtime.recovery_inject import (
    MEMORY_PREFIX,
    cap_notes,
    evidence_text,
    extract_operative_plan,
    operative_header,
    apply_recovery_injection,
)

# Condensed but structurally faithful copy of the real v6 django-14034 a1 rewind
# memory: all labels appear inline inside one flowing string.
REAL_NOTES = (
    "failed_check: Official acceptance submissions 1, 2, and 3 all failed "
    "`test_render_required_attributes (forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest)`. "
    "observed_loop: Submitted three variants of the same repair to `MultiValueField.clean()`'s all-empty branch; "
    "all were rejected on the identical rendering test. why_local_repair_failed: The failure concerns HTML "
    "`required` attributes and the investigation never verified the actual rendering test fixture. root_cause: "
    "Hypothesis uncertain: the hidden official rendering test is an independent existing regression. "
    "evidence_observed: The all-empty behavior self-check passed under the mutated code. "
    "do_not_repeat: Do not edit `MultiValueField.clean()` all-empty shortcut again based only on the original "
    "issue description. do_not_call_before: Do not call `submit_patch` before verifying rendering test "
    "input/expected output and running the exact available rendering-related native tests. "
    "must_satisfy_before_mutation: Read actual widget and BoundField required-attribute code. "
    "mutation_preflight: Before the next write/edit, confirm `git diff -- django/forms/fields.py` is empty. "
    "first_3_tool_calls: `functions.bash` to verify clean diff; `functions.grep` for MultiWidget tests; "
    "`functions.read_file` on located rendering implementation. success_invariant: A source change fixes the "
    "all-empty validation, the official rendering acceptance item passes, and official `submit_patch` reports "
    "accepted."
)

NEW_STYLE_NOTES = (
    "observed_fact: FAIL test_render_required_attributes - Couldn't find "
    "'<input id=\"id_f_1\" name=\"f_1\" type=\"text\">' in response.\n"
    "hypothesis: widget required-attribute propagation is missing; disconfirm by rendering an optional "
    "MultiValueField with a required subfield and inspecting the HTML.\n"
    "verify_first: run `python tests/runtests.py forms_tests.field_tests.test_multivaluefield -v 2` and "
    "expect all listed tests to pass before any edit.\n"
    "next_plan: 1. add probe under $JWS_SCRATCH_DIR rendering the form as_p(); 2. confirm f_1 lacks required; "
    "3. patch BoundField.build_widget_attrs; 4. re-run probe and native tests; 5. submit_patch.\n"
    "do_not_repeat: do not resubmit any patch that only alters MultiValueField.clean()'s empty branch.\n"
)


def make_override(notebook_items=None):
    """Reproduce the v7 regenerated input shape: duplicated notice + memory message."""
    notebook = (
        MEMORY_PREFIX
        + "\n\nEXECUTION STATE: THE REWIND HAS ALREADY COMPLETED. You are now re-deciding the selected "
        "LLM invocation after restoring its earlier file state.\n\n"
    )
    for index, item in enumerate(notebook_items or ["sample item text"], start=1):
        notebook += f"{index}. {item} (target=rec_000007; reason=replace bad decision)\n"
    notice_content = (
        "[Host recovery notification; the original user task is unchanged.]\n"
        "A rewind has already completed. The failed trial below already happened in the abandoned branch. "
        "Once the corrected approach has passed verification, stop calling tools and give the user the "
        "final answer.\n\n" + notebook
    )
    return {
        "messages": [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Repair the repository."},
            {
                "role": "user",
                "content": notice_content,
                "metadata": {"jiuwenswarm_recovery_notice": True, "jiuwenswarm_recovery_position": 2},
            },
            {"role": "system", "content": notebook},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    }


def messages_of(override):
    return override["messages"]


def memory_message(override):
    found = [m for m in messages_of(override) if m["role"] == "system" and m["content"].startswith(MEMORY_PREFIX)]
    assert len(found) == 1, "expected exactly one backtrack memory message"
    return found[0]


def evidence_messages(override):
    return [
        m
        for m in messages_of(override)
        if (m.get("metadata") or {}).get("jiuwenswarm_recovery_notice")
        and m["content"].startswith("Official evaluator feedback")
    ]


class TestCapNotes:
    def test_short_notes_unchanged(self):
        assert cap_notes("short notes") == "short notes"

    def test_long_notes_capped_with_marker(self):
        text = "paragraph one.\n" + "word " * 4000
        capped = cap_notes(text, limit=200)
        assert len(capped) <= 200 + 64
        assert capped.endswith("...[recovery notes truncated]")

    def test_cap_prefers_paragraph_boundary(self):
        text = "first paragraph kept whole.\n" + "tail " * 200
        capped = cap_notes(text, limit=60)
        assert capped.startswith("first paragraph kept whole.")
        assert "...[recovery notes truncated]" in capped


class TestExtractOperativePlan:
    def test_inline_labels_from_real_v6_memory(self):
        plan = extract_operative_plan(REAL_NOTES)
        assert "do_not_repeat" in plan and "MultiValueField.clean()" in plan["do_not_repeat"]
        assert plan["do_not_repeat"].startswith("Do not edit")
        assert "first_3_tool_calls" in plan
        assert "submit_patch" not in plan["do_not_repeat"]  # section ends at the next label

    def test_newline_style_labels(self):
        plan = extract_operative_plan(NEW_STYLE_NOTES)
        assert "verify_first" in plan and "runtests.py" in plan["verify_first"]
        assert "next_plan" in plan and "submit_patch" in plan["next_plan"]
        assert "hypothesis" in plan

    def test_missing_labels_return_empty(self):
        assert extract_operative_plan("no labeled sections here") == {}

    def test_field_text_is_bounded(self):
        plan = extract_operative_plan("do_not_repeat: " + "x " * 5000)
        assert len(plan["do_not_repeat"]) <= 700


class TestOperativeHeader:
    def test_header_renders_priority_fields(self):
        plan = extract_operative_plan(REAL_NOTES)
        header = operative_header(plan)
        assert header.startswith("OPERATIVE RECOVERY PLAN")
        assert header.index("do_not_repeat") < header.index("must_satisfy_before_mutation")

    def test_empty_plan_no_header(self):
        assert operative_header({}) == ""


class TestEvidenceText:
    def test_evidence_message_content(self):
        entry = {"status": "product_failure", "failure_id": "test_x", "submission_id": 3}
        text = evidence_text([entry])
        assert text.startswith("Official evaluator feedback (observed, not a hypothesis): ")
        assert json.loads(text.split(": ", 1)[1]) == [entry]


class TestApplyRecoveryInjection:
    def test_dedupes_notice_embedding_notebook(self):
        override = make_override()
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES)
        notices = [
            m
            for m in messages_of(override)
            if (m.get("metadata") or {}).get("jiuwenswarm_recovery_notice")
            and "Host recovery notification" in m["content"]
        ]
        assert notices, "persistent notice must survive"
        for notice in notices:
            assert MEMORY_PREFIX not in notice["content"], "notice must not duplicate the notebook"

    def test_memory_message_keeps_prefix_and_gains_header(self):
        override = make_override(["sample item"])
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES)
        memory = memory_message(override)
        assert memory["content"].startswith(MEMORY_PREFIX), "prefix must stay first for later stripping"
        assert "OPERATIVE RECOVERY PLAN" in memory["content"]
        assert memory["content"].index("OPERATIVE RECOVERY PLAN") < memory["content"].index("1. sample item")

    def test_evidence_inside_notebook_before_items(self):
        """Evidence must live in the notebook message, not as its own anchored
        message: boundaries hard-fails any positioned attachment once history
        compaction shortens the message list below the recorded index."""
        override = make_override()
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES)
        assert evidence_messages(override) == []
        memory = memory_message(override)
        assert memory["content"].count("Official evaluator feedback (observed, not a hypothesis)") == 1
        content = memory["content"]
        assert content.index("Official evaluator feedback") < content.index("OPERATIVE RECOVERY PLAN")
        assert content.index("OPERATIVE RECOVERY PLAN") < content.index("1. sample item text")

    def test_idempotent(self):
        override = make_override()
        first = json.dumps(apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES))
        second = json.dumps(apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES))
        assert first == second

    def test_tools_untouched(self):
        override = make_override()
        before = json.dumps(override["tools"])
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES)
        assert json.dumps(override["tools"]) == before

    def test_no_extractable_notes_no_header_but_still_deduped(self):
        override = make_override(["plain item without labels"])
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), "no labels")
        memory = memory_message(override)
        assert "OPERATIVE RECOVERY PLAN" not in memory["content"]
        notices = [m for m in messages_of(override) if "Host recovery notification" in m["content"]]
        assert notices and all(MEMORY_PREFIX not in n["content"] for n in notices)

    def test_survives_without_memory_message(self):
        override = {"messages": [{"role": "user", "content": "task"}]}
        apply_recovery_injection(override, evidence_text([{"submission_id": 3}]), REAL_NOTES)
        assert evidence_messages(override) == []
        assert "Official evaluator feedback" not in json.dumps(override["messages"])
