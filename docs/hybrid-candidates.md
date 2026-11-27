# 异步混合回退候选

[English version](hybrid-candidates.en.md)

混合策略在代码和实验产物中称为 `semantic`，对照策略 `recent` 返回最近的符合条件的检查点。混合策略解决的是固定候选预算下的**可见性**问题。长时间原地修复会产生大量近期检查点，破坏性操作之前的状态即使还在轨迹里，也可能排在最近窗口之外。

Replay 已经在模型调用边界录制可恢复的检查点。混合策略给这些点加索引并重新选择要展示的候选。实际回退仍由 agent 选择 `record_uid` 后提交，运行时按该点恢复工作区与上下文。

## 1. 从已落盘轨迹识别工作轮次

一轮由一次 LLM 调用和它发出的普通工具调用组成。索引从上次读取位置继续扫描 trace JSONL，只读完整写入的记录。运行时通常在下一次实时 LLM 调用前封口上一轮。查询候选时也会读取新增记录，只有预期的普通工具调用都已有结果，才封口尚未完成的轮次。回退查询和提交工具本身排除在轮次证据之外。

封口时逐条检查工具记录的 `metadata.filesystem.workspaces`。只要一条记录的 Git `before_tree` 与 `after_tree` 不同，就将整轮入队一次。即使后一个工具撤销了前一个工具的文件修改，前一个工具记录仍会触发判定。队列项是 `(record_uid, round_card, evidence)`。

| 队列字段 | 内容 |
|---|---|
| `record_uid` | 发出工具动作的 LLM 检查点。回退到它会重新生成这次模型决策。 |
| `round_card` | 模型决策、工具名与参数和结果、变更文件摘要、Git tree 变化，以及是否属于受保护变更。 |
| `evidence` | 前面未改变工作区的轮次留下的最近最多 6 条工具结果。读文件、测试和验收可以提供不同证据，即使文件状态相同。下一次写入轮入队后清空这批证据。 |

[识别工作轮次与入队](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L214) · [运行时挂钩](../tools/run_mettle_paper_recovery.py#L34)

## 2. 在后台按子目标分段

异步 worker 顺序处理队列，以保持分段随轨迹推进。每次使用任务配置的模型端点发起独立请求，输入是当前段的目标和进度、本轮摘要以及近期证据。工具结果以截断后的摘要提供。判定提示要求将输入文本当作证据，并输出 JSON，其中包含 `decision`、`goal`、`progress` 和 `reason`。

- `same_goal` 表示当前轮仍在推进同一个直接子目标，更新现有分段的末尾、目标描述和进度。
- `new_goal` 表示目标或正在验证的方案有实质变化，以本轮检查点开启新段。
- 判定超时、异常或输出无效时，登记一个没有语义描述的单点段。单次判定的超时为 10 秒。

判定缓存以提示词、判定输入和 Git tree 变化的摘要为键，落盘到 `semantic-candidate-index.jsonl`。另一条分支重放到相同前缀时，可以复用输入相同的判定。模型负责划分子目标，agent 仍根据自己的失败证据决定是否回退。

[判定提示词](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L16) · [缓存与分段更新](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L275)

## 3. 在固定预算内组合候选

`backtrack_candidates` 查询时，混合策略最多等待后台索引约 2 秒，然后从 Replay 取出符合条件的历史点，按下列顺序挑选。

1. 最近 8 个检查点，覆盖刚发生的决策。
2. 受保护的变更点和失败编辑点。受保护变更由 Git 删除、重命名或覆盖式写入启发式识别。
3. 语义段的起点，以及已入队但尚未判定的写入点。
4. 各语义段的末尾。

每组内从近到远，按 `record_uid` 去重，达到总预算后截断，再按时间顺序返回。有分段描述的候选附带 `goal`、`progress` 和位置。预算是返回数量的上限，有效的优先候选可以少于预算。如果还没有任何成功判定，直接返回同一预算下的最近窗口。运行时记录最终实际展示的候选列表，供后续选点和审计使用。

以 Age 为例，模型要重新执行的是迁移命令发出前的决策。最近窗口在长时间修复后看不到该点。混合策略在 20、40 和 80 三档预算下都展示了迁移前的 `rec_000031`，模型选中后完成恢复，并通过官方验收。

[排序与回落实现](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L325) · [查询接入](../tools/run_mettle_paper_recovery.py#L51)

## 4. 实验结果与审计

| 候选预算 | 案例 | 最近窗口 | 混合策略 |
|---|---|---:|---:|
| 20 | Bat、Ctags、Age | 0/3 | 3/3 |
| 40 | Bat、Ctags、Age | 0/3 | 3/3 |
| 80 | Bat、Age | 1/2 | 2/2 |

每题每种配置从各自相同的归档失败现场运行一次，由官方 verifier 验收。Ctags 使用其归档的模型默认设置。预设目标为 Bat 的 `rec_000055` 和 `rec_000041`（20/40）、`rec_000041`（80），Ctags 的 `rec_000042` 和 `rec_000040`，以及 Age 的 `rec_000031`。例如，Bat 在两档小窗口都回退到预设集合之外的 `rec_000053` 并通过，说明候选可见、实际选点和最终验收需要分别记录。

`semantic-candidate-events.jsonl` 记录查询返回的 ID、返回字节数、查询等待、判定事件和可用的 token 用量。后台模型有额外开销，缺失的用量需要单独计数。这些定向结果说明该策略在所测失败现场有用。总体通过率需要在更广的样本上评估。 [逐臂结果](candidate-results.json) · [指标实现](../bench_tools/jiuwenswarm_runtime/semantic_candidates.py#L157)

## 5. 复现配对实验

按[配置指南](setup.md)准备 MettleBench、runtime、Docker 和模型环境。为混合策略安装一个新运行目录。要复用已有失败现场，本地归档需要备齐 manifest、trace、快照库、工作区和 scratch 归档。仓库提供结果表和 runner，运行时需自行提供失败现场归档。

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

每个预算都会运行最近窗口和混合策略。启动器校验配对环境，为每个实验臂写一份含验收结果和检索指标的 JSON。需要沿用归档模型请求设置时使用 `--archived-model-defaults`。换用其他失败现场时，`--target` 填写该归档中的检查点 ID。
