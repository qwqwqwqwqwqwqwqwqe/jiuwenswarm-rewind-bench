# BenchRunner 容器内恢复接入

本入口保留原 driver → AgentServer → chat_send → Runtime 协议。
Runtime 将本次执行交给任务容器内的尝试进程，尝试进程使用原 Agent 工厂。
回退重建尝试进程，既有任务容器、AgentServer、`/testbed` 和官方评分器不重建。
它与旧的 `rewind.bench` 独立 Docker 运行器、Web 宿主队列是不同入口。

## 执行机部署

1. 停止使用待修改 runtime 的任务，备份或复制 Agent runtime；不要修改 runner/eval Python。
2. 使用交付包的 `install.py` 安装三个匹配 wheel，再运行 `verify_install.py`。
   这两个脚本必须由注入任务容器的 Linux Python 3.12 runtime 执行。
3. 应用交付包 `benchrunner-driver.patch`；先用 `git apply --check` 检查版本差异。
   仅更新 `benchrunner-backend/runners/agents/_jiuwen_driver.py`，不替换调度或评分代码。
4. 在该 runtime 根目录（`bin/`、`lib/` 同级）创建 `rewind-bench.json`：

```json
{
  "schema": "jiuwenswarm.rewind.bench.v1",
  "profile": "single-agent-code-v1",
  "mode": "on",
  "token_limit": 2000000,
  "max_rewinds": 3
}
```

`token_limit` 必须显式提供；示例值不是评分预算建议。
预算口径为 `input_tokens - cache_read_tokens + output_tokens`，不再用含缓存的 total_tokens
或对话字节数/最大输出预留拒绝调用。report 的 token_accounting 为 uncached_usage；
usage.tokens 是预算成本，input_tokens/output_tokens/cache_read_tokens/total_tokens 分别保留原始汇总。
每次真实调用成功后累计成本；达到上限后不发起下一次调用。若本次响应使累计用量
超过上限，响应后报告预算耗尽，因此可能超出最后一次调用的额度。
失败/中断而未获得 usage 的调用在 `unsettled_calls` 中标为未知；估算值仅用于诊断，
不作为实际消耗。token 数不等于货币账单，缓存计费折扣仍取决于模型服务。
独立离线旧入口仍保留其 conservative 策略；本文件描述的 chat_send 入口统一使用
uncached_usage，off/record-only/on 一致。旧案例需使用新版本重新运行。
driver 从 `sys.prefix` 读取此文件，把已有剩余超时作为 `remaining_seconds` 传入。
不要在配置中写剩余时间、模型密钥或工作区路径。
文件缺失继续原执行路径；无效文件会报错，不会静默退回旧路径。

5. 继续使用原批跑命令、case 列表和普通代码模式（例如 `code.normal`）。
   runtime 仍按原方式只读挂入 `/opt/jws_runtime`，项目和 testbed Python 不替换。
6. 正式比较使用独立的 off/on 实验 runtime 和输出根。代码版本相同，配置仅 mode 不同。
   `record-only` 用于隔离记录开销。运行中的任务不切换配置或重装 wheel。

## 首期能力边界

只支持新建的单次文本任务、普通单 Agent 模式，driver 的 web/tui 通道。
各实验组统一关闭子 Agent 委派、任务循环、长期记忆、技能演进和后台能力；
工具执行边界仅允许 read_file/write_file/edit_file/glob/grep/list_files/bash。
原模型配置和采样参数保留，模型流完整收集、工具串行执行。
on 额外暴露 backtrack_candidates/backtrack_commit 并附加恢复说明。
因此历史旧配置结果不能直接当作新的配对基线。

只保证受管理工作区内的文件恢复，不撤销外部网络写入、依赖安装或外部服务状态。
工作区内部的相对符号链接按链接保存并恢复，逃逸链接仍由 Replay 校验拒绝。
代码任务应限制在项目编辑与测试；不允许脱离管理的后台工作。

## 验证入口与证据

使用 `tests/unit_tests/rewind/test_bench_chat.py` 的确定性 HTTP 模型用例验证
真实 Runtime、原 Agent 工厂、ReActAgent、bash 工具及 Record/Replay。
夹具模型用于验证实现，不证明生产模型会选择最佳回退时机。

会话用户消息记录一次，中间尝试只输出审计事件，不发送完成信号。
最终回复在尝试进程及工具进程清理后发布，任务成功仍由原 evaluator 判定。

`<JIUWENSWARM_DATA_DIR>/rewind-bench/<task>/` 保存：

- `report.json`：整体终态、累计用量、回退次数、生效配置哈希及版本证据。
- `attempt-N/result.json`、`process.log`：尝试终态与诊断。
- `traces/`、`snapshot-store/`：记录、恢复记忆及工作区快照。
- `usage.jsonl`：跨尝试保留的 API 成本账本；重放不新增实际调用成本。

这些目录位于工作区外，随原 driver 的数据根日志归档。
密钥配置源放在原 config 排除目录；尝试配置副本在退出时移除。
`prefix-mismatch.json` 出现表示严格输入匹配失败，禁止将其当作成功恢复。
取消、超时或清理失败后不得启动新尝试。

## 代码任务恢复策略 v2

本次显式 benchmark 路由使用 `code-recovery-v2`，不再注入作者仓库的通用强制回退提示词。
候选查询是辅助判断，查询后允许继续检查、原地修复或直接完成。新增代码任务工具 schema
要求提交 `review`：`evidence_origin`、`failure_tool_call_id`、
`independent_check_tool_call_id`、`alternative_explanation`、`why_rewind`。
自写或来源不明的测试必须引用失败之后实际发生的独立检查；缺失、伪造 ID、重复原命令、
失败的检查和环境问题会被拒绝，Agent 仍可继续解题。来源与独立性语义由模型解释，
宿主仅核对调用记录、顺序、成功状态与相同命令，不声称识别所有错误断言。

回退次数用完后工具返回不可用说明，不再直接结束任务。恢复记忆单条限制 2000 字符、
总计 6000 字符，只注入一份正文，明确假设可被新证据推翻。用户需求不受笔记覆盖。
`report.json` 新增 `recovery_policy` 和 `recovery_instructions_hash`；
`policy-events.jsonl` 的 `commit_accepted → prefix_*_replayed → target_regenerated`
用于核实实际恢复，拒绝事件为 `commit_rejected`。最终解题质量仍由原评分器判断。

新包须同时安装到 off/on runtime；正式对比不得混用旧版 on 结果。先在独立输出根小批验证，
再按预先固定的任务和预算做配对测评。确定性夹具只证明这些行为，不能保证解决率提升。
原独立离线/Web 路由保留原恢复策略；本节描述的是 BenchRunner chat_send 路由。

## 模型服务饱和与超时

显式 off/record-only/on 统一支持以下可选配置；旧文件无需补字段即可获得默认值：

```json
{
  "model_max_retries": 12,
  "model_call_timeout_seconds": 600,
  "model_retry_initial_delay_seconds": 15,
  "model_retry_max_delay_seconds": 120
}
```

将字段合入原 rewind-bench.json，保留 mode、token_limit 等必需项。max_retries 不含首次。
15 秒起指数退避、上限 120 秒并带抖动，服务端 Retry-After 可要求更长等待，但不能超过
任务剩余时间。该策略用于周期性饱和，仍不能保证跨过文档中的 51 分钟长窗口。

HTTP 429/500/502/503/504、连接/超时及 HTTP-200 SSE 错误帧都在同一模型调用边界内
重试。核心异常链已保留 SDK 原因，直接读取 code/type/body，无需修改 SDK、伪造
APIStatusError 或重启 Agent。失败流的暂存内容不交付；之前的文件修改和工具结果保留。
鉴权/非法请求不重试；调用超时先取消当前调用，再决定是否重发；取消不触发重试。
次数、等待时间、单次超时都受任务原 deadline 和累计预算约束，重放前缀不重发模型。

model-events.jsonl 保存每次请求与退避的安全诊断，不保存请求正文或密钥。
report.model_transport 汇总重试、失败、超时、耗尽及最后终止原因。持续失败仍返回原错误
协议，并带 failure_detail；不能把 provider 恢复当作任务 solved。失败请求没有 usage 时
保留 unsettled_calls，它不是已知免费调用，也不是重复计入会话足迹。

并发批次互相清理工作区/容器属于独立的调度问题。本改动不添加整 case 重跑，不调整
BenchRunner 调度、prepare、patch 提取、评分或 provider 健康判定。正式比较必须两臂
使用相同新版本与传输配置；provider 故障与能力失败分别记录。
