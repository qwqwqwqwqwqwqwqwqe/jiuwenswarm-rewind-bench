"""Compare candidate policies on a locally archived MettleBench failure endpoint."""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

from run_mettle_paper_recovery import BASE, ROOT, fetch_workspace, resolve_layout, runner
from bench_tools.batch import _closed_book
from bench_tools.jiuwenswarm_runtime.ledger import atomic
from bench_tools.jiuwenswarm_runtime.semantic_candidates import comparison_metrics


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Archived run with endpoint/manifest.json")
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=os.environ.get("JWS_MODEL_ENV_FILE"))
    parser.add_argument("--limit", type=int, choices=(20, 40, 80), action="append", required=True)
    parser.add_argument("--target", action="append", default=[], help="Expected LLM record UID")
    parser.add_argument("--archived-model-defaults", action="store_true")
    options = parser.parse_args()
    if not options.env_file:
        parser.error("Supply --env-file or set JWS_MODEL_ENV_FILE")
    layout = resolve_layout(BASE)
    overlay = layout.runtime / "paper-recovery-overlay.json"
    if not overlay.is_file() or not read_json(overlay).get("semantic_candidates"):
        parser.error("Install the paper runtime with --install --semantic-candidates")
    rows = read_json(ROOT / "local/mettlebench/sampling.json")["population"]
    row = next((row for row in rows if row["task_id"] == options.task), None)
    if row is None:
        parser.error("Task is missing from local/mettlebench/sampling.json")
    source = options.source.resolve(strict=True)
    seed = read_json(source / "endpoint/manifest.json")["seed"]
    fetch_workspace(row)
    output = options.output.resolve()
    output.mkdir(parents=True)
    env_file = options.env_file.resolve(strict=True)
    handle = _closed_book.setup_batch_egress(env_file, output)
    reference = None
    try:
        for limit in dict.fromkeys(options.limit):
            for policy in ("recent", "semantic"):
                arm = output / f"{policy}-{limit}"
                args = SimpleNamespace(
                    repo=str(ROOT / "vendor/MettleBench"), output_root=str(arm), isolated=True,
                    env_file=env_file, candidate_policy=policy, candidate_limit=limit,
                    recovery_trial={"seed": seed, "source": str(source),
                                    "archived_model_defaults": options.archived_model_defaults},
                )
                summary = runner.episode(layout, args, row, "rewind", handle.endpoint_url, handle.network)
                episode = arm / options.task / "rewind"
                audit = episode / "ipc/paired-environment-audit.json"
                environment = read_json(audit) if audit.is_file() else None
                verified = environment is not None and (reference is None or environment == reference)
                metrics = comparison_metrics(episode, summary, options.target)
                atomic(output / f"{policy}-{limit}.json",
                       {"verified_start": verified, "result": summary, "candidate_metrics": metrics})
                if not verified or summary["state"] not in {"RESOLVED", "UNRESOLVED", "BUDGET_EXHAUSTED"}:
                    raise RuntimeError(f"{policy}-{limit} did not complete from a verified start")
                reference = environment
    finally:
        _closed_book.teardown_batch_egress(handle)


if __name__ == "__main__":
    main()
