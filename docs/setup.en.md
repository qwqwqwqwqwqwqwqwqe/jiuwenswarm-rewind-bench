# Setup and reproduction from a fresh clone

[中文](setup.md) · [Home](../README.en.md)

Run on Linux x86-64 or inside WSL2. Install Python 3.12, Conda, Git, curl and Docker. In WSL, keep the checkout on the Linux filesystem so symlinks, executable bits and nested Git metadata are preserved.

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

Replace the values with your OpenAI-compatible Chat Completions endpoint. Use an address reachable from the container. Keep credentials in the ignored `local/` directory.

## Prepare the agent runtime

Install a complete Linux Python prefix. The controller copies it into a separate experiment directory and mounts it into the agent container. The agent runtime, host evaluator Python and task image's Python are separate.

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

Installation needs access to Python package indexes and the Git dependencies under `gitcode.com/openJiuwen` declared by the wheels. The bundled installer uses `--no-deps` and is suitable when preparing a copy of an already provisioned compatible runtime. Use the assembled release for installation. The [source snapshot](jiuwenswarm.en.md) is for review.

## SWE-bench Verified

Use the [official harness](https://github.com/SWE-bench/SWE-bench) and the [Verified dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified). The following example runs three strategies on one task.

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

The task image is named `swebench/sweb.eval.x86_64.<image-instance-id>:latest`, with the first `__` in the instance ID replaced by `_1776_`. The harness prepares its evaluation images. Match the task JSONL, image and evaluator dataset. Multiple case IDs may be comma-separated and must appear in `--cases-file`.

## MettleBench

The [MettleBench repository](https://github.com/Kelvin-Coffee/MettleBench) provides task definitions and verifiers, and its [dataset](https://huggingface.co/datasets/KelvinCoffee/MettleBench) provides workspaces. Build the shared experiment image and download the input task trees.

```bash
docker build -f bench_tools/mettlebench/Dockerfile -t jws-mettle:20261123-resume-fix .
export JWS_METTLE_IMAGE=jws-mettle:20261123-resume-fix
python3.12 tools/prepare_mettlebench.py
python3.12 -m bench_tools.mettlebench.runner \
  --root "$PWD/local/mettle-pilot" --runtime-source "$JWS_RUNTIME_SOURCE" \
  --env-file "$JWS_MODEL_ENV_FILE"
```

The default preparation samples three tasks by structural complexity. Use `tools/prepare_mettlebench.py --task TASK_ID` to download a particular workspace. Historical preflight failures identified four missing packages: torch, openpyxl, pandas, and scipy. Build an image with task-specific dependencies if needed, use `JWS_METTLE_IMAGE` to select it, and record its version.

```bash
JWS_METTLE_ROOT="$PWD/local/mettle-pilot" python3.12 tools/run_mettle_full.py --workers 2
export JWS_METTLE_ROOT="$PWD/local/mettle-paired"
python3.12 tools/run_mettle_paper_recovery.py --install
python3.12 tools/run_mettle_paper_recovery.py
```

Use `--resume /path/to/batch` to resume. Use `--restart-from /path/to/validated-batch` to add Restart arms to validated pairs. Results, submissions and traces are stored in the experiment root's `results/`.

```bash
python -m pytest -q tests --ignore=tests/test_paper_recovery_runtime.py bench_tools/mettlebench/test_core.py
```
