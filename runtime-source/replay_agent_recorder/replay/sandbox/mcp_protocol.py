"""Minimal zero-dependency MCP stdio server (tools-only).

Implements the legacy-handshake protocol subset Claude Code speaks over
stdio: initialize / notifications/initialized / tools/list / tools/call /
ping. Facts baked in from the 2025-11-25 spec and wire captures against
claude 2.1.222 (see plan Global Constraints): newline-delimited JSON-RPC,
no batching, ids never null, unparseable lines ignored (never answered),
stdout carries protocol messages only, logging goes to stderr.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
FALLBACK_PROTOCOL_VERSION = "2025-06-18"


class McpToolError(Exception):
    """Tool-level failure the model should see (isError result) and self-correct."""


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]


class McpStdioServer:
    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        tools: Sequence[McpTool],
        instructions: str | None = None,
    ) -> None:
        self._server_name = server_name
        self._server_version = server_version
        self._tools = {tool.name: tool for tool in tools}
        self._instructions = instructions

    # ---- single-message entry point (tests drive this directly) ----

    def handle_line(self, raw: str) -> str | None:
        line = raw.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self._log(f"ignoring unparseable line ({len(line)} chars)")
            return None
        if not isinstance(message, dict):
            self._log("ignoring non-object message (batching is not supported)")
            return None
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        if is_notification or msg_id is None:
            # Requests without a usable id are notifications (or malformed);
            # MCP forbids id:null responses, so never answer them — even when
            # the message is doubly malformed (id:null + non-string method).
            return None
        if not isinstance(method, str):
            return self._error(msg_id, -32600, "Invalid Request: missing method")
        if method == "initialize":
            return self._reply(msg_id, self._initialize_result(message))
        if method == "ping":
            return self._reply(msg_id, {})
        if method == "tools/list":
            return self._reply(msg_id, {"tools": self._tool_listing()})
        if method == "tools/call":
            return self._reply(msg_id, self._call_tool(message))
        if method.startswith("notifications/"):
            return None
        return self._error(msg_id, -32601, f"Method not found: {method}")

    # ---- serve loop ----

    def serve(
        self,
        instream: TextIO | None = None,
        outstream: TextIO | None = None,
    ) -> None:
        instream = instream if instream is not None else sys.stdin
        outstream = outstream if outstream is not None else sys.stdout
        for raw in instream:
            response = self.handle_line(raw)
            if response is not None:
                outstream.write(response + "\n")
                outstream.flush()

    # ---- internals ----

    def _initialize_result(self, message: dict) -> dict:
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        client_version = params.get("protocolVersion")
        version = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else FALLBACK_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self._server_name,
                "version": self._server_version,
            },
        }
        if self._instructions is not None:
            result["instructions"] = self._instructions
        return result

    def _tool_listing(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def _call_tool(self, message: dict) -> dict | "_ProtocolError":
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _ProtocolError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = tool.handler(arguments)
            is_error = False
        except McpToolError as exc:
            text = str(exc)
            is_error = True
        except Exception as exc:  # noqa: BLE001 - never crash the server
            self._log(f"tool {name} crashed: {exc!r}")
            text = f"tool failed: {type(exc).__name__} (details logged to stderr)"
            is_error = True
        return {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }

    def _reply(self, msg_id: object, result: "dict | _ProtocolError") -> str:
        if isinstance(result, _ProtocolError):
            return self._error(msg_id, result.code, result.message)
        return self._dump({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: object, code: int, message: str) -> str:
        return self._dump(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        )

    @staticmethod
    def _dump(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _log(text: str) -> None:
        print(f"[replay-sandbox mcp] {text}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class _ProtocolError:
    code: int
    message: str
