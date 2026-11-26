"""net 包：闭卷网络（防作弊）通用组件。见 egress.py 模块 docstring。"""

from .egress import (  # noqa: F401
    EgressError,
    EgressHandle,
    allowed_from_env,
    create_egress,
    endpoint_path_from_env,
    host_gateway,
)
