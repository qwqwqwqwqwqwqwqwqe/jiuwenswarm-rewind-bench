# JiuwenSwarm integration and architecture

[中文](jiuwenswarm.md) · [Home](../README.en.md)

## Components

```mermaid
flowchart TB
    Legacy["Standard channels / legacy driver"] --> Service["JiuwenSwarm AgentRuntime"]
    Bench["Benchmark agent_entry.py"] --> Chat["Rewind chat supervisor"]
    Chat --> Attempt["Isolated attempt worker"]
    Attempt -->|"AgentRequest + host tools"| Service
    Service --> Adapter["server/runtime/agent_adapter<br/>Agent and mode adapters"]
    Adapter --> Core["openJiuwen agent-core<br/>Agent loop, model, tools, execution scope"]
    Core --> Model["Model endpoint"]
    Core --> Tools["Workspace tools and submit_patch"]
    Core <-->|"model / tool boundaries"| Trace["AttemptBoundaries + Replay"]
    Trace --> Store["Traces and workspace snapshots"]
```

- `agent_entry.py` owns the benchmark episode and translates settings into an AgentRequest. The Rewind supervisor starts and fences attempt processes.
- `AgentRuntime` manages the agent request. The [agent adapters](../runtime-source/workswarm/jiuwenswarm/server/runtime/agent_adapter/) connect the runtime to particular agent modes. `interface_deep.py` is one implementation in that directory.
- [agent-core](../runtime-source/openjiuwen/openjiuwen/) provides model, tool, agent loop, and execution-scope APIs. [Replay](../runtime-source/replay_agent_recorder/replay/) records and restores supported boundaries.
- The feedback overlay adds the benchmark submission tool. The verifier runs separately under host control. The legacy AgentServer driver has a separate integration path described in the packaged guide.

## Sequence after official evaluation

```mermaid
sequenceDiagram
    participant H as Host controller
    participant J as JiuwenSwarm supervisor
    participant A as Agent attempt
    participant S as Replay store
    participant V as Official verifier
    H->>J: Start a strategy and deliver the task
    J->>A: Construct AgentRequest and tools
    loop Live reasoning and tools
        A->>S: Record aligned decisions, results, and checkpoints
    end
    A->>H: submit_patch requests evaluation
    H->>V: Evaluate a patch or workspace copy
    V-->>H: Official verdict
    H-->>A: Return failure feedback or acceptance
    alt Continue
        A->>A: Keep context and workspace, repair in place
    else Restart
        J->>H: Request pristine workspace
        H-->>J: Workspace restored
        J->>A: Start a fresh attempt with failure feedback
    else Rewind selected
        A->>S: Query candidates and request a checkpoint
        A->>J: Return accepted rewind control
        J->>S: Restore state and prepare prefix replay
        J->>A: Resume at target with operational memory
    end
```

Continue and Rewind both receive the first failed criterion. A rewind only happens after a model-selected checkpoint is accepted. Strict replay uses recorded responses and results instead of rerunning old side-effecting commands.

## Exact experimental distributions

| Component | Version | Upstream |
|---|---|---|
| JiuwenSwarm | `0.2.5b1+rewind.bench.20261112.retry1` | [JiuwenSwarm](https://github.com/openJiuwen-ai/jiuwenswarm) |
| openJiuwen / agent-core | `0.1.20+rewind.bench.20261110` | [agent-core](https://gitcode.com/openJiuwen/agent-core) |
| Replay recorder | `1.0.0a1+rewind.20261112.escape1` | [replay-agent-recorder](https://github.com/Futuresis/replay-agent-recorder) |

Browse the extracted source in [runtime-source](../runtime-source/README.en.md). The build [manifest](../runtime-source/package/manifest.json) records upstream commits and wheel hashes. Some builds included local changes. Install the original matching wheels from the [release](https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/tag/runtime-20261112-173855) using the [setup guide](setup.en.md). The historical AgentServer path also requires the legacy driver patch in the original bundle.
