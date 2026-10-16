"""麦麦课间窗口 —— 麦当劳 MCP 客户端

零依赖（仅标准库），实现 MCP Streamable HTTP 的最小客户端：
initialize → notifications/initialized → tools/list / tools/call

Token 从环境变量 MCD_MCP_TOKEN 读取，绝不写入代码或仓库。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """MCP 调用失败。"""


class McdMcpClient:
    """麦当劳中国 MCP 服务的轻量客户端。"""

    def __init__(self, token: str | None = None, url: str = DEFAULT_URL, timeout: int = 30) -> None:
        self.token = (token or os.environ.get("MCD_MCP_TOKEN", "")).strip()
        if self.token.lower().startswith("bearer "):
            self.token = self.token[7:].strip()
        if not self.token:
            raise McpError(
                "未找到 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
                "例如：set MCD_MCP_TOKEN=你的Token（Windows）"
                " 或 export MCD_MCP_TOKEN=你的Token（macOS/Linux）"
            )
        self.url = url
        self.timeout = timeout
        self._session_id: str | None = None
        self._seq = 0
        self._initialized = False

    # ---------- 传输层 ----------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        if self._session_id:
            req.add_header("mcp-session-id", self._session_id)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            hint = ""
            if exc.code == 401:
                hint = "（Token 无效或已过期，请到 https://open.mcd.cn/mcp 重新获取）"
            elif exc.code == 429:
                hint = "（触发限流，每 Token 每分钟最多 600 次请求）"
            raise McpError(f"HTTP {exc.code} {hint} {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"网络错误：{exc.reason}") from exc

        if not raw.strip():
            return None  # notification 无响应体
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        """兼容 SSE 分帧与纯 JSON 两种返回形式。"""
        text = raw.strip()
        if text.startswith("{"):
            return json.loads(text)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    return json.loads(payload)
        raise McpError(f"无法解析响应：{text[:300]}")

    def _rpc(self, method: str, params: dict[str, Any] | None = None, notify: bool = False) -> Any:
        self._seq += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._seq

        data = self._post(payload)
        if notify or data is None:
            return None
        if "error" in data:
            raise McpError(f"{method} 失败：{data['error']}")
        return data.get("result")

    # ---------- 协议层 ----------

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-break-window", "version": "1.0.0"},
            },
        )
        self._rpc("notifications/initialized", notify=True)
        self._initialized = True
        return result or {}

    def list_tools(self) -> list[dict[str, Any]]:
        self._ensure_init()
        result = self._rpc("tools/list", {}) or {}
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用工具，优先返回结构化结果，否则返回文本内容。"""
        self._ensure_init()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result is None:
            return None
        if result.get("isError"):
            raise McpError(f"工具 {name} 返回错误：{result.get('content')}")
        if "structuredContent" in result:
            return result["structuredContent"]
        blocks = result.get("content") or []
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        return "\n".join(texts)

    def _ensure_init(self) -> None:
        if not self._initialized:
            self.initialize()


# ---------- 业务便捷封装 ----------

def envelope_data(payload: Any) -> Any:
    """麦当劳 MCP 统一返回 {success, code, message, data}，这里剥出 data。"""
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        return payload["data"]
    return payload


def parse_menu_block(text: str) -> dict[str, Any]:
    """query-meals 的文本块里含原始 JSON，截取出来解析。"""
    start = text.find('{"success"')
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    return {}
