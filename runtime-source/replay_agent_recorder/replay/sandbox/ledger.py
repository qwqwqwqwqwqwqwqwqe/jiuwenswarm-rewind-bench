"""External side-effect ledger: what left the sandbox, and can it be undone?

Checkpoint/rollback cannot undo an HTTP POST that already reached a real
service, so M3 records instead of pretending: an egress proxy on the host
logs every request the container makes (method/host/path/status; never
bodies), and mutating shell commands are logged too. Rewind reports which
recorded actions it rolled *over* but could not undo.
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import shlex
import socket
import socketserver
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

SAFE = "safe"
IRREVERSIBLE = "irreversible"
OPAQUE = "opaque"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

IRREVERSIBLE_COMMAND_PATTERNS: tuple[str, ...] = (
    "git push", "git tag -d", "npm publish", "yarn publish", "twine upload",
    "docker push", "kubectl apply", "kubectl delete", "helm install",
    "helm upgrade", "terraform apply", "terraform destroy", "gh pr create",
    "gh release create", "aws s3 cp", "aws s3 rm", "aws s3 sync", "gcloud ",
    "az ", "ssh ", "scp ", "rsync ", "curl -X POST", "curl -X PUT",
    "curl -X DELETE", "curl -X PATCH", "curl -d ", "wget --post",
)

# Categories that host-side guards block outside the sandbox. Inside a
# rollback-capable sandbox they are allowed and merely recorded: the rootfs
# domain undoes them, so they are reversible, not irreversible.
SANDBOX_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("apt-get install", "global_package_install"),
    ("apt install", "global_package_install"),
    ("apk add", "global_package_install"),
    ("yum install", "global_package_install"),
    ("dnf install", "global_package_install"),
    ("conda install", "global_package_install"),
    ("conda remove", "global_package_install"),
    ("npm install -g", "global_package_install"),
    ("pip install", "global_package_install"),
    ("pip3 install", "global_package_install"),
    ("gem install", "global_package_install"),
    ("cargo install", "global_package_install"),
    ("sudo ", "privileged"),
    ("chown -R", "privileged"),
    ("> /etc/", "external_write"),
    ("> /usr/", "external_write"),
    ("> /var/", "external_write"),
    ("> /opt/", "external_write"),
)


def classify_sandbox_action(command: str) -> tuple[str, str] | None:
    """Categorize a sandbox-contained action (allowed, but worth recording)."""
    text = " ".join(command.split())
    for pattern, category in sorted(
        SANDBOX_ACTION_PATTERNS, key=lambda item: len(item[0]), reverse=True
    ):
        if pattern in text:
            return category, pattern
    return None


# Commands that cannot change the world. Used to skip checkpoints on the hot
# path (DeltaBox calls this lightweight-skip). Deliberately conservative:
# anything unrecognized, redirected, chained or substituted is NOT read-only.
# `env`, `awk`, `sed` and `uniq` are deliberately absent: `env VAR=x CMD`
# runs an arbitrary command, `awk` can shell out via system(), `sed` scripts
# mutate from inside the SCRIPT argument (`w`/`s///w` write files, GNU `e`
# runs commands, `-f` defers to a script file) where no flag scan can see
# them, and `uniq IN OUT` writes a file without any redirect token.
READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "grep", "egrep", "fgrep", "rg", "ag", "head", "tail", "wc",
    "find", "which", "type", "file", "stat", "du", "df", "pwd",
    "printenv", "date", "uname", "whoami", "id", "echo", "cut",
    "sort", "tr", "diff", "cmp", "md5sum", "sha256sum", "tree",
    "readlink", "basename", "dirname", "ps", "top", "free", "uptime", "jq",
    "less", "more", "nl", "column", "seq", "test", "true", "false",
})
# Only subcommands with NO mutating argument form. `git branch`/`remote`/
# `config`/`tag` merely list when bare, but `git branch -D x`, `git tag v1`,
# `git config k v`, `git remote add ...` all mutate `.git` — excluded.
READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({
        "status", "log", "diff", "show", "rev-parse", "describe", "blame",
        "ls-files", "shortlog", "grep", "cat-file",
    }),
    "docker": frozenset({"ps", "images", "inspect", "logs", "version", "info"}),
    "npm": frozenset({"ls", "list", "view", "outdated"}),
    "pip": frozenset({"list", "show", "freeze"}),
    "pip3": frozenset({"list", "show", "freeze"}),
    "kubectl": frozenset({"get", "describe", "logs", "version"}),
}
_UNSAFE_SHELL_TOKENS = (">", "<", "|", "&", ";", "$(", "`", "\n")
# Flags that make an otherwise read-only program write or execute, scoped
# per program so `grep -i` / `diff -i` / `ls -i` keep their checkpoint skip.
_PROGRAM_UNSAFE_FLAGS = {
    "sort": frozenset({"-o", "--output"}),
    "find": frozenset({
        "-delete", "-exec", "-execdir", "-ok", "-okdir",
        "-fprint", "-fprint0", "-fprintf", "-fls",
    }),
    "tree": frozenset({"-o", "--output"}),
    "git": frozenset({"--output", "--output-directory"}),
}


def _extends_short_unsafe_flag(token: str, unsafe: frozenset[str]) -> bool:
    """Attached short-option arguments (`sort -o/tmp/x`) evade exact and
    `=`-split matching: a token that extends a single-dash unsafe flag with
    more characters is unsafe too."""
    return any(
        flag.startswith("-") and not flag.startswith("--")
        and token.startswith(flag) and len(token) > len(flag)
        for flag in unsafe
    )


def is_read_only_command(command: str) -> bool:
    """True only when the command provably cannot change the sandbox world."""
    text = command.strip()
    if not text:
        return False
    # Scan the RAW text first: shlex strips the very characters this detects.
    if any(token in text for token in _UNSAFE_SHELL_TOKENS):
        return False
    # shlex, not str.split: quoting must not hide a flag from the deny maps
    # (`find . '-delete'` deletes exactly like `find . -delete`), and a
    # command shlex cannot tokenize is never read-only.
    try:
        parts = shlex.split(text)
    except ValueError:
        return False
    if not parts:
        return False
    program = parts[0].rsplit("/", 1)[-1]
    unsafe = _PROGRAM_UNSAFE_FLAGS.get(program)
    if unsafe and any(
        token.split("=", 1)[0] in unsafe
        or _extends_short_unsafe_flag(token, unsafe)
        for token in parts[1:]
    ):
        return False
    subcommands = READ_ONLY_SUBCOMMANDS.get(program)
    if subcommands is not None:
        return len(parts) > 1 and parts[1] in subcommands
    return program in READ_ONLY_PROGRAMS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerStore:
    """Append-only JSONL ledger; `seq` is the line number, injected on read."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: dict) -> None:
        payload = dict(entry)
        payload.setdefault("at", _now())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self) -> list[dict]:
        if not self._path.exists():
            return []
        rows: list[dict] = []
        for index, line in enumerate(
            self._path.read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payload["seq"] = len(rows)
                rows.append(payload)
        return rows

    def count(self) -> int:
        return len(self.load())

    def entries_after(self, seq: int) -> list[dict]:
        return [row for row in self.load() if row["seq"] > seq]


def classify_ledger_entry(entry: Mapping) -> str:
    kind = entry.get("kind")
    if kind == "connect":
        return OPAQUE
    if kind == "action":
        return SAFE  # contained by the sandbox: the rootfs domain undoes it
    if kind == "command":
        return IRREVERSIBLE
    method = str(entry.get("method", "")).upper()
    return SAFE if method in _SAFE_METHODS else IRREVERSIBLE


def classify_command(command: str) -> str | None:
    text = " ".join(command.split())
    for pattern in IRREVERSIBLE_COMMAND_PATTERNS:
        if pattern.endswith(" ") and " " not in pattern[:-1]:
            # Single-token patterns ("ssh ", "az ", ...) need a word boundary:
            # plain substring matching flags `grep ssh config.txt` and
            # `echo laz a`, spuriously blocking strict-mode rewinds. Require
            # the pattern to start the command or follow a shell separator.
            if text.startswith(pattern) or any(
                sep + pattern in text
                for sep in ("; ", "| ", "&& ", "|| ", "$(")
            ):
                return pattern
        elif pattern in text:
            return pattern
    return None


def describe_entry(entry: Mapping) -> str:
    seq = entry.get("seq")
    prefix = f"#{seq} " if seq is not None else ""
    kind = entry.get("kind")
    if kind == "connect":
        return (
            f"{prefix}CONNECT {entry.get('host')}:{entry.get('port')} "
            "(encrypted; contents unknown)"
        )
    if kind == "action":
        return (
            f"{prefix}{entry.get('category')} {entry.get('command')} "
            "(reversible inside the sandbox)"
        )
    if kind == "command":
        return f"{prefix}command {entry.get('command')}"
    query = "?<redacted>" if entry.get("query") else ""
    return (
        f"{prefix}{entry.get('method')} {entry.get('host')}{entry.get('path')}{query} "
        f"-> {entry.get('status')}"
    )


def _is_allowed_peer(address: str) -> bool:
    """The proxy binds 0.0.0.0 so containers can reach it; serve only loopback
    and private-range (RFC1918 / IPv6 ULA) peers, never the wider network."""
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return False
    return peer.is_loopback or peer.is_private


def _is_blocked_upstream(host: str) -> bool:
    """Loopback upstreams would let the sandbox reach host-only services."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "localhost.localdomain"}


class _ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:  # noqa: C901 - protocol dispatch is linear
        store: LedgerStore = self.server.store  # type: ignore[attr-defined]
        if not _is_allowed_peer(self.client_address[0]):
            try:
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            except OSError:
                pass
            return
        try:
            request_line = self.rfile.readline().decode("latin1").strip()
        except OSError:
            return
        parts = request_line.split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]
        headers: list[bytes] = []
        while True:
            try:
                header = self.rfile.readline()
            except OSError:
                return
            if not header or header in (b"\r\n", b"\n"):
                break
            headers.append(header)
        if method.upper() == "CONNECT":
            self._tunnel(store, target)
            return
        self._forward(store, method, target, headers)

    def _tunnel(self, store: LedgerStore, target: str) -> None:
        host, _, port_text = target.partition(":")
        port = int(port_text or 443)
        if _is_blocked_upstream(host) and not self.server.allow_loopback_upstream:  # type: ignore[attr-defined]
            # Refused, and recorded as such (append before any client byte).
            store.append(
                {"kind": "connect", "host": host, "port": port, "blocked": True}
            )
            try:
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            except OSError:
                pass
            return
        store.append({"kind": "connect", "host": host, "port": port})
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError:
            try:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:  # client went away; the ledger already has the entry
                pass
            return
        try:
            try:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
            except OSError:  # client vanished before the handshake completed
                return

            def pipe(source: socket.socket, sink: socket.socket) -> None:
                try:
                    while True:
                        chunk = source.recv(65536)
                        if not chunk:
                            break
                        sink.sendall(chunk)
                except OSError:
                    pass

            pump = threading.Thread(
                target=pipe, args=(self.connection, upstream), daemon=True
            )
            pump.start()
            pipe(upstream, self.connection)
        finally:
            upstream.close()

    def _forward(
        self, store: LedgerStore, method: str, target: str, headers: list[bytes]
    ) -> None:
        _, _, rest = target.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port_text = hostport.partition(":")
        port = int(port_text or 80)
        path = "/" + path
        # Privacy: the ledger records only the path portion — query strings
        # routinely carry tokens and must not persist in plaintext on the
        # host. `query: true` still shows that parameters existed.
        ledger_path, query_sep, _ = path.partition("?")
        entry: dict = {
            "kind": "http",
            "method": method.upper(),
            "host": hostport,
            "path": ledger_path,
        }
        if query_sep:
            entry["query"] = True
        if _is_blocked_upstream(host) and not self.server.allow_loopback_upstream:  # type: ignore[attr-defined]
            # Refused, and recorded as such (append before any client byte).
            store.append({**entry, "status": 0, "blocked": True})
            try:
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            except OSError:
                pass
            return
        # Forward the client's headers verbatim (hop-by-hop ones excepted) so
        # the origin sees the real request; none of them reach the ledger.
        content_length = 0
        chunked = False
        has_host = False
        forwarded: list[bytes] = []
        for line in headers:
            name, _, value = line.partition(b":")
            lowered = name.strip().lower()
            if lowered in (b"proxy-connection", b"connection", b"keep-alive"):
                continue
            if lowered == b"host":
                has_host = True
            elif lowered == b"content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = 0
            elif lowered == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
            forwarded.append(line)
        status = 0
        appended = False
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection((host, port), timeout=15)
            head = [f"{method} {path} HTTP/1.1\r\n".encode("latin1")]
            head.extend(forwarded)
            if not has_host:
                head.append(f"Host: {hostport}\r\n".encode("latin1"))
            head.append(b"Connection: close\r\n\r\n")
            upstream.sendall(b"".join(head))
            if content_length > 0:
                self._relay_exact(upstream, content_length)
            elif chunked:
                self._relay_chunked(upstream)
            # Read only until the status line is parseable; buffering the
            # whole response would OOM the daemon on large downloads and
            # stall streaming/long-poll responses until upstream close.
            buffered = b""
            while b"\r\n" not in buffered:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                buffered += chunk
            if buffered.startswith(b"HTTP/"):
                try:
                    status = int(buffered.split(b" ")[1])
                except (IndexError, ValueError):
                    status = 0
            # Ledger entry lands BEFORE the client can observe the outcome,
            # so a completed (or failed, status 0) egress is never visible to
            # the agent while still absent from the ledger.
            store.append({**entry, "status": status})
            appended = True
            try:
                self.wfile.write(buffered)
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except OSError:
                pass  # client/upstream died mid-stream; the entry is appended
        except OSError:
            if not appended:
                store.append({**entry, "status": 0})
            try:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:  # client went away; the ledger already has the entry
                pass
        finally:
            if upstream is not None:
                upstream.close()

    def _relay_exact(self, upstream: socket.socket, remaining: int) -> None:
        """Stream exactly `remaining` body bytes upstream, 64 KiB at a time
        (arbitrarily large Content-Length never gets buffered whole)."""
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            upstream.sendall(chunk)
            remaining -= len(chunk)

    def _relay_chunked(self, upstream: socket.socket) -> None:
        """Relay a chunked body verbatim through the 0-size terminator."""
        while True:
            size_line = self.rfile.readline()
            if not size_line:
                return
            upstream.sendall(size_line)
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                return
            if size == 0:
                upstream.sendall(self.rfile.readline())
                return
            self._relay_exact(upstream, size + 2)  # chunk data + CRLF

    def log_message(self, *args) -> None:  # pragma: no cover - not a BaseHTTPRequestHandler
        return


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    allow_loopback_upstream = False  # see EgressProxy.__init__


class EgressProxy:
    """Records what the sandbox sends outbound; never inspects TLS payloads."""

    def __init__(
        self,
        store: LedgerStore,
        host: str = "0.0.0.0",
        port: int = 0,
        allow_loopback_upstream: bool = False,
    ) -> None:
        """`allow_loopback_upstream` is a test-only affordance: unit tests
        bind their origin servers on 127.0.0.1. Production daemons keep the
        default False, so the sandbox cannot use the proxy as a pinhole into
        host-loopback services (`CONNECT 127.0.0.1:22`, ...)."""
        self._server = _ProxyServer((host, port), _ProxyHandler)
        self._server.store = store  # type: ignore[attr-defined]
        self._server.allow_loopback_upstream = allow_loopback_upstream
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def run_proxy_daemon(session_dir: Path, preferred_port: int = 0) -> int:
    """Foreground egress proxy for one session; writes proxy.json then serves."""
    session_dir = Path(session_dir)
    store = LedgerStore(session_dir / "ledger.jsonl")
    try:
        proxy = EgressProxy(store, host="0.0.0.0", port=preferred_port)
    except OSError:
        proxy = EgressProxy(store, host="0.0.0.0", port=0)
    (session_dir / "proxy.json").write_text(
        json.dumps({"port": proxy.port, "pid": os.getpid()}, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[replay-sandbox proxy] listening on {proxy.port}", file=sys.stderr)
    proxy.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:  # pragma: no cover - signal path
        pass
    finally:
        proxy.stop()
    return 0
