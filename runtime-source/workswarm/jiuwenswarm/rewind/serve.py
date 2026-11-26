# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Launch the normal Web application with an opt-in isolated recovery backend."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--port", type=int, default=3100)
    args = parser.parse_args()
    from jiuwenswarm.rewind.contracts import Manifest

    manifest_path = args.manifest.resolve(strict=True)
    manifest = Manifest.load(manifest_path)
    if len(manifest.tasks) != 1 or not manifest.image:
        parser.error("Web recovery requires one repository and a pinned task image")
    data = args.data_dir.resolve()
    os.environ.update(
        JIUWENSWARM_DATA_DIR=str(data), JIUWENSWARM_REWIND_WEB_MANIFEST=str(manifest_path),
        JIUWENSWARM_REWIND_WEB_QUEUE=str(args.queue.resolve()),
        OPENJIUWEN_RUNTIME_LOG_DIR=str(data / "runtime-logs"), MEMORY_ENGINE="none",
        FRONTEND_PORT=str(args.port), FRONTEND_HOST="0.0.0.0",
        WEB_PORT=str(args.port + 1), WEB_HOST="0.0.0.0", GATEWAY_HOST="0.0.0.0",
        GATEWAY_PORT=str(args.port + 2), AGENT_SERVER_PORT=str(args.port + 3),
        AGENT_PORT=str(args.port + 3), AGENT_SERVER_HOST="127.0.0.1",
        JIUWENSWARM_FRONTEND_PORT=str(args.port), JIUWENSWARM_WEB_PORT=str(args.port + 1),
        JIUWENSWARM_GATEWAY_PORT=str(args.port + 2), JIUWENSWARM_AGENT_SERVER_PORT=str(args.port + 3),
    )
    if not (data / "config/config.yaml").exists():
        from jiuwenswarm.rewind.native import prepare_config

        prepare_config(data, api_mode=manifest.api_mode)
    from jiuwenswarm.start_services import main as start

    sys.argv = ["jiuwenswarm-start", "all"]
    start()


if __name__ == "__main__":
    main()
