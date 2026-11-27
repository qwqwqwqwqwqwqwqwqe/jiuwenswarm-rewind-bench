# JiuwenSwarm Rewind Bench

<p align="center">
  <img src="docs/assets/project-banner.png" alt="JiuwenSwarm Rewind Bench" width="440" />
</p>

<p align="center">
  <a href="docs/setup.en.md">Setup guide</a>
  · <a href="README.md">中文 README</a>
  · <a href="https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/tag/runtime-20261112-173855">Runtime ZIP</a>
</p>

<p align="center">
  <a href="docs/setup.en.md"><img src="https://img.shields.io/badge/Python-3.12-465CAB" alt="Python 3.12" /></a>
  <a href="docs/setup.en.md"><img src="https://img.shields.io/badge/Platform-Linux%20%2F%20WSL2-53617E" alt="Linux / WSL2" /></a>
  <a href="https://github.com/SWE-bench/SWE-bench"><img src="https://img.shields.io/badge/Benchmark-SWE--bench-AA6236" alt="SWE-bench" /></a>
  <a href="https://github.com/Kelvin-Coffee/MettleBench"><img src="https://img.shields.io/badge/Benchmark-MettleBench-187F70" alt="MettleBench" /></a>
</p>

This project feeds official benchmark verdicts back to JiuwenSwarm and compares three response strategies: Continue repairs in place, Restart starts over with failure feedback, and Rewind restores an earlier decision point. SWE-bench evaluates a Git patch; MettleBench evaluates the submitted workspace.

| Guide | Contents |
|---|---|
| [Setup and reproduction](docs/setup.en.md) | Linux/WSL, runtime installation, model credentials, data, and images |
| [JiuwenSwarm architecture and source](docs/jiuwenswarm.en.md) | Component diagrams, execution sequence, versions, and licensing |
| [Rewind mechanics](docs/rewind.en.md) | Checkpoints, replay, recovery boundaries, and auditing |
| [Use your own agent](docs/custom-agent.en.md) | Independent scoring, feedback, and recovery integration |
| [Experiments](docs/experiments.en.md) | Three-arm results, matched recovery outcomes, costs, and limitations |
| [Runtime source snapshot](runtime-source/README.en.md) | Browse all shipped Python source in the experimental distributions |

## Experimental contribution: asynchronous hybrid rewind candidates

A recency window can fill with decisions from a long repair attempt and hide the checkpoint preceding a destructive action. The hybrid policy indexes existing LLM checkpoints. A Git tree change queues the tool round once for background subgoal classification. Candidate selection combines the latest eight checkpoints, protected edits and failed edit attempts, and semantic segment boundaries under the same budget. Queries wait up to about two seconds and fall back to recency when no classification has succeeded.

### Matched recovery from archived failures

Both policies start from the same archived failed workspace and context and use the official verifier. Each task, policy, and budget combination was run once.

| Case | Recent 20 | Hybrid 20 | Recent 40 | Hybrid 40 | Recent 80 | Hybrid 80 |
|---|---|---|---|---|---|---|
| Bat | FAIL | PASS | FAIL | PASS | PASS | PASS |
| Ctags | FAIL | PASS | FAIL | PASS | — | — |
| Age | FAIL | PASS | FAIL | PASS | FAIL | PASS |

**Hybrid passed 3/3 and recency passed 0/3 at each of the 20- and 40-candidate caps.** For Age, hybrid selected the pre-migration checkpoint `rec_000031` and passed at all three tested caps. Ctags recovered from `rec_000042` at both smaller caps. Bat passed after rewinding to `rec_000053` at both smaller caps, outside the preset target set. These results show the policy helped on the selected archived failures. The independent 76-task comparison is a separate experiment. [Per-arm results](docs/candidate-results.json) · [Semantic index](bench_tools/jiuwenswarm_runtime/semantic_candidates.py) · [Reproduction](docs/hybrid-candidates.md)


## Repository tree

```text
jiuwenswarm-rewind-bench/
├── bench_tools/
│   ├── jiuwenswarm_runtime/          # Feedback, replay, and recovery integration
│   ├── swebench_runner/              # Patch capture and SWE-bench evaluation
│   ├── mettlebench/                  # Full workspace evaluation and recovery pairs
│   ├── batch/                        # Batch orchestration helpers
│   └── net/                          # Container model egress
├── runtime-source/
│   ├── workswarm/jiuwenswarm/         # JiuwenSwarm distribution source
│   ├── openjiuwen/openjiuwen/         # agent-core distribution source
│   ├── replay_agent_recorder/replay/ # Replay distribution source
│   └── package/                      # Original manifest and legacy driver
├── tools/                            # Benchmark data and batch launchers
├── tests/                            # Offline regression and integration tests
└── docs/                             # Chinese and English guides
```

## System architecture

![Host controller, isolated agent, replay store and two official evaluators](docs/assets/evaluation-architecture.png)

The host controls submission capture and official scoring. The agent works in a separate container. The score reports, recovery artifacts, and model cost are recorded separately so they can be compared per strategy.

A fresh clone can be configured without mirroring the original author's directory. Install the dependencies in the setup guide and download the [exact experimental runtime](https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/tag/runtime-20261112-173855).

Across 76 comparable MettleBench tasks, Continue, Restart, and Rewind solved **71, 73, and 72**. On the matched Bat endpoint, Continue failed the same criterion five more times; Rewind recovered the original cache input and passed its first new evaluation.

Upstream: [SWE-bench](https://github.com/SWE-bench/SWE-bench) · [MettleBench](https://github.com/Kelvin-Coffee/MettleBench) · [JiuwenSwarm](https://github.com/openJiuwen-ai/jiuwenswarm) · [AgentRewind](https://arxiv.org/abs/2608.14380)
