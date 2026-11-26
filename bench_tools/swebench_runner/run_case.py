#!/usr/bin/env python3
"""Run the agent stage for one SWE-bench case in an isolated container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from bench_tools.jiuwenswarm_runtime.layout import (  # noqa: E402
    atomic_write_json,
    docker_name,
    read_json,
    redact,
    resolve_layout,
    run_command,
    shell_quote,
    utc_now,
)
from ._git_history import prune_git_future_history  # noqa: E402
from .git_patch import capture_git_patch, snapshot_git_workspace  # noqa: E402
from ._test_environment import describe_test_environment, record_test_environment  # noqa: E402

SWEBENCH_USER_PROMPT = """
You are given a code repository in the current directory (/testbed).
The bug description is:
{description}
=================================================
Environment rules (must follow):
1. 项目根目录是 /testbed。项目代码操作限定在该目录树内；允许按测试入口要求切换到 /testbed/tests 等子目录。
2. 使用下方实测的项目 Python 绝对路径，不要使用 Agent runtime Python 或裸 python 执行项目测试。
3. 镜像中的测试工具因项目而异；缺少 pytest 不代表镜像有问题。容器执行阶段禁止使用 pip/conda 安装、卸载或升级依赖；runner 和 agent 均不得补装。
4. 测试入口以仓库自身 runner、配置和文档为准。遇到环境错误，记录具体命令和错误；语法检查不等于测试通过。
{test_environment}
=================================================
Your task is to fix the bug with the following steps:
(1) Read the repository's testing instructions and use the available project test entry point. If a command is unavailable, inspect the repository for an existing alternative; do not install dependencies or treat the command failure as evidence that the code is wrong.
(2) Reproduce the reported behavior. Check that the reproduction's expected result follows from the task and repository contract; distinguish a product defect from an incorrect test, invocation, or environment problem.
(3) Trace the behavior through its callers and shared implementation before choosing where to edit. If a local workaround fixes one caller, check whether the same invalid state can still reach other callers. Use observations to compare these explanations.
(4) Make a repair supported by that evidence. Do not modify unrelated project behavior merely to make a self-authored test pass.
(5) After the reproduction passes, validate relevant existing tests and additional boundary or error cases justified by the repository contract, shared implementation, and its callers. A passing reproduction or a small selected test group alone does not establish that the bug is fully fixed. Choose checks within the remaining budget and state any coverage gaps.
(6) If new evidence contradicts the repair, continue diagnosis instead of finishing. Reconsider the repair location and assumptions. When backtrack tools are available, use them if an earlier decision needs to be replaced and the observed evidence meets their review requirements; changing an unavailable test command can be done in place without rolling back correct edits. Repeated errors alone are not evidence for a rewind.
(7) Remove temporary reproduction tests you added, preserve the repository's existing tests, and review the final diff. Report which commands actually ran, their results, and remaining uncertainty; do not equate syntax checks or environment failures with passing tests.
Please do not commit your edits. We will do it later.
"""

# team 模式（--mode code.team）追加在 SWEBENCH_USER_PROMPT 之后的独立模板；
# 两种模式共用同一份环境探测和基础提示词，team 模式额外追加上限。
# agent 数上限是提示词软约束（leader 是 LLM，无机制强制），并同值记入
# params.team_max_agents 供平台侧未来做硬控。
SWEBENCH_TEAM_CONSTRAINT_PROMPT = (
    "\nTeam constraint: run in team mode with at most {max_agents} agents "
    "concurrent (including the team leader).\n"
)

# 触发提示词追加的 mode 集合：镜像 jiuwenswarm mode_matrix 的 TEAM_CANONICAL_MODES
# 中「raw 串以 code 开头 → tui 通道 → code profile」的 team 形态（当前仅 code.team；
# driver 的通道推导见 benchrunner-backend/runners/agents/_jiuwen_driver.py）。
# 扩 team 模式支持时在此加一行。
_TEAM_MODES = frozenset({"code.team"})

DEFAULT_MAX_AGENTS = 3

VALIDATION_GATE_INSTRUCTIONS = """
Mandatory evidence workflow (runtime-enforced):
- After reading repository evidence, declare a validation_plan using real inspection tool IDs, the intended execution preconditions, and expected correct behavior.
- Before repair, use validation_probe(phase='reproduce') with project Python code that sets facts to real observed JSON values. A command running successfully does not establish reproduction; intended preconditions must match and a declared behavior defect must be observed.
- After repair, use validation_probe(phase='verify') and validation_test with the existing project test runner argv. Repeat verification after any later code or test changes, including cleanup.
- A precondition mismatch means the probe reached the wrong object or execution path. Investigate and correct the reproduction; do not relabel output as expected behavior without repository-grounded evidence.
- Completion is checked against actual recorded evidence and current code state. If blocked, continue this task and address the reported gaps. Do not install dependencies or use hidden evaluator tests.
"""


def _positive_int(value: str) -> int:
    """argparse type：正整数（--max-agents 下限校验）。"""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要整数，得到 {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"需要 >= 1，得到 {number}")
    return number

# 与原 JiuwenSwarm SWE-bench runner 的任务边界保持一致。
# 这两个片段会作为独立 driver 的 system_prompt 前缀发送；driver 会在其后
# 加入分隔符和 SWEBENCH_USER_PROMPT。不要把 API 配置或运行时密钥放入这里。
SWEBENCH_SYSTEM_PROMPT = "You are an expert software engineer solving swebench bug fixing tasks."
NON_INTERACTIVE_INSTRUCTIONS = """\
This task is running in an unattended benchmark. User interaction is unavailable.
Do not ask the user questions. Resolve ambiguity from the task, repository, tests,
and data; choose the most reasonable deterministic default, continue the task, and
document any material assumption in the final response.
"""

# 原 runner 将 system prompt、非交互约束和任务 prompt 作为同一条 chat 文本
# 按此顺序拼接。保留在 runner 侧，确保 params.json 中有可审计的明确输入来源。
SWE_SYSTEM_PROMPT = "\n\n---\n\n".join(
    (SWEBENCH_SYSTEM_PROMPT.strip(), NON_INTERACTIVE_INSTRUCTIONS.strip())
)

# 容器内 runtime 挂载点：不再覆盖镜像自带 /opt/miniconda3（保留 testbed 环境可用，
# 提示词里的 /opt/miniconda3/envs/testbed/bin/python 因此真实可达）。与 rust 管线
# 挂 /opt/jiuwen-swarm 的思路一致。同一 runtime 另按宿主同路径再挂一份，供 pip 安装的
# console script（jiuwenswarm-agentserver 的 shebang 写死宿主 runtime 路径）解析。
_CONTAINER_RUNTIME = "/opt/jws_runtime"
_CONTAINER_PYTHON = f"{_CONTAINER_RUNTIME}/bin/python3"
_CONTAINER_AGENTSERVER_BIN = f"{_CONTAINER_RUNTIME}/bin/jiuwenswarm-agentserver"


_PATCH_EXCLUDE_PATHS = (
    "logs/deployment_service.log",
    "logs/logs/**",
    "memory.db",
    "memory.db-shm",
    "memory.db-wal",
    "transcript.jsonl",
    "todo/**/todo.json",
    "context/**",
    "coding_memory/**",
    ".agent_history/**",
    "agent_memory.db",
)

_PATCH_GITIGNORE_STRIP_MARKERS = (
    "# JiuwenSwarm runtime file operation logs",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the agent stage for one SWE-bench case.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True, help="SWE-bench JSONL dataset")
    parser.add_argument("--case-id", required=True, help="SWE-bench instance_id")
    parser.add_argument("--image", required=True, help="SWE-bench Docker image")
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=None, help="AI dotenv 文件")
    parser.add_argument("--base-url-env", default="JWS_BASE_URL")
    parser.add_argument("--model-name-env", default="JWS_MODEL_NAME")
    parser.add_argument("--api-key-env", default="API_KEY")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--jiuwen-port", type=int, default=28092, help="测试容器内 AgentServer 端口")
    parser.add_argument("--mode", default="code.normal")
    parser.add_argument(
        "--max-agents",
        type=_positive_int,
        default=DEFAULT_MAX_AGENTS,
        help="team 模式（code.team）最大 agent 数：写入提示词并记 params.team_max_agents",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=None,
        help=(
            "扩展提示词模板文件（显式指定才生效）：文档中的字面量 {tasks} 会被替换为"
            "默认提示词（已替换 {description}），生成新提示词执行；不传则零改动"
        ),
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--preflight-only", action="store_true", help="只创建、检查并删除容器")
    parser.add_argument("--validation-gate", action=argparse.BooleanOptionalAction, default=True,
                        help="启用运行时复现与完成校验；--no-validation-gate 可用于对照")
    # M3 closed 透传（open 批不传，全部默认 None=no-op；批层保证成对下发）：
    parser.add_argument(
        "--egress-endpoint-url", default=None,
        help="closed 专用：egress endpoint_url，整串替换注入的 BASE_URL",
    )
    parser.add_argument(
        "--egress-network", default=None,
        help="closed 专用：agent 容器 docker run 追加 --network <该 internal 网>",
    )
    parser.add_argument(
        "--egress-allowed", default=None,
        help="closed 专用：逗号分隔 host:port 白名单（V1 探测端口数/network 记录用）",
    )
    return parser


# ---------------------------------------------------------------------------
# M3 closed：V1 容器内探针（py/rust 两线共用本函数，rust 经 _standalone 引用）
# ---------------------------------------------------------------------------

EXIT_EGRESS_V1_FAILED = 86


class EgressV1Failure(RuntimeError):
    """closed V1 容器内探针失败：case 以哨兵码 86 终止，不消耗模型预算。"""


def _egress_constants():
    """懒加载 M2 组件常量（仅 closed 分支调用；open 路径连 import 都不发生）。"""
    net_dir = _SCRIPT_DIR.parent / "net"
    if str(net_dir) not in sys.path:
        sys.path.insert(0, str(net_dir))
    import egress  # noqa: PLC0415
    return egress


def closed_v1_probe(container_name: str, endpoint_url: str, allowed: list[str],
                    *, timeout: int = 120) -> dict:
    """V1：在 worker 容器内实测（设计稿 §7 V1，判据在网络命名空间层面成立）。

    两条断言，全部经 `docker exec <container> bash -lc` 在容器 netns 内执行，
    只用 bash /dev/tcp + coreutils timeout（sweb.eval 镜像自带，无需额外工具）：
    - allowed_ok：对 egress 转发器的每个监听端口（endpoint 主机 :8080+i，i 按
      白名单条数）送 PROBE_MAGIC，收到 OK* 回包 → 转发整链可达；
    - external_blocked：EXTERNAL_PROBE_DOMAINS × {443,80} 的 TCP 全部连不通。
    返回 {"allowed_ok","external_blocked","at","detail"}，语义与 M2 probe 对齐，
    但探测发起点是真实 agent 容器（比 M2 临时探针容器更强：连容器挂载/入网
    路径一起验了）。输出不含任何凭据。
    """
    egress = _egress_constants()
    parsed = urlparse(endpoint_url)
    host, base_port = parsed.hostname, parsed.port or 8080
    count = max(1, len([item for item in allowed if item.strip()]))
    magic = egress.PROBE_MAGIC.decode("ascii")
    rendered: list[str] = []
    for index in range(count):
        port = base_port + index
        rendered.append(
            "r=$( timeout 8 bash -c 'exec 3<>/dev/tcp/{host}/{port} && "
            "printf %s {magic} >&3 && head -c 16 <&3' 2>/dev/null ); "
            'case "$r" in OK*) echo "ALLOW {port} ok";; *) echo "ALLOW {port} fail";; esac'.format(
                host=host, port=port, magic=magic)
        )
    for domain in egress.EXTERNAL_PROBE_DOMAINS:
        for port in egress.EXTERNAL_PROBE_PORTS:
            rendered.append(
                'if timeout 3 bash -c "exec 3<>/dev/tcp/{domain}/{port}" '
                '>/dev/null 2>&1; then echo "EXT {domain}:{port} open"; '
                'else echo "EXT {domain}:{port} blocked"; fi'.format(domain=domain, port=port)
            )
    script = "\n".join(rendered)
    result = run_command(["docker", "exec", container_name, "bash", "-lc", script],
                         timeout=timeout)
    stdout = result.stdout or ""
    allow_lines = [line for line in stdout.splitlines() if line.startswith("ALLOW ")]
    ext_lines = [line for line in stdout.splitlines() if line.startswith("EXT ")]
    expected_ext = len(egress.EXTERNAL_PROBE_DOMAINS) * len(egress.EXTERNAL_PROBE_PORTS)
    allowed_ok = len(allow_lines) == count and all(line.endswith(" ok") for line in allow_lines)
    external_blocked = len(ext_lines) == expected_ext and all(
        line.endswith("blocked") for line in ext_lines
    )
    return {
        "allowed_ok": allowed_ok,
        "external_blocked": external_blocked,
        "at": utc_now(),
        "detail": {
            "allow": allow_lines,
            "ext": ext_lines,
            "rc": result.returncode,
            "stderr": (result.stderr or "").strip()[:200],
        },
    }


def _validate_egress_args(args: argparse.Namespace) -> None:
    """closed 参数自洽校验（成对 + http:// 前缀），不合规 SystemExit（fail-closed）。"""
    endpoint_url = getattr(args, "egress_endpoint_url", None)
    network = getattr(args, "egress_network", None)
    if not endpoint_url and not network:
        return
    if endpoint_url and not network:
        raise SystemExit("ERROR: --egress-endpoint-url 必须与 --egress-network 成对出现（closed 透传断裂？）")
    if network and not endpoint_url:
        raise SystemExit("ERROR: --egress-network 必须与 --egress-endpoint-url 成对出现（closed 透传断裂？）")
    if not str(endpoint_url).startswith("http://"):
        raise SystemExit(
            f"ERROR: --egress-endpoint-url 必须是 http:// 的 egress 端点（得到 {endpoint_url!r}）；"
            "https 目标不经闭卷 TCP 转发，拒绝静默降级"
        )


def load_case(dataset: Path, case_id: str) -> dict:
    dataset = dataset.expanduser().absolute()
    if not dataset.is_file():
        raise ValueError(f"dataset 不是普通文件: {dataset}")
    found: dict | None = None
    seen: set[str] = set()
    with dataset.open("r", encoding="utf-8") as stream:
        for number, raw_line in enumerate(stream, 1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"dataset 第 {number} 行不是有效 JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"dataset 第 {number} 行不是 JSON object")
            instance_id = value.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError(f"dataset 第 {number} 行缺少有效 instance_id")
            if instance_id in seen:
                raise ValueError(f"dataset 存在重复 instance_id: {instance_id}")
            seen.add(instance_id)
            if instance_id == case_id:
                found = value
    if found is None:
        raise ValueError(f"dataset 中找不到 case_id: {case_id}")
    if not isinstance(found.get("problem_statement"), str):
        raise ValueError(f"case 缺少 problem_statement: {case_id}")
    return found


def render_swe_prompt(problem_statement: str, max_agents: int | None = None, *, test_environment: dict | None = None,
                      validation_gate: bool = False) -> str:
    """渲染 SWE 任务提示词。

    使用当次容器探测结果描述环境；team 模式另追加上限。
    """
    prompt = SWEBENCH_USER_PROMPT.format(description=problem_statement.replace('"""', "'''"),
                                       test_environment=describe_test_environment(test_environment))
    if max_agents is not None:
        prompt += SWEBENCH_TEAM_CONSTRAINT_PROMPT.format(max_agents=max_agents)
    if validation_gate:
        prompt += VALIDATION_GATE_INSTRUCTIONS
    return prompt


def apply_prompt_template(prompt: str, template_path: Path | None) -> tuple[str, dict]:
    """显式指定模板文件时的扩展提示词包装。

    模板文档中以字面量 ``{tasks}`` 占位，按 ``template.replace("{tasks}", prompt)``
    整体替换（不用 str.format，避免模板内其他花括号报错）。prompt 应为已完成
    ``{description}`` 替换（含 team 约束，如适用）后的默认提示词。

    未指定模板 → 原样返回、零改动（默认关闭）。
    """
    if template_path is None:
        return prompt, {}
    path = Path(template_path).expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"提示词模板文件不存在或不是普通文件: {path}")
    template = path.read_text(encoding="utf-8")
    if "{tasks}" not in template:
        raise ValueError(f"提示词模板缺少 {{tasks}} 占位符: {path}")
    trace = {
        "path": str(path),
        "sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
    }
    return template.replace("{tasks}", prompt), trace


from bench_tools.jiuwenswarm_runtime.host import _ai_values


def _state_path(layout, run_id: str) -> Path:
    return layout.state / f"run-{run_id}.json"


def write_run_state(layout, run_id: str, state: dict) -> None:
    atomic_write_json(_state_path(layout, run_id), state)


def _runner_log_path(layout, run_id: str) -> Path:
    path = layout.shared / "driver" / run_id / "runner.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log(log_path: Path, phase: str, message: str) -> None:
    line = f"[{utc_now()}] [{phase}] {message}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()


def _wait_result(result_path: Path, timeout: int, log_path: Path) -> dict:
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    next_heartbeat = started + 30
    _log(log_path, "agent", f"waiting for result timeout={timeout}s path={result_path}")
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                value = read_json(result_path)
            except (OSError, ValueError, json.JSONDecodeError):
                time.sleep(1)
                continue
            elapsed = int(time.monotonic() - started)
            _log(log_path, "agent", f"result received elapsed={elapsed}s")
            return value
        now = time.monotonic()
        if now >= next_heartbeat:
            elapsed = int(now - started)
            remaining = max(0, int(deadline - now))
            _log(
                log_path,
                "agent",
                f"still waiting elapsed={elapsed}s remaining={remaining}s",
            )
            next_heartbeat += 30
        time.sleep(1)
    raise TimeoutError(f"等待 agent result 超时: {result_path}")


def _validate_backend(backend_root: Path) -> Path:
    backend_root = backend_root.expanduser().absolute()
    if not backend_root.is_dir():
        raise ValueError(f"backend root 不是目录: {backend_root}")
    driver = backend_root / "runners" / "agents" / "_jiuwen_driver.py"
    for path in (
        driver,
        backend_root / "llm_forward",
        backend_root / "utils",
        backend_root / "schemas",
    ):
        if not path.exists():
            raise ValueError(f"backend root 缺少 driver 依赖: {path}")
    return backend_root


def start_agent_container(
    layout,
    *,
    image: str,
    container_name: str,
    backend_root: Path,
    jiuwen_port: int,
    validation_gate: bool = False,
    network: str | None = None,
) -> None:
    prepare_state = read_json(layout.state / "prepare.json")
    if prepare_state.get("status") != "prepared":
        raise ValueError("prepare.json 状态不是 prepared，请先执行 prepare_runtime.py")
    runtime_log_dir = layout.shared / "log" / container_name / "runtime"
    runtime_log_dir.mkdir(parents=True, exist_ok=True)
    runtime_log_dir.chmod(0o777)
    command = [
        "docker", "run", "-d", "--name", container_name, "--workdir", "/testbed",
    ]
    if network:  # M3 closed：挂 internal 闭卷网；open（None）时命令逐字节不变
        command += ["--network", network]
    command += [
        "-e", f"JIUWENSWARM_PORT={jiuwen_port}",
        "-e", f"AGENT_SERVER_PORT={jiuwen_port}",
        "-e", f"OPENJIUWEN_RUNTIME_LOG_DIR=/shared/log/{container_name}/runtime",
        "-v", f"{layout.runtime}:{_CONTAINER_RUNTIME}:ro",
        # shebang 同路径挂载：console script 的 #! 指向宿主 runtime 绝对路径，
        # 容器内也需要在该路径访问 runtime。
        "-v", f"{layout.runtime}:{layout.runtime}:ro",
        "-v", f"{layout.shared}:/shared:rw",
        "-v", f"{backend_root / 'runners'}:/benchrunner-backend/runners:ro",
        "-v", f"{backend_root / 'llm_forward'}:/benchrunner-backend/llm_forward:ro",
        "-v", f"{backend_root / 'utils'}:/benchrunner-backend/utils:ro",
        "-v", f"{backend_root / 'schemas'}:/benchrunner-backend/schemas:ro",
        image, "/bin/sleep", "infinity",
    ]
    if validation_gate:
        command[2:2] = ["-e", "JWS_REPRODUCTION_GATE=1"]
    result = run_command(command, timeout=300)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker run failed").strip()
        raise RuntimeError(f"启动 agent 容器失败: {detail}")


def preflight_container(container_name: str) -> None:
    checks = [
        [_CONTAINER_PYTHON, "-c", "import sys; print(sys.executable)"],
        [_CONTAINER_PYTHON, "-c", "import jiuwenswarm; print(jiuwenswarm.__file__)"],
        ["/bin/sh", "-c", "test -d /testbed && test -d /shared"],
    ]
    for command in checks:
        result = run_command(["docker", "exec", container_name, *command], timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "container preflight failed").strip()
            raise RuntimeError(f"容器预检失败: {detail}")


def _driver_paths(layout, run_id: str) -> tuple[Path, Path, Path, Path, Path, Path]:
    driver_root = layout.shared / "driver" / run_id
    driver_root.mkdir(parents=True, exist_ok=True)
    host_params = driver_root / "params.json"
    host_result = driver_root / "result.json"
    host_log = driver_root / "driver.log"
    relative_root = driver_root.relative_to(layout.shared)
    container_root = Path("/shared") / relative_root
    return (
        host_params,
        host_result,
        host_log,
        container_root / host_params.name,
        container_root / host_result.name,
        container_root / host_log.name,
    )


def _start_driver(
    container_name: str,
    container_backend_root: str,
    container_params_path: Path,
    container_log_path: Path,
) -> None:
    container_driver = Path(container_backend_root) / "runners" / "agents" / "_jiuwen_driver.py"
    command = (
        "exec " + shell_quote(_CONTAINER_PYTHON) + " "
        + shell_quote(str(container_driver)) + " " + shell_quote(str(container_params_path))
        + " >" + shell_quote(str(container_log_path)) + " 2>&1"
    )
    result = run_command(["docker", "exec", "-d", container_name, "bash", "-lc", command], timeout=120)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "driver start failed").strip()
        raise RuntimeError(f"启动 JiuwenSwarm driver 失败: {detail}")


def _capture_patch(
    container_name: str,
    result_dir: Path,
    *,
    baseline_tree: str,
) -> tuple[str, str]:
    capture = capture_git_patch(
        container_name,
        "/testbed",
        timeout=300,
        exclude_paths=_PATCH_EXCLUDE_PATHS,
        baseline_tree=baseline_tree,
        gitignore_strip_markers=_PATCH_GITIGNORE_STRIP_MARKERS,
    )
    if capture.error:
        raise RuntimeError(f"提取 git patch 失败: {capture.error}")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "git-status.txt").write_text(capture.status, encoding="utf-8")
    (result_dir / "patch.diff").write_text(capture.patch, encoding="utf-8")
    return str(result_dir / "patch.diff"), str(result_dir / "git-status.txt")


def remove_container(container_name: str) -> None:
    run_command(["docker", "rm", "-f", container_name], timeout=120)


def _relax_log_permissions(
    container_name: str,
    case_id: str,
    host_log_dir: Path,
    runner_log_path: Path,
) -> None:
    """放开共享日志目录读权限，保证 host 侧归档不被容器内 root 属主挡住。

    team 模式的 .agent_teams/<队>/workspaces/<成员>_workspace 由容器内进程
    创建，归档时刻可能对 host 不可读（E6 冒烟 5/5 复现）。三层降级：
    1) 容器存活：容器内 root 执行 chmod（最彻底，本函数在 remove 之前调用）；
    2) 容器已死：探测 sudo -n 可用则 host 侧 chmod（root 可改任意属主）；
    3) 均不可用：放弃，由 single_case 归档容错跳过兜底。
    任何失败只记日志绝不抛出——本函数在 finally 清理路径上调用。
    """
    container_log_dir = f"/shared/log/{case_id}"
    try:
        result = run_command(
            ["docker", "exec", container_name, "chmod", "-R", "a+rX", container_log_dir],
            timeout=120,
        )
        if result.returncode == 0:
            _log(runner_log_path, "cleanup", f"relaxed log permissions in container ({container_log_dir})")
            return
        _log(runner_log_path, "cleanup",
             f"in-container chmod failed rc={result.returncode} (container may have exited); trying host fallback")
    except Exception as exc:
        _log(runner_log_path, "cleanup", f"in-container chmod unavailable: {exc}; trying host fallback")
    if not host_log_dir.is_dir():
        return
    try:
        probe = run_command(["sudo", "-n", "true"], timeout=15)
        if probe.returncode != 0:
            _log(runner_log_path, "cleanup",
                 "passwordless sudo unavailable; skip host fallback (archive tolerance will skip unreadable dirs)")
            return
        result = run_command(["sudo", "-n", "chmod", "-R", "a+rX", str(host_log_dir)], timeout=120)
        _log(runner_log_path, "cleanup",
             "host sudo chmod done" if result.returncode == 0
             else f"host sudo chmod failed rc={result.returncode}")
    except Exception as exc:
        _log(runner_log_path, "cleanup", f"host fallback error (give up): {exc}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _validate_egress_args(args)
    layout = resolve_layout(args.root.expanduser().absolute())
    run_id = args.run_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise SystemExit("ERROR: run-id 只允许字母、数字、点、下划线和短横线")
    container_name = docker_name("jws-swe-test", run_id, args.case_id)
    runner_log_path = _runner_log_path(layout, run_id)
    result_dir = layout.results / re.sub(r"[^A-Za-z0-9_.-]+", "-", args.case_id)
    state = {
        "schema_version": 1, "status": "starting", "run_id": run_id,
        "case_id": args.case_id, "image": args.image, "container": container_name,
        "root": str(layout.root), "runtime": str(layout.runtime), "shared": str(layout.shared),
        "dataset": str(args.dataset.expanduser().absolute()),
        "backend_root": str(args.backend_root.expanduser().absolute()),
        "result_dir": str(result_dir), "runner_log": str(runner_log_path), "host_pids": [],
        "network": {"name": None, "owned": False}, "started_at": utc_now(),
    }
    if args.egress_endpoint_url:  # M3 closed：state 里落 network 段（供 single_case V3 汇总）
        state["network"] = {
            "name": args.egress_network,
            "owned": False,  # 网络归批级 egress 句柄所有，run_case 不建不删
            "policy": "closed_book_v2",
            "endpoint_url": args.egress_endpoint_url,
            "allowed": [item.strip() for item in (args.egress_allowed or "").split(",") if item.strip()],
        }
    write_run_state(layout, run_id, state)
    _log(runner_log_path, "run", f"started case={args.case_id} run_id={run_id}")
    try:
        _log(runner_log_path, "case", f"loading dataset={args.dataset}")
        case = load_case(args.dataset, args.case_id)
        _log(runner_log_path, "case", "loaded")
        backend_root = _validate_backend(args.backend_root)
        base_url, model_name, api_key = _ai_values(args)
        if args.egress_endpoint_url:
            # M3 closed：注入链唯一咽喉收口——params.base_url（容器内 llm_forward
            # --base-url 的来源）整串换成 egress endpoint；key/model 不动。
            # JWS_BASE_URL/JIUWEN_MODEL_URL/OPENAI_BASE_URL 三个 env 名都由该值
            # 派生，故整串替换即全量覆盖。
            _log(runner_log_path, "egress",
                 f"closed: BASE_URL 整串替换为 egress endpoint（原 {urlparse(base_url).netloc} "
                 f"→ {urlparse(args.egress_endpoint_url).netloc}），network={args.egress_network}")
            base_url = args.egress_endpoint_url
        # team 模式才把上限写进提示词；一次渲染三处共用（长度统计 + prompt/user_prompt）。
        prompt_max_agents = args.max_agents if args.mode in _TEAM_MODES else None
        user_prompt = render_swe_prompt(case["problem_statement"], prompt_max_agents)
        user_prompt, prompt_template = apply_prompt_template(user_prompt, args.prompt_template)
        state["prompt_length"] = len(user_prompt)
        if prompt_template:
            state["prompt_template"] = prompt_template
        _log(runner_log_path, "container", f"starting image={args.image} name={container_name}")
        start_agent_container(
            layout,
            image=args.image,
            container_name=container_name,
            backend_root=backend_root,
            jiuwen_port=args.jiuwen_port,
            validation_gate=args.validation_gate,
            network=args.egress_network,
        )
        state.update({"status": "running", "container_started_at": utc_now()})
        write_run_state(layout, run_id, state)
        _log(runner_log_path, "container", "started")
        _log(runner_log_path, "preflight", "starting")
        preflight_container(container_name)
        test_environment = record_test_environment(container_name, result_dir)
        prompt_max_agents = args.max_agents if args.mode in _TEAM_MODES else None
        user_prompt = render_swe_prompt(case["problem_statement"], prompt_max_agents,
                                       test_environment=test_environment, validation_gate=args.validation_gate)
        user_prompt, prompt_template = apply_prompt_template(user_prompt, args.prompt_template)
        prompt_hash = hashlib.sha256((SWE_SYSTEM_PROMPT + "\n\n---\n\n" + user_prompt).encode("utf-8")).hexdigest()
        prompt_version = "swe-validation-gate-v4" if args.validation_gate else "swe-agent-validation-v3"
        state.update(prompt_length=len(user_prompt), prompt_version=prompt_version, validation_gate=args.validation_gate,
                     prompt_sha256=prompt_hash, test_environment=str(result_dir / "test-environment.json"))
        if prompt_template:
            state["prompt_template"] = prompt_template
        (result_dir / "effective-user-prompt.txt").write_text(user_prompt, encoding="utf-8")
        (result_dir / "effective-system-prefix.txt").write_text(SWE_SYSTEM_PROMPT, encoding="utf-8")
        state.update({"status": "preflighted", "preflight_at": utc_now()})
        write_run_state(layout, run_id, state)
        _log(runner_log_path, "preflight", "passed")
        if args.egress_endpoint_url:
            # M3 V1：agent 阶段之前、在本 case 的 worker 容器内实测闭卷网络。
            # 放在 --preflight-only 短路之前：preflight-only+egress 参数即成为
            # 零模型请求的 egress 验证模式（M4 冒烟预检可直接用）。
            _log(runner_log_path, "egress", "V1 in-container probe starting")
            probe = closed_v1_probe(
                container_name, args.egress_endpoint_url,
                state["network"].get("allowed") or [],
            )
            state["network"]["v1"] = probe
            write_run_state(layout, run_id, state)
            _log(runner_log_path, "egress",
                 f"V1 allowed_ok={probe['allowed_ok']} "
                 f"external_blocked={probe['external_blocked']}")
            if not (probe["allowed_ok"] and probe["external_blocked"]):
                raise EgressV1Failure(
                    "closed V1 容器内探针未通过（fail-closed，本 case 立即终止、"
                    "不消耗模型预算、不写 manifest 供 resume 重派发）: "
                    + json.dumps(probe["detail"], ensure_ascii=False)[:600]
                )
        if args.preflight_only:
            remove_container(container_name)
            state.update({"status": "preflighted_clean", "container_removed_at": utc_now()})
            write_run_state(layout, run_id, state)
            print(f"status: {state['status']}")
            print(f"case: {args.case_id}")
            print(f"container: {container_name}")
            print(f"state: {_state_path(layout, run_id)}")
            return 0

        _log(runner_log_path, "workspace", "pruning git future history")
        prune = prune_git_future_history(container_name, "/testbed", timeout=300)
        if prune.error:
            raise RuntimeError(f"剪枝 workspace git future history 失败: {prune.error}")
        if prune.skipped:
            _log(runner_log_path, "workspace", "git future history prune skipped: not a git repository")
        else:
            _log(runner_log_path, "workspace", "git future history pruned")

        _log(runner_log_path, "workspace", "capturing git baseline")
        baseline = snapshot_git_workspace(container_name, "/testbed", timeout=300)
        if baseline.error:
            raise RuntimeError(f"捕获 workspace baseline 失败: {baseline.error}")
        state.update({
            "status": "workspace_baselined",
            "workspace_baseline_tree": baseline.tree,
            "workspace_baseline_at": utc_now(),
        })
        write_run_state(layout, run_id, state)
        _log(runner_log_path, "workspace", f"baseline captured tree={baseline.tree}")

        (
            params_path,
            result_path,
            log_path,
            container_params_path,
            container_result_path,
            container_log_path,
        ) = _driver_paths(layout, run_id)
        params = {
            "system_prompt": SWE_SYSTEM_PROMPT,
            "prompt": user_prompt,
            "user_prompt": user_prompt,
            "prompt_version": prompt_version, "prompt_sha256": prompt_hash, "validation_gate": args.validation_gate,
            "test_environment": test_environment,
            "session_id": f"jws-swe-{run_id}", "workspace": "/testbed",
            "timeout": args.timeout, "mode": args.mode,
            # 恒记录 CLI 值（含非 team 模式），供审计与平台侧未来硬控钩子；
            # server 当前忽略未知 params 键。
            "team_max_agents": args.max_agents,
            "base_url": base_url, "api_key": api_key, "model_name": model_name,
            "backend_root": "/benchrunner-backend",
            "result_file": str(container_result_path),
            "instance_id": args.case_id,
            "jiuwen_port": args.jiuwen_port,
            "llm_forward_port": 8000,
            "container_python": _CONTAINER_PYTHON,
            "container_agentserver_bin": _CONTAINER_AGENTSERVER_BIN,
            "server_startup_timeout": 360,
        }
        atomic_write_json(params_path, params)
        state.update({"status": "agent_starting", "params": str(params_path), "result": str(result_path), "driver_log": str(log_path)})
        write_run_state(layout, run_id, state)
        _log(runner_log_path, "driver", f"starting timeout={args.timeout}s port={args.jiuwen_port}")
        _start_driver(
            container_name,
            "/benchrunner-backend",
            container_params_path,
            container_log_path,
        )
        state.update({"status": "agent_running", "driver_started_at": utc_now()})
        write_run_state(layout, run_id, state)
        _log(runner_log_path, "driver", "started; waiting for agent result")
        result = _wait_result(result_path, args.timeout + 600, runner_log_path)
        _log(runner_log_path, "result", "received; saving result")
        result_path_host = result_dir / "agent-result.json"
        atomic_write_json(result_path_host, result)
        if log_path.is_file():
            (result_dir / "driver.log").write_text(
                log_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        _log(runner_log_path, "patch", "capturing git status and diff")
        patch_path, status_path = _capture_patch(
            container_name,
            result_dir,
            baseline_tree=baseline.tree,
        )
        _log(runner_log_path, "patch", f"saved patch={patch_path}")
        agent_completed = result.get("completed") is True
        state.update({
            "status": "agent_completed" if agent_completed else "agent_failed",
            "result_file": str(result_path_host),
            "patch": patch_path,
            "git_status": status_path,
            "completed_at": utc_now(),
            "agent_error": result.get("error"),
        })
        write_run_state(layout, run_id, state)
        print(f"status: {state['status']}")
        print(f"case: {args.case_id}")
        print(f"result: {result_path_host}")
        print(f"patch: {patch_path}")
        print(f"state: {_state_path(layout, run_id)}")
        if not agent_completed:
            print(f"driver-log: {result_dir / 'driver.log'}")
            print(f"agentserver-log-dir: {layout.shared / 'log' / args.case_id}")
        return 0
    except EgressV1Failure as exc:
        # M3 V1 哨兵路径：状态记 egress_v1_failed、rc=86（single_case 不写 manifest，
        # resume 视为未跑重派发）。通用 except 之前拦截，避免混入普通 agent_failed。
        _log(runner_log_path, "egress", str(exc))
        state.update({"status": "egress_v1_failed", "error": str(exc), "failed_at": utc_now()})
        write_run_state(layout, run_id, state)
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_EGRESS_V1_FAILED
    except Exception as exc:
        secret_values = tuple(
            value for value in (locals().get("api_key"),) if isinstance(value, str) and value
        )
        safe_error = redact(exc, secret_values)
        _log(runner_log_path, "error", safe_error)
        state.update({"status": "failed", "error": safe_error, "failed_at": utc_now()})
        write_run_state(layout, run_id, state)
        print(f"ERROR: {safe_error}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_container and state.get("container"):
            try:
                _relax_log_permissions(
                    container_name, args.case_id,
                    layout.shared / "log" / args.case_id, runner_log_path,
                )
            except Exception as exc:  # 双保险：清理路径上的意外绝不阻断收尾
                _log(runner_log_path, "cleanup", f"permission relax error (cleanup continues): {exc}")
            _log(runner_log_path, "cleanup", f"removing container={container_name}")
            remove_container(container_name)
            if state.get("status") in {"agent_completed", "failed", "egress_v1_failed"}:
                state["container_removed_at"] = utc_now()
                write_run_state(layout, run_id, state)
            _log(runner_log_path, "cleanup", "container removed")


if __name__ == "__main__":
    raise SystemExit(main())
