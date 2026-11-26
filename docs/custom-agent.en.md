# Use your own agent

[中文](custom-agent.md) · [Home](../README.en.md)

You can score any agent's output with the same official verifier. To use real-time feedback or rewind, adapt the agent lifecycle and tool APIs. `JWS_MODEL_NAME` selects the model used by JiuwenSwarm.

## Offline scoring

Run your agent from the same SWE-bench image and initial commit and capture its Git patch.

```bash
python -m bench_tools.swebench_runner.evaluate_patch \
  --patch local/patch.diff --instance-id django__django-11477 --output-dir local/custom-swe-eval
```

For MettleBench, work on a copy of the starting tree and preserve required links, permissions and Git metadata. Build the evaluator image first using the [setup guide](setup.en.md).

```bash
python tools/prepare_mettlebench.py --task programbench_bat_cache_handoff
python - <<'PY'
from bench_tools.mettlebench.core import evaluate
feedback, _ = evaluate('vendor/MettleBench', 'programbench_bat_cache_handoff',
                       'local/my-agent-workspace', 'local/custom-mettle-eval')
print(feedback)
PY
```

## Add online strategies

1. Register a submission tool, issue a submission ID, and pause writes while the host captures a patch or workspace.
2. Score the copy outside the agent container. Return `status`, `resolved`, `failure_id` and the first failure diagnostic while keeping the verifier outside the agent's writable boundary.
3. Continue preserves the current session. Restart rebuilds the pristine workspace and session, and passes failure experience. Apply identical accounting and termination across arms.
4. Rewind also needs serializable context and an aligned workspace snapshot at decision boundaries. Let the model inspect and request candidates. Fence the old attempt, restore the aligned state, add memory and record the outcome. Replay recorded observations so old effects are not repeated.

Reuse the [ledger](../bench_tools/jiuwenswarm_runtime/ledger.py), [SWE-bench bridge](../bench_tools/swebench_runner/evaluator_bridge.py), and [MettleBench evaluator](../bench_tools/mettlebench/core.py). The [JiuwenSwarm submission tool](../bench_tools/jiuwenswarm_runtime/runtime_tools.py) illustrates the host IPC. Validate matched cloning, independent failure counters and rollback using a mock model before paid experiments.
