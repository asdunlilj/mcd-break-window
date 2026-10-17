"""抓取真实 MCP 返回，落成本地离线快照（scripts/sample_data/）

只在开发时运行一次，产物入库，供**没有 MCP Token**的人直接跑通。
运行方式（需要环境变量 MCD_MCP_TOKEN）：
    python scripts/capture_snapshot.py --store 3450415
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import McdMcpClient, McpError, envelope_data  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")


def dump(name: str, payload) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    size = os.path.getsize(path)
    print(f"  {name:<20} {size/1024:7.1f} KB")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--store", default="3450415")
    p.add_argument("--city", default="德阳市")
    p.add_argument("--keyword", default="四川工业科技学院")
    a = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    client = McdMcpClient()
    info = client.initialize()
    server = info.get("serverInfo", {})
    tools = client.list_tools()
    print(f"服务端 {server.get('name')} v{server.get('version')}，Tool {len(tools)} 个")

    meta = {
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "server": server,
        "protocol": info.get("protocolVersion"),
        "tool_count": len(tools),
        "store": a.store,
        "notice": "离线快照：数据为抓取时刻的真实 MCP 返回，不是实时数据",
    }

    try:
        stores = envelope_data(client.call_tool("query-nearby-stores", {
            "city": a.city, "keyword": a.keyword, "beType": 1, "searchType": 2,
        }))
        dump("stores.json", stores)
    except McpError as exc:
        print(f"  stores 失败: {exc}")

    try:
        menu = envelope_data(client.call_tool("query-meals", {
            "storeCode": a.store, "orderType": 1, "beType": 1,
        }))
        dump("menu.json", menu)
        cats = len(menu.get("categories", []) or [])
        meals = len(menu.get("meals", {}) or {})
        print(f"    分类 {cats} 个，餐品 {meals} 项")
    except McpError as exc:
        print(f"  menu 失败: {exc}")

    try:
        camp = envelope_data(client.call_tool("campaign-calendar", {}))
        dump("campaign.json", camp)
    except McpError as exc:
        print(f"  campaign 失败: {exc}")

    try:
        nut = envelope_data(client.call_tool("list-nutrition-foods", {}))
        dump("nutrition.json", nut)
    except McpError as exc:
        print(f"  nutrition 失败: {exc}")

    with open(os.path.join(OUT_DIR, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"  meta.json            {os.path.getsize(os.path.join(OUT_DIR, 'meta.json'))/1024:7.1f} KB")
    print(f"\n快照已写入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
