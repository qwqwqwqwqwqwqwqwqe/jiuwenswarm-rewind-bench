"""Probe the project interpreter before describing its tools to the agent."""
from __future__ import annotations

import json
from pathlib import Path

from bench_tools.jiuwenswarm_runtime.layout import atomic_write_json, run_command

PROJECT_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"

# This code runs under historical project Pythons, including Python 3.6.
_PROBE = r'''
import json,os,pathlib,shutil,sys
root=pathlib.Path('/testbed')
native=('django' if (root/'django').is_dir() and (root/'tests/runtests.py').is_file()
        else 'sympy' if (root/'sympy').is_dir() and (root/'bin/test').is_file() else None)
try:
    import pytest
    pytest_info={'available':True,'version':pytest.__version__,'module':pytest.__file__,'error':None}
except Exception as e:
    pytest_info={'available':False,'version':None,'module':None,'error':type(e).__name__+': '+str(e)}
print(json.dumps({'python':sys.executable,'python_version':sys.version.split()[0],
    'bare_python':shutil.which('python'),'native_runner':native,'pytest':pytest_info,
    'cwd':os.getcwd()}))
'''


def probe_test_environment(container_name: str) -> dict:
    result = run_command(["docker", "exec", "-w", "/testbed", container_name,
                          PROJECT_PYTHON, "-B", "-c", _PROBE], timeout=120)
    if result.returncode:
        raise RuntimeError(f"项目 Python 环境探测失败 ({PROJECT_PYTHON}): {(result.stderr or result.stdout).strip()}")
    try:
        # Dependency imports may print banners; the probe's final line is JSON.
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("项目环境探测未返回有效 JSON") from exc


def record_test_environment(container_name: str, evidence_dir: Path) -> dict:
    """Record read-only observations; never install or change container dependencies."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    environment = probe_test_environment(container_name)
    atomic_write_json(evidence_dir / "test-environment.json", environment)
    return environment


def describe_test_environment(environment: dict | None) -> str:
    if environment is None:
        return "环境尚未探测。先验证项目 Python 和仓库测试入口；不要假设 pytest 已安装。"
    python = environment["python"]
    lines = ["启动时实测的项目测试环境：",
             f"- 项目 Python：{python}（{environment['python_version']}）。",
             f"- 裸 python 当前指向：{environment.get('bare_python') or '未找到'}；项目命令使用上述绝对路径。"]
    pytest = environment["pytest"]
    if pytest["available"]:
        lines.append(f"- pytest 可导入，版本 {pytest['version']}；调用方式：{python} -m pytest。")
    else:
        lines.append(f"- pytest 不可用（实测：{pytest['error']}）。不要尝试 pytest 或 python -m pytest，也不要安装；按仓库文档使用可用的测试入口。")
    native = environment.get("native_runner")
    if native == "django":
        lines.extend([f"- Django 原生测试：cd /testbed/tests && {python} /testbed/tests/runtests.py <test_label> --parallel 1",
                      f"- Django 帮助：cd /testbed/tests && {python} /testbed/tests/runtests.py --help"])
    elif native == "sympy":
        lines.extend([f"- SymPy 原生测试：cd /testbed && {python} /testbed/bin/test <test_file_or_directory>",
                      f"- SymPy 帮助：cd /testbed && {python} /testbed/bin/test --help"])
    else:
        lines.append("- 结合 CONTRIBUTING、测试配置和项目文档选择具体测试；工具可导入不代表项目测试必然通过。")
    return "\n".join(lines)
