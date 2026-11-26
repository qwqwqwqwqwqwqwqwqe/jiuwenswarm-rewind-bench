#!/usr/bin/env python3
"""闭卷（book_mode=closed）批/单 case 接入层（M3）。

对应任务书 20261112-runner-anti-cheat §二 M3、设计稿《闭卷网络设计 v0.2》§6/§7/§8。
本模块是 run_batch*.py / single_case*.py 消费 M2 组件（bench_tools/net/egress.py）的
唯一入口，把 egress 生命周期、closed 参数透传、V3 审计、manifest/summary 的
``network`` 段收在一处；run_case*.py（容器侧动作）需要的 V1 探针实现放在
run_case.py（py/rust 共用），本模块只留语义说明。

C4（open 分支逐字节不变）如何成立：
- 本模块的所有函数只在 ``book_mode == "closed"`` 或 ``egress_endpoint_url`` 非空的
  代码路径被调用；open 批永远不调用（egress 的 import 也是懒加载，open 路径连
  import 都不发生，更不会有任何 docker/网络动作）。
- ``append_egress_args`` 对未携带 egress 参数的 args 是显式 no-op。

fail-closed 口径（C7）：
- 批级：egress 构建或批级 probe 失败 → 抛 ValueError（run_batch 的既有
  ``except (OSError, ValueError)`` 打印 ERROR 并 rc=1），整批不启动、零预算消耗；
  异常/中断路径 finally 必调 ``teardown_batch_egress``。
- case 级 V1 探针失败 → run_case 以哨兵退出码 ``EXIT_EGRESS_V1_FAILED``(86) 结束，
  single_case 不写 manifest 直接透传 86；rust resume 的扫描把"无 manifest 目录"
  记为 pending、重派发动作前本就先归档旧目录——V1 失败天然可重跑且不进结果。

https 护栏：闭卷 TCP 转发器不解析 TLS，CONNECT 语义不适用；env 里三个
BASE_URL 键（JWS_BASE_URL/JIUWEN_MODEL_URL/OPENAI_BASE_URL）任一为 https scheme
时，批在 egress 建立前直接报错终止（不静默降级为 http 明文转发）。

日志纪律：只记 endpoint 的 host:port/path；key 值永不进入本模块任何输出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

EXIT_EGRESS_V1_FAILED = 86
POLICY = "closed_book_v2"

BASE_URL_KEYS = ("JWS_BASE_URL", "JIUWEN_MODEL_URL", "OPENAI_BASE_URL")

# V3 审计点名工具（设计稿 §6：fetch_webpage 等外网取数工具保留启用、成功计数）。
FETCH_TOOL_NAMES = ("fetch_webpage", "paid_search", "web_search", "web_fetch", "fetch_url")

_NET_DIR = Path(__file__).resolve().parent.parent / "net"


def _load_egress():
    """懒加载 M2 组件（只有 closed 路径会走到，open 批零 import）。"""
    if str(_NET_DIR) not in sys.path:
        sys.path.insert(0, str(_NET_DIR))
    import egress  # noqa: PLC0415
    return egress


# ---------------------------------------------------------------------------
# env 口径检查（https 护栏；自带极简 dotenv 解析，不依赖 M2 私有函数）
# ---------------------------------------------------------------------------

def _parse_env_basic(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def check_base_url_schemes(env_file: Path) -> None:
    """closed 下 env BASE_URL 出现 https scheme → ValueError（fail-fast）。"""
    path = Path(env_file).expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"闭卷检查失败：env 文件不存在 {path}")
    values = _parse_env_basic(path)
    https_keys = []
    for key in BASE_URL_KEYS:
        val = values.get(key)
        if not val:
            continue
        url = val if "://" in val else "http://" + val
        if urlparse(url).scheme == "https":
            https_keys.append(key)
    if https_keys:
        raise ValueError(
            "闭卷模式拒绝 https BASE_URL（键: " + ", ".join(https_keys) +
            "）：egress 转发器是固定目标 TCP 字节拼接，不做 TLS 终结/透传语义，"
            "https 目标需另议（设计稿 §4）。请勿静默改用 http，先解决端点 scheme。"
        )


# ---------------------------------------------------------------------------
# 批级生命周期
# ---------------------------------------------------------------------------

def setup_batch_egress(env_file: Path, batch_log_dir: Path, *, log=print) -> "Any":
    """批级 egress 创建 + 批级 V1 probe（第一个 case 派发之前调用）。

    返回 EgressHandle；任何失败（构建/探测）→ 已自建资源就地 teardown 后抛
    ValueError（run_batch 外层既有处理 = 整批终止、零预算消耗）。
    成功后把 handle.to_record() 落 ``_batch/egress.json``（网络资源审计凭证）。
    """
    egress = _load_egress()
    env_file = Path(env_file).expanduser().absolute()
    check_base_url_schemes(env_file)
    try:
        allowed = egress.allowed_from_env(env_file)
        endpoint_path = egress.endpoint_path_from_env(env_file)
    except egress.EgressError as exc:
        raise ValueError(f"闭卷 allow 目标派生失败，拒绝启动（fail-closed）: {exc}") from exc
    log(f"[egress] closed 批：白名单目标 {['%s:%d' % (h, p) for h, p in allowed]}")
    try:
        handle = egress.create_egress(allowed, endpoint_path=endpoint_path)
    except egress.EgressError as exc:
        raise ValueError(f"闭卷 egress 构建失败，拒绝启动（fail-closed）: {exc}") from exc
    try:
        probe = handle.probe()
    except egress.EgressError as exc:
        handle.teardown()
        raise ValueError(f"闭卷批级探针无法执行，拒绝启动（fail-closed）: {exc}") from exc
    if not (probe.get("allowed_ok") and probe.get("external_blocked")):
        handle.teardown()
        raise ValueError(
            "闭卷批级探针未通过，拒绝启动（fail-closed，不消耗任何模型预算）: "
            f"allowed_ok={probe.get('allowed_ok')} "
            f"external_blocked={probe.get('external_blocked')} "
            f"detail={json.dumps(probe.get('detail', {}), ensure_ascii=False)[:400]}"
        )
    record = handle.to_record()
    batch_log_dir.mkdir(parents=True, exist_ok=True)
    (batch_log_dir / "egress.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"[egress] closed 批就绪 network={handle.network} endpoint={handle.endpoint_url}")
    return handle


def teardown_batch_egress(handle: "Any", *, log=print) -> None:
    """批收尾：teardown 幂等、吞异常，只在 errors 非空时打告警（绝不抛出）。"""
    if handle is None:
        return
    try:
        result = handle.teardown()
    except Exception as exc:  # noqa: BLE001 - finally 路径绝不阻断收尾
        log(f"[egress] WARNING: teardown 异常（请人工核对 jws-egress-* 残留）: {exc}")
        return
    if result.get("errors"):
        log(f"[egress] WARNING: teardown 有错误: {result['errors']}")


def set_batch_egress_args(args: Any, handle: Any) -> None:
    """把 handle 摊平成透传字段，挂到批 args 上（build_*_command 用）。"""
    args.egress_endpoint_url = handle.endpoint_url
    args.egress_network = handle.network
    args.egress_allowed = ",".join("%s:%d" % (host, port) for host, port in handle.allowed)


# ---------------------------------------------------------------------------
# 透传与参数守卫
# ---------------------------------------------------------------------------

def append_egress_args(command: list[str], args: Any) -> list[str]:
    """closed 才追加 egress 三参；open（无该属性/为 None）时零改动（C4）。"""
    endpoint_url = getattr(args, "egress_endpoint_url", None)
    if not endpoint_url:
        return command
    command += [
        "--egress-endpoint-url", str(endpoint_url),
        "--egress-network", str(getattr(args, "egress_network", "")),
        "--egress-allowed", str(getattr(args, "egress_allowed", "")),
    ]
    return command


def add_egress_args(parser: Any) -> None:
    """single_case*.py / run_case*.py 共用的三个透传参数定义（默认 None=no-op）。"""
    parser.add_argument(
        "--egress-endpoint-url", default=None,
        help="closed 专用：egress endpoint_url，整串替换注入的 BASE_URL",
    )
    parser.add_argument(
        "--egress-network", default=None,
        help="closed 专用：agent 容器 docker run 追加 --network <该网>",
    )
    parser.add_argument(
        "--egress-allowed", default=None,
        help="closed 专用：逗号分隔 host:port 白名单（记录/V1 探测端口数用）",
    )


def validate_egress_args(args: Any, *, script_name: str) -> None:
    """run_case 层参数自洽校验（不合规即 SystemExit，fail-closed）。"""
    endpoint_url = getattr(args, "egress_endpoint_url", None)
    network = getattr(args, "egress_network", None)
    if not endpoint_url and not network:
        return
    if endpoint_url and not network:
        raise SystemExit(f"ERROR: {script_name}: --egress-endpoint-url 必须与 --egress-network 成对出现")
    if network and not endpoint_url:
        raise SystemExit(f"ERROR: {script_name}: --egress-network 必须与 --egress-endpoint-url 成对出现")
    if not str(endpoint_url).startswith("http://"):
        raise SystemExit(
            f"ERROR: {script_name}: --egress-endpoint-url 必须是 http:// 形式的 egress "
            f"端点（得到 {endpoint_url!r}）；https 不经闭卷转发，拒绝静默降级"
        )


def require_closed_egress_args(args: Any, *, runner_hint: str) -> None:
    """single_case 层：closed 批却没收到 egress 参数 = 透传链断裂 → 拒绝执行。

    覆盖"直接手工调用 single_case 且缺省 book_mode=closed"的兼容悬崖（C7：
    宁可显式报错，绝不静默按 open 联网跑）。批层（run_batch*）永远成对下发。
    """
    if getattr(args, "book_mode", "open") != "closed":
        return
    if getattr(args, "egress_endpoint_url", None):
        return
    raise ValueError(
        "book_mode=closed 但缺少 --egress-endpoint-url/--egress-network 透传参数，"
        f"拒绝执行（fail-closed）。请经 run_batch*/{runner_hint} 批入口发起；"
        "确需开卷请用 --book-mode open。"
    )


def reject_raw_agent_runner(args: Any) -> None:
    """护栏：closed + agent_runner=run_case_raw → 显式报错（M3 未接入 raw 线）。"""
    if getattr(args, "book_mode", "open") != "closed":
        return
    if getattr(args, "agent_runner", "run_case") == "run_case_raw":
        raise ValueError(
            "闭卷（--book-mode closed）不支持 --agent-runner run_case_raw："
            "raw 线经宿主进程内 JiuwenSwarmAgent 直连模型端点，无容器网络可收口，"
            "fail-closed 拒绝启动。请改用 --agent-runner run_case。"
        )


# ---------------------------------------------------------------------------
# case 级 network 记录（state → manifest）与 V3 审计
# ---------------------------------------------------------------------------

def read_case_network(root: Path, run_id: str) -> dict | None:
    """读 run_case 落盘的 state/run-<run_id>.json 的 network 段（缺失返回 None）。"""
    path = Path(root) / "state" / f"run-{run_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = value.get("network") if isinstance(value, dict) else None
    return section if isinstance(section, dict) else None


def audit_external_fetches(case_output: Path) -> dict:
    """V3 审计：统计 case 产物中外网取数工具的**成功**事件数。

    数据面（按可信度分层，全部只读）：
    1. ``history.jsonl``（py 线逐会话事件流）：chat.tool_call 记住在途 fetch 工具
       名，其后 chat.tool_result 结果字符串不以 ``[ERROR]`` 开头即计一次成功；
    2. ``*.json`` 文档（rust transcript/agent-result/swarm_export 快照）：递归走
       结构，节点 tool/name 命中 fetch 名单、且 result/output/content 字符串非
       ``[ERROR]`` 开头 → 计一次成功（形态未全验证，见回报残余风险）；
    3. ``*.log`` 纯文本：``"result"`` 冒号空白 ``"URL: http`` 成功行 +
       ``Executing tool: <name>`` 尝试行（py jiuwen.log 标记）。
    verdict：external_fetch_events>0 → network_leak（single_case 写 manifest 顶层
    ``network_leak=true`` 并由批汇总计数）。注意：闭卷下 V1+网络层才是硬约束，
    V3 是审计兜底（计数 0 不等于从未尝试）。
    """
    case_output = Path(case_output)
    events = 0
    attempts = 0
    signals: dict[str, int] = {}
    scanned = 0
    skipped_large: list[str] = []
    max_file_bytes = 64 * 1024 * 1024

    def bump(name: str, success: int, try_count: int) -> None:
        nonlocal events, attempts
        events += success
        attempts += try_count
        if success or try_count:
            entry = signals.setdefault(name, [0, 0])
            entry[0] += success
            entry[1] += try_count

    if not case_output.is_dir():
        return {"policy": POLICY, "external_fetch_events": 0, "fetch_attempts": 0,
                "verdict": "closed", "signals": {}, "scanned_files": 0,
                "skipped_large": [], "note": "case_output 不存在"}

    # 1) py history.jsonl 顺序配对
    for path in sorted(case_output.rglob("history.jsonl")):
        try:
            if path.stat().st_size > max_file_bytes:
                skipped_large.append(str(path.relative_to(case_output)))
                continue
            scanned += 1
            success, tried = _pair_history_file(path)
            bump(str(path.relative_to(case_output)), success, tried)
        except OSError:
            continue

    # 2) JSON 文档递归（rust 为主；py 的 agent-result.json 也顺带覆盖）
    for path in sorted(case_output.rglob("*.json")):
        if path.name == "manifest.json" or not path.is_file():
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                skipped_large.append(str(path.relative_to(case_output)))
                continue
            scanned += 1
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        success, tried = _walk_json_node(value)
        bump(str(path.relative_to(case_output)), success, tried)

    # 3) 文本日志标记
    for path in sorted(case_output.rglob("*.log")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                skipped_large.append(str(path.relative_to(case_output)))
                continue
            scanned += 1
            success, tried = _scan_text_log(path)
            bump(str(path.relative_to(case_output)), success, tried)
        except OSError:
            continue

    verdict = "network_leak" if events > 0 else "closed"
    return {
        "policy": POLICY,
        "external_fetch_events": events,
        "fetch_attempts": attempts,
        "verdict": verdict,
        "signals": signals,
        "scanned_files": scanned,
        "skipped_large": skipped_large,
    }


def _pair_history_file(path: Path) -> tuple[int, int]:
    """history.jsonl 顺序配对：fetch 工具 call → 下一个 result 非 [ERROR] 记成功。"""
    success = tried = 0
    pending: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("event_type")
            if event_type == "chat.tool_call":
                call = event.get("tool_call")
                name = call.get("name") if isinstance(call, dict) else event.get("name")
                if isinstance(name, str) and name in FETCH_TOOL_NAMES:
                    tried += 1
                    pending = name
            elif event_type == "chat.tool_result" and pending is not None:
                result = event.get("result")
                if isinstance(result, str) and result.strip() and not result.lstrip().startswith("[ERROR"):
                    success += 1
                pending = None
    return success, tried


def _walk_json_node(node: Any) -> tuple[int, int]:
    """递归走 JSON：dict 节点 tool/name 命中名单计尝试，其结果串非 [ERROR] 计成功。"""
    success = tried = 0
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            name = None
            for key in ("name", "tool", "tool_name", "toolName"):
                value = current.get(key)
                if isinstance(value, str) and value in FETCH_TOOL_NAMES:
                    name = value
                    break
            if name is not None:
                tried += 1
                payload = None
                for key in ("result", "output", "content", "text"):
                    value = current.get(key)
                    if isinstance(value, str):
                        payload = value
                        break
                if payload is not None and payload.strip() and not payload.lstrip().startswith("[ERROR"):
                    success += 1
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return success, tried


def _scan_text_log(path: Path) -> tuple[int, int]:
    """文本日志：py 线标记串（成功行以 [ERROR] 前缀除外；尝试行含 Executing tool）。"""
    success = tried = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"result": "URL: http' in line or '"result":"URL: http' in line:
                if "[ERROR]" not in line:
                    success += 1
            for tool in FETCH_TOOL_NAMES:
                if f"Executing tool: {tool}" in line or f'"name": "{tool}"' in line:
                    tried += 1
                    break
    return success, tried


def build_case_network_section(state_network: dict | None, audit: dict) -> dict:
    """manifest.network 段（设计稿 §8 形制；state 缺失时退化为最小记录）。"""
    state_network = state_network or {}
    return {
        "policy": state_network.get("policy", POLICY),
        "network": state_network.get("name"),
        "allowed": state_network.get("allowed") or [],
        "endpoint_url": state_network.get("endpoint_url"),
        "probe": state_network.get("v1"),
        "audit": audit,
    }


def batch_network_section(handle: Any, outcomes: Sequence[Any]) -> dict:
    """batch-summary.network 段：批级 egress 记录 + 逐 case V3 汇总。

    outcomes 需鸭子类型 .case_id/.output_dir（两条线 CaseOutcome 同字段）。
    """
    record = handle.to_record() if handle is not None else {"policy": POLICY}
    leaked: list[str] = []
    audited = 0
    missing_section = 0
    for outcome in outcomes:
        manifest_path = Path(outcome.output_dir) / "manifest.json" if outcome.output_dir else None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path and manifest_path.is_file() else None
        except (OSError, ValueError):
            manifest = None
        if not isinstance(manifest, dict):
            continue
        section = manifest.get("network")
        if isinstance(section, dict):
            audited += 1
        else:
            missing_section += 1
        if manifest.get("network_leak"):
            leaked.append(outcome.case_id)
    record["audit"] = {
        "cases_with_manifest": audited + missing_section,
        "cases_audited": audited,
        "cases_missing_network_section": missing_section,
        "leaked_cases": leaked,
        "leaked_count": len(leaked),
        "verdict": "network_leak" if leaked else "closed",
    }
    return record


if __name__ == "__main__":  # 极简自查：py_compile 之外无副作用
    print(__doc__.splitlines()[0])
