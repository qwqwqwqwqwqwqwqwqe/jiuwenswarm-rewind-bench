"""Unit tests for replay-prefix candidate filtering."""
from bench_tools.jiuwenswarm_runtime.runtime_messages import filter_replay_candidates, replay_prefix_llm_uids


def llm(uid, matched_by=None):
    return {"record_uid": uid, "kind": "llm",
            "metadata": {"matched_by": matched_by} if matched_by else {}}


class TestReplayPrefixLlmUids:
    def test_paired_fixture_shape_target_is_regenerated(self):
        # Seed has 4 llm requests; the fork replays the 3 before the target,
        # then REGENERATES the 4th (bootstrap target) as the first live
        # request. Replayed records carry no matched_by; the regenerated
        # request carries materialized_prefix...; later live requests carry
        # post_fork_live.
        fork_records = [
            llm("rec_000002"), llm("rec_000004"), llm("rec_000006"),   # replayed
            {"record_uid": "rec_000007", "kind": "tool"},
            llm("rec_000008", "materialized_prefix_input_id+path_id"),  # regenerated
            llm("rec_000010", "post_fork_live"),
        ]
        assert replay_prefix_llm_uids(fork_records) == [
            "rec_000002", "rec_000004", "rec_000006"]

    def test_real_v8_shape(self):
        # v8.2 a1: 12 replayed llm records, then the regenerated 13th.
        fork_records = [llm(f"rec_{n:06d}") for n in range(1, 25, 2)]
        fork_records.append(llm("rec_000025", "materialized_prefix_input_id+path_id"))
        fork_records.append(llm("rec_000027", "post_fork_live"))
        assert replay_prefix_llm_uids(fork_records) == [f"rec_{n:06d}" for n in range(1, 25, 2)]

    def test_first_record_live_means_no_replay_prefix(self):
        fork_records = [llm("rec_000001", "post_fork_live")]
        assert replay_prefix_llm_uids(fork_records) == []

    def test_all_unmarked_records_are_replay(self):
        fork_records = [llm("rec_000001"), llm("rec_000003"), {"record_uid": "x", "kind": "tool"}]
        assert replay_prefix_llm_uids(fork_records) == ["rec_000001", "rec_000003"]

    def test_meta_and_workspace_records_are_ignored(self):
        fork_records = [
            {"record_uid": "rec_000001", "kind": None},   # fork metadata
            llm("rec_000002"),
            {"record_uid": "rec_000003", "kind": "workspace"},
            llm("rec_000004", "post_fork_live"),
        ]
        assert replay_prefix_llm_uids(fork_records) == ["rec_000002"]


class TestFilterReplayCandidates:
    def test_drops_replay_segment_keeps_live_and_boundary(self):
        candidates = [
            {"record_uid": "rec_000002"}, {"record_uid": "rec_000004"},
            {"record_uid": "rec_000006"},   # replayed
            {"record_uid": "rec_000008"},   # regenerated live boundary
            {"record_uid": "rec_000010"},   # later live decision
        ]
        filtered = filter_replay_candidates(
            candidates, ["rec_000002", "rec_000004", "rec_000006"])
        assert [c["record_uid"] for c in filtered] == ["rec_000008", "rec_000010"]

    def test_empty_prefix_is_a_no_op(self):
        candidates = [{"record_uid": "rec_000001"}, {"record_uid": "rec_000002"}]
        assert filter_replay_candidates(candidates, []) == candidates

    def test_ignores_entries_without_uid(self):
        filtered = filter_replay_candidates(
            [{"record_uid": "rec_000001"}, {"other": "shape"}], ["rec_000001"])
        assert filtered == [{"other": "shape"}]
