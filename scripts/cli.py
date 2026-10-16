"""麦麦课间窗口 —— 命令行入口

用法（Windows 用 set 设置环境变量，macOS/Linux 用 export）：

    set MCD_MCP_TOKEN=你的Token

    python cli.py doctor
    python cli.py campaign
    python cli.py menu --store 3450415
    python cli.py advise --store 3450415 --break 10 --budget 25
    python cli.py watch --order <orderId>          # 轮询订单状态，实测真实耗时
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from break_window import Item, PrepModel, humanize, load_nutrition, parse_menu, recommend
from mcd_client import McdMcpClient, McpError

NUTRITION_CACHE = "nutrition.txt"
CAMPAIGN_KEYWORDS = ("上新", "联名", "限时", "第二份", "促销", "抢购")

# 订单状态：1待支付 2配餐中 3配送中 4已完成 5已取消 6已评价 10餐厅确认配餐中
READY_STATUSES = {"2", "10", "3", "4"}


def _client() -> McdMcpClient:
    try:
        return McdMcpClient()
    except McpError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2)


def _data(payload):
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload["data"]
    if isinstance(payload, str):
        from mcd_client import parse_menu_block
        parsed = parse_menu_block(payload)
        if parsed:
            return parsed.get("data", parsed)
    return payload


def _campaign_active(client: McdMcpClient, today: str) -> bool:
    """今天是否有进行中的促销/上新活动。"""
    try:
        raw = _data(client.call_tool("campaign-calendar"))
        text = json.dumps(raw, ensure_ascii=False)
        idx = text.find(today)
        if idx < 0:
            return False
        window = text[idx : idx + 1200]
        return any(k in window for k in CAMPAIGN_KEYWORDS)
    except McpError:
        return False


# ---------------------------------------------------------------- 子命令

def cmd_doctor(args) -> int:
    client = _client()
    info = client.initialize()
    server = info.get("serverInfo", {})
    tools = client.list_tools()
    print("=== 连接正常 ===")
    print(f"服务端      : {server.get('name')} v{server.get('version')}")
    print(f"协议版本    : {info.get('protocolVersion')}")
    print(f"可用 Tool 数: {len(tools)}")
    names = [t["name"] for t in tools]
    print("Tool 列表   : " + ", ".join(names))
    return 0


def cmd_campaign(args) -> int:
    client = _client()
    payload = _data(client.call_tool("campaign-calendar"))
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    today = datetime.now().strftime("%Y-%m-%d")
    print(text[:4000])
    print()
    print(f"今日({today})是否存在促销/上新活动：{_campaign_active(client, today)}")
    return 0


def cmd_menu(args) -> int:
    client = _client()
    raw = _data(client.call_tool("query-meals", {
        "storeCode": args.store, "orderType": 1, "beType": 1,
    }))
    items = parse_menu(raw if isinstance(raw, dict) else {})
    if not items:
        print("未解析到菜单，原始返回：")
        print(str(raw)[:2000])
        return 1

    model = PrepModel()
    campaign = _campaign_active(client, datetime.now().strftime("%Y-%m-%d"))
    at = datetime.now()
    print(f"门店 {args.store} 在售餐品 {len(items)} 项，基准时间 {at:%Y-%m-%d %H:%M}")
    print(f"{'类别':<12}{'基准':>6}  {'餐品':<24}{'现价':>8}   预估等待")
    print("-" * 78)
    rows = []
    for item in items:
        est = model.estimate([item], at=at, store_code=args.store, campaign_active=campaign)
        rows.append((est.minutes, item))
    for minutes, item in sorted(rows):
        print(f"{item.category:<12}{item.base_minutes:>6.1f}  {item.name[:22]:<24}"
              f"{item.price:>8.1f}   {minutes:>5.1f} 分钟")
    return 0


def cmd_advise(args) -> int:
    client = _client()
    raw = _data(client.call_tool("query-meals", {
        "storeCode": args.store, "orderType": 1, "beType": 1,
    }))
    items = parse_menu(raw if isinstance(raw, dict) else {})
    if not items:
        print("未解析到菜单。")
        return 1

    model = PrepModel()
    at = datetime.now()
    campaign = _campaign_active(client, at.strftime("%Y-%m-%d"))
    nutrition = load_nutrition(NUTRITION_CACHE)

    budget = args.budget if args.budget is not None else args.break_minutes + 3
    print("=" * 78)
    print(f"麦麦课间窗口 · 门店 {args.store}")
    print(f"当前时间 {at:%H:%M}　课间时长 {args.break_minutes} 分钟　"
          f"可接受等待上限 {budget:.1f} 分钟（含 {budget - args.break_minutes:.0f} 分钟缓冲）")
    print("=" * 78)

    picks = recommend(model, items, budget_minutes=budget, at=at,
                      store_code=args.store, campaign_active=campaign,
                      nutrition=nutrition, top_k=args.top)

    print("\n【推荐】现在下单，可在课间内取到：")
    if not picks or not picks[0].fits:
        print("  当前时段没有任何餐品能在课间窗口内出餐，建议提前下单或选择非课间时段。")
    for rank, pick in enumerate(picks[: args.top], 1):
        if not pick.fits:
            continue
        print(f"\n  {rank}. {pick.item.name}　{cpick(pick.item.price)}")
        print(f"     保守预估 {pick.safe_wait_minutes:.1f} 分钟"
              f"｜点估计 {pick.wait_minutes:.1f} 分钟｜综合得分 {pick.score:.3f}")
        for reason in pick.reasons:
            print(f"     · {reason}")

    slow = [p for p in picks if not p.fits]
    if slow:
        worst = max(slow, key=lambda p: p.safe_wait_minutes)
        print(f"\n【避坑】{worst.item.name} 保守预估 {worst.safe_wait_minutes:.1f} 分钟，"
              f"超出课间窗口 {worst.safe_wait_minutes - budget:.1f} 分钟，本时段不建议点。")

    if picks and picks[0].guard_note:
        print(f"\n⚠️  {picks[0].guard_note}")

    print("\n【模型明细】以最快推荐项为例：")
    fastest = picks[0].item if picks else None
    if fastest is not None:
        print("— 点估计（用于排序）—")
        print(humanize(model.estimate([fastest], at=at, store_code=args.store,
                                      campaign_active=campaign, safe=False)))
        print("\n— 保守包络（用于判定，强制 K_store ≥ 1.0）—")
        print(humanize(model.estimate([fastest], at=at, store_code=args.store,
                                      campaign_active=campaign, safe=True)))

    report = model.integrity_report()
    print(f"\n【校准完整性】已采纳 {report['samples_accepted']} 条，"
          f"已丢弃 {report['samples_rejected']} 条，冻结状态：{report['frozen']}")
    if report["frozen"]:
        print(f"  冻结原因：{report['frozen_reason']}")
    return 0


def cpick(price: float) -> str:
    return f"¥{price:.1f}" if price else "价格未知"


def _extract_status(detail) -> str:
    blob = json.dumps(detail, ensure_ascii=False)
    for key in ("orderStatus", "status"):
        marker = f'"{key}":"'
        pos = blob.find(marker)
        if pos >= 0:
            return blob[pos + len(marker): blob.find('"', pos + len(marker))]
    return ""


def cmd_watch(args) -> int:
    """轮询订单状态实测耗时，并支持用「用户确认真值」做更可信的校准。

    ⚠️ 门店压单时会批量把订单点成「已完成」清屏，因此 orderStatus 只作参考，
    真正可信的真值是用户实际拿到餐的时刻（source="user"）。
    """
    client = _client()
    model = PrepModel()
    print(f"开始轮询订单 {args.order}（Ctrl+C 终止）…")
    print("提示：压单时门店可能提前点『已完成』，状态时刻仅作参考；")
    print("      更可信的是你实际拿到餐的时刻，可用 --actual 直接传入分钟数。")

    t0 = time.time()
    last = None
    first_observed = None
    status_elapsed = None

    try:
        while True:
            detail = _data(client.call_tool("query-order", {"orderId": args.order}))
            status = _extract_status(detail)
            elapsed = time.time() - t0
            if status != last:
                print(f"  [{elapsed:6.1f}s] 状态 → {status or '未知'}")
                if first_observed is None:
                    first_observed = elapsed
                if status in READY_STATUSES and status_elapsed is None:
                    status_elapsed = elapsed
                last = status
            if status_elapsed is not None:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已中断轮询。")

    load = model.load_factor(datetime.now(), args.store)[1]

    if status_elapsed is not None:
        print("\n— 来自状态机的样本（可能被批量点完成污染）—")
        result = model.calibrate(
            args.store, args.predicted, status_elapsed / 60.0,
            base_minutes=args.base_minutes, source="status",
            load=load, first_observed_seconds=first_observed or 999.0,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    actual = args.actual
    if actual is None:
        try:
            line = input("\n你实际拿到餐大约在下单后多少分钟？（直接回车跳过）").strip()
            if line:
                actual = float(line)
        except (EOFError, ValueError, RuntimeError):
            actual = None

    if actual is not None:
        print("\n— 来自用户确认的样本（可信度最高）—")
        result = model.calibrate(
            args.store, args.predicted, float(actual),
            base_minutes=args.base_minutes, source="user", load=load,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_integrity(args) -> int:
    """输出校准数据完整性报告，供人工审计。"""
    model = PrepModel()
    print(json.dumps(model.integrity_report(), ensure_ascii=False, indent=2))
    return 0


def cmd_unfreeze(args) -> int:
    """人工复核确认数据可信后，解除校准冻结。"""
    model = PrepModel()
    print(json.dumps(model.unfreeze(args.reason), ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    parser = argparse.ArgumentParser(prog="mcd-break-window",
                                     description="麦麦课间窗口 · 取餐等待预估与快取推荐")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="连通性与工具清单自检").set_defaults(func=cmd_doctor)
    sub.add_parser("campaign", help="查看当月活动日历与今日促销状态").set_defaults(func=cmd_campaign)

    p_menu = sub.add_parser("menu", help="拉取门店菜单及全菜单预估")
    p_menu.add_argument("--store", required=True, help="门店编码 storeCode")
    p_menu.set_defaults(func=cmd_menu)

    p_adv = sub.add_parser("advise", help="课间窗口快取推荐（核心功能）")
    p_adv.add_argument("--store", required=True)
    p_adv.add_argument("--break", dest="break_minutes", type=float, default=10.0,
                       help="课间时长（分钟），默认 10")
    p_adv.add_argument("--budget", type=float, default=None,
                       help="可接受等待上限（分钟），默认＝课间时长＋3 分钟缓冲")
    p_adv.add_argument("--top", type=int, default=3)
    p_adv.set_defaults(func=cmd_advise)

    p_watch = sub.add_parser("watch", help="轮询订单状态实测耗时并校准（含完整性守卫）")
    p_watch.add_argument("--order", required=True)
    p_watch.add_argument("--store", default="-")
    p_watch.add_argument("--predicted", type=float, default=3.0, help="下单时的预估分钟数")
    p_watch.add_argument("--base-minutes", dest="base_minutes", type=float, default=None,
                         help="该单的基准出餐分钟数，用于物理下界检验；缺省取 --predicted")
    p_watch.add_argument("--actual", type=float, default=None,
                         help="已知的真实取餐分钟数（用户确认口径，非交互）")
    p_watch.add_argument("--interval", type=float, default=10.0)
    p_watch.set_defaults(func=cmd_watch)

    sub.add_parser("integrity", help="输出校准数据完整性报告（供审计）").set_defaults(func=cmd_integrity)

    p_uf = sub.add_parser("unfreeze", help="人工复核后解除校准冻结")
    p_uf.add_argument("--reason", default="人工复核后解除")
    p_uf.set_defaults(func=cmd_unfreeze)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
