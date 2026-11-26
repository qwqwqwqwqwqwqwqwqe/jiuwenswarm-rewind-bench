"""Host-side runtime preparation and model configuration, shared by both benchmarks."""
import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .layout import resolve_layout
from .ledger import atomic

ROOT = Path(__file__).resolve().parents[2]

def setup(args):
    root=Path(args.root).resolve()
    if root in {Path('/'),Path.home(),Path('/opt/miniconda3')}:raise ValueError('Unsafe experimental root')
    root.mkdir(parents=True,exist_ok=True)
    if not (root/'runtime').exists():
        if shutil.disk_usage(ROOT).free<8*1024**3:
            raise RuntimeError('Runtime clone refused: workspace volume has less than 8 GiB free; check the host volume backing WSL VHD before copying runtimes.')
        subprocess.run(['cp','-a','--reflink=auto',args.runtime_source,str(root/'runtime')],check=True)
    from .install_runtime import install
    install(root/'runtime')
    layout=resolve_layout(root)
    for path in (layout.state,layout.shared,layout.results):path.mkdir(parents=True,exist_ok=True)
    atomic(layout.state/'prepare.json',{'status':'prepared','source':'feedback-runner','isolated':True})
    return layout


def _load_ai_env(path: Path | None, names: tuple[str, ...]) -> dict[str, str]:
    if path is None:
        return {}
    path = path.expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"AI 环境变量文件不是普通文件: {path}")
    wanted = set(names)
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in wanted:
            continue
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"AI 环境变量文件第 {number} 行格式无效: {path}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _ai_values(args: argparse.Namespace) -> tuple[str, str, str]:
    names = (args.base_url_env, args.model_name_env, args.api_key_env)
    file_values = _load_ai_env(args.env_file, names)
    values = [file_values.get(name, os.environ.get(name)) for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ValueError("运行 agent 所需 AI 环境变量未设置: " + ", ".join(missing))
    return values[0], values[1], values[2]
