#!/usr/bin/env python3
"""Run the original JiuwenSwarm driver without persisting the API key in params."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path


_ORIGINAL_DRIVER = Path("/benchrunner-backend/runners/agents/_jiuwen_driver.py")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: driver_wrapper.py <params.json>")
    params_path = Path(sys.argv[1])
    with params_path.open("r", encoding="utf-8") as stream:
        params = json.load(stream)
    if not isinstance(params, dict):
        raise ValueError("driver params 必须是 JSON object")
    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise RuntimeError("容器环境缺少 API_KEY")
    params["api_key"] = api_key
    namespace = runpy.run_path(str(_ORIGINAL_DRIVER), run_name="_jws_original_driver")
    namespace["run_driver"](params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
