"""JiuwenSwarm 容器内 driver (并行改造: 驱动全进容器)。

在用例容器内被 ``docker exec`` 拉起, 自包含完成一整套「起 llm_forward + 起 agentserver +
发 prompt 收 answer + 取 token + 写结果 JSON」流程, 全程用容器内回环 ``127.0.0.1``, 与其他
并行用例容器完全隔离 (bridge 网络)。

设计动机 (见 plans/swebench_pro_jiuwenswarm.md):
  - 旧 agent.run 在 host 进程跑, llm_forward 在 host 起, 所有用例共用一个 → 并行时 token
    统计混乱 + 端口争抢。本 driver 把整套链路搬进容器, 每用例容器各自一个 llm_forward
    (127.0.0.1:8000) + agentserver (127.0.0.1:18092), bridge 网络天然隔离, 无需端口映射。
  - host 进程只 docker exec 起 driver + 轮询结果文件, 零网络接触容器。

逻辑平移自 ``jiuwenswarm.py`` 的 ``run`` 容器分支 (``_run``/``_consume``/``_setup_in_container``/
``_teardown_in_container``/``_load_session_transcript``/``_parse_streaming_history``/
``_normalize_usage``/``_wait_health``/``_wait_agentserver_ready``), 自包含为独立函数 (driver 在
容器内跑, 无 JiuwenSwarmAgent 实例, 故不能复用实例方法)。

运行方式 (容器内, 由 host 的 agent.run 容器分支 docker exec 起动):
  python <backend_root>/runners/agents/_jiuwen_driver.py <params_json_path>

**直接执行脚本文件, 不走 ``-m runners.agents._jiuwen_driver``**: ``-m`` 会先初始化父包
(runners/__init__ → base_runner → infrastructure → openpyxl → numpy), 把 driver 不需要的
SSHClient/Excel/openpyxl/numpy 全被动 import 进来, 容器内 numpy 一旦有问题 driver 就崩在
副作用 import 上。driver 只 import 标准库 + 运行时 utils.jiuwenswarm_client + llm_forward,
不依赖 runners/infrastructure, 直接执行脚本不触发包 __init__, import 链干净。

params_json 字段:
  prompt, session_id, workspace (容器内路径, 如 /app 或 /workspace), timeout, mode,
  base_url, api_key, model_name (llm_forward 上游真实 endpoint/key/model),
  backend_root (容器内挂载点, 如 /home/.../benchrunner-backend, driver 据此 sys.path 加),
  result_file (容器内要写的结果 JSON 路径, 如 /shared/result/{iid}.json),
  instance_id (快照日志用), jiuwen_port (默认 18092), llm_forward_port (默认 8000),
  server_startup_timeout (AgentServer 应用层就绪等待上限, 默认 360 秒),
  channel_id (可选; host 通常按 mode 显式传入；缺失时 driver 仍按 code.*→tui、
    其余→web 推导，且拒绝与 mode 冲突的显式值),
  frontmatter (可选; 带 sessions 列表时走多会话逐条投递, 见 run_driver docstring),
  system_prompt (可选; 前置拼进每条 prompt 文本),
  skills (可选; 任务声明的 skill 名, 每行一个; WildClaw task.md ## Skills 段),
  skills_path (可选; skill 源目录容器内绝对路径, 须在 backend_root 挂载子树下才可达;
    WildClawBench 经 host 端 docker cp 到容器 /tmp_wildclaw_skills 并重写为此路径)
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("jiuwen_driver")

# 容器内固定常量 (与 jiuwenswarm.py 的 _SHARD_DIR / _JIUWEN_HOME_DIRNAME / _CONTAINER_* 一致)
_SHARD_DIR = "/shared"
_JIUWEN_HOME_DIRNAME = ".jiuwenswarm"
_CONTAINER_PYTHON = "/opt/miniconda3/bin/python3"
_CONTAINER_AGENTSERVER_BIN = "/opt/miniconda3/bin/jiuwenswarm-agentserver"


def _wait_health(port, timeout=30):
    """等本地端口就绪 (best-effort, 平移自 jiuwenswarm._wait_health)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _wait_agentserver_ready(jiwen_port, timeout=360.0):
    """等 jiuwenswarm-agentserver WebSocket 就绪 (用 AgentServerClient.session_list 探活)。

    平移自 jiuwenswarm._wait_agentserver_ready: 用 session_list 而非 config_get, 避免启动期
    A2UI prompt decision 自调 LLM 污染 token 统计。仍是应用层探活 (需 agent 真正能处理请求)。
    """
    from utils.jiuwenswarm_client import AgentServerClient
    ws_uri = f"ws://127.0.0.1:{jiwen_port}"

    async def _wait():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with AgentServerClient(ws_uri) as client:
                    await client.session_list()
                    return True
            except Exception:
                await asyncio.sleep(1.0)
        return False

    return asyncio.run(_wait())


def _normalize_usage(stats):
    """llm_forward token stats → 统一 usage 字段 (平移自 jiuwenswarm._normalize_usage)。"""
    s = stats or {}
    return {
        "input_tokens": int(s.get("prompt_tokens", 0) or 0),
        "output_tokens": int(s.get("completion_tokens", 0) or 0),
        "request_count": int(s.get("total_requests", 0) or 0),
        "prompt_cache_hit_tokens": int(s.get("prompt_cache_hit_tokens", 0) or 0),
        "first_token_time": float(s.get("first_token_time", 0) or 0),
        "llm_time": float(s.get("llm_time", 0) or 0),
    }


def _sq(s) -> str:
    """shell 单引号包裹 (内部单引号转义为 '\\'')。与 _dsh/_openclaw/_claude driver 同款。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _rewrite_default_model(config_path, model_name):
    """把当前任务模型写入 JiuwenSwarm 的默认文本模型配置。

    shared config 是部署时生成的；跳过部署后它可能仍带着上一任务的模型。Driver 每次
    启动 AgentServer 前都必须用本次 params 中的 model_name 覆盖它。这里只改
    models.defaults 下的第一个 model_name，不影响 video/audio/vision 等独立模型。
    """
    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    models_indent = None
    defaults_indent = None
    replacement = json.dumps(str(model_name), ensure_ascii=False)

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        key = stripped.split("#", 1)[0].strip()

        if models_indent is None:
            if indent == 0 and key == "models:":
                models_indent = indent
            continue
        if defaults_indent is None:
            if indent <= models_indent:
                break
            if key == "defaults:":
                defaults_indent = indent
            continue
        if indent <= defaults_indent:
            break
        if key.startswith("model_name:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{' ' * indent}model_name: {replacement}{newline}"
            path.write_text("".join(lines), encoding="utf-8")
            logger.info("当前任务模型已写入 JiuwenSwarm 配置: %s", model_name)
            return

    raise RuntimeError(
        f"JiuwenSwarm 配置缺少 models.defaults.model_name: {path}"
    )


def _parse_streaming_history(raw):
    """解析 history.jsonl/json 为结构化 transcript (平移自 jiuwenswarm._parse_streaming_history)。"""
    raw = raw.strip()
    if not raw:
        return []
    events = []
    try:
        whole = json.loads(raw)
        if isinstance(whole, list):
            events = whole
    except json.JSONDecodeError:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
                elif isinstance(obj, list):
                    events.extend(obj)
            except json.JSONDecodeError:
                continue

    _SKIP = {"chat.delta", "context.compressed", "chat.processing_status",
             "chat.reasoning", "chat.evolution_status",
             "chat.usage_metadata", "chat.tool_update", "chat.usage_summary"}
    transcript = []
    for event in events:
        event_type = event.get("event_type")
        role = event.get("role")
        if role == "user" and not event_type:
            transcript.append({"type": "message", "message": {
                "role": "user",
                "content": [{"type": "text", "text": event.get("content", "")}]}})
            continue
        if event_type in _SKIP or not event_type:
            continue
        payload = event.get("event_payload") or event
        if event_type == "chat.final":
            text = payload.get("content", event.get("content", ""))
            transcript.append({"type": "message", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}]}})
        elif event_type == "chat.tool_call":
            tc = payload.get("tool_call") or event.get("tool_call", {})
            args = tc.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            transcript.append({"type": "message", "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": tc.get("name"),
                             "tool_call_id": tc.get("tool_call_id"), "arguments": args}]}})
        elif event_type == "chat.tool_result":
            result = payload.get("result") or event.get("result", "")
            if not isinstance(result, dict):
                result = {"type": "toolResult",
                          "text": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}
            else:
                result = {"type": "toolResult", **result}
            transcript.append({"type": "message", "message": {
                "role": "toolResult", "content": [result],
                "tool_name": payload.get("tool_name") or event.get("tool_name")}})
    return transcript


def _load_session_transcript(workspace):
    """从容器内 ~/.jiuwenswarm/agent/sessions/*/history.jsonl 读结构化 transcript。

    平移自 jiuwenswarm._load_session_transcript (容器内跑, ~ 是容器内的 home)。
    单会话路径 (Swe): 按 mtime 取最新 history。多会话路径见 _read_session_history(sid)。
    """
    transcript = _read_session_history(sid=None)
    # 写 transcript.jsonl 到 workspace (容器内)
    if transcript:
        _write_transcript_jsonl(transcript, workspace)
    return transcript


def _read_session_history(sid=None):
    """按 session_id 读 history (优先), 找不到回退扫最新 (mtime)。不写 transcript.jsonl。

    平移自 jiuwenswarm._load_session_transcript(session_id, workspace) 的读取部分:
    多会话用例每个 session 独立读自己的历史 (新段前归档上一段), 写盘交由调用方合并后统一做。
    """
    sessions_dir = Path.home() / ".jiuwenswarm" / "agent" / "sessions"
    if not sessions_dir.is_dir():
        return []
    candidates = []
    if sid:
        # 优先按 session_id 定位 (服务端新版会用 server-generated id 覆盖本地 id,
        # 找不到时回退扫最新 —— 与 host 形态一致)
        for name in ("history.jsonl", "history.json"):
            p = sessions_dir / sid / name
            if p.exists():
                candidates.append(p)
    if not candidates:
        candidates = list(sessions_dir.glob("*/history.jsonl")) + list(sessions_dir.glob("*/history.json"))
    if not candidates:
        return []
    active = max(candidates, key=lambda p: p.stat().st_mtime)

    # 等文件写完稳定 (max 30s)
    deadline = time.time() + 30
    last_sig = None
    while time.time() < deadline:
        if active.exists() and active.stat().st_size > 0:
            sig = (str(active), active.stat().st_mtime_ns, active.stat().st_size)
            if sig == last_sig:
                break
            last_sig = sig
        time.sleep(1)

    if not active.exists():
        return []
    raw = active.read_text(encoding="utf-8")
    return _parse_streaming_history(raw)


def _write_transcript_jsonl(transcript, workspace):
    """把 (合并后的) transcript 写 transcript.jsonl 到 workspace, 供 grader/judge 读。"""
    if not transcript:
        return
    try:
        tpath = Path(workspace) / "transcript.jsonl"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        with tpath.open("w", encoding="utf-8") as f:
            for entry in transcript:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _parse_sessions(sessions):
    """解析 frontmatter.sessions → [(prompt, new_session)] 列表。

    平移自 host 形态 openclaw._parse_sessions / jiuwenswarm 多会话段解析: 条目兼容
    str 与 dict {prompt|message, new_session}; 非法/空条目跳过。
    """
    parsed = []
    for entry in sessions or []:
        if isinstance(entry, str):
            if entry.strip():
                parsed.append((entry, False))
        elif isinstance(entry, dict):
            prompt = (entry.get("prompt") or entry.get("message") or "").strip()
            if prompt:
                parsed.append((prompt, bool(entry.get("new_session", False))))
        else:
            logger.warning("跳过非法 session 条目: %s", entry)
    return parsed


def _safe_sid(prefix: str, suffix: str) -> str:
    """多会话新 sid: 合法字符 + ≤80 (agentserver is_valid_session_id 正则约束)。"""
    import re as _re
    base = _re.sub(r"[^A-Za-z0-9_.-]", "_", f"{prefix}_{suffix}")[:78]
    return base if base else f"s{int(time.time() * 1000)}"


def _setup_in_container(case, jiwen_port):
    """容器内恢复 jiuwenswarm 运行环境 + 装任务 skill + 后台起 agentserver
    (平移自 jiuwenswarm._setup_in_container)。

    driver 本身就在容器内跑, 故直接用本地 subprocess (不再 docker exec)。
    """
    home = os.path.expanduser("~")
    # 1) 恢复 .jiuwenswarm: cp /shared/config/.jiuwenswarm → ~/
    #    必须先恢复: 下面装的 skill 进 ~/.jiuwenswarm/agent/workspace/skills/, rm -rf 会清掉
    #    之前装的; 也必须在起 agentserver 之前装 (SkillUseRail 启动时扫该目录)。
    subprocess.run(
        ["bash", "-lc", f"rm -rf ~/{_JIUWEN_HOME_DIRNAME} && "
                        f"cp -r {_SHARD_DIR}/config/{_JIUWEN_HOME_DIRNAME} ~/"],
        check=True, capture_output=True, text=True, timeout=300)
    # shared 配置属于部署产物；need_deployment=False 时可能仍是上一任务的模型。
    # 当前任务参数才是运行时事实源，必须在 AgentServer 读取配置前覆盖。
    model_name = str(case.get("model_name") or "").strip()
    if model_name:
        _rewrite_default_model(
            Path(home) / _JIUWEN_HOME_DIRNAME / "config" / "config.yaml",
            model_name,
        )
    # 1.5) 装任务声明的 skill 进 jiuwenswarm 真正读的 skill 目录
    #    (~/.jiuwenswarm/agent/workspace/skills/, 即 get_agent_skills_dir)。
    #    SkillUseRail (被动注入 skill 给 agent 的 rail) 实例化扫 _skill_scan_dirs 首项即
    #    get_agent_skills_dir; _register_unmanaged_local_skills 自动把"手动拷进 skills 目录、
    #    未经安装流程"的 skill 补登记到 local_skills (无需重启/手写注册表, 排除与 builtin 同名)。
    #    **driver 在容器内**, skill 源是 case["skills_path"]。WildClawBench 经 wildclawbench
    #    _setup_container (SKILLS_DIR=None 分支) host 端 docker cp 把 skill 装进容器内
    #    /tmp_wildclaw_skills/ 并重写 skills_path 为该容器内路径 —— 不依赖 backend_root/
    #    benchmarks 挂载 (JiuwenSwarm get_mount_dirs 已收紧为只挂 runners/+llm_forward/+utils/
    #    ro, 防 benchmarks/ 答案泄露)。故此处 skills_path 是容器内真实可达的绝对路径, 直接 cp。
    #    镜像可能非 root, skill 目录用容器内 HOME 展开结果拼 (不用 host 端 expanduser)。
    skills = (case.get("skills") or "").strip()
    skills_path = (case.get("skills_path") or "").strip()
    if skills and skills_path:
        skills_dir_rel = f"~/{_JIUWEN_HOME_DIRNAME}/agent/workspace/skills"
        skills_dir_abs = f"{home}/{_JIUWEN_HOME_DIRNAME}/agent/workspace/skills"
        subprocess.run(["bash", "-lc", f"mkdir -p {skills_dir_rel}"],
                       capture_output=True, text=True, timeout=15)
        seen_names = set()
        for line in skills.splitlines():
            src_rel = line.strip().replace("\\", "/").strip("/")
            if not src_rel:
                continue
            dest_name = src_rel.rstrip("/").rsplit("/", 1)[-1]
            if not dest_name or dest_name in seen_names:
                continue
            seen_names.add(dest_name)
            # skills_path 已是容器内可达的绝对路径 (WildClaw 经 host 端 docker cp 装进
            # /tmp_wildclaw_skills/ 并重写; 其它 benchmark 可能是 backend_root 子树同路径挂载)。
            # 路径一律 _sq() 包裹 (评审 #2): skills/skills_path 来自任务声明与 host 投递路径,
            # 含空格/引号/$ 时裸拼进 bash -lc 会拷失败 (best-effort 只告警 → skill 静默没装,
            # 就是 0902 run "33 任务 skill 静默没装" 同款难查症状)。
            # ⚠️ skills_dir_rel 以 ``~`` 开头, **不能整体加引号** (单引号内 ~ 不展开, 会建出
            # 字面量 ``~`` 目录); 它是模块常量拼的固定前缀无外部输入, 保持裸写, 只引 dest_name。
            src_full = os.path.join(skills_path, src_rel)
            dest_full = f"{skills_dir_abs}/{dest_name}"
            subprocess.run(["bash", "-lc", f"mkdir -p {skills_dir_rel}/{_sq(dest_name)}"],
                           capture_output=True, text=True, timeout=15)
            cp_r = subprocess.run(
                ["bash", "-lc",
                 f"cp -r {_sq(src_full.rstrip('/') + '/.')} {_sq(dest_full + '/')}"],
                capture_output=True, text=True, timeout=120)
            if cp_r.returncode != 0:
                logger.warning("装 skill %s 失败 (best-effort): %s",
                               dest_name, cp_r.stderr.strip())
            else:
                logger.info("skill 已装进 %s/ (SkillUseRail 将扫描生效)", dest_full)
    # 1.7) rg 进 PATH: openjiuwen 内置 grep 工具按 ``shutil.which("rg")`` 分档, 命中走
    # ripgrep, 缺失降级 GNU grep (type 过滤/multiline 直接报错)。sweb.eval 镜像与
    # miniconda 均不带 rg, 唯一来源是 prepare_container_runtime 第 5 步 stage 的
    # /shared/ripgrep/rg。老流程 (jiuwenswarm_controller.init_container 第 3 步) 曾把它
    # cp 进 /usr/local/bin, driver 平移时该步丢失 → 20260915 前的跑批 grep 工具一直
    # 降级。幂等 (rg 已在则跳过); best-effort: stage 失败只告警不炸 (grep 降级仍可用)。
    # (workbuddy harness 侧同款命令, 见 workbuddy-bench jiuwenswarm_agent.py。)
    rg_r = subprocess.run(
        ["bash", "-lc",
         "(command -v rg >/dev/null 2>&1 || [ -x /usr/local/bin/rg ]) "
         "|| (cp -f /shared/ripgrep/rg /usr/local/bin/rg && chmod +x /usr/local/bin/rg)"],
        capture_output=True, text=True, timeout=30)
    if rg_r.returncode != 0:
        logger.warning("ripgrep staging 失败 (best-effort, grep 工具将降级 GNU grep): %s",
                       (rg_r.stderr or rg_r.stdout or "").strip()[:200])
    # 2) 后台起 agentserver (容器内), 写日志到 ~/.jiuwenswarm/logs/
    subprocess.run(["bash", "-lc", f"mkdir -p ~/{_JIUWEN_HOME_DIRNAME}/logs"],
                   capture_output=True, text=True, timeout=15)
    startup_log = f"~/{_JIUWEN_HOME_DIRNAME}/logs/agentserver_startup.log"
    # 2.4) Code Graph 语法缓存: get_mount_dirs 把 host ~/.cache/tree-sitter-language-pack
    # ro 挂到 /root/.cache/...。bind-mount 保留 host uid(1000) 属主, 容器内 root 进程会被
    # pack 拒用 ("owned by uid 1000, not the current process's uid 0") → 起 agentserver 前用
    # TREE_SITTER_LANGUAGE_PACK_LIBS_DIR 显式指到 libs 目录绕过属主检查 (操作员显式指定
    # 视为信任)。版本目录 (v1.16.1) 随 pack 版本变, glob 动态解析; 目录不存在则不设 env,
    # agentserver 照常起 (graph 档回退 grep, profile=off 任务无感知)。
    # (同款逻辑 3ea95d6c 曾补在 jiuwenswarm._setup_in_container, 但该方法并行改造起即不可达,
    #  实际执行路径是本 driver —— 20260914 run 复现 refusal 后迁至此处。)
    agentserver_bin = str(case.get("container_agentserver_bin") or _CONTAINER_AGENTSERVER_BIN)
    subprocess.run(
        ["bash", "-lc",
         "GRAPH_LIBS=$(ls -d /root/.cache/tree-sitter-language-pack/*/libs 2>/dev/null | head -1); "
         "if [ -n \"$GRAPH_LIBS\" ]; then export TREE_SITTER_LANGUAGE_PACK_LIBS_DIR=\"$GRAPH_LIBS\"; fi; "
         f"nohup {agentserver_bin} "
         f">{startup_log} 2>&1 </dev/null & disown"],
        capture_output=True, text=True, timeout=15)
    # 2.5) agentserver 启动若立即崩 (如 cryptography 的 _rust.abi3.so 需 GLIBC_2.33 而镜像
    # glibc 不足 → ImportError 退出), nohup 照样返回成功 (后台进程), _wait_agentserver_ready 会
    # 傻等 360s 超时再崩成 ConnectionRefused, 排查只见 empty_patch + run_time=0 (test_
    # swebenchpro_20260808_210621 的 NodeBB/ansible empty_patch 根因)。故 nohup 后等几秒
    # 检查 startup_log: 若已写崩溃 traceback (ImportError/GLIBC 等), 立即抛明确异常,
    # 让 driver 快速失败 + _teardown 归档 startup_log, 而非干等 6 分钟。
    time.sleep(5)
    _check_agentserver_crashed(startup_log)


def _check_agentserver_crashed(startup_log: str) -> None:
    """检查 agentserver 启动是否崩溃; 崩了读 startup_log 抛明确异常。

    agentserver 是 ``nohup &`` 后台起的, ``subprocess.run`` 只等 nohup 命令本身返回
    (立即返回), 不代表 agentserver 进程存活。若启动期 import 崩溃 (cryptography GLIBC
    不兼容 / 缺依赖 / 配置错误), 进程几秒内退出, 但 driver 无感知, 继续探活 6 分钟超时。

    判据用 **startup_log 内容** (比 pgrep 进程名匹配可靠: agentserver 经 python 解释器启动,
    进程名/时序在不同镜像下不稳定)。仅在日志里出现明确崩溃标志 (Traceback / ImportError /
    ModuleNotFound / GLIBC / OSError) 时才判定崩溃并抛异常; 没有则放行 (含 ready 标志或
    还在启动中), 交给 _wait_agentserver_ready 的 session_list 探活定夺 —— 避免误杀正常
    启动中的 agentserver (test_swebenchpro_20260808_225244 的 qutebrowser 已 ready 却被
    pgrep 误判退出)。
    """
    tail = ""
    try:
        tr = subprocess.run(
            ["bash", "-lc", f"tail -40 {startup_log} 2>/dev/null || true"],
            capture_output=True, text=True, timeout=10)
        tail = tr.stdout
    except Exception:
        pass
    if not tail:
        return  # 日志还没写出来 (agentserver 刚起): 放行, 交给探活
    # 崩溃标志 (按 import/链接失败的典型 traceback 关键字)。ready 是成功标志, 不在此列。
    crash_markers = ("Traceback (most recent call last)", "ImportError",
                     "ModuleNotFoundError", "GLIBC_", "OSError", "Symbol not found")
    if any(m in tail for m in crash_markers):
        snippet = tail.strip()[-1200:]
        raise RuntimeError(
            f"agentserver 启动崩溃 (import/链接失败)。startup_log 尾部:\n{snippet}")




def _teardown_in_container(instance_id, workspace):
    """停容器内 agentserver + 快照 ~/.jiuwenswarm 和 {workspace}/logs 到 /shared/log/<id>。

    平移自 jiuwenswarm._teardown_in_container (driver 在容器内, 直接本地 bash)。
    排除 config 目录 (含 api_key 等敏感配置, 每用例都一样无需快照)。
    """
    dest = f"{_SHARD_DIR}/log/{instance_id}"
    ws = workspace or "/app"

    def _run(cmd, timeout=60):
        try:
            subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        except Exception:
            logger.warning("teardown 命令失败 (best-effort): %s", cmd[:80], exc_info=True)

    # 1) 停 agentserver (先停, 防止文件占用导致 cp 不全)
    _run("pkill -TERM -f jiuwenswarm-agentserver 2>/dev/null || true; sleep 2; "
         "pkill -KILL -f jiuwenswarm-agentserver 2>/dev/null || true", timeout=60)
    # 2) 快照 ~/.jiuwenswarm (排除 config + agent_initial + agent/workspace)
    #    config 含 api_key 等敏感配置 (每用例都一样无需快照); agent_initial 是 jiuwenswarm
    #    启动时复制 workspace 模板生成的初始态 (17M mcp builtins + skills, 每用例完全相同,
    #    逐用例归档无价值且显著膨胀日志 —— 任务 226 单用例 240M 中近 34M 是它); agent/workspace
    #    同为模板副本 (mcp/skills 与 agent_initial 一致)。排查所需的会话 transcript 在
    #    agent/sessions/ (不在 workspace 下), 不受排除影响。
    _run(f"rm -rf {dest} && mkdir -p {dest} && cp -a ~/{_JIUWEN_HOME_DIRNAME} {dest} "
         f"&& rm -rf {dest}/{_JIUWEN_HOME_DIRNAME}/config "
         f"{dest}/{_JIUWEN_HOME_DIRNAME}/agent_initial "
         f"{dest}/{_JIUWEN_HOME_DIRNAME}/agent/workspace", timeout=300)
    # 3) 快照 {workspace}/logs
    _run(f"if [ -d {ws}/logs ]; then rm -rf {dest}/logs && cp -a {ws}/logs {dest}/logs; fi", timeout=300)
    # 4) 快照容器内 llm_forward 日志 (/tmp/llm_forward_logs, run_driver 起的 llm_forward 写)。
    #    容器删后 /tmp 随之消失, 必须在此 cp 出到 /shared/log/{id}/llm_forward_logs, 否则
    #    _collect_case_logs 取不到 (容器内 llm_forward 写 /tmp/llm_forward_logs)。
    _run(f"if [ -d /tmp/llm_forward_logs ]; then rm -rf {dest}/llm_forward_logs && "
         f"cp -a /tmp/llm_forward_logs {dest}/llm_forward_logs; fi", timeout=120)
    # 5) 放权 (容器内 root 写出的文件 host 非特权用户读不到 → _collect_case_logs 的 move
    #    跨设备时 copy 成功但 delete 源失败, 留 residue)。a+rwX 让 host 非 root 用户能
    #    unlink/delete 源文件, move 后源被清干净, 重跑不累积 root 属主残留。
    _run(f"chmod -R a+rwX {dest} 2>/dev/null || true", timeout=120)


def run_driver(params):
    """driver 主流程: 起 llm_forward + agentserver → 发 prompt 收 answer → 取 token → 写结果。

    全程容器内回环 127.0.0.1。结果写到 params['result_file']。
    """
    prompt = params.get("prompt") or params.get("user_prompt") or ""
    workspace = params.get("workspace", "/app")
    session_id = params.get("session_id") or f"br-driver-{int(time.time())}"
    timeout = int(params.get("timeout", 300))
    base_url = params.get("base_url", "")
    api_key = params.get("api_key", "")
    backend_root = params.get("backend_root", "")
    result_file = params.get("result_file", "")
    instance_id = params.get("instance_id") or session_id
    jiwen_port = int(params.get("jiuwen_port", "18092"))
    llm_port = int(params.get("llm_forward_port", "8000"))
    server_startup_timeout = int(params.get("server_startup_timeout", "360"))
    mode = params.get("mode") or "agent"
    requested_mode = str(mode).strip().lower()
    expected_channel_id = "tui" if requested_mode.startswith("code") else "web"
    # 多会话 (PinchBench multi_session 用例): frontmatter.sessions 逐条投递, 见 run_driver
    # docstring。channel_id: host 显式传入且须与 mode 一致；缺失时 driver 按 mode
    # 推导兜底。channel 在建会话时固化 work_mode (tui→code, web→work)，两者冲突时
    # 必须失败，不能把 code.normal 静默降成 agent profile。
    raw_channel_id = params.get("channel_id")
    channel_id = (
        str(raw_channel_id).strip().lower()
        if raw_channel_id is not None and str(raw_channel_id).strip()
        else expected_channel_id
    )
    expected_work_mode = "code" if expected_channel_id == "tui" else "work"
    system_prompt = params.get("system_prompt") or ""
    frontmatter = params.get("frontmatter") or {}
    sessions_raw = frontmatter.get("sessions") if isinstance(frontmatter, dict) else None
    session_prompts = _parse_sessions(sessions_raw) if sessions_raw else None

    # sys.path: 直接执行脚本 (python /path/_jiuwen_driver.py, 非 -m) 时, Python 自动把
    # 脚本所在目录 (runners/agents/) 放进 sys.path[0]。这会**遮蔽** site-packages 里的
    # jiuwenswarm 包: runners/agents/ 下有个 jiuwenswarm.py (agent 实现), import jiuwenswarm
    # 会命中它 (模块) 而非真包 → 'jiuwenswarm is not a package' (runners/agents/jiuwenswarm.py
    # 不是包, 而 utils.jiuwenswarm_client 要 from jiuwenswarm.common.e2a import ...)。
    # 故先把脚本目录从 sys.path 摘掉, 再把 backend_root 加最前 (供 import utils/llm_forward)。
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != script_dir]
    if backend_root and backend_root not in sys.path:
        sys.path.insert(0, backend_root)

    forward_proc = None
    forward_client = None
    usage = {}
    completed = False
    error = None
    answer = None
    verified_work_mode = None
    merged_transcript = None  # 多会话路径: 各段合并的结构化 transcript (None=单会话路径)
    _run_duration = 0.0

    try:
        if channel_id != expected_channel_id:
            raise RuntimeError(
                f"运行模式与通道冲突: mode={mode!r} 要求 channel_id="
                f"{expected_channel_id!r}, 实际为 {channel_id!r}"
            )
        logger.info(
            "运行模式已锁定: mode=%s channel_id=%s expected_work_mode=%s",
            mode, channel_id, expected_work_mode,
        )
        # ===== 1) 起 llm_forward (容器内 127.0.0.1, 独立 log_dir 避免多容器共用挂载目录互相清空) =====
        if base_url:
            os.environ["LLM_FORWARD_LOG_DIR"] = "/tmp/llm_forward_logs"
            llm_script = f"{backend_root}/llm_forward/llm_forward.py"
            llm_log = "/tmp/llm_forward_driver.log"
            forward_env = os.environ.copy()
            forward_env["LLM_FORWARD_API_KEY"] = api_key
            container_python = str(params.get("container_python") or _CONTAINER_PYTHON)
            forward_proc = subprocess.Popen(
                [container_python, llm_script, "--host", "127.0.0.1", "--port", str(llm_port),
                 "--base-url", base_url],
                stdout=open(llm_log, "w"), stderr=subprocess.STDOUT, env=forward_env)
            if not _wait_health(llm_port, 30):
                raise RuntimeError(f"容器内 llm_forward 启动超时 (port={llm_port})")
            from llm_forward.llm_client import LLMForwardClient
            forward_client = LLMForwardClient(base_url=f"http://127.0.0.1:{llm_port}")
            forward_client.clear_token_stats()
            logger.info("容器内 llm_forward 已启动 (port=%s)", llm_port)

        # ===== 2) 起 agentserver (容器内) =====
        _setup_in_container(params, jiwen_port)
        if not _wait_agentserver_ready(jiwen_port, timeout=server_startup_timeout):
            _check_agentserver_crashed(
                f"~/{_JIUWEN_HOME_DIRNAME}/logs/agentserver_startup.log"
            )
            raise RuntimeError(
                f"AgentServer 启动超时 (port={jiwen_port}, "
                f"timeout={server_startup_timeout}s)"
            )

        # ===== 3) 发 prompt 收 answer (平移自 jiuwenswarm._run / _consume) =====
        from utils.jiuwenswarm_client import AgentServerClient
        ws_uri = f"ws://127.0.0.1:{jiwen_port}"
        async def _run():
            async with AgentServerClient(ws_uri, channel_id=channel_id,
                                         ping_timeout=None, ping_interval=None) as client:
                if not client.server_ready:
                    return "", False, None

                async def _create_session(desired_sid: str) -> str:
                    """自适应建会话 (平移自 host _create_session, 两版服务端协议兼容)。

                    先带本地 id (老版正解, 多用例隔离好); 新版 (2026080701+) 非 TUI channel
                    拒绝显式 session_id ("no longer accepts session_id") → 退回无参 create
                    取回 server-generated id。返回服务端实际使用的 session_id。
                    """
                    def _verify_work_mode(response) -> None:
                        nonlocal verified_work_mode
                        if expected_work_mode != "code":
                            return
                        payload = getattr(response, "payload", None) or {}
                        actual = payload.get("workMode") or payload.get("work_mode")
                        if actual != "code":
                            raise RuntimeError(
                                "session.create 未进入 code profile: "
                                f"mode={mode!r}, channel_id={channel_id!r}, "
                                f"workMode={actual!r}"
                            )
                        verified_work_mode = actual

                    create_resp = await client.session_create(desired_sid)
                    # 业务错误 → 失败 (平移自 _require_ok)
                    if not getattr(create_resp, "ok", True):
                        payload = getattr(create_resp, "payload", None) or {}
                        err = str(payload.get("error", ""))
                        if "no longer accepts session_id" in err:
                            create_resp = await client.session_create()
                            if not getattr(create_resp, "ok", True):
                                payload = getattr(create_resp, "payload", None) or {}
                                raise RuntimeError(
                                    f"session.create 失败: {payload.get('code', 'UNKNOWN')}: "
                                    f"{payload.get('error', payload)}")
                            payload = getattr(create_resp, "payload", None) or {}
                            _verify_work_mode(create_resp)
                            got = payload.get("session_id") or payload.get("sessionId")
                            if not got:
                                raise RuntimeError("session.create 失败: 服务端未返回 session_id")
                            return got
                        raise RuntimeError(
                            f"session.create 失败: {payload.get('code', 'UNKNOWN')}: "
                            f"{payload.get('error', payload)}")
                    _verify_work_mode(create_resp)
                    return desired_sid

                extra = {}
                if workspace:
                    ws_abs = os.path.realpath(workspace)
                    if not os.path.isabs(ws_abs):
                        raise ValueError(f"workdir 必须是绝对路径: {workspace}")
                    extra = {"workspace_dir": ws_abs, "cwd": ws_abs,
                             "project_dir": ws_abs, "trusted_dirs": [ws_abs]}

                async def _send_one(send_prompt: str, sid: str, per_timeout: float):
                    """发单条 prompt 到 sid 会话, 收完整回复, 返回 (拼接文本, done)。

                    平移自 host _send_one: per_timeout 是本条剩余预算 (多会话分摊总 timeout);
                    system_prompt 前置拼进 prompt (服务端不读 extra_params["system_prompt"])。
                    不主动 session.delete (见下方单会话路径注释, 容器随用例删, 无需删 session)。
                    """
                    if system_prompt:
                        send_prompt = f"{system_prompt.strip()}\n\n---\n\n{send_prompt}"
                    send_extra = dict(extra)
                    rewind_config = Path(sys.prefix) / "rewind-bench.json"
                    if rewind_config.exists():
                        if session_prompts:
                            raise ValueError("Rewind benchmark currently supports one task session")
                        from jiuwenswarm.rewind.bench_config import load_driver_options

                        send_extra["rewind"] = load_driver_options(Path(sys.prefix), per_timeout)
                    chunks = await client.chat_send(
                        session_id=sid, text=send_prompt, stream=True,
                        mode=mode, extra_params=send_extra or None)
                    answer_parts = []
                    transcript_parts = []
                    done = False

                    async def _consume():
                        nonlocal done
                        async for chunk in chunks:
                            payload = (chunk.payload or {}) if hasattr(chunk, "payload") else {}
                            et = payload.get("event_type", "")
                            if et == "chat.delta" and payload.get("content"):
                                answer_parts.append(payload["content"])
                                transcript_parts.append(payload["content"])
                            elif et == "chat.tool_call":
                                tc = payload.get("tool_call") or {}
                                transcript_parts.append(f"\n[tool_call: {tc.get('name','?')}]")
                            elif et == "chat.tool_result":
                                tr = payload.get("tool_result") or payload
                                transcript_parts.append(f"\n[tool_result: {str(tr)[:300]}]")
                            elif et == "chat.error":
                                # agent 报错 → 任务判失败 (平移自 task_runner, commit b7527ca):
                                # 旧实现不处理 chat.error, agent 报错时被静默吞掉, 直到超时才结束。
                                error_type = (payload.get("error_type")
                                              or payload.get("code") or "AGENT_ERROR")
                                error_msg = (payload.get("error")
                                             or payload.get("content") or payload)
                                # 把完整 payload JSON 序列化拼进异常消息 (平移自 task_runner,
                                # commit 0c3f3309): 排查时能看清 error_type/error 之外的字段
                                # (stack/details/request_id 等)。新链路经 result JSON 回传无
                                # Harbor exception_info 1000 字符截断, 故不带 marker 行 (marker
                                # 是给 WorkBench harbor agent 从截断输出里捞 payload 用的,
                                # 新链路不需要)。
                                serialized_payload = json.dumps(
                                    payload, ensure_ascii=False,
                                    separators=(",", ":"), default=str)
                                raise RuntimeError(
                                    f"agent 报错: {error_type}: {error_msg}; "
                                    f"payload={serialized_payload}")
                            elif et == "chat.ask_user_question":
                                logger.warning("agent 追问, 自动答 continue: %s", payload)
                                try:
                                    await client.chat_user_answer(sid, "continue", stream=False)
                                except Exception as exc:
                                    logger.warning("自动应答失败: %s", exc)
                            if et == "chat.complete" or getattr(chunk, "is_complete", False):
                                done = True
                                return

                    try:
                        await asyncio.wait_for(_consume(), timeout=per_timeout)
                    except asyncio.TimeoutError:
                        logger.warning("对话超时 (%ss), 取已收内容", per_timeout)
                        if "rewind" in send_extra:
                            await asyncio.wait_for(client.call(
                                "chat.interrupt", {"wait_for_stop": True}, session_id=sid), timeout=30)
                    return "\n".join(transcript_parts) or "".join(answer_parts), done

                base_sid = await _create_session(session_id)

                if session_prompts:
                    # ===== 多会话: 遍历 frontmatter.sessions 逐条投递 (平移自 host 形态) =====
                    logger.info("📋 多会话用例, %d 个 session", len(session_prompts))
                    current_sid = base_sid
                    all_transcripts = []  # 各会话顺序合并的结构化 transcript
                    overall_done = True
                    run_start = time.time()
                    for i, (seg_prompt, is_new) in enumerate(session_prompts, 1):
                        if is_new:
                            # 起新会话前先归档当前会话 transcript: 服务端仅保留末段对话,
                            # 不归档会丢前几段 → judge 看不到完整跨会话历史。
                            try:
                                archived = _read_session_history(current_sid)
                                if archived:
                                    all_transcripts.extend(archived)
                            except Exception:
                                logger.warning("归档 session[%s] history 失败 (best-effort)",
                                               current_sid, exc_info=True)
                            new_sid = _safe_sid(instance_id, f"s{i}_{int(time.time() * 1000)}")
                            try:
                                current_sid = await _create_session(new_sid)
                            except Exception as exc:
                                logger.warning("新建 session 失败, 复用当前 sid: %s", exc)
                        remaining = timeout - (time.time() - run_start)
                        if remaining <= 0:
                            logger.warning("多会话总超时, 提前结束 (已 %d/%d)",
                                           i - 1, len(session_prompts))
                            overall_done = False
                            break
                        logger.info("发 session %d/%d (sid=%s, new=%s)",
                                    i, len(session_prompts), current_sid, is_new)
                        _, seg_done = await _send_one(seg_prompt, current_sid, remaining)
                        overall_done = seg_done  # 任一段未完成即整体未完成 (取末段完成态)
                    # 末段 transcript
                    try:
                        last_transcript = _read_session_history(current_sid)
                        if last_transcript:
                            all_transcripts.extend(last_transcript)
                    except Exception:
                        logger.warning("读末段 session history 失败 (best-effort)",
                                       exc_info=True)
                    return None, overall_done, all_transcripts

                # ===== 单会话 (Swe 原路径, 行为不变) =====
                # 不主动 session.delete 删 session: session.delete 会 rmtree 整个 session 目录
                # (含 history.jsonl), 而本 driver 的 _teardown_in_container 紧随其后用
                # ``cp -a ~/.jiuwenswarm`` 快照对话痕迹到 /shared/log/<id>, _load_session_transcript
                # 也要从 sessions/*/history.jsonl 读 transcript —— 先删会让快照目录变空壳、
                # transcript 读空 (JiuwenSwarmTest_20260808_091649 sessions 空目录根因)。
                # 容器形态每用例一个容器、用完即删, session 随容器消失, 无需主动删 (与
                # host 形态 jiuwenswarm.run 的不删策略对齐, 见其 _run finally 注释)。
                text, done = await _send_one(prompt, base_sid, timeout)
                return text, done, None

        _msg_start = time.time()
        answer, completed, merged_transcript = asyncio.run(_run())
        _run_duration = time.time() - _msg_start

    except Exception as e:
        logger.exception("driver 异常: %s", e)
        error = str(e)

    finally:
        # 先取 token (llm_forward 还活着时)
        if forward_client is not None:
            try:
                stats = forward_client.get_token_stats()
                usage = _normalize_usage(stats)
            except Exception as exc:
                logger.warning("get token stats failed: %s", exc)
        # 停 llm_forward
        if forward_proc is not None:
            forward_proc.terminate()
            try:
                forward_proc.wait(timeout=10)
            except Exception:
                forward_proc.kill()
        # 停 agentserver (容器内 pkill 兜底, _teardown 也会停)
        subprocess.run(["bash", "-lc",
                        "pkill -TERM -f jiuwenswarm-agentserver 2>/dev/null || true; sleep 1; "
                        "pkill -KILL -f jiuwenswarm-agentserver 2>/dev/null || true"],
                       capture_output=True, text=True, timeout=30)
        # 快照 ~/.jiuwenswarm + workspace/logs 到 /shared/log/<id>
        try:
            _teardown_in_container(instance_id, workspace)
        except Exception:
            logger.warning("容器内 teardown 失败 (best-effort)", exc_info=True)

    # 从 session history 读结构化 transcript。
    # 多会话: 各段已在 _run 内按 sid 归档合并 (merged_transcript), 统一写 transcript.jsonl;
    # 单会话: 按 mtime 读最新 history (原行为)。
    if merged_transcript is not None:
        transcript = merged_transcript or _read_session_history(None)
        if transcript:
            _write_transcript_jsonl(transcript, workspace)
    else:
        transcript = _load_session_transcript(workspace)
    answer = json.dumps(transcript, ensure_ascii=False) if transcript else (answer if completed else None)
    if completed and not answer:
        completed = False
        error = error or "agent 完成但返回空回复"
        answer = None

    # 写结果 JSON (host 侧轮询此文件出现即完成)
    result = {
        "case_name": instance_id,
        "answer": answer,
        "usage": usage or None,
        "duration": round(_run_duration, 2),
        "error": error,
        "completed": completed,
        "workspace": str(workspace),
        "requested_mode": mode,
        "channel_id": channel_id,
        "expected_work_mode": expected_work_mode,
        "verified_work_mode": verified_work_mode,
    }
    if result_file:
        try:
            Path(result_file).parent.mkdir(parents=True, exist_ok=True)
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception as e:
            logger.exception("写结果文件失败: %s", e)
            # 兜底写 stderr (host 侧至少能看到)
            print(f"DRIVER_RESULT_WRITE_FAILED: {e}", file=sys.stderr)
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [driver] %(message)s")
    if len(sys.argv) < 2:
        print("usage: python runners/agents/_jiuwen_driver.py <params.json>", file=sys.stderr)
        sys.exit(2)
    params_path = sys.argv[1]
    with open(params_path, "r", encoding="utf-8") as f:
        params = json.load(f)
    try:
        os.remove(params_path)
    except OSError:
        pass
    result = run_driver(params)
    # 进程退出 (容器仍由 host 删除)
    sys.exit(0 if not result.get("error") else 0)


if __name__ == "__main__":
    main()
