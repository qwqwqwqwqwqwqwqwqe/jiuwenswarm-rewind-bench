"""Verify the installed distributions, wheel integrity and actual imported source."""
from pathlib import Path
import hashlib
import importlib
from importlib import metadata
import json
import sys

ROOT = Path(__file__).resolve().parent


def main():
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Linux Python 3.12 in the Agent runtime, not Windows or the evaluator-only environment")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    hashes = json.loads((ROOT / "source-sha256.json").read_text(encoding="utf-8"))
    for item in manifest["wheels"]:
        wheel = ROOT / item["file"]
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == item["sha256"], "Wheel checksum mismatch"
        actual = metadata.version(item["name"])
        assert actual == item["version"], f"Wrong distribution {item['name']}: {actual}"
        print(item["name"], actual)
    for module, source in (
        ("jiuwenswarm.rewind.worker", "jiuwenswarm/jiuwenswarm/rewind/worker.py"),
        ("jiuwenswarm.rewind.boundaries", "jiuwenswarm/jiuwenswarm/rewind/boundaries.py"),
        ("jiuwenswarm.rewind.memory", "jiuwenswarm/jiuwenswarm/rewind/memory.py"),
        ("jiuwenswarm.rewind.code_policy", "jiuwenswarm/jiuwenswarm/rewind/code_policy.py"),
        ("jiuwenswarm.rewind.model_retry", "jiuwenswarm/jiuwenswarm/rewind/model_retry.py"),
        ("jiuwenswarm.rewind.bench_config", "jiuwenswarm/jiuwenswarm/rewind/bench_config.py"),
        ("jiuwenswarm.rewind.storage", "jiuwenswarm/jiuwenswarm/rewind/storage.py"),
        ("jiuwenswarm.rewind.native", "jiuwenswarm/jiuwenswarm/rewind/native.py"),
        ("jiuwenswarm.rewind.attempt", "jiuwenswarm/jiuwenswarm/rewind/attempt.py"),
        ("jiuwenswarm.rewind.chat", "jiuwenswarm/jiuwenswarm/rewind/chat.py"),
        ("openjiuwen.core.execution.scope", "agent-core/openjiuwen/core/execution/scope.py"),
        ("openjiuwen.core.foundation.llm.model", "agent-core/openjiuwen/core/foundation/llm/model.py"),
        ("openjiuwen.core.single_agent.ability_manager", "agent-core/openjiuwen/core/single_agent/ability_manager.py"),
        ("replay.context", "replay-agent-recorder/replay/context.py"),
        ("replay.path_normalization", "replay-agent-recorder/replay/path_normalization.py"),
    ):
        path = Path(importlib.import_module(module).__file__)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hashes[source], f"Wrong imported code: {path}"
        print(module, path)
    from openjiuwen.core.execution import ExecutionScope
    from openjiuwen.core.foundation.llm import Model
    from jiuwenswarm.rewind.native import prepare_config
    import tempfile
    with tempfile.TemporaryDirectory() as name:
        prepare_config(Path(name), api_mode="responses")
    print("PASS: paired versions, actual imports, source hashes and native config resources")


if __name__ == "__main__":
    main()
