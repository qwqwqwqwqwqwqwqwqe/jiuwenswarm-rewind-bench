#!/usr/bin/env python3
"""Evaluate an existing SWE-bench patch with the installed harness."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO


DEFAULT_DATASET_NAME = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_MODEL_NAME = "jiuwenswarm_test"
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
HF_HUB_ENABLE_HF_TRANSFER = "0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a generated patch.diff with SWE-bench."
    )
    parser.add_argument("--patch", required=True, help="Input patch.diff path")
    parser.add_argument("--instance-id", required=True, help="SWE-bench instance ID")
    parser.add_argument(
        "--workdir",
        default=".",
        help="Base directory for resolving a relative patch path (default: .)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for pred.json, logs, and evaluator output (default: .)",
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_DATASET_NAME,
        help=f"SWE-bench dataset name (default: {DEFAULT_DATASET_NAME})",
    )
    parser.add_argument(
        "--split",
        default=DEFAULT_SPLIT,
        help=f"Dataset split (default: {DEFAULT_SPLIT})",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum evaluator workers (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Per-instance evaluator timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help=f"Prediction model name (default: {DEFAULT_MODEL_NAME})",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Evaluator run ID; generated when omitted",
    )
    return parser


def default_run_id(instance_id: str, now: datetime | None = None) -> str:
    """Build the default evaluator run ID for an instance."""
    timestamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"{instance_id}_run_{timestamp}"


def resolve_patch_path(workdir: Path, patch_arg: str) -> Path:
    """Resolve a patch path relative to the requested Linux work directory."""
    patch_path = Path(patch_arg).expanduser()
    if not patch_path.is_absolute():
        patch_path = workdir / patch_path
    return patch_path.resolve()


def build_prediction(
    instance_id: str,
    patch_text: str,
    model_name: str,
) -> list[dict[str, str]]:
    """Build the single prediction record expected by SWE-bench."""
    return [
        {
            "instance_id": instance_id,
            "model_patch": patch_text,
            "model_name_or_path": model_name,
        }
    ]


def write_prediction(path: Path, prediction: list[dict[str, str]]) -> None:
    """Write a prediction JSON file without changing the patch text."""
    with path.open("w", encoding="utf-8") as stream:
        json.dump(prediction, stream, indent=2)
        stream.write("\n")


def build_evaluator_command(
    args: argparse.Namespace,
    predictions_path: Path,
    run_id: str,
) -> list[str]:
    """Build the native SWE-bench evaluator command."""
    return [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        args.dataset_name,
        "--split",
        args.split,
        "--instance_ids",
        args.instance_id,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(args.max_workers),
        "--timeout",
        str(args.timeout),
        "--run_id",
        run_id,
    ]


def _validate_run_id(run_id: str) -> str:
    if not run_id.strip():
        raise ValueError("run-id 不能为空")
    if run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise ValueError("run-id 不能包含路径分隔符")
    return run_id


def _hf_dataset_cached(dataset_name: str) -> bool:
    """检测数据集本地缓存命中（datasets 两级布局，任一命中即认为可用）。

    单3：命中 → HF_HUB_OFFLINE=1，跳过 hf-mirror HEAD 探活（镜像超时 5 连
    指数退避 ≈30-50s/case 纯死等，2026-10-23 数据已缓存时零收益）；未命中 →
    不设，正常 online 拉取（换新数据集首跑不被 offline 卡死，自适应不硬编码）。

    布局：hub 层 datasets--<org>--<name>（纯机械变换）；processed 层
    <org>___<name>（datasets 库对 name 做小写化，实测
    SWE-bench/SWE-bench_Verified → SWE-bench___swe-bench_verified）。
    """
    bases = [Path.home() / ".cache" / "huggingface"]
    for var in ("HF_HOME", "HF_HUB_CACHE", "HF_DATASETS_CACHE"):
        value = os.environ.get(var)
        if value:
            bases.append(Path(value))
    candidates = []
    for base in bases:
        candidates.append(base / "hub" / ("datasets--" + dataset_name.replace("/", "--")))
        candidates.append(base / "datasets" / dataset_name.replace("/", "___"))
        if "/" in dataset_name:
            org, name = dataset_name.split("/", 1)
            candidates.append(base / "datasets" / f"{org}___{name.lower()}")
    for path in candidates:
        if path.is_dir() and any(path.iterdir()):
            return True
    return False


def _write_log_header(
    stream: TextIO,
    *,
    command: list[str],
    cwd: Path,
    instance_id: str,
    predictions_path: Path,
    started_at: datetime,
    hf_offline: bool,
) -> None:
    stream.write(f"started_at: {started_at.isoformat()}\n")
    stream.write(f"instance_id: {instance_id}\n")
    stream.write(f"cwd: {cwd}\n")
    stream.write(f"predictions: {predictions_path}\n")
    stream.write(f"command: {shlex.join(command)}\n")
    stream.write(f"environment: HF_ENDPOINT={HF_ENDPOINT}\n")
    stream.write(
        "environment: HF_HUB_ENABLE_HF_TRANSFER="
        f"{HF_HUB_ENABLE_HF_TRANSFER}\n"
    )
    stream.write(f"environment: HF_HUB_OFFLINE={'1' if hf_offline else '0'}\n")
    stream.write("--- evaluator output ---\n")
    stream.flush()


def _run_evaluator(
    command: list[str],
    *,
    cwd: Path,
    log_stream: TextIO,
    hf_offline: bool,
) -> int:
    env = os.environ.copy()
    env["HF_ENDPOINT"] = HF_ENDPOINT
    env["HF_HUB_ENABLE_HF_TRANSFER"] = HF_HUB_ENABLE_HF_TRANSFER
    if hf_offline:
        # 单3：本地缓存命中，跳过 hf-mirror HEAD 探活直接用缓存
        env["HF_HUB_OFFLINE"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        log_stream.write(line)
        log_stream.flush()
    return process.wait()


def _print_summary(
    *,
    succeeded: bool,
    instance_id: str,
    run_id: str,
    predictions_path: Path,
    log_path: Path,
    output_dir: Path,
    exit_code: int,
) -> None:
    print(f"status: {'succeeded' if succeeded else 'failed'}")
    print(f"instance_id: {instance_id}")
    print(f"run_id: {run_id}")
    print(f"predictions: {predictions_path}")
    print(f"log: {log_path}")
    print(f"output_dir: {output_dir}")
    print(f"exit_code: {exit_code}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.instance_id.strip():
            raise ValueError("instance-id 不能为空")
        if args.max_workers <= 0:
            raise ValueError("max-workers 必须大于 0")
        if args.timeout <= 0:
            raise ValueError("timeout 必须大于 0")

        workdir = Path(args.workdir).expanduser().resolve()
        output_dir = Path(args.output_dir).expanduser().resolve()
        patch_path = resolve_patch_path(workdir, args.patch)
        if not patch_path.is_file():
            raise ValueError(f"patch 不是普通文件: {patch_path}")
        patch_text = patch_path.read_text(encoding="utf-8")

        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = _validate_run_id(args.run_id or default_run_id(args.instance_id))
        predictions_path = output_dir / "pred.json"
        log_path = output_dir / f"{run_id}.log"
        prediction = build_prediction(args.instance_id, patch_text, args.model_name)
        write_prediction(predictions_path, prediction)
        command = build_evaluator_command(args, predictions_path, run_id)
        # 单3：探活自适应——本地缓存命中才离线（未命中走 online，见 _hf_dataset_cached）
        hf_offline = _hf_dataset_cached(args.dataset_name)

        started_at = datetime.now()
        started_monotonic = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log_stream:
            _write_log_header(
                log_stream,
                command=command,
                cwd=output_dir,
                instance_id=args.instance_id,
                predictions_path=predictions_path,
                started_at=started_at,
                hf_offline=hf_offline,
            )
            try:
                exit_code = _run_evaluator(
                    command,
                    cwd=output_dir,
                    log_stream=log_stream,
                    hf_offline=hf_offline,
                )
            except OSError as exc:
                log_stream.write(f"evaluator_start_error: {exc}\n")
                log_stream.flush()
                exit_code = 1
            elapsed = time.monotonic() - started_monotonic
            log_stream.write(f"--- evaluator finished ---\n")
            log_stream.write(f"elapsed_seconds: {elapsed:.3f}\n")
            log_stream.write(f"exit_code: {exit_code}\n")

        process_exit_code = exit_code if 0 <= exit_code <= 255 else 1
        _print_summary(
            succeeded=process_exit_code == 0,
            instance_id=args.instance_id,
            run_id=run_id,
            predictions_path=predictions_path,
            log_path=log_path,
            output_dir=output_dir,
            exit_code=process_exit_code,
        )
        return process_exit_code
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
