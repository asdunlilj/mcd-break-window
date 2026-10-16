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
from mcd_client import McdMcpClient, McpError  # noqa: E402

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
    client = McdMcpClient()
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

        windows_payload.append({
            **w.as_dict(),
            "budget": round(budget, 1),
            "weights": w_weights,
            "recommendations": recs,
            "avoid": avoid,
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
        "frozen": bool(model.state.get("frozen")),
        "windows": windows_payload,
    }
    return render(data)


def w_minus(hhmm: str, now: datetime):
    return now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)


# ---------------------------------------------------------------- HTML 渲染

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1a1d21;line-height:1.6;padding:28px 20px 60px}
.wrap{max-width:1060px;margin:0 auto}
header{background:#fff;border-radius:14px;padding:24px 28px;border:1px solid #e6e8ec;margin-bottom:18px}
h1{font-size:23px;font-weight:700;letter-spacing:-.2px}
h1 span{color:#c0392b}
.sub{color:#5b6270;font-size:13.5px;margin-top:6px}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.chip{background:#f1f3f6;border:1px solid #e2e5ea;border-radius:999px;padding:4px 11px;font-size:12.5px;color:#39404d}
section{background:#fff;border-radius:14px;padding:22px 26px;border:1px solid #e6e8ec;margin-bottom:18px}
h2{font-size:15px;font-weight:650;margin-bottom:14px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:15px;background:#c0392b;border-radius:2px}
.prob{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.prob{grid-template-columns:1fr}}
.pcard{border-radius:10px;padding:14px 16px;font-size:13.5px;border:1px solid}
.pcard h3{font-size:13.5px;font-weight:650;margin-bottom:6px}
.pcard.s{background:#fdf6f5;border-color:#f3dbd8;color:#7a2e26}
.pcard.m{background:#f4f8ff;border-color:#d8e5fb;color:#1c3f77}
.tl{display:flex;gap:9px;overflow-x:auto;padding-bottom:6px}
.win{flex:0 0 auto;cursor:pointer;border:1.5px solid #e2e5ea;background:#fff;border-radius:10px;
     padding:10px 13px;text-align:left;min-width:132px;transition:.15s;font-family:inherit}
.win:hover{border-color:#b9c0cc}
.win.on{border-color:#c0392b;background:#fef7f6;box-shadow:0 0 0 3px rgba(192,57,43,.09)}
.win .t{font-size:14px;font-weight:650;font-variant-numeric:tabular-nums}
.win .d{font-size:11.5px;color:#6b7280;margin-top:2px}
.win .k{font-size:11px;font-weight:600;margin-top:5px;display:inline-block;padding:1px 7px;border-radius:999px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.rec{border:1px solid #e6e8ec;border-radius:11px;padding:15px 16px;background:#fff;position:relative}
.rec.no{opacity:.55;border-style:dashed}
.rec .rk{position:absolute;top:13px;right:14px;font-size:11px;color:#9aa1ad;font-weight:600}
.rec .nm{font-size:15px;font-weight:650;padding-right:26px;line-height:1.35}
.rec .pr{font-size:13px;color:#c0392b;font-weight:650;margin-top:5px}
.rec .wt{font-size:12.5px;color:#4a5160;margin-top:9px;padding-top:9px;border-top:1px dashed #e9ebef}
.rec .wt b{font-size:16px;color:#1a1d21;font-variant-numeric:tabular-nums}
.tag{display:inline-block;background:#f1f3f6;border-radius:5px;padding:1px 7px;font-size:11.5px;color:#4a5160;margin-top:7px}
.avoid{margin-top:13px;background:#fffbeb;border:1px solid #f5e2ac;border-radius:10px;padding:11px 14px;font-size:13px;color:#7a5c12}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef0f3}
th{color:#6b7280;font-weight:600;font-size:12px}
td.n{font-variant-numeric:tabular-nums;text-align:right}
code{background:#f1f3f6;padding:1.5px 6px;border-radius:5px;font-size:12.5px;font-family:ui-monospace,Menlo,Consolas,monospace}
.formula{background:#f8f9fb;border:1px solid #e9ebef;border-radius:9px;padding:13px 15px;font-size:13px;
         font-family:ui-monospace,Menlo,Consolas,monospace;color:#2b3038;overflow-x:auto;margin-bottom:13px}
details{margin-top:13px}
summary{cursor:pointer;font-size:13px;color:#1f6feb;font-weight:600;user-select:none}
.note{font-size:12.5px;color:#6b7280;margin-top:11px;line-height:1.75}
.warn{background:#fef7f6;border:1px solid #f3dbd8;border-radius:9px;padding:11px 14px;font-size:13px;color:#7a2e26;margin-bottom:13px}
footer{text-align:center;font-size:12px;color:#9aa1ad;margin-top:26px;line-height:1.9}
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
<meta name="viewport" content="width=device-width,initial-scale=1">
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
    `共 ${{n}} 个窗口。小课间只来得及点快餐品，大课间与餐段才推荐完整正餐。`
    + `　当前时间 ${{DATA.generated_at.slice(11)}}`;
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

  p.innerHTML = `
    <h2><span style="color:${{col}}">${{w.kind_text}}</span>　${{w.start}}–${{w.end}}　<span style="color:#6b7280;font-weight:500">${{w.label}}</span></h2>
    <div class="meta" style="margin-bottom:15px">
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="preview", description="生成本地 HTML 预览看板")
    parser.add_argument("--store", required=True, help="门店编码 storeCode")
    parser.add_argument("--city", default="德阳市", help="用于补全门店名称与地址")
    parser.add_argument("--keyword", default="四川工业科技学院", help="位置关键词")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--out", default=None, help="输出 HTML 路径，默认 ../preview.html")
    args = parser.parse_args()

    try:
        out = args.out or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.html"
        )
        doc = build(args)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print(f"预览已生成：{out}")
        print(f"文件大小：{len(doc.encode('utf-8')) / 1024:.1f} KB（单文件、可离线打开）")
        return 0
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
