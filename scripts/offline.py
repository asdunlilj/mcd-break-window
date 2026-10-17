"""离线快照客户端 —— 让**没有 MCP Token** 的人也能跑通

设计目标：与 `McdMcpClient` 保持同一接口（`initialize` / `list_tools` / `call_tool`），
调用方不需要改代码，只在入口做一次工厂选择。

数据来源：`sample_data/` 下由 `capture_snapshot.py` 抓取的真实 MCP 返回。
**必须明确告知用户这是快照、不是实时数据** —— 调用方有责任在输出里标注。
"""

from __future__ import annotations

import json
import os
from typing import Any

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")

# 工具名 → 快照文件
TOOL_FILES = {
    "query-nearby-stores": "stores.json",
    "query-meals": "menu.json",
    "campaign-calendar": "campaign.json",
    "list-nutrition-foods": "nutrition.json",
}

TOOL_NAMES = [
    "now-time-info", "query-nearby-stores", "query-meals", "query-meal-detail",
    "calculate-price", "create-order", "cancel-order", "query-order", "order-list",
    "campaign-calendar", "list-nutrition-foods", "query-store-coupons",
    "query-my-coupons", "available-coupons", "auto-bind-coupons", "query-my-account",
    "mall-points-products", "mall-product-detail", "mall-create-order",
    "mall-order-list", "mall-order-detail", "query-lottery-info", "draw-lottery",
    "query-my-prizes", "query-party-city", "query-party-store",
    "query-party-store-date", "query-party-store-session", "party-order-create",
    "query-meal-assistance", "query-promotions", "delivery-query-stores",
    "delivery-query-addresses", "delivery-create-address", "query-survey-coupon",
]


class OfflineUnavailableError(RuntimeError):
    pass


def snapshot_meta() -> dict[str, Any]:
    path = os.path.join(SNAPSHOT_DIR, "meta.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def snapshot_available() -> bool:
    return all(os.path.exists(os.path.join(SNAPSHOT_DIR, f)) for f in TOOL_FILES.values())


class OfflineClient:
    """只读快照的 MCP 客户端替身。"""

    def __init__(self, snapshot_dir: str = SNAPSHOT_DIR) -> None:
        self.dir = snapshot_dir
        self.meta = snapshot_meta()
        self.is_offline = True

    def initialize(self) -> dict[str, Any]:
        server = self.meta.get("server") or {"name": "mcd-mcp", "version": "1.0.0"}
        return {
            "serverInfo": server,
            "protocolVersion": self.meta.get("protocol") or "2024-11-05",
            "offline": True,
            "captured_at": self.meta.get("captured_at", ""),
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": "offline snapshot stub"} for n in TOOL_NAMES]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        fname = TOOL_FILES.get(name)
        if not fname:
            raise OfflineUnavailableError(
                f"离线快照未包含工具 {name}。需要真实数据请设置 MCD_MCP_TOKEN。"
            )
        path = os.path.join(self.dir, fname)
        if not os.path.exists(path):
            raise OfflineUnavailableError(f"缺少快照文件 {fname}，请先运行 capture_snapshot.py")
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        # now-time-info 用抓取时刻代替，避免离线模式下时间语义混乱
        if name == "now-time-info":
            return {"success": True, "data": {"formatted": self.meta.get("captured_at", "")}}
        return payload


def offline_banner() -> str:
    m = snapshot_meta()
    at = m.get("captured_at", "未知时间")
    return (
        f"[离线快照模式] 未检测到 MCD_MCP_TOKEN，已改用本地快照（抓取于 {at}）。\n"
        f"               这是**历史数据，不是实时结果**；如需实时数据请设置 MCD_MCP_TOKEN。"
    )
