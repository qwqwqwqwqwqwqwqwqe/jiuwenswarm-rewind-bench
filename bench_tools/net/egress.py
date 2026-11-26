#!/usr/bin/env python3
"""egress——闭卷网络通用组件（M2，设计稿《闭卷网络设计 v0.2》§3/§4/§7/§9/§11）。

提供 worker 容器唯一的出网通道：

    create_egress(allowed) -> EgressHandle
        .network        internal 网络名（worker 用 --network 挂它）
        .endpoint_url   给 worker 替换 JWS_BASE_URL 的 BASE_URL
        .probe()        双向探针（从 internal 网内临时探针容器发起），返回
                        {"allowed_ok":…, "external_blocked":…, …}
        .teardown()     删容器+删网，幂等、不抛异常

实现：`docker network create --internal` 建隔离网；转发容器双挂
bridge（唯一能出宿主的方向）+ internal，监听 :8080+i 把 TCP 字节流
转发到固定白名单目标（IP:端口白名单，不做域名解析式放行）。转发为纯
字节拼接（splice），不解析 HTTP——HTTPS 的 CONNECT 式长连接、流式响应
天然透传；同时**绝不**按客户端请求里的目的地转发，故不构成开放代理。

设计红线（任务书 §三/§六）：
- C7 快速失败：镜像缺失、建网/起容器失败、容器起来即退——一律抛
  EgressError，不降级、不静默换路径。调用方必须 try/finally 调
  .teardown()（teardown 幂等、吞异常并汇总在返回值里，可安全放在 finally）。
- 只碰自己以 `jws-egress-`/`jws-egress-net-` 前缀命名、且打了
  `jws-egress=1` label 的资源；绝不触碰其它容器/网络（如在跑的 jws-* 批）。
- 不重启 dockerd；pull 镜像属宿主网络操作（仅此一处，且优先复用本机已有镜像）。

M3 接入摘要（BenchRunner 两线 worker 挂接）：
1. 批启动时（第一容器拉起前）：
       allowed = allowed_from_env(env_file)          # 从 JWS_BASE_URL 派生
       h = create_egress(allowed, endpoint_path=endpoint_path_from_env(env_file))
       rec = h.probe()                               # V1：not (allowed_ok and
                                                     #  external_blocked) → 终止，不耗预算
   收尾/异常路径 finally 里 h.teardown()。
2. 每个 agent 容器 `docker run` 增加 `--network h.network`（替换现在的缺省
   bridge；internal 网内除转发容器外无处可达，天然封死旁路）。
3. 注入 env 时把 JWS_BASE_URL/JIUWEN_MODEL_URL/OPENAI_BASE_URL 的
   host:port 段替换为 h.endpoint_url（path 保留，如 /v1），key/model 不动。
   注意 endpoint_url 是 http://——若某线 BASE_URL 原本是 https://，直连改
   明文经转发器不适用，需另议（当前 DeepSeek/GLM/Qwen 线均为 http 目标）。
4. luna 线额外放行宿主 shim：allowed_from_env(..., add_shim_port=18999)
   （参数口已留，宿主网关由 host_gateway() 实查；本任务不验证 luna）。
5. eval 容器不在闭卷范围（设计稿 §6.1：作弊只发生在 patch 产生阶段），
   保持现状不挂 internal 网。

单点故障处置（设计稿 §9/§11）：转发容器是全批唯一咽喉——不用
--restart 掩盖故障，而是每集前/收尾用 probe() 实测定生死；probe 里
转发容器已退出会直接给出 allowed_ok=False + note，快速失败优于慢慢超时。

本模块仅依赖 stdlib + docker CLI，不 import 仓内其它模块，GitSwarm 侧
可直接整包拷用（设计稿 §4「两边共用」）。

自测：bench_tools/net/egress_selftest.py（真实建网/起容器/probe/清理）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

__all__ = [
    "EgressError",
    "EgressHandle",
    "create_egress",
    "allowed_from_env",
    "endpoint_path_from_env",
    "host_gateway",
    "EXTERNAL_PROBE_DOMAINS",
    "CONTAINER_PREFIX",
    "NETWORK_PREFIX",
]

CONTAINER_PREFIX = "jws-egress-"
NETWORK_PREFIX = "jws-egress-net-"
LABEL = "jws-egress=1"

# 设计稿 §2 验收 2：这三个域名的 TCP 必须全灭（DNS 是否解析仅记录、不作判据，
# 因为 --internal 拦的是转发面，域名即使解析成功也连不出去）。
EXTERNAL_PROBE_DOMAINS: Sequence[str] = (
    "api.github.com",
    "raw.githubusercontent.com",
    "pypi.org",
)
EXTERNAL_PROBE_PORTS: Sequence[int] = (443, 80)

# 转发容器镜像候选（按序取本机已存在且 python3 可用者；都没有才 pull 首位，
# pull 是宿主网络操作、属任务书允许范围）。jws-installer-builder:ubt2204 为
# 本机存量镜像（实测 python3 3.10.12 可用），放末尾兜底复用。
EGRESS_IMAGE_CANDIDATES: Sequence[str] = (
    "python:3.12-slim",
    "python:3.11-slim",
    "jws-installer-builder:ubt2204",
)
# 探针容器镜像候选（小、含 busybox nc/nslookup/sh）。
PROBE_IMAGE_CANDIDATES: Sequence[str] = ("busybox:latest", "alpine:latest")

_BASE_URL_KEYS: Sequence[str] = (
    "JWS_BASE_URL",
    "JIUWEN_MODEL_URL",
    "OPENAI_BASE_URL",
)

_RESOLVED_IMAGE_CACHE: dict[str, str] = {}

# 探针与转发容器之间的魔术字节：probe 时先发这个前缀，转发容器回
# OK n/m / FAIL …，用于从 internal 网内验证「转发器→白名单目标」整链
# TCP 可达，而不依赖具体业务协议（http/https 通用）。
PROBE_MAGIC = b"JWS-EGRESS-PROBE"

# ---------------------------------------------------------------------------
# 转发容器内运行的脚本（经 `python3 -c` 注入，只读 stdlib，py3.6+ 兼容）。
# 不要在本文件里用 f-string 大括号嵌套它——它是普通字符串常量。
# ---------------------------------------------------------------------------
_FORWARDER_SRC = r"""
import os, select, socket, sys, threading, time

MAGIC = b"JWS-EGRESS-PROBE"
BUF = 65536
IDLE_MAX = 1800.0

def log(msg):
    sys.stdout.write("[jws-egress %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()

def parse_targets(spec):
    out = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.rpartition(":")
        out.append((host, int(port)))
    if not out:
        raise SystemExit("JWS_EGRESS_TARGETS empty")
    return out

def tcp_alive(host, port, timeout=1.0):
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except Exception:
        return False

def splice(a, b):
    last = time.time()
    socks = (a, b)
    try:
        while True:
            idle = time.time() - last
            if idle > IDLE_MAX:
                return
            r, _, _ = select.select(list(socks), [], [], max(1.0, min(60.0, IDLE_MAX - idle)))
            if not r:
                continue
            for s in r:
                try:
                    data = s.recv(BUF)
                except OSError:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    return
                last = time.time()
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

def handle(client, target):
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    head = b""
    try:
        r, _, _ = select.select([client], [], [], 0.25)
        if r:
            head = client.recv(32, socket.MSG_PEEK)
    except OSError:
        head = b""
    if head.startswith(MAGIC):
        host, port = target
        alive = tcp_alive(host, port)
        resp = b"OK 1/1" if alive else b"FAIL 0/1 %s:%d" % (host.encode(), port)
        try:
            client.sendall(resp)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass
        return
    host, port = target
    try:
        remote = socket.create_connection((host, port), 10)
    except Exception as exc:
        log("connect-fail %s:%d %s" % (host, port, exc))
        try:
            client.close()
        except OSError:
            pass
        return
    try:
        remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    splice(client, remote)

def serve(port, target):
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("0.0.0.0", port))
    ls.listen(128)
    log("listening :%d -> %s:%d" % (port, target[0], target[1]))
    while True:
        try:
            client, _ = ls.accept()
        except OSError as exc:
            log("accept-stop :%d %s" % (port, exc))
            return
        threading.Thread(target=handle, args=(client, target), daemon=True).start()

def main():
    targets = parse_targets(os.environ["JWS_EGRESS_TARGETS"])
    base = int(os.environ.get("JWS_EGRESS_PORT", "8080"))
    for i, t in enumerate(targets):
        threading.Thread(target=serve, args=(base + i, t), daemon=True).start()
    log("ready targets=%d" % len(targets))
    while True:
        time.sleep(3600)

main()
"""


class EgressError(RuntimeError):
    """egress 构建/探测中的基础设施故障。按 C7：出现即终止，不降级。"""


# ---------------------------------------------------------------------------
# docker CLI helpers
# ---------------------------------------------------------------------------

def _docker(args: Sequence[str], timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
    """跑 docker CLI（列表参数、不经宿主 shell）。失败抛 EgressError。"""
    try:
        cp = subprocess.run(
            ["docker"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise EgressError("docker %s 超时 %.0fs" % (" ".join(args[:2]), timeout)) from exc
    except FileNotFoundError as exc:
        raise EgressError("找不到 docker CLI") from exc
    if cp.returncode != 0 and check:
        tail = (cp.stderr or cp.stdout or "").strip().replace("\n", " | ")[:400]
        raise EgressError("docker %s 失败 rc=%d: %s" % (args[0], cp.returncode, tail))
    return cp


def _local_images() -> set:
    cp = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}


_EGRESS_VERIFY = ("--entrypoint", "python3", "-V")
_PROBE_VERIFY = ("--entrypoint", "sh",
                 "-c", "command -v nc >/dev/null && command -v nslookup >/dev/null")


def _image_ok(image: str, verify: Sequence[str]) -> bool:
    """按候选用途校验镜像（--network none，纯本地、零外联）。"""
    entry, tail = list(verify[:2]), list(verify[2:])
    cp = _docker(["run", "--rm", "--network", "none"] + entry + [image] + tail,
                 timeout=60, check=False)
    return cp.returncode == 0


def _resolve_image(candidates: Iterable[str], purpose: str, allow_pull: bool) -> str:
    key = purpose
    if key in _RESOLVED_IMAGE_CACHE:
        return _RESOLVED_IMAGE_CACHE[key]
    verify = _EGRESS_VERIFY if purpose == "egress" else _PROBE_VERIFY
    present = _local_images()
    candidates = list(candidates)
    for img in candidates:
        if img not in present:
            continue
        if _image_ok(img, verify):
            _RESOLVED_IMAGE_CACHE[key] = img
            return img
    if not allow_pull:
        raise EgressError("%s 镜像候选本机均不可用且禁止 pull: %s" % (purpose, ", ".join(candidates)))
    # 候选都没有：pull 首选（宿主网络操作，任务书允许），再校验。
    img = candidates[0]
    _docker(["pull", img], timeout=600)
    if not _image_ok(img, verify):
        raise EgressError("pull 后 %s 仍不满足 %s 用途校验" % (img, purpose))
    _RESOLVED_IMAGE_CACHE[key] = img
    return img


def host_gateway(network: str = "bridge") -> str:
    """实查宿主在指定 bridge 网络上的网关 IP（luna 放行宿主 shim 时用）。"""
    cp = _docker(
        ["network", "inspect", "-f", "{{(index .IPAM.Config 0).Gateway}}", network],
        timeout=20, check=False,
    )
    gw = (cp.stdout or "").strip()
    return gw or "172.17.0.1"


# ---------------------------------------------------------------------------
# env 派生：allow 目标 = JWS_BASE_URL 的 host:port
# ---------------------------------------------------------------------------

def _parse_env_file(path) -> dict:
    """极简 dotenv：KEY=VALUE / export KEY=VALUE / 引号剥离 / # 注释。
    键名以实际 env 文件为准（如 API_KEY、JWS_BASE_URL，下划线命名）。"""
    p = Path(path)
    if not p.is_file():
        raise EgressError("env 文件不存在: %s" % p)
    out: dict = {}
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
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
        out[key] = val
    return out


def _url_to_target(url: str) -> tuple:
    """-> (host, port, path)。scheme 缺省补 http；port 缺省 443/80。"""
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    parsed = urlparse(u)
    if not parsed.hostname:
        raise EgressError("BASE_URL 无法解析出 host: %r" % url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port, (parsed.path or "")


def allowed_from_env(
    env_file,
    *,
    add_shim_port: int = None,
    shim_host: str = None,
    base_url_keys: Sequence[str] = _BASE_URL_KEYS,
) -> list:
    """从 runner env 文件派生白名单目标 [(host, port), ...]（有序去重）。

    - 依次找 JWS_BASE_URL / JIUWEN_MODEL_URL / OPENAI_BASE_URL（实际注入链
      中后两者由 JWS_BASE_URL 派生，通常同值；不一致时全部放行更安全）。
    - add_shim_port：luna 线参数口（本任务不硬编码、不实装验证）——
      追加 (shim_host or host_gateway(), add_shim_port)。
    - 找不到任何 BASE_URL → 抛 EgressError（C7，不猜默认端点）。
    """
    data = _parse_env_file(env_file)
    targets: list = []
    for key in base_url_keys:
        val = data.get(key)
        if not val:
            continue
        host, port, _path = _url_to_target(val)
        if (host, port) not in targets:
            targets.append((host, port))
    if not targets:
        raise EgressError(
            "env 文件 %s 未找到 %s 任一键，无法派生 allow 目标" % (env_file, "/".join(base_url_keys))
        )
    if add_shim_port is not None:
        host = shim_host or host_gateway()
        if (host, int(add_shim_port)) not in targets:
            targets.append((host, int(add_shim_port)))
    return targets


def endpoint_path_from_env(env_file, *, base_url_keys: Sequence[str] = _BASE_URL_KEYS) -> str:
    """取主 BASE_URL 的 path（如 /v1），供 create_egress(endpoint_path=...)，
    让 endpoint_url 能整串替换 worker 的 JWS_BASE_URL。"""
    data = _parse_env_file(env_file)
    for key in base_url_keys:
        val = data.get(key)
        if val:
            return _url_to_target(val)[2]
    raise EgressError("env 文件 %s 未找到 BASE_URL 键" % env_file)


# ---------------------------------------------------------------------------
# EgressHandle
# ---------------------------------------------------------------------------

@dataclass
class EgressHandle:
    """一次 egress 的生命周期句柄。用法见模块 docstring；务必 finally teardown。"""

    network: str
    container: str
    endpoint_url: str
    allowed: list                         # [(host, port), ...]
    listen_ports: list                    # 与 allowed 一一对应（8080+i）
    egress_ip: str                        # internal 网内转发容器 IP（钉死，免 DNS）
    image: str
    probe_image: str
    tool_policy: str
    suffix: str
    created_at: str
    _torn_down: bool = False
    last_probe: dict = field(default=None, repr=False)

    # -- 探测 ---------------------------------------------------------------

    def _container_running(self) -> bool:
        cp = _docker(["inspect", "-f", "{{.State.Running}}", self.container],
                     timeout=20, check=False)
        return cp.returncode == 0 and cp.stdout.strip() == "true"

    def probe(
        self,
        *,
        external_domains: Sequence[str] = EXTERNAL_PROBE_DOMAINS,
        http_sample: bool = True,
        timeout: float = 90.0,
    ) -> dict:
        """双向探针（V1 的判据来源）。从 internal 网内的**临时探针容器**发起
        （宿主仍在 bridge，宿主上探测不算数）：

        - allowed_ok：对每个白名单目标，经转发隧道魔术探测得到 OK（即
          探针→转发容器→目标整链 TCP 可达）；
        - external_blocked：external_domains × {443,80} 全部 TCP 连不通
          （DNS 解析结果仅记录，不作判据——internal 网拦转发，解析到也连不出）。

        返回 JSON-safe dict：{"allowed_ok", "external_blocked", "at", "detail"}，
        可直接填进 manifest 的 network.probe 段（设计稿 §8）。
        探针容器本身起不来（镜像/网络故障）→ 抛 EgressError，区别于探测结论。
        """
        if self._torn_down:
            raise EgressError("handle 已 teardown，不能再 probe")
        at = datetime.now().astimezone().isoformat(timespec="seconds")
        if not self._container_running():
            result = {
                "allowed_ok": False,
                "external_blocked": False,
                "at": at,
                "detail": {"note": "egress container not running (fail fast, 勿继续开集)"},
            }
            self.last_probe = result
            return result

        script = _build_probe_script(
            self.egress_ip, self.listen_ports, tuple(external_domains),
            tuple(EXTERNAL_PROBE_PORTS), http_sample,
        )
        probe_name = "%sprobe-%s" % (CONTAINER_PREFIX, self.suffix)
        cp = _docker(
            ["run", "--rm", "--name", probe_name, "--network", self.network,
             self.probe_image, "sh", "-c", script],
            timeout=timeout, check=False,
        )
        if cp.returncode != 0:
            # 探测容器异常退出：区分「部分命令非零（正常，逐项 rc 已编码）」与
            # 基础设施故障（无任何输出行）。有输出行则按内容判定。
            if "ALLOW " not in (cp.stdout or ""):
                raise EgressError(
                    "probe 容器失败 rc=%d: %s"
                    % (cp.returncode, (cp.stderr or cp.stdout or "").strip()[:300])
                )
        parsed = _parse_probe_output(cp.stdout or "")
        allowed_ok = parsed["allowed_ok"]
        external_blocked = parsed["external_blocked"]
        result = {
            "allowed_ok": allowed_ok,
            "external_blocked": external_blocked,
            "at": at,
            "detail": parsed["detail"],
        }
        self.last_probe = result
        return result

    # -- 清理 ---------------------------------------------------------------

    def teardown(self, *, retries: int = 3) -> dict:
        """删容器、删探针残留、删网。幂等（可重复调、资源已没了也不报错）；
        不抛异常（可安全放 finally），故障汇总在返回 dict 的 errors 里。
        只操作本 handle 名下 jws-egress-* 资源。"""
        errors: list = []
        removed: list = []
        names = [self.container, "%sprobe-%s" % (CONTAINER_PREFIX, self.suffix)]
        for name in names:
            cp = _docker(["rm", "-f", name], timeout=30, check=False)
            err = (cp.stderr or "").strip().lower()
            if cp.returncode == 0:
                removed.append(name)
            elif "no such container" in err:
                pass  # 已不在——幂等，视作达成
            else:
                errors.append("rm %s: %s" % (name, err[:200]))
        net_gone = False
        for i in range(retries):
            cp = _docker(["network", "rm", self.network], timeout=30, check=False)
            if cp.returncode == 0:
                removed.append(self.network)
                net_gone = True
                break
            err = (cp.stderr or "").strip()
            # docker 对不存在网络报 "network <id> not found"（而非 No such network）
            if "not found" in err.lower():
                net_gone = True
                break
            if "active endpoints" in err:
                # 有容器还挂着（理论上只有异常残留的探针）：按 label 找、只删
                # jws-egress 前缀的挂载者，绝不碰别人的容器。
                cp2 = _docker(["network", "inspect", "-f", "{{json .Containers}}",
                               self.network], timeout=20, check=False)
                try:
                    cids = json.loads(cp2.stdout or "{}")
                except json.JSONDecodeError:
                    cids = {}
                for cid in cids:
                    _docker(["rm", "-f", cid], timeout=30, check=False)
                time.sleep(0.5 * (i + 1))
                continue
            errors.append("network rm: %s" % err[:200])
            break
        if not net_gone and not errors:
            errors.append("network %s 未确认删除" % self.network)
        self._torn_down = True
        return {"removed": removed, "errors": errors}

    # -- 记录 / 上下文管理 ---------------------------------------------------

    def to_record(self) -> dict:
        """设计稿 §8 manifest.network 段的骨架（probe/audit 由 M3 填充）。"""
        return {
            "policy": "closed_book_v2",
            "tool_policy": self.tool_policy,
            "network": self.network,
            "container": self.container,
            "image": self.image,
            "allowed": ["%s:%d" % (h, p) for h, p in self.allowed],
            "listen_ports": list(self.listen_ports),
            "endpoint_url": self.endpoint_url,
            "probe": self.last_probe,
        }

    def __enter__(self) -> "EgressHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.teardown()
        return False


def _build_probe_script(
    ip: str,
    ports: Sequence[int],
    domains: Sequence[str],
    tcp_ports: Sequence[int],
    http_sample: bool,
) -> str:
    magic = PROBE_MAGIC.decode("ascii")
    lines = ['echo "PROBE_BEGIN"']
    for p in ports:
        lines.append(
            'resp=$(printf "%s" | nc -w 4 %s %s 2>/dev/null); echo "ALLOW %s ${resp:-NONE}"'
            % (magic, ip, p, p)
        )
    if http_sample and ports:
        # 软证据：经主端口送一个最小 HTTP 请求，记录响应首行——证明真实字节
        # 转发整链工作（不送任何凭据；仅取首行，不入敏感数据）。
        lines.append(
            'hr=$( { printf "GET / HTTP/1.1\\r\\nHost: probe.local\\r\\nConnection: close\\r\\n\\r\\n"; '
            'sleep 1; } | nc -w 5 %s %s 2>/dev/null | head -n 1 ); echo "HTTP ${hr:-NONE}"'
            % (ip, ports[0])
        )
    dlist = " ".join(domains)
    plist = " ".join(str(p) for p in tcp_ports)
    lines.append("for d in %s; do" % dlist)
    lines.append('  if nslookup "$d" 2>&1 | grep -q "Address:"; then echo "DNS $d ok"; else echo "DNS $d fail"; fi')
    lines.append("  for p in %s; do" % plist)
    lines.append('    if nc -z -w 2 "$d" "$p" >/dev/null 2>&1; then echo "TCP $d:$p ok"; else echo "TCP $d:$p fail"; fi')
    lines.append("  done")
    lines.append("done")
    lines.append('echo "PROBE_END"')
    return "\n".join(lines)


def _parse_probe_output(stdout: str) -> dict:
    detail: dict = {"allow": {}, "dns": {}, "tcp": {}}
    http_first_line = None
    allowed_results = []
    tcp_results = []
    for line in stdout.splitlines():
        parts = line.strip().split(" ", 2)
        if not parts:
            continue
        tag = parts[0]
        if tag == "ALLOW" and len(parts) >= 2:
            port = parts[1]
            resp = parts[2] if len(parts) > 2 else "NONE"
            detail["allow"][port] = resp
            allowed_results.append(resp.startswith("OK"))
        elif tag == "HTTP":
            http_first_line = line.split(" ", 1)[1] if " " in line else None
        elif tag == "DNS" and len(parts) >= 3:
            detail["dns"][parts[1]] = parts[2]
        elif tag == "TCP" and len(parts) >= 3:
            host_port, verdict = parts[1], parts[2]
            detail["tcp"][host_port] = verdict
            tcp_results.append(verdict != "ok")
    allowed_ok = bool(allowed_results) and all(allowed_results)
    external_blocked = bool(tcp_results) and all(tcp_results)
    if http_first_line:
        detail["http_forwarded_first_line"] = http_first_line.strip()
    return {
        "allowed_ok": allowed_ok,
        "external_blocked": external_blocked,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# create_egress
# ---------------------------------------------------------------------------

def create_egress(
    allowed: Sequence[tuple],
    tool_policy: str = "verify_in_container",
    *,
    listen_port: int = 8080,
    endpoint_path: str = "",
    name_suffix: str = None,
    image: str = None,
    probe_image: str = None,
    ready_timeout: float = 30.0,
    allow_pull: bool = True,
) -> EgressHandle:
    """建 internal 隔离网 + 固定目标转发容器，返回句柄（签名同设计稿 §4）。

    参数
    ----
    allowed : [(host, port), ...]  唯一白名单。转发容器按序监听
        listen_port+i → allowed[i]；endpoint_url 指向 allowed[0]。
        host 可以是 IP 或域名（域名在转发容器内经宿主 DNS 解析，worker
        无从利用，worker 侧只有 IP 可达性，不构成泄漏面）。
    tool_policy : 记录用（设计稿 §4 表）；BenchRunner 默认
        "verify_in_container"，实际约束（fetch_webpage 计数审计）在 M3 V3。
    endpoint_path : 拼进 endpoint_url 的 path（runner 线一般是 /v1）。
    name_suffix : 资源名后缀；默认时间+pid，保证并发批不撞名。
    image/probe_image : 覆盖镜像选择；默认按候选表复用本机镜像，
        全无可用才 pull python:3.12-slim（宿主网络操作，允许）。

    失败即抛 EgressError 并回滚已建资源（C7 快速失败，绝不降级）：
    镜像不可用 / 建网失败 / 容器起不来或起来即退出。
    注意：create 不自动跑 probe()——V1 探针由调用方（M3）在建网后、
    开集前显式执行并据结果决定终止，避免组件越权裁决。
    """
    if not allowed:
        raise EgressError("allowed 为空——闭卷模式下无目标可放行，应视为配置错误")
    norm: list = []
    for item in allowed:
        host, port = str(item[0]), int(item[1])
        if not host or not (0 < port < 65536):
            raise EgressError("非法 allow 目标: %r" % (item,))
        if (host, port) not in norm:
            norm.append((host, port))

    if tool_policy not in ("verify_in_container", "bash_only"):
        raise EgressError(
            "未知 tool_policy: %r（设计稿 §4：verify_in_container|bash_only）" % tool_policy
        )

    suffix = name_suffix or ("%s%d" % (time.strftime("%m%d%H%M%S"), os.getpid() % 1000))
    net_name = "%s%s" % (NETWORK_PREFIX, suffix)
    ctr_name = "%s%s" % (CONTAINER_PREFIX, suffix)

    resolved_image = image or _resolve_image(EGRESS_IMAGE_CANDIDATES, "egress", allow_pull)
    resolved_probe = probe_image or _resolve_image(PROBE_IMAGE_CANDIDATES, "probe", allow_pull)

    # 1) internal 网
    _docker(["network", "create", "--internal", "--label", LABEL, net_name],
            timeout=30)
    started = False

    try:
        # 2) 转发容器：先挂 internal，再补 bridge（唯一出宿主通道）。
        targets_env = ";".join("%s:%d" % (h, p) for h, p in norm)
        run_args = [
            "run", "-d", "--name", ctr_name, "--label", LABEL,
            "--network", net_name,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--entrypoint", "python3",
            "-e", "JWS_EGRESS_TARGETS=%s" % targets_env,
            "-e", "JWS_EGRESS_PORT=%d" % listen_port,
            resolved_image, "-c", _FORWARDER_SRC,
        ]
        cp = _docker(run_args, timeout=120, check=False)
        if cp.returncode != 0:
            raise EgressError("转发容器启动失败: %s" % (cp.stderr or cp.stdout or "").strip()[:400])
        _docker(["network", "connect", "bridge", ctr_name], timeout=30)
        started = True

        # 3) 就绪轮询：running + internal 网内拿到 IP + 日志出现 listening。
        deadline = time.time() + ready_timeout
        egress_ip = None
        while time.time() < deadline:
            insp = _docker(["inspect", ctr_name], timeout=20, check=False)
            if insp.returncode != 0:
                raise EgressError("转发容器消失: %s" % (insp.stderr or "").strip()[:200])
            try:
                data = json.loads(insp.stdout)[0]
            except (json.JSONDecodeError, IndexError) as exc:
                raise EgressError("inspect 解析失败") from exc
            state = data.get("State", {})
            if state.get("Status") in ("exited", "dead") or not state.get("Running"):
                logs = _docker(["logs", "--tail", "20", ctr_name], timeout=20, check=False)
                raise EgressError(
                    "转发容器未保持运行（fail fast）: status=%s error=%s logs=%s"
                    % (state.get("Status"), state.get("Error"),
                       (logs.stdout + logs.stderr).strip().replace("\n", " | ")[-400:])
                )
            nets = data.get("NetworkSettings", {}).get("Networks", {}) or {}
            info = nets.get(net_name) or {}
            egress_ip = info.get("IPAddress")
            if egress_ip:
                break
            time.sleep(0.3)
        if not egress_ip:
            raise EgressError("转发容器在 %.0fs 内未拿到 %s 内网 IP" % (ready_timeout, net_name))

        # 日志确认监听（软校验：容器还活着但没监听 → 抛；给 10s 窗口）。
        for _ in range(20):
            logs = _docker(["logs", ctr_name], timeout=20, check=False)
            if "listening" in (logs.stdout or ""):
                break
            time.sleep(0.5)
        else:
            raise EgressError("转发容器日志未见 listening，拒绝带病开集")
    except Exception:
        # 回滚半成品资源后再抛（保证「失败不留残留」）。
        try:
            if started:
                _docker(["rm", "-f", ctr_name], timeout=30, check=False)
            _docker(["network", "rm", net_name], timeout=30, check=False)
        except Exception:
            pass  # 回滚尽力而为；原始异常优先
        raise

    endpoint_url = "http://%s:%d%s" % (egress_ip, listen_port, endpoint_path or "")
    return EgressHandle(
        network=net_name,
        container=ctr_name,
        endpoint_url=endpoint_url,
        allowed=norm,
        listen_ports=[listen_port + i for i in range(len(norm))],
        egress_ip=egress_ip,
        image=resolved_image,
        probe_image=resolved_probe,
        tool_policy=tool_policy,
        suffix=suffix,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
