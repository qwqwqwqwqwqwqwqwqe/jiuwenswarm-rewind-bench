# Asynchronous hybrid rewind candidates

[中文版](hybrid-candidates.md)

The hybrid policy is named `semantic` in code and run artifacts. The `recent` baseline returns the latest eligible checkpoints. After a long repair attempt, a checkpoint preceding a destructive action can still exist in the trace while falling outside the fixed candidate budget.

Replay records recoverable checkpoints at LLM call boundaries. The hybrid policy indexes those checkpoints and selects which ones to show. The agent still chooses a `record_uid` and commits the rewind. The runtime then restores the workspace and context for that checkpoint.

## 1. Group tool records by LLM call

A round consists of an LLM call and its ordinary tool calls. The index reads complete lines appended to the trace JSONL since its last read. The runtime normally closes the previous round just before the next live LLM call. A candidate query also reads new records and closes the open round if all expected ordinary tool results have been recorded. Candidate lookup and rewind commit calls are excluded from the evidence.

At the end of a round, the index checks each tool record's `metadata.filesystem.workspaces`. A difference between Git `before_tree` and `after_tree` on any tool record queues the round once. A later tool can undo the change and the earlier tool record still triggers classification. Each queue entry is `(record_uid, round_card, evidence)`.

| Field | Contents |
|---|---|
| `record_uid` | The LLM checkpoint that issued the tool calls. Rewinding here regenerates that decision. |
| `round_card` | The decision, tool names, arguments and results, changed files, Git tree transitions, and a flag for protected changes. |
| `evidence` | Up to six recent tool results from rounds that left the workspace unchanged. Reads, tests, and verifier feedback can all add evidence. The buffer is cleared once the next writing round is queued. |

[Round grouping and enqueueing](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L214) · [Runtime hooks](../tools/run_mettle_paper_recovery.py#L34)

## 2. Classify subgoal boundaries in the background

An asynchronous worker processes rounds in order so that each classification sees the current segment. It uses the task's configured model endpoint for a separate request. The input includes the current segment's goal and progress, the current round summary, and recent evidence. Tool results are truncated for the summary. The system prompt treats supplied text as evidence and requests a JSON object with `decision`, `goal`, `progress`, and `reason`.

- `same_goal` advances the current segment's end and updates its goal description and progress.
- `new_goal` starts a segment at the current checkpoint when the objective or the approach under test changes materially.
- A timeout, error, or invalid response creates a one-point segment without a semantic description. Each classification has a 10-second timeout.

The cache in `semantic-candidate-index.jsonl` is keyed by a digest of the prompt, classification input, and Git tree transitions. A new branch can reuse a result when it encounters the same input while replaying a shared prefix. The classifier segments the work, and the agent decides whether to rewind using its own failure evidence.

[Classifier prompt](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L16) · [Caching and segment updates](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L275)

## 3. Select candidates under a fixed budget

`backtrack_candidates` waits up to about two seconds for background work, then obtains eligible historical checkpoints from Replay. The hybrid policy fills the budget in this order.

1. The latest eight checkpoints, which cover recent decisions.
2. Protected changes and failed edit attempts. Git deletions, renames, and overwrite-like operations are used to identify protected changes.
3. Semantic segment starts and writing rounds awaiting classification.
4. Semantic segment ends.

Within each group, newer checkpoints take priority. The selector deduplicates by `record_uid`, applies the cap, and returns the selected candidates in chronological order. Candidates with segment descriptions carry the goal, progress, and position within the segment. A query may return fewer candidates than the cap. If classification has yet to succeed, the query falls back to the same-sized recency window. The runtime records the list actually returned for target selection and auditing.

In Age, the agent needed to revisit the decision that issued the migration command. By the time it queried the recent window, that decision was out of view. The hybrid policy exposed the pre-migration checkpoint `rec_000031` under all three tested caps. The agent selected it, rewound, and passed official verification.

[Ranking and fallback](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L325) · [Candidate query integration](../tools/run_mettle_paper_recovery.py#L51)

## 4. Results and audit

| Candidate cap | Cases | Recent | Hybrid |
|---|---|---:|---:|
| 20 | Bat, Ctags, Age | 0/3 | 3/3 |
| 40 | Bat, Ctags, Age | 0/3 | 3/3 |
| 80 | Bat, Age | 1/2 | 2/2 |

Each task, policy, and budget configuration was run once from the same archived failure endpoint for that task and scored by the official verifier. Ctags used its archived model defaults. The preset targets were `rec_000055` and `rec_000041` for Bat at 20/40, `rec_000041` for Bat at 80, `rec_000042` and `rec_000040` for Ctags, and `rec_000031` for Age. Bat passed after selecting `rec_000053` at both smaller caps, illustrating why target visibility, actual selection, and final acceptance are reported separately.

`semantic-candidate-events.jsonl` records returned IDs, response bytes, query wait time, classification events, and available token usage. Background classification adds cost, and calls with missing usage are counted separately. The results establish how the policy behaved on these selected failure endpoints. Estimating its effect on overall pass rates requires a broader sample. [Per-arm results](candidate-results.json) · [Metric implementation](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L157)

## 5. Reproduce a paired comparison

Follow the [setup guide](setup.en.md) for MettleBench, the runtime, Docker, and the model environment. Install the hybrid runtime in a fresh directory. A local failure archive needs its manifest, traces, snapshot store, and workspace and scratch archives. The repository ships the runner and result table. The runner takes the archive as a local input.

```bash
export JWS_METTLE_ROOT="$PWD/local/mettle-hybrid"
# JWS_RUNTIME_SOURCE and JWS_MODEL_ENV_FILE are set during setup.
python tools/run_mettle_paper_recovery.py --install --semantic-candidates

python tools/run_mettle_candidate_windows.py \
  --source /path/to/archived/age/seed \
  --task programbench_age_crypto_fixture_handoff \
  --output "$JWS_METTLE_ROOT/results/age-comparison" \
  --limit 20 --limit 40 --target rec_000031
```

The launcher runs both policies for each cap, verifies the paired environment, and writes one JSON result per arm. Use `--archived-model-defaults` to retain the archived model request settings where required. For a different endpoint, supply checkpoint IDs from that archive through `--target`.
