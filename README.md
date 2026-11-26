# JiuwenSwarm Rewind Bench

本项目将官方验收反馈接入 JiuwenSwarm，评估智能体失败后继续修复（Continue）、带反馈重启（Restart）和回到历史检查点（Rewind）的表现。复用 AgentRewind 的检查点与工具，在同一个评测驱动中记录验收、工具调用、实际恢复与 token 用量。

## 目录

```text
bench_tools/
  jiuwenswarm_runtime/  通用运行时覆盖、反馈协议、恢复记忆和重放
  swebench_runner/     SWE-bench 容器适配、Git 补丁生成与官方验收
  mettlebench/         MettleBench 完整工作区提交、验收与失败终点归档
  batch/ 和 net/       闭卷网络与模型出口管理
tools/                 数据准备、MettleBench 批跑及恢复实验入口
tests/                 离线协议与运行时回归测试
docs/                  实验口径与脱敏结果
```

两套评测共享 JiuwenSwarm 集成层，使用不同的提交机制。SWE-bench 将 `/testbed` 的 Git 补丁交给官方 harness。MettleBench 在独立验收环境检查完整工作区，包括任务需要的文件、数据和 Git 状态。草稿路径 `/bench-scratch` 位于评测工作区之外。配对实验保留失败终点的 scratch 备份，并核验两臂的上下文和文件状态。

## 环境

在 Linux 或 WSL2 运行，准备 Python 3.12、Docker、Git、curl 和可供容器访问的模型端点。JiuwenSwarm runtime 与 `jiuwenswarm-rewind-20261112-173855.zip` 需要通过原有分发渠道取得。安装脚本会校验该 ZIP 的 SHA-256。所有运行产物、数据和本地配置可放在忽略的 `local/` 或 `vendor/` 中。

```bash
mkdir -p local
export JWS_REWIND_ARCHIVE="$PWD/local/jiuwenswarm-rewind-20261112-173855.zip"
export JWS_RUNTIME_SOURCE=/path/to/prepared/jiuwenswarm/runtime
export JWS_MODEL_ENV_FILE="$PWD/local/model.env"
```

在 `local/model.env` 中配置 `JWS_BASE_URL`、`JWS_MODEL_NAME` 和 `API_KEY`，每行 `变量=值`。准备后的 runtime 应包含 Python 3.12、JiuwenSwarm 及对应的 Rewind 依赖。SWE-bench 的 Docker 镜像、JSONL 数据和本机安装的 `swebench` harness 按所用数据集准备。实验用的部分 MettleBench 任务也依赖特定系统包或 Python 包，应在镜像构建阶段补齐并预检。

### SWE-bench

SWE-bench 适配层需要已有的 BenchRunner backend checkout 作为容器驱动依赖。将 `--backend-root` 指向包含 `runners/agents/_jiuwen_driver.py`、`llm_forward/`、`utils/` 和 `schemas/` 的目录。`--select` 传入以逗号分隔的 case ID，且须在 `--cases-file` 中。以下为三臂运行示例。

```bash
python -m bench_tools.swebench_runner.controller \
  --root "$PWD/local/swe-run" --runtime-source "$JWS_RUNTIME_SOURCE" \
  --backend-root /path/to/BenchRunner/benchrunner-backend \
  --dataset /path/to/swebench_samples.jsonl --env-file "$JWS_MODEL_ENV_FILE" \
  --output-root "$PWD/local/swe-results" \
  --cases-file bench_tools/swebench_runner/cases.txt --select django__django-11477 \
  --strategies continue,restart,rewind
```

评测已有补丁也可运行 `python -m bench_tools.swebench_runner.evaluate_patch --help`。SWE-bench 实验入口需要可以启动任务容器与官方评分镜像的 Docker daemon。

### MettleBench

```bash
docker build -t jws-mettle:20261123-resume-fix -f bench_tools/mettlebench/Dockerfile .
python tools/prepare_mettlebench.py
python -m bench_tools.mettlebench.runner \
  --root "$PWD/local/mettle-pilot" --runtime-source "$JWS_RUNTIME_SOURCE" \
  --env-file "$JWS_MODEL_ENV_FILE"
```

数据准备脚本拉取 MettleBench 公开任务，并用固定种子选择三个结构复杂度样本。完整批跑可复用试运行生成的 runtime，通过 `JWS_METTLE_ROOT="$PWD/local/mettle-pilot" python tools/run_mettle_full.py` 启动。工具从容器外下载工作区，评测时把公开验收器与 agent 容器隔离。

失败终点配对实验使用单独的 runtime。先运行 `python tools/run_mettle_paper_recovery.py --install`，再运行 `python tools/run_mettle_paper_recovery.py`。只有 Continue 自然达到连续五次相同的官方失败，才进入配对恢复。Continue 与 Rewind 从同一失败终点开始，重置失败计数。可用 `--resume` 延续原批次，或用 `--restart-from` 从任务初始状态补做带相同失败反馈的 Restart 对照。

## 实验结果

MettleBench 的 76 个可比任务中，Continue、Restart、Rewind 分别通过 **71、73、72** 个。Rewind 的未缓存输入加输出 token 总量较 Restart 低约 **22%**。这是单次、跨版本的实验汇总。

同失败终点的 Bat 配对中，Continue 连续五次卡在缓存证据验收。Rewind 自主回到删除原始输入前，第一次新提交即通过，同时使用了更多未缓存输入与输出 token。后续两次重复中，Bat 的 Restart 与 Rewind 均通过；Age 的两次 Restart 通过，两次 Rewind 均失败。[实验口径与结果](docs/experiments.md)

## 验证与来源

```bash
python -m pytest -q tests --ignore=tests/test_paper_recovery_runtime.py bench_tools/mettlebench/test_core.py
```

`tests/test_paper_recovery_runtime.py` 是容器内集成测试，需要独立 runtime、无外网的测试容器和文件系统权限。测试使用本地假模型及验收服务。原始的 JiuwenSwarm wheel、BenchRunner backend、数据集、验收器、模型凭据与原始轨迹均作为外部依赖或本地产物管理。任务与验收器由各基准项目提供。[SWE-bench](https://github.com/SWE-bench/SWE-bench) · [MettleBench](https://github.com/Kelvin-Coffee/MettleBench) · [AgentRewind](https://arxiv.org/abs/2608.14380)
