# 从干净 clone 开始

[English](setup.en.md) · [首页](../README.md)

## 系统与模型

使用 Linux x86-64 或 WSL2。准备 Python 3.12、Conda、Docker、Git、curl 和可供容器访问的 OpenAI 兼容 Chat Completions 服务。WSL 建议 clone 到 Linux 文件系统，以便保留工作区内的执行位、符号链接和 Git 元数据。

```bash
git clone https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench.git
cd jiuwenswarm-rewind-bench
python3.12 --version
docker info
mkdir -p local
umask 077
cat > local/model.env <<'EOF'
JWS_BASE_URL=https://your-provider.example/v1
JWS_MODEL_NAME=your-model-id
API_KEY=replace-with-your-own-key
EOF
export JWS_MODEL_ENV_FILE="$PWD/local/model.env"
```

将示例值替换成自己的配置。Docker 容器中的 localhost 指向容器本身，宿主机服务需使用容器可达地址。`local/` 在 Git 忽略范围内。

## 实验 runtime

用 Conda 准备完整的 Linux Python 3.12 前缀。该前缀会被控制器复制到各自运行根，用于容器内 agent。它与 host 控制器 Python、项目测试镜像内的 Python 是不同环境。

```bash
export JWS_REWIND_ARCHIVE="$PWD/local/jiuwenswarm-rewind-20261112-173855.zip"
curl -fL --retry 3 https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/download/runtime-20261112-173855/jiuwenswarm-rewind-20261112-173855.zip -o "$JWS_REWIND_ARCHIVE"
echo "0361783cef566cdff2fa867f46a553e639731498c0e22466d346e9a6c4c70cbd  $JWS_REWIND_ARCHIVE" | sha256sum -c -
python3.12 -m zipfile -e "$JWS_REWIND_ARCHIVE" local/runtime-bundle
bundle="$PWD/local/runtime-bundle/jiuwenswarm-rewind-20261112-173855"
export JWS_RUNTIME_SOURCE="$PWD/local/agent-runtime"
conda create -y --prefix "$JWS_RUNTIME_SOURCE" python=3.12 pip
"$JWS_RUNTIME_SOURCE/bin/python" -m pip install "$bundle"/wheels/*.whl
"$JWS_RUNTIME_SOURCE/bin/python" "$bundle/verify_install.py"
```

安装依赖时需访问 Python 包源以及 wheel 声明的 `gitcode.com/openJiuwen` Git 依赖。已有依赖齐全的匹配 runtime 时，可在其副本运行包内 `install.py`。它使用 `--no-deps`，仅用于已准备好的前缀。所有模型调用在依赖安装完成后进行。

## SWE-bench Verified

harness 来自 [SWE-bench 官方仓库](https://github.com/SWE-bench/SWE-bench)，输入数据来自 [Verified 数据集](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install swebench datasets pytest
python - <<'PY'
from datasets import load_dataset
load_dataset('SWE-bench/SWE-bench_Verified', split='test').to_json('local/swebench-verified.jsonl')
PY
docker pull --platform linux/amd64 swebench/sweb.eval.x86_64.django_1776_django-11477:latest

python -m bench_tools.swebench_runner.controller \
  --root "$PWD/local/swe-run" --runtime-source "$JWS_RUNTIME_SOURCE" \
  --dataset "$PWD/local/swebench-verified.jsonl" --env-file "$JWS_MODEL_ENV_FILE" \
  --output-root "$PWD/local/swe-results" \
  --cases-file bench_tools/swebench_runner/cases.txt --select django__django-11477 \
  --strategies continue,restart,rewind
```

任务镜像规则为 `swebench/sweb.eval.x86_64.<image-instance-id>:latest`，其中实例 ID 第一个 `__` 换成 `_1776_`。官方 harness 根据实例准备评分镜像。换数据集时，任务数据、镜像与官方评分数据集应一致；手动评分可为 `evaluate_patch` 传 `--dataset-name`。运行多个案例时，`--select` 使用逗号分隔的 ID，全部须出现在 `--cases-file` 中。

## MettleBench

任务定义与验收器来自 [MettleBench](https://github.com/Kelvin-Coffee/MettleBench)，工作区下载自 [公开数据集](https://huggingface.co/datasets/KelvinCoffee/MettleBench)。本项目用 Dockerfile 构建通用镜像，并将工作区注入容器。

```bash
docker build -f bench_tools/mettlebench/Dockerfile -t jws-mettle:20261123-resume-fix .
export JWS_METTLE_IMAGE=jws-mettle:20261123-resume-fix
python3.12 tools/prepare_mettlebench.py
python3.12 -m bench_tools.mettlebench.runner \
  --root "$PWD/local/mettle-pilot" --runtime-source "$JWS_RUNTIME_SOURCE" \
  --env-file "$JWS_MODEL_ENV_FILE"
```

默认准备三个结构复杂度样本，`tools/prepare_mettlebench.py --task TASK_ID` 下载指定工作区。历史镜像有四项预检分别缺 torch、openpyxl、pandas、scipy。要覆盖这些任务，在构建时补齐依赖并通过 `JWS_METTLE_IMAGE` 指向新标签，记录镜像变化。

```bash
# 复用试运行 runtime，执行独立三臂批跑
JWS_METTLE_ROOT="$PWD/local/mettle-pilot" python3.12 tools/run_mettle_full.py --workers 2

# 建立单独的配对恢复 runtime
export JWS_METTLE_ROOT="$PWD/local/mettle-paired"
python3.12 tools/run_mettle_paper_recovery.py --install
python3.12 tools/run_mettle_paper_recovery.py
```

用 `--resume /path/to/batch` 续跑。已完成的配对可用 `--restart-from /path/to/validated-batch` 补跑 Restart。各实验根的 `results/` 保存官方报告、提交和轨迹。先运行离线检查，再执行付费模型评测。

```bash
python -m pytest -q tests --ignore=tests/test_paper_recovery_runtime.py bench_tools/mettlebench/test_core.py
```
