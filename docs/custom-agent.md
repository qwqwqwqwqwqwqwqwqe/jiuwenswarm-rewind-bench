# 接入自己的 agent

可以先用同一验收器评测任意 agent 产物，再接入在线反馈。当前的 JiuwenSwarm 控制器使用该框架自己的 Request 和工具接口。修改 `JWS_MODEL_NAME` 将更换其模型，替换 agent 要实现启动适配层。

## 先跑离线验收

让自己的 agent 在同一 SWE-bench 任务镜像和初始提交工作，生成 Git 补丁，临时探针放在提交范围外。

```bash
python -m bench_tools.swebench_runner.evaluate_patch \
  --patch local/patch.diff --instance-id django__django-11477 --output-dir local/custom-swe-eval
```

MettleBench 在原始工作区副本上运行 agent，保留任务所需链接、权限与 Git 元数据，然后提交整棵树。运行命令前按[环境文档](setup.md)构建镜像并准备对应数据。

```bash
python tools/prepare_mettlebench.py --task programbench_bat_cache_handoff
python - <<'PY'
from bench_tools.mettlebench.core import evaluate
feedback, _ = evaluate('vendor/MettleBench', 'programbench_bat_cache_handoff',
                       'local/my-agent-workspace', 'local/custom-mettle-eval')
print(feedback)
PY
```

## 再接入在线反馈和恢复

1. 给 agent 提供 submission 工具。赋予提交 ID，在捕获 Git diff 或工作区时暂停并发修改。
2. 在 agent 外调用验收器并返回 `status`、`resolved`、`failure_id` 和首个失败诊断。官方验收器在 agent 的隔离边界之外运行。
3. Continue 保留会话与环境。Restart 重建会话与初始环境，同时携带失败反馈。各臂使用相同计数口径和终止条件。
4. Rewind 额外要求 agent 能序列化模型上下文，并在决策前创建对齐的工作区快照。实现候选查询、旧尝试收尾、还原、失败经验注入及审计。能从已记录的工具返回重放历史时，恢复不再重复那些工具操作。

可复用 [提交账本](../bench_tools/jiuwenswarm_runtime/ledger.py)、[SWE-bench 桥接](../bench_tools/swebench_runner/evaluator_bridge.py) 和 [MettleBench 验收](../bench_tools/mettlebench/core.py)。[JiuwenSwarm 工具接线](../bench_tools/jiuwenswarm_runtime/runtime_tools.py) 可作为实现参考。先用假模型验证隔离、计数归零和恢复，再用真实任务做小规模比较。
