"""麦麦课间窗口 —— 本地预览生成器

拉取真实门店数据，为当天每个课间窗口生成推荐结果，输出**单文件 HTML 看板**：
无需联网、无需服务器、无需第三方依赖，双击即可打开，适合给门店经理演示。

用法：
    set MCD_MCP_TOKEN=你的Token
    python preview.py --store 3450415
    python preview.py --store 3450415 --out ..\\preview.html

说明：生成的 HTML 会把数据快照内嵌进去，因此可以离线传阅。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from break_window import (  # noqa: E402
    WINDOW_KIND_TEXT,
    PrepModel,
    load_nutrition,
    parse_menu,
    recommend,
    score_weights,
)
from mcd_client import McpError, make_client  # noqa: E402

NUTRITION_CACHE = "nutrition.txt"
CAMPAIGN_KEYWORDS = ("上新", "联名", "限时", "第二份", "促销", "抢购")
KIND_ORDER = {"short": 0, "long": 1, "meal": 2}
KIND_COLOR = {"short": "#c0392b", "long": "#1f6feb", "meal": "#2e7d32"}


def envelope(payload):
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload["data"]
    return payload


def campaign_active(client: McdMcpClient, today: str) -> bool:
    try:
        text = json.dumps(envelope(client.call_tool("campaign-calendar")), ensure_ascii=False)
        idx = text.find(today)
        if idx < 0:
            return False
        return any(k in text[idx: idx + 1200] for k in CAMPAIGN_KEYWORDS)
    except McpError:
        return False


def build(args) -> str:
    client, mode = make_client()
    info = client.initialize()
    server = info.get("serverInfo", {})

    today = datetime.now().strftime("%Y-%m-%d")

    # 门店信息
    store = {"storeCode": args.store, "storeName": "", "address": "", "distance": 0}
    if args.city and args.keyword:
        try:
            stores = envelope(client.call_tool("query-nearby-stores", {
                "city": args.city, "keyword": args.keyword,
                "beType": 1, "searchType": 2,
            }))
            for s in stores or []:
                if str(s.get("storeCode")) == str(args.store):
                    store = s
                    break
        except McpError as exc:
            print(f"[warn] 门店信息获取失败：{exc}", file=sys.stderr)

    # 菜单
    raw = envelope(client.call_tool("query-meals", {
        "storeCode": args.store, "orderType": 1, "beType": 1,
    }))
    items = parse_menu(raw if isinstance(raw, dict) else {})
    if not items:
        raise SystemExit("未解析到菜单，无法生成预览。")

    nutrition = load_nutrition(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            NUTRITION_CACHE))
    model = PrepModel()
    camp = campaign_active(client, today)
    walk = float((model.schedule.get("walking") or {}).get("round_trip_minutes", 4.0))
    now = datetime.now()

    windows_payload = []
    for w in model.day_windows(now):
        at = now.replace(hour=int(w.start[:2]), minute=int(w.start[3:]), second=0, microsecond=0)
        budget = max(1.0, w.minutes - walk)
        picks = recommend(model, items, budget_minutes=budget, at=at, store_code=args.store,
                          campaign_active=camp, nutrition=nutrition, top_k=args.top)
        w_weights = score_weights(budget)

        recs = []
        for rank, p in enumerate(picks, 1):
            est_point = model.estimate([p.item], at=at, store_code=args.store,
                                       campaign_active=camp, safe=False)
            recs.append({
                "rank": rank,
                "name": p.item.name,
                "price": p.item.price,
                "category": p.item.category,
                "completeness": p.item.completeness,
                "fits": p.fits,
                "wait": round(p.wait_minutes, 1),
                "safe_wait": round(p.safe_wait_minutes, 1),
                "score": round(p.score, 3),
                "breakdown": est_point.breakdown,
            })

        avoid = None
        infeasible = [p for p in picks if not p.fits]
        if infeasible:
            worst = max(infeasible, key=lambda p: p.safe_wait_minutes)
            avoid = {"name": worst.item.name, "safe_wait": round(worst.safe_wait_minutes, 1),
                     "over": round(worst.safe_wait_minutes - budget, 1)}

        # 客户版用：同窗口下最慢的在售餐品，用于「推荐 vs 最慢」的价值对比
        slowest = None
        for it in items:
            e = model.estimate([it], at=at, store_code=args.store,
                               campaign_active=camp, safe=True)
            if slowest is None or e.minutes > slowest[1]:
                slowest = (it, e.minutes)

        windows_payload.append({
            **w.as_dict(),
            "budget": round(budget, 1),
            "weights": w_weights,
            "recommendations": recs,
            "avoid": avoid,
            "slowest": ({"name": slowest[0].name, "wait": round(slowest[1], 1),
                         "category": slowest[0].category} if slowest else None),
            "slow_count": sum(
                1 for it in items
                if model.estimate([it], at=at, store_code=args.store,
                                  campaign_active=camp, safe=True).minutes > budget
            ),
            "infeasible_count": sum(1 for r in recs if not r["fits"]),
            "remaining": max(0, int((w_minus(w.start, now)).total_seconds() // 60))
            if w.start <= now.strftime("%H:%M") < w.end else None,
        })

    windows_payload.sort(key=lambda x: (KIND_ORDER.get(x["kind"], 9), x["start"]))

    data = {
        "store": store,
        "storeCode": args.store,
        "date": today,
        "weekday": "一二三四五六日"[now.weekday()],
        "schedule_label": model.profile_for(now).get("label", ""),
        "school": model.schedule.get("school", ""),
        "term": model.schedule.get("term", ""),
        "effective": model.schedule.get("effective", ""),
        "store_hours": model.schedule.get("store_hours") or {},
        "walking": walk,
        "menu_count": len(items),
        "campaign_active": camp,
        "server": f"{server.get('name')} v{server.get('version')}",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "offline": mode == "offline",
        "offline_captured_at": info.get("captured_at", "") if mode == "offline" else "",
        "frozen": bool(model.state.get("frozen")),
        "windows": windows_payload,
    }
    return data


def w_minus(hhmm: str, now: datetime):
    return now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)


# ---------------------------------------------------------------- HTML 渲染

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1a1d21;line-height:1.65;
     padding:clamp(14px,3vw,28px) clamp(12px,2.6vw,20px) clamp(34px,6vw,60px);
     overflow-wrap:break-word;word-break:break-word}
button,summary,a{-webkit-tap-highlight-color:transparent;touch-action:manipulation}
img,table{max-width:100%}
.wrap{max-width:1060px;margin:0 auto}

header{background:#fff;border-radius:clamp(10px,2.4vw,14px);padding:clamp(16px,3.4vw,24px) clamp(15px,3.4vw,28px);
       border:1px solid #e6e8ec;margin-bottom:clamp(12px,2.4vw,18px)}
h1{font-size:clamp(18px,4.4vw,23px);font-weight:700;letter-spacing:-.2px;line-height:1.3}
h1 span{color:#c0392b}
.sub{color:#5b6270;font-size:clamp(12.5px,3.1vw,13.5px);margin-top:6px}
.meta{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}
.chip{background:#f1f3f6;border:1px solid #e2e5ea;border-radius:999px;padding:4px 11px;
      font-size:clamp(11.5px,2.9vw,12.5px);color:#39404d}

section{background:#fff;border-radius:clamp(10px,2.4vw,14px);
        padding:clamp(15px,3.4vw,22px) clamp(14px,3.4vw,26px);
        border:1px solid #e6e8ec;margin-bottom:clamp(12px,2.4vw,18px)}
h2{font-size:clamp(14px,3.4vw,15px);font-weight:650;margin-bottom:13px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:15px;background:#c0392b;border-radius:2px;flex:0 0 auto}
/* 面板标题：类型 / 时间 / 课程对 三段可换行对齐，窄屏不会挤成一团 */
h2.ph{flex-wrap:wrap;gap:4px 10px;align-items:baseline;line-height:1.5}
.ph .pt{font-weight:700;font-size:clamp(14px,3.6vw,15.5px)}
.ph .pw{font-weight:650;font-variant-numeric:tabular-nums;font-size:clamp(13.5px,3.4vw,15px)}
.ph .pl{color:#6b7280;font-weight:500;font-size:.92em}

.prob{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:12px}
.pcard{border-radius:10px;padding:13px 15px;font-size:clamp(12.5px,3.1vw,13.5px);border:1px solid}
.pcard h3{font-size:clamp(12.5px,3.1vw,13.5px);font-weight:650;margin-bottom:6px}
.pcard.s{background:#fdf6f5;border-color:#f3dbd8;color:#7a2e26}
.pcard.m{background:#f4f8ff;border-color:#d8e5fb;color:#1c3f77}

/* 时间轴用自适应网格而不是横向滚动：
   移动端横向滚动是可用性陷阱（用户看不出右边还有内容） */
.tl{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,124px),1fr));gap:8px;margin-bottom:11px}
.win{min-height:54px;cursor:pointer;border:1.5px solid #e2e5ea;background:#fff;border-radius:10px;
     padding:8px 12px;text-align:left;font-family:inherit;transition:.15s;
     display:flex;flex-direction:column;justify-content:center;gap:1px}
.win:hover{border-color:#b9c0cc}
.win.on{border-color:#c0392b;background:#fef7f6;box-shadow:0 0 0 3px rgba(192,57,43,.09)}
.win .t{font-size:clamp(13px,3.3vw,14px);font-weight:650;font-variant-numeric:tabular-nums}
.win .d{font-size:clamp(11px,2.9vw,11.5px);color:#6b7280}
.win .k{font-size:11px;font-weight:600;margin-top:3px;display:inline-block;padding:1px 7px;border-radius:999px;align-self:flex-start}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:11px}
.rec{border:1px solid #e6e8ec;border-radius:11px;padding:14px 15px;background:#fff;position:relative}
.rec.no{opacity:.55;border-style:dashed}
.rec .rk{position:absolute;top:12px;right:13px;font-size:11px;color:#9aa1ad;font-weight:600}
.rec .nm{font-size:clamp(13.5px,3.5vw,15px);font-weight:650;padding-right:26px;line-height:1.4}
.rec .pr{font-size:clamp(12.5px,3.2vw,13px);color:#c0392b;font-weight:650;margin-top:4px}
.rec .wt{font-size:clamp(12px,3vw,12.5px);color:#4a5160;margin-top:9px;padding-top:9px;border-top:1px dashed #e9ebef}
.rec .wt b{font-size:clamp(15px,3.9vw,16px);color:#1a1d21;font-variant-numeric:tabular-nums}
.tag{display:inline-block;background:#f1f3f6;border-radius:5px;padding:1px 7px;
     font-size:clamp(11px,2.8vw,11.5px);color:#4a5160;margin-top:6px}
.avoid{margin-top:12px;background:#fffbeb;border:1px solid #f5e2ac;border-radius:10px;
       padding:10px 13px;font-size:clamp(12px,3vw,13px);color:#7a5c12}

table{width:100%;border-collapse:collapse;font-size:clamp(11.5px,2.9vw,13px)}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #eef0f3;overflow-wrap:anywhere;vertical-align:top}
th{color:#6b7280;font-weight:600;font-size:clamp(11px,2.8vw,12px)}
td.n{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}

code{background:#f1f3f6;padding:1.5px 6px;border-radius:5px;font-size:clamp(11.5px,2.9vw,12.5px);
     font-family:ui-monospace,Menlo,Consolas,monospace}
.formula{background:#f8f9fb;border:1px solid #e9ebef;border-radius:9px;padding:12px 14px;
         font-size:clamp(10.5px,2.7vw,13px);font-family:ui-monospace,Menlo,Consolas,monospace;
         color:#2b3038;line-height:1.75;margin-bottom:13px}
details{margin-top:12px}
summary{cursor:pointer;font-size:clamp(12px,3vw,13px);color:#1f6feb;font-weight:600;
        user-select:none;padding:7px 0;min-height:34px;display:flex;align-items:center}
.note{font-size:clamp(11.5px,2.9vw,12.5px);color:#6b7280;margin-top:11px;line-height:1.8}
.warn{background:#fef7f6;border:1px solid #f3dbd8;border-radius:9px;padding:10px 13px;
      font-size:clamp(12px,3vw,13px);color:#7a2e26;margin-bottom:13px}
footer{text-align:center;font-size:clamp(11px,2.8vw,12px);color:#9aa1ad;margin-top:clamp(18px,4vw,26px);line-height:1.9}

/* 窄屏二次收紧：更小的行内边距 + 详情表格字号 */
@media(max-width:420px){
  th,td{padding:6px 4px}
  .rec{padding:13px 13px}
  .chip{padding:3px 9px}
}
/* 打印/导出 PDF 时去掉交互背景色 */
@media print{body{background:#fff;padding:0}section,header{border-color:#ddd;break-inside:avoid}}
"""



def esc(v) -> str:
    return html.escape(str(v))


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    store = data["store"]
    name = esc(store.get("storeName") or f"门店 {data['storeCode']}")
    addr = esc(store.get("address") or "")
    hours = data["store_hours"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#f5f6f8">
<meta name="format-detection" content="telephone=no,address=no,email=no">
<title>麦麦课间窗口 · {name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>麦麦课间窗口 <span id="hdr-kind"></span></h1>
    <div class="sub">{name}{"　·　" + addr if addr else ""}</div>
    <div class="meta">
      <span class="chip">门店编号 {esc(data['storeCode'])}</span>
      <span class="chip">营业 {esc(hours.get('open','—'))}–{esc(hours.get('close','—'))}</span>
      <span class="chip">{esc(data['date'])}（周{esc(data['weekday'])}）</span>
      <span class="chip">适用作息：{esc(data['schedule_label'])}</span>
      <span class="chip">在售餐品 {data['menu_count']} 项</span>
      <span class="chip">MCP {esc(data['server'])}</span>
      {('<span class="chip" style="background:#fff7ed;border-color:#f3ddc0;color:#8a4b12">离线快照 · 抓取于 ' + esc(data['offline_captured_at']) + '</span>') if data.get('offline') else ''}
    </div>
  </header>

  <section>
    <h2>要解决的问题</h2>
    <div class="prob">
      <div class="pcard s">
        <h3>学生侧</h3>
        课间只有 10 分钟。到店再下单怕等餐超时迟到；提前点好，又怕等太久餐品放凉。
        <b>完全不知道要等多久，只能赌运气。</b>
      </div>
      <div class="pcard m">
        <h3>门店侧</h3>
        课间瞬时客流集中，出餐压力陡增，被迫压单。
        <b>由信息不足造成的顾客选择，正撞在最忙的时段上。</b>
      </div>
    </div>
    <div class="note">
      本看板把等待时长显性化，并在课间窗口内推荐出餐快的餐品——学生不焦虑，门店把峰值削平一点。
      数据来自麦当劳 MCP 的实时接口，非模拟数据。
    </div>
  </section>

  <section>
    <h2>今天的课间窗口（点击切换）</h2>
    <div class="tl" id="tl"></div>
    <div class="note" id="tl-note"></div>
  </section>

  <section id="panel"></section>

  <section>
    <h2>预估模型（可审计）</h2>
    <div class="formula">T_wait = T_base(餐品) × K_time(时段) × K_break(课间) × K_campaign(促销) × K_store(门店) × K_load(负载)</div>
    <table>
      <tr><th>系数</th><th>取值来源</th><th>当前状态</th></tr>
      <tr><td>基准出餐 T_base</td><td>按餐品工序分类查表（饮品 0.8 → 安格斯 4.2 分钟）</td><td>见下方单项拆解</td></tr>
      <tr><td>时段 K_time</td><td>校园作息曲线（午高峰 1.55 → 下午课 0.95）</td><td>见下方单项拆解</td></tr>
      <tr><td>课间脉冲 K_break</td><td>小课间 1.35 / 大课间 1.20 / 餐段 1.10</td><td>见下方单项拆解</td></tr>
      <tr><td>促销 K_campaign</td><td>当日活动日历命中促销/上新</td><td>{"已命中（×1.25）" if data['campaign_active'] else "未命中（×1.00）"}</td></tr>
      <tr><td>门店 K_store</td><td>用 query-order 实测真实耗时自校准</td><td>{"已冻结（疑似批量点完成污染）" if data['frozen'] else "正常"}</td></tr>
      <tr><td>负载 K_load</td><td>本机近 15 分钟成交样本数</td><td>见下方单项拆解</td></tr>
    </table>
    <div class="note">
      <b>为什么必须自己建模：</b>麦当劳 MCP 全部 35 个 Tool 中<b>没有任何一个提供出餐时长或排队队列信号</b>，
      唯一的"预计"字段属于外送场景的预计送达时间。等待时长无法查询，只能建模预估。
      <br><b>可用于等待的时间</b> = 课间时长 − 往返步行（默认 {data['walking']:.0f} 分钟），不额外放宽。
      <br><b>门店压单的应对：</b>门店压单时会批量把订单点成"已完成"清屏，会污染校准数据。
      已实施五道防线，其中最关键的保证是：<b>判定口径永远不会比未校准基线更乐观</b>。
    </div>
  </section>

  <footer>
    {esc(data['school'])}　{esc(data['term'])}（{esc(data['effective'])}）· 数据生成于 {esc(data['generated_at'])}<br>
    本页面为参赛作品演示，非麦当劳官方产品；餐品信息与价格以麦当劳官方渠道为准
  </footer>
</div>

<script>
const DATA = {payload};
const KIND_COLOR = {json.dumps(KIND_COLOR)};

function fmt(n, d) {{ return (n === null || n === undefined) ? "—" : Number(n).toFixed(d === undefined ? 1 : d); }}

function renderTimeline() {{
  const tl = document.getElementById("tl");
  tl.innerHTML = "";
  DATA.windows.forEach((w, i) => {{
    const b = document.createElement("button");
    b.className = "win" + (i === 0 ? " on" : "");
    b.dataset.i = i;
    const col = KIND_COLOR[w.kind] || "#666";
    b.innerHTML = `<div class="t">${{w.start}}–${{w.end}}</div>`
      + `<div class="d">${{w.minutes}} 分钟</div>`
      + `<span class="k" style="color:${{col}};background:${{col}}18">${{w.kind_text.split("（")[0]}}</span>`;
    b.onclick = () => {{
      document.querySelectorAll(".win").forEach(e => e.classList.remove("on"));
      b.classList.add("on");
      renderPanel(i);
    }};
    tl.appendChild(b);
  }});
  const n = DATA.windows.length;
  document.getElementById("tl-note").textContent =
    `共 ${{n}} 个窗口，点击任意窗口查看该时段的推荐。`
    + `小课间只来得及点快餐品，大课间与餐段才推荐完整正餐。`;
}}

function renderPanel(i) {{
  const w = DATA.windows[i];
  const p = document.getElementById("panel");
  const col = KIND_COLOR[w.kind] || "#666";
  const cards = w.recommendations.map(r => {{
    const fit = r.fits;
    const bd = r.breakdown || {{}};
    return `<div class="rec ${{fit ? "" : "no"}}">
      <div class="rk">#${{r.rank}}</div>
      <div class="nm">${{r.name}}</div>
      <div class="pr">¥${{fmt(r.price, 1)}}</div>
      <span class="tag">${{r.category}}</span>
      <div class="wt">
        ${{fit ? "预计可等待" : "超出窗口"}} <b>${{fmt(r.safe_wait, 1)}}</b> 分钟
        <span style="color:#9aa1ad">（点估计 ${{fmt(r.wait, 1)}} 分钟）</span>
      </div>
    </div>`;
  }}).join("");

  const bd = w.recommendations[0] ? (w.recommendations[0].breakdown || {{}}) : null;
  const detail = bd ? `
    <details>
      <summary>查看 ${{w.recommendations[0].name}} 的完整系数拆解</summary>
      <table style="margin-top:11px">
        <tr><th>项</th><th>值</th><th>说明</th></tr>
        <tr><td>基准出餐</td><td class="n">${{fmt(bd.base_minutes, 2)}} 分</td><td>按餐品工序分类</td></tr>
        <tr><td>时段系数</td><td class="n">${{fmt(bd.time_factor.value, 2)}}</td><td>${{bd.time_factor.label}}</td></tr>
        <tr><td>课间脉冲</td><td class="n">${{fmt(bd.break_pulse.value, 2)}}</td><td>${{w.kind_text}}</td></tr>
        <tr><td>促销系数</td><td class="n">${{fmt(bd.campaign_factor, 2)}}</td><td>${{DATA.campaign_active ? "当日有促销/上新活动" : "当日无促销活动"}}</td></tr>
        <tr><td>门店系数</td><td class="n">${{fmt(bd.store_factor, 2)}}</td><td>校准样本 ${{bd.calibration_samples}} 条</td></tr>
        <tr><td>负载系数</td><td class="n">${{fmt(bd.load_factor.value, 2)}}</td><td>近 15 分钟 ${{bd.load_factor.recent_orders}} 单</td></tr>
        <tr><td><b>合计</b></td><td class="n"><b>${{fmt(w.recommendations[0].wait, 1)}} 分</b></td><td>点估计口径</td></tr>
      </table>
    </details>` : "";

  const avoid = w.avoid ? `<div class="avoid">
      <b>避坑提示：</b>${{w.avoid.name}} 保守预估 ${{fmt(w.avoid.safe_wait, 1)}} 分钟，
      超出本窗口 ${{fmt(w.avoid.over, 1)}} 分钟，这个课间不建议点。
    </div>` : "";

  const kindShort = w.kind_text.split("（")[0];
  const kindNote = (w.kind_text.match(/（(.+)）/) || [null, ""])[1];

  p.innerHTML = `
    <h2 class="ph">
      <span class="pt" style="color:${{col}}">${{kindShort}}</span>
      <span class="pw">${{w.start}}–${{w.end}}</span>
      <span class="pl">${{w.label}}</span>
    </h2>
    <div class="meta" style="margin-bottom:15px">
      <span class="chip">${{kindNote}}</span>
      <span class="chip">窗口 ${{w.minutes}} 分钟</span>
      <span class="chip">扣步行 ${{fmt(DATA.walking, 0)}} 分钟后可用于等待 ${{fmt(w.budget, 1)}} 分钟</span>
      <span class="chip">打分权重：速度 ${{w.weights.speed}}｜完整度 ${{w.weights.completeness}}｜营养 ${{w.weights.nutrition}}｜价格 ${{w.weights.price}}</span>
    </div>
    <div class="grid">${{cards}}</div>
    ${{avoid}}
    ${{detail}}`;

  document.getElementById("hdr-kind").textContent = "· " + w.kind_text.split("（")[0];
}}

renderTimeline();
renderPanel(0);
</script>
</body>
</html>
"""


CLIENT_EXTRA_CSS = """
/* ── 客户版专用组件 ── */
.hero{background:linear-gradient(160deg,#fff 0%,#fff8f6 100%);border:1px solid #f0e2de}
.hero h1{font-size:clamp(21px,5.2vw,30px);line-height:1.28;margin-bottom:8px}
.hero .lead{font-size:clamp(13.5px,3.5vw,16px);color:#4a5160;line-height:1.7}
.hero .lead b{color:#c0392b}
.brand{display:inline-flex;align-items:center;gap:8px;background:#c0392b;color:#fff;
       border-radius:999px;padding:5px 14px;font-size:clamp(12px,3vw,13px);font-weight:650;margin-bottom:14px}
.quote{background:#fbfbfc;border-left:4px solid #c0392b;border-radius:0 10px 10px 0;
       padding:14px 16px;font-size:clamp(13px,3.4vw,14.5px);line-height:1.85;color:#33393f;margin-bottom:15px}
.quote .src{display:block;margin-top:9px;font-size:.86em;color:#8b93a0}
.verdict{display:flex;align-items:flex-start;gap:12px;background:#f1f8f3;border:1px solid #cfe6d6;
         border-radius:12px;padding:14px 16px;margin-bottom:14px}
.verdict.maybe{background:#fffaf0;border-color:#f0e0bd}
.verdict .ico{font-size:20px;line-height:1.35;flex:0 0 auto}
.verdict .vt{font-size:clamp(13.5px,3.5vw,15.5px);font-weight:650;color:#1d5c37;line-height:1.6}
.verdict.maybe .vt{color:#8a6412}
.verdict .vs{font-size:clamp(11.5px,3vw,12.5px);color:#5b6270;font-weight:400;margin-top:4px}
.chev{width:100%;background:#fff;border:1px solid #e6e8ec;border-radius:10px;padding:12px 14px;
      margin-bottom:10px;display:flex;align-items:center;gap:12px;text-align:left;font-family:inherit}
.chev .n{flex:0 0 auto;width:26px;height:26px;border-radius:50%;background:#c0392b;color:#fff;
         font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center}
.chev .t{font-size:clamp(13px,3.3vw,14.5px);font-weight:650;line-height:1.45}
.chev .d{font-size:clamp(11.5px,3vw,12.5px);color:#6b7280;margin-top:2px;font-weight:400}
.bar{background:#f4f6f8;border-radius:10px;padding:13px 15px;margin-top:14px}
.bar .row{display:flex;justify-content:space-between;font-size:clamp(12px,3.1vw,13px);margin-bottom:7px}
.bar .row b{font-variant-numeric:tabular-nums}
.bar .track{height:9px;border-radius:5px;background:#e4e8ed;overflow:hidden;margin-bottom:5px}
.bar .fill{height:100%;border-radius:5px}
.bar .cap{font-size:clamp(11.5px,3vw,12.5px);color:#6b7280;margin-top:9px;line-height:1.7}
.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:11px;margin-top:4px}
.num{background:#f8f9fb;border:1px solid #eaedf2;border-radius:11px;padding:14px 15px}
.num .k{font-size:clamp(20px,5vw,25px);font-weight:750;color:#c0392b;font-variant-numeric:tabular-nums;line-height:1.15}
.num .l{font-size:clamp(11.5px,3vw,12.5px);color:#5b6270;margin-top:5px;line-height:1.6}
.steps{counter-reset:s}
.step{display:flex;gap:13px;padding:13px 0;border-bottom:1px dashed #e9ebef}
.step:last-child{border-bottom:0}
.step .idx{flex:0 0 auto;width:26px;height:26px;border-radius:8px;background:#f1f3f6;color:#39404d;
           font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center}
.step .sm{font-size:clamp(12.5px,3.2vw,13.5px);font-weight:650}
.step .sd{font-size:clamp(11.5px,3vw,12.5px);color:#6b7280;margin-top:3px;line-height:1.75}
.ask{background:#f4f8ff;border:1px solid #d8e5fb;border-radius:11px;padding:14px 16px;
     font-size:clamp(12.5px,3.2vw,13.5px);color:#1c3f77;line-height:1.8;margin-top:13px}
.ask b{color:#123a6e}
"""

CLIENT_KIND = {"short": "课间", "long": "大课间", "meal": "饭点"}


def render_client(data: dict) -> str:
    """客户版：以门店经理的视角组织内容——从他的问题开头，以「你该怎么做」收尾。"""
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    store = data["store"]
    name = esc(store.get("storeName") or f"门店 {data['storeCode']}")
    addr = esc(store.get("address") or "")
    hours = data["store_hours"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#fff8f6">
<meta name="format-detection" content="telephone=no,address=no,email=no">
<title>麦麦课间窗口 · {name}</title>
<style>{CSS}{CLIENT_EXTRA_CSS}</style>
</head>
<body>
<div class="wrap">

  <header class="hero">
    <span class="brand">课间取餐体验方案</span>
    <h1>让课间十分钟，<br>不再靠运气</h1>
    <div class="lead">
      为 <b>{name}</b> 定制。学生下课就知道<b>该点什么、大概几分钟能拿到</b>；
      门店的课间订单从做得慢的餐品，流向做得快的餐品。
    </div>
    <div class="meta">
      <span class="chip">{esc(addr)}</span>
      <span class="chip">营业 {esc(hours.get('open','—'))}–{esc(hours.get('close','—'))}</span>
      <span class="chip">{esc(data['date'])}（周{esc(data['weekday'])}）</span>
      <span class="chip">在售餐品 {data['menu_count']} 项</span>
      <span class="chip">数据来自麦当劳官方接口</span>
      {('<span class="chip" style="background:#fff7ed;border-color:#f3ddc0;color:#8a4b12">离线快照 · 抓取于 ' + esc(data['offline_captured_at']) + '</span>') if data.get('offline') else ''}
    </div>
  </header>

  <section>
    <h2>您提出的问题</h2>
    <div class="quote">
      课间休息时间有限，同学们到店后再下单可能会怕等餐超时迟到；但提前把餐点好，
      如果等很久来取又会餐品品质不好。系统如果能提前告知大概多久可取餐，
      且如果能做到推荐餐品，既能减少学生焦虑，也能平缓门店高峰压力。
      <span class="src">—— 门店经理反馈</span>
    </div>
    <div class="prob">
      <div class="pcard s">
        <h3>学生这边</h3>
        只有 10 分钟课间，完全不知道要等多久。到店再点怕迟到，提前点又怕放凉——
        <b>只能赌</b>。
      </div>
      <div class="pcard m">
        <h3>门店这边</h3>
        课间客流集中，做得慢的餐品一旦被集中点，后面全堵住。
        <b>不是人手不够，是点单结构不巧</b>。
      </div>
    </div>
  </section>

  <section>
    <h2>我们做了三件事</h2>
    <div class="chev"><span class="n">1</span><span><span class="t">下单前就告诉学生大概几分钟能拿到</span>
      <span class="d">依据餐品做法、当前时段忙闲、是不是课间、当天有没有活动综合估算</span></span></div>
    <div class="chev"><span class="n">2</span><span><span class="t">在课间优先推荐做得快的餐品</span>
      <span class="d">同样的课间订单量，结构变了，出餐峰值就被削平一部分</span></span></div>
    <div class="chev"><span class="n">3</span><span><span class="t">越用越贴合这家店</span>
      <span class="d">参考本店实际取餐记录自动校准，不需要人工填表</span></span></div>
  </section>

  <section>
    <h2>今天的实际效果（点击时段切换）</h2>
    <div class="tl" id="tl"></div>
    <div class="note" id="tl-note"></div>
  </section>

  <section id="panel"></section>

  <section>
    <h2>对门店的三个好处</h2>
    <div class="prob">
      <div class="pcard m"><h3>削平课间峰值</h3>
        课间只推做得快的餐品，把慢工序餐品的订单分流出去。看板里的对比条就是这个差值的量化。</div>
      <div class="pcard m"><h3>减少"等太久不来了"</h3>
        学生知道要等多久，就能自己决定点还是换。减少点完又取消、或不取造成的损耗。</div>
      <div class="pcard m"><h3>减少口感投诉</h3>
        提前点好却放凉的投诉，本质是"学生高估了取餐时间"。预估做准，这类投诉自然减少。</div>
    </div>
    <div class="ask">
      <b>需要说明的一点：</b>这套预估是"算出来的"，不是接门店的排队系统——
      麦当劳开放接口里<b>没有</b>提供等待时长数据，我们是用餐品做法和客流规律推算的。
      所以初期会有偏差，靠实际记录逐步校准。这一点我们如实告知，不做夸大。
    </div>
  </section>

  <section>
    <h2>建议的试点方式</h2>
    <div class="steps">
      <div class="step"><span class="idx">1</span><span><span class="sm">先在校内群或门店小范围试两周</span>
        <span class="sd">学生打开看板就知道当下点什么、大概多久能拿到，不需要装任何东西。</span></span></div>
      <div class="step"><span class="idx">2</span><span><span class="sm">请店长校准两个参数</span>
        <span class="sd">① 从教室走到门店再走回来大约几分钟；② 哪些餐品做得快、哪些做得慢。
        这两项确认后，预估准确度会明显提升。</span></span></div>
      <div class="step"><span class="idx">3</span><span><span class="sm">两周后看三个数</span>
        <span class="sd">课间时段的订单结构是否向快餐品偏移、学生超时投诉是否减少、餐品放凉的反馈是否减少。
        这些都是可对照的，不必凭感觉判断效果。</span></span></div>
    </div>
  </section>

  <section>
    <h2>数据与隐私</h2>
    <div class="note">
      只看餐品、价格、活动这类公开信息，以及本店自己的取餐耗时记录用于校准。
      <b>不采集学生个人信息，不存储订单号、手机号</b>，不做二次分发。
      高峰期操作繁忙时产生的异常记录会被自动过滤，避免把预估带偏 —— 也就是说，
      <b>再忙的时候，这套预估也只会偏保守，不会报得比实际更乐观</b>。
    </div>
  </section>

  <footer>
    {esc(data['school'])}　{esc(data['term'])}（{esc(data['effective'])}）· 数据生成于 {esc(data['generated_at'])}<br>
    本页面为参赛作品演示，非麦当劳官方产品；餐品信息与价格以麦当劳官方渠道为准
  </footer>
</div>

<script>
const DATA = {payload};
const KIND_COLOR = {json.dumps(KIND_COLOR)};
const KIND_SHORT = {json.dumps(CLIENT_KIND, ensure_ascii=False)};

function fmt(n, d) {{ return (n === null || n === undefined) ? "—" : Number(n).toFixed(d === undefined ? 1 : d); }}

function verdictOf(w) {{
  const best = w.recommendations.find(r => r.fits);
  if (best) {{
    return {{ ok: true, ico: "✅", t: `这个时段推荐点「${{best.name}}」，约 ${{fmt(best.safe_wait, 0)}} 分钟能拿到，来得及`,
             s: `价格 ¥${{fmt(best.price, 1)}}　·　按 ${{w.minutes}} 分钟的时间窗口估算，已扣掉路上往返的时间` }};
  }}
  return {{ ok: false, ico: "⚠️", t: "这个时间段太紧，没有餐品能在窗口内做好",
           s: "建议提前一节课点好，或者放到饭点再点" }};
}}

function renderTimeline() {{
  const tl = document.getElementById("tl");
  tl.innerHTML = "";
  DATA.windows.forEach((w, i) => {{
    const b = document.createElement("button");
    b.className = "win" + (i === 0 ? " on" : "");
    // 徽章显示该时段最快多久能拿到——比恒定的「来得及」有信息量得多
    const fits = w.recommendations.filter(r => r.fits).map(r => r.safe_wait);
    const badge = fits.length
      ? `<span class="k" style="color:#2e7d32;background:#2e7d3218">最快 ${{fmt(Math.min.apply(null, fits), 0)}} 分钟</span>`
      : `<span class="k" style="color:#c0392b;background:#c0392b18">来不及</span>`;
    b.innerHTML = `<div class="t">${{w.start}}–${{w.end}}</div>`
      + `<div class="d">${{KIND_SHORT[w.kind] || w.kind}} · ${{w.minutes}} 分钟</div>` + badge;
    b.onclick = () => {{
      document.querySelectorAll(".win").forEach(e => e.classList.remove("on"));
      b.classList.add("on");
      renderPanel(i);
    }};
    tl.appendChild(b);
  }});
  document.getElementById("tl-note").textContent =
    `共 ${{DATA.windows.length}} 个时段。点任意时段，看那个时间点该推什么、大概多久能拿到。`;
}}

function renderPanel(i) {{
  const w = DATA.windows[i];
  const p = document.getElementById("panel");
  const v = verdictOf(w);
  const col = KIND_COLOR[w.kind] || "#666";

  const cards = w.recommendations.filter(r => r.fits).map(r => `
    <div class="rec">
      <div class="rk">#${{r.rank}}</div>
      <div class="nm">${{r.name}}</div>
      <div class="pr">¥${{fmt(r.price, 1)}}</div>
      <span class="tag">${{r.category}}</span>
      <div class="wt">约 <b>${{fmt(r.safe_wait, 0)}}</b> 分钟能拿到</div>
    </div>`).join("");

  // 价值对比条：推荐的餐品 vs 同一时段最慢的餐品
  let compare = "";
  const best = w.recommendations.find(r => r.fits);
  if (best && w.slowest) {{
    const bMax = Math.max(best.safe_wait, w.slowest.wait, 1);
    const cap = w.slow_count > 0
      ? `这个时段的 ${{DATA.menu_count}} 项在售餐品里，有 <b>${{w.slow_count}}</b> 项点了会赶不上——`
        + `看板直接把它们排除掉，学生不用自己判断该点哪个。`
      : `这个时段时间充裕，所有餐品都来得及。看板改为按「吃得饱、性价比」排序，`
        + `而不是一味求快——所以推的是套餐而不是饮料。`;
    compare = `<div class="bar">
      <div class="row"><span>推荐的 ${{best.name}}</span><b style="color:#2e7d32">约 ${{fmt(best.safe_wait, 0)}} 分钟</b></div>
      <div class="track"><div class="fill" style="width:${{(best.safe_wait / bMax * 100).toFixed(0)}}%;background:#2e7d32"></div></div>
      <div class="row" style="margin-top:10px"><span>同一时段最慢的 ${{w.slowest.name}}</span><b style="color:#c0392b">约 ${{fmt(w.slowest.wait, 0)}} 分钟</b></div>
      <div class="track"><div class="fill" style="width:${{(w.slowest.wait / bMax * 100).toFixed(0)}}%;background:#c0392b"></div></div>
      <div class="cap">${{cap}}</div>
    </div>`;
  }}

  p.innerHTML = `
    <h2 class="ph">
      <span class="pt" style="color:${{col}}">${{KIND_SHORT[w.kind] || w.kind}}</span>
      <span class="pw">${{w.start}}–${{w.end}}</span>
      <span class="pl">${{w.label}}</span>
    </h2>
    <div class="verdict ${{v.ok ? "" : "maybe"}}">
      <span class="ico">${{v.ico}}</span>
      <span><span class="vt">${{v.t}}</span><span class="vs">${{v.s}}</span></span>
    </div>
    <div class="meta" style="margin-bottom:14px">
      <span class="chip">${{w.minutes}} 分钟时间窗口</span>
      <span class="chip">扣掉路上往返后，可用于等餐 ${{fmt(w.budget, 1)}} 分钟</span>
    </div>
    <div class="grid">${{cards || '<div class="note">该时段没有餐品能在窗口内做好。</div>'}}</div>
    ${{compare}}`;
}}

renderTimeline();
renderPanel(0);
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(prog="preview", description="生成本地 HTML 预览看板")
    parser.add_argument("--store", required=True, help="门店编码 storeCode")
    parser.add_argument("--city", default="德阳市", help="用于补全门店名称与地址")
    parser.add_argument("--keyword", default="四川工业科技学院", help="位置关键词")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--mode", choices=["client", "review", "both"], default="both",
                        help="client=客户展示版；review=技术审核版；both=两份都生成（默认）")
    parser.add_argument("--out", default=None, help="输出目录或文件路径，默认项目根目录")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.out and os.path.splitext(args.out)[1].lower() == ".html":
        base_dir = os.path.dirname(os.path.abspath(args.out))
    else:
        base_dir = os.path.abspath(args.out) if args.out else root

    targets: list[tuple[str, str]] = []
    if args.mode in ("client", "both"):
        targets.append(("client", os.path.join(base_dir, "preview-client.html")))
    if args.mode in ("review", "both"):
        targets.append(("review", os.path.join(base_dir, "preview.html")))
    if args.out and os.path.splitext(args.out)[1].lower() == ".html" and len(targets) == 1:
        targets[0] = (targets[0][0], os.path.abspath(args.out))

    try:
        data = build(args)
        if data.get("offline"):
            from offline import offline_banner
            print(offline_banner())
            print()
        for mode, path in targets:
            doc = render_client(data) if mode == "client" else render(data)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(doc)
            tag = "客户展示版" if mode == "client" else "技术审核版"
            print(f"[{tag}] {path}")
            print(f"           {len(doc.encode('utf-8')) / 1024:.1f} KB（单文件、可离线打开）")
        return 0
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
