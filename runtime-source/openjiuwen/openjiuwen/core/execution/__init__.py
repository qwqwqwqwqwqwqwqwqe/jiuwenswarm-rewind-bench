# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in, task-local execution boundaries for external task runners.

This module deliberately depends only on the standard library. Installing a
scope before creating agent tasks also makes the scope available to those tasks.
"""

from openjiuwen.core.execution.scope import (
    ExecutionControl,
    ExecutionRequest,
    ExecutionScope,
    current_execution_scope,
)

__all__ = ["ExecutionControl", "ExecutionRequest", "ExecutionScope", "current_execution_scope"]
