"""下单后实时追踪（order watch）

解决三件事：
1. **实时剩余等待**：进度条显示「已等 X 分钟 / 预计还需 Y 分钟」，本地持久化，刷新不丢
2. **提前点完成识别**：门店批量把订单点成「已完成」时，不显示"已完成"，
   而是按物理下界 + 保守包络给出「可能尚未做好，预计还需 Z 分钟」，并提示到柜台核对
3. **官方赏味倒计时**：薯条 7 分钟 / 汉堡 20–30 分钟（官方口径），超时告警

两个运行模式：
    python order_watch.py --order <orderId>     # 真实订单，轮询麦当劳 MCP
    python order_watch.py --demo                # 模拟订单，无需下单即可演示全链路

输出：
    终端实时输出 + 持续重写一份自刷新的本地页面 `order-status.html`
    （`<meta http-equiv="refresh">` 让浏览器每 8 秒自动重读文件，**不需要任何服务器**）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import official_times as ot
import queue_estimate as qe
from break_window import Item, PrepModel

TRACK_DIR = os.path.join(os.path.expanduser("~"), ".mcd-break-window", "tracking")
OUT_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "order-status.html")
OUT_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "order-status-data.js")
DEFAULT_REFRESH = 20  # 秒。8 秒太快，页面跳动会让人以为是 bug

# 订单状态：以 query-order 的官方 schema 为准
#   1待支付 / 2配餐中-已支付 / 4配送中 / 6已完成 / 7已取消 / 8已评价
#   10配餐中-餐厅确认配餐中
# 实测：orderStatus 返回**中文文本**，status 返回**数字码**（如已完成时 status="40"）
# 两种口径都要认。
STATUS_MAP = {
    "1": ("待支付", "pending_pay"),
    "2": ("配餐中", "preparing"),
    "4": ("配送中", "delivering"),
    "6": ("已完成", "done"),
    "7": ("已取消", "cancelled"),
    "8": ("已评价", "rated"),
    "10": ("餐厅确认配餐中", "preparing"),
    "40": ("已完成", "done"),
    "待支付": ("待支付", "pending_pay"),
    "配餐中": ("配餐中", "preparing"),
    "餐厅确认配餐中": ("餐厅确认配餐中", "preparing"),
    "配送中": ("配送中", "delivering"),
    "订单已完成": ("已完成", "done"),
    "已完成": ("已完成", "done"),
    "已取消": ("已取消", "cancelled"),
    "已评价": ("已评价", "rated"),
}
READY_CODES = {"done"}


def parse_status(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if raw in STATUS_MAP:
        return STATUS_MAP[raw]
    for k, v in STATUS_MAP.items():
        if k and k in raw:
            return v
    return (raw or "未知", "unknown")


def state_path(key: str) -> str:
    os.makedirs(TRACK_DIR, exist_ok=True)
    return os.path.join(TRACK_DIR, f"{key}.json")


def load_state(key: str) -> dict:
    try:
        with open(state_path(key), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(key: str, data: dict) -> None:
    with open(state_path(key), "w", encoding="utf-8") as fh:
        # default=str 兜住 datetime 等非 JSON 原生类型
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)


def evaluate(snap: dict, model: PrepModel, store: str) -> dict:
    """把一次快照翻译成面向学生的结论。"""
    now = snap["now"]
    created = snap["created_at"]
    elapsed = max(0.0, (now - created).total_seconds() / 60.0)

    items = [Item(code=c, name=n) for c, n in snap.get("items", [])]
    est = model.estimate(items, at=created, store_code=store,
                         campaign_active=snap.get("campaign", False), safe=True)
    # 物理下界必须与模型口径一致（并行折扣后的基准），否则会对"做得快"的门店误报
    base = float(est.breakdown.get("base_minutes") or 3.0)
    floor = max(0.7, 0.55 * base)
    predicted_total = max(est.minutes, base)  # 保守包络口径
    remaining = max(0.0, predicted_total - elapsed)

    label, code = parse_status(snap.get("raw_status", ""))

    # ── 提前点完成识别 ──────────────────────────────
    anomaly = False
    if code in READY_CODES and elapsed < floor:
        anomaly = True
        verdict = (
            f"门店已标记「{label}」，但距下单只有 {elapsed:.1f} 分钟，"
            f"低于物理下界 {floor:.1f} 分钟——餐品不可能已做好。"
            f"按当前压力推算，预计还需约 {remaining:.1f} 分钟。建议到柜台核对实物。"
        )
        tone = "warn"
    elif code in READY_CODES and elapsed < predicted_total * 0.7:
        anomaly = True
        verdict = (
            f"门店已标记「{label}」，耗时 {elapsed:.1f} 分钟，明显早于预估的 "
            f"{predicted_total:.1f} 分钟。高峰期这类记录可能是批量标记，"
            f"建议到店后以实物为准。"
        )
        tone = "warn"
    elif code in READY_CODES:
        verdict = f"订单已完成，实际耗时 {elapsed:.1f} 分钟（预估 {predicted_total:.1f} 分钟）。"
        tone = "ok"
    elif code == "cancelled":
        verdict = "订单已取消。"
        tone = "warn"
    elif code == "pending_pay":
        verdict = "等待支付，支付后才开始配餐。"
        tone = "info"
    else:
        verdict = f"门店正在配餐（{label}），预计还需约 {remaining:.1f} 分钟。"
        tone = "info"

    progress = min(100.0, (elapsed / predicted_total * 100.0) if predicted_total > 0 else 0)

    shelf = ot.shelf_life_status(elapsed, [n for _c, n in snap.get("items", [])])
    queue = qe.estimate_queue(model, store, now, predicted_total)

    pressure = model.state
    frozen = bool(pressure.get("frozen"))
    _, w = model.current_window(now)
    window, remaining_win = model.current_window(now)

    return {
        "order_id": snap.get("order_id", ""),
        "store": store,
        "pickup_code": snap.get("pickup_code", ""),
        "status_label": label,
        "display_status": (f"{label}（存疑）" if anomaly and code in READY_CODES else label),
        "status_code": code,
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(elapsed, 1),
        "predicted_total": round(predicted_total, 1),
        "remaining": round(remaining, 1),
        "progress": round(progress, 1),
        "floor": round(floor, 1),
        "anomaly": anomaly,
        "tone": tone,
        "verdict": verdict,
        "shelf": shelf,
        "queue": queue,
        "items": snap.get("items", []),
        "window": ({"label": window.label, "kind": window.kind,
                    "end": window.end, "remaining": remaining_win} if window else None),
        "frozen": frozen,
        "history": snap.get("history", []),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<title>订单追踪 · __STATUS__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1a1d21;line-height:1.65;
     padding:clamp(14px,3vw,28px) clamp(12px,2.6vw,20px) 40px}
.wrap{max-width:720px;margin:0 auto}
.card{background:#fff;border:1px solid #e6e8ec;border-radius:14px;padding:clamp(16px,3.4vw,24px);
      margin-bottom:14px}
h1{font-size:clamp(17px,4.2vw,21px);font-weight:700;margin-bottom:6px}
.sub{font-size:13px;color:#5b6270}
.big{font-size:clamp(38px,11vw,56px);font-weight:800;line-height:1.1;
     font-variant-numeric:tabular-nums;letter-spacing:-1px}
.big small{font-size:.34em;font-weight:600;color:#6b7280;margin-left:6px}
.bar{height:14px;border-radius:7px;background:#e9ecf1;overflow:hidden;margin:16px 0 8px}
.bar>i{display:block;height:100%;border-radius:7px;background:linear-gradient(90deg,#2e7d32,#4caf50);
       transition:width .6s ease}
.bar.warn>i{background:linear-gradient(90deg,#c0392b,#e07b6f)}
.row{display:flex;justify-content:space-between;font-size:13px;color:#5b6270}
.verdict{border-radius:11px;padding:13px 15px;font-size:clamp(13px,3.3vw,14px);margin-top:14px;border:1px solid}
.verdict.warn{background:#fff7ed;border-color:#f3ddc0;color:#8a4b12}
.verdict.ok{background:#f1f8f3;border-color:#cfe6d6;color:#1d5c37}
.verdict.info{background:#f4f8ff;border-color:#d8e5fb;color:#1c3f77}
h2{font-size:14px;font-weight:650;margin-bottom:10px;display:flex;gap:8px;align-items:center}
h2::before{content:"";width:3px;height:14px;background:#c0392b;border-radius:2px}
.chip{display:inline-block;background:#f1f3f6;border:1px solid #e2e5ea;border-radius:999px;
      padding:3px 10px;font-size:12px;color:#39404d;margin:0 6px 6px 0}
.shelf{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px dashed #eef0f3;font-size:13px}
.shelf:last-child{border-bottom:0}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.dot.ok{background:#2e7d32}.dot.warning{background:#e0a800}.dot.expired{background:#c0392b}.dot.unknown{background:#b9c0cc}
.tiny{font-size:11.5px;color:#9aa1ad;margin-top:4px}
footer{text-align:center;font-size:11.5px;color:#9aa1ad;margin-top:20px;line-height:1.9}

/* 自动更新状态条：明确告知「在自动更新」而不是让人以为页面在闪 bug */
.livebar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;
         background:#eef4ff;border:1px solid #d8e5fb;border-radius:11px;
         padding:9px 13px;font-size:12.5px;color:#1c3f77;margin-bottom:14px}
.livebar .pulse{width:8px;height:8px;border-radius:50%;background:#1f6feb;flex:0 0 auto;
                animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.28}}
.livebar.err{background:#fff7ed;border-color:#f3ddc0;color:#8a4b12}
.livebar.err .pulse{background:#c0392b;animation:none}
.livebar b{font-variant-numeric:tabular-nums}
@media print{.livebar{display:none}}
</style></head><body><div class="wrap">

<div class="livebar" id="livebar">
  <span class="pulse" id="liveDot"></span>
  <span>本页<b>每 __REFRESH__ 秒自动更新</b>（不是页面异常刷新）</span>
  <span style="color:#6b7280">·</span>
  <span>上次更新 <b id="updatedAt">__NOW__</b></span>
  <span style="color:#6b7280">·</span>
  <span>下次 <b id="countdown">__REFRESH__</b> 秒</span>
</div>
<div class="tiny" id="liveMsg" style="margin:-8px 0 14px 2px"></div>

<div class="card">
  <h1>订单追踪 · <span id="status">__STATUS__</span></h1>
  <div class="sub">订单 __ORDER__ · 门店 __STORE__ · 下单 __CREATED__</div>
  <div class="big" style="margin-top:18px"><span id="remain">__REMAIN__</span><small>分钟后可取</small></div>
  <div class="bar __BARCLS__" id="bar"><i id="progress" style="width:__PROGRESS__%"></i></div>
  <div class="row"><span>已等待 <b id="elapsed">__ELAPSED__</b> 分钟</span><span>预计总时长 <b id="total">__TOTAL__</b> 分钟</span></div>
  <div class="verdict __TONE__" id="verdict">__VERDICT__</div>
</div>

<div class="card">
  <h2>官方赏味倒计时</h2>
  <div id="shelfBox">__SHELF__</div>
  <div class="tiny">依据麦当劳官方公开标准：薯条起锅后最佳赏味 7 分钟；汉堡制作后 20–30 分钟可售时限。
  官方未发布其余品类的时限，因此显示"未知"，不做推测。</div>
</div>

<div class="card">
  <h2>门店当前忙闲</h2>
  <div style="font-size:clamp(26px,7vw,36px);font-weight:800;line-height:1.25;font-variant-numeric:tabular-nums">
    约 <span id="qcount">__QCOUNT__</span> 单在做
  </div>
  <div class="row" style="margin-top:12px">
    <span>每分钟新增 <b id="qlam">__QLAM__</b> 单</span>
    <span>每单平均 <b id="qwait">__QWAIT__</b> 分钟</span>
  </div>
  <div class="tiny" id="qnote" style="margin-top:8px">__QNOTE__</div>
  <div class="warn" style="margin-top:12px">__QCAVEAT__</div>
</div>

<div class="card">
  <h2>追踪依据</h2>
  <div>
    <span class="chip">物理下界 __FLOOR__ 分钟</span>
    <span class="chip">取餐码 <b style="font-variant-numeric:tabular-nums">__PICKUP__</b></span>
    <span class="chip">订单状态 <span id="status2">__STATUS__</span></span>
    <span class="chip" id="win">__WINDOW__</span>
    <span class="chip">校准状态：__FROZEN__</span>
  </div>
  <div class="tiny">取餐码是门店给出的<b>真实序号</b>（由门店当日/累计单量决定），不是估算值。
  同一门店多次下单时，取餐码差 ÷ 时间差 ≈ 该店出单速率，可用于校准队列长度。</div>
  <div class="tiny">数据来源：麦当劳 MCP + 本机模型输出。页面读取同目录下的
  <code>order-status-data.js</code>，只更新数字与文案，不刷新整页。</div>
</div>

<footer>麦麦课间窗口 · 参赛作品演示，非麦当劳官方产品<br>页面生成于 __NOW__</footer>
</div>

<script>
window.__REFRESH__ = __REFRESH__;
(function () {
  var REFRESH = __REFRESH__;
  var left = REFRESH;
  var failed = false;

  function fmt(n) { return (Math.round(n * 10) / 10).toFixed(1); }

  function applyData(d) {
    if (!d) return;
    document.getElementById('status').textContent = d.display_status;
    document.getElementById('status2').textContent = d.display_status;
    document.getElementById('remain').textContent = fmt(d.remaining);
    document.getElementById('elapsed').textContent = fmt(d.elapsed);
    document.getElementById('total').textContent = fmt(d.predicted_total);
    document.getElementById('progress').style.width = d.progress + '%';
    document.getElementById('bar').className = 'bar' + (d.anomaly ? ' warn' : '');
    var v = document.getElementById('verdict');
    v.className = 'verdict ' + d.tone;
    v.textContent = d.verdict;
    document.getElementById('win').textContent = d.window
      ? ('当前 ' + d.window.label + ' 剩余 ' + d.window.remaining + ' 分钟')
      : '当前不在课间窗口';
    document.getElementById('shelfBox').innerHTML = (d.shelf || []).map(function (s) {
      return '<div class="shelf"><span class="dot ' + s.status + '"></span>'
        + '<span style="flex:1">' + s.item + '<div class="tiny">' + s.message + '</div></span>'
        + '<span style="font-variant-numeric:tabular-nums;color:#6b7280">'
        + fmt(s.elapsed_minutes) + ' 分钟</span></div>';
    }).join('');
    document.getElementById('updatedAt').textContent = d.now;
    if (d.queue) {
      document.getElementById('qcount').textContent = d.queue.in_system;
      document.getElementById('qlam').textContent = d.queue.lambda_per_min;
      document.getElementById('qwait').textContent = fmt(d.queue.wait_minutes);
      document.getElementById('qnote').textContent =
        d.queue.formula + ' · ' + d.queue.basis + ' · ' + d.queue.note
        + ' · 置信度 ' + d.queue.confidence;
    }
    document.title = '订单追踪 · ' + d.display_status;
  }

  function loadData() {
    var s = document.createElement('script');
    s.src = 'order-status-data.js?t=' + Date.now();
    s.onload = function () {
      failed = false;
      document.getElementById('livebar').className = 'livebar';
      document.getElementById('liveMsg').textContent = '';
      if (window.ORDER_DATA) applyData(window.ORDER_DATA);
    };
    s.onerror = function () {
      failed = true;
      document.getElementById('livebar').className = 'livebar err';
      document.getElementById('liveMsg').textContent =
        '自动更新暂时停了（没读到 order-status-data.js）。上面的数字停在「上次更新」那一刻，'
        + '不是页面出错——重新运行 order_watch.py 就会继续。';
    };
    document.body.appendChild(s);
  }

  setInterval(function () {
    left -= 1;
    if (left <= 0) {
      left = REFRESH;
      loadData();
    }
    document.getElementById('countdown').textContent = left;
  }, 1000);

  loadData();
})();
</script>
</body></html>"""


def _shelf_html(r: dict) -> str:
    rows = []
    for s in r["shelf"]:
        rows.append(
            f'<div class="shelf"><span class="dot {s["status"]}"></span><span style="flex:1">'
            f'{s["item"]}<div class="tiny">{s["message"]}</div></span>'
            f'<span style="font-variant-numeric:tabular-nums;color:#6b7280">'
            f'{s["elapsed_minutes"]:.1f} 分钟</span></div>'
        )
    return "".join(rows) or '<div class="tiny">本单无餐品明细。</div>'


def render_html(r: dict, refresh: int = 20) -> str:
    win = r.get("window")
    win_txt = (f"当前 {win['label']} 剩余 {win['remaining']} 分钟" if win else "当前不在课间窗口")
    return (HTML_TEMPLATE
            .replace("__ORDER__", r["order_id"] or "演示订单")
            .replace("__STORE__", r["store"])
            .replace("__CREATED__", r["created_at"])
            .replace("__STATUS__", r["display_status"])
            .replace("__REMAIN__", f'{r["remaining"]:.1f}')
            .replace("__PROGRESS__", f'{r["progress"]:.0f}')
            .replace("__BARCLS__", "warn" if r["anomaly"] else "")
            .replace("__ELAPSED__", f'{r["elapsed"]:.1f}')
            .replace("__TOTAL__", f'{r["predicted_total"]:.1f}')
            .replace("__TONE__", r["tone"])
            .replace("__VERDICT__", r["verdict"])
            .replace("__SHELF__", _shelf_html(r))
            .replace("__QCOUNT__", str(r["queue"]["in_system"]))
            .replace("__QLAM__", str(r["queue"]["lambda_per_min"]))
            .replace("__QWAIT__", f'{r["queue"]["wait_minutes"]}')
            .replace("__QNOTE__",
                     f'{r["queue"]["formula"]} · {r["queue"]["basis"]} · {r["queue"]["note"]}'
                     f' · 置信度 {r["queue"]["confidence"]}')
            .replace("__QCAVEAT__", r["queue"]["caveat"])
            .replace("__FLOOR__", f'{r["floor"]:.1f}')
            .replace("__PICKUP__", r.get("pickup_code") or "—")
            .replace("__WINDOW__", win_txt)
            .replace("__FROZEN__", "已冻结（数据异常）" if r["frozen"] else "正常")
            .replace("__NOW__", r["now"])
            .replace("__REFRESH__", str(refresh)))


def render_data_js(r: dict) -> str:
    payload = json.dumps({
        "display_status": r["display_status"],
        "remaining": r["remaining"],
        "elapsed": r["elapsed"],
        "predicted_total": r["predicted_total"],
        "progress": r["progress"],
        "anomaly": r["anomaly"],
        "tone": r["tone"],
        "verdict": r["verdict"],
        "now": r["now"],
        "window": r["window"],
        "shelf": r["shelf"],
        "queue": r["queue"],
    }, ensure_ascii=False)
    return "window.ORDER_DATA = " + payload + ";\n"


def push_snapshot(snap: dict, model: PrepModel, store: str, key: str,
                  quiet: bool, refresh: int = 20) -> dict:
    r = evaluate(snap, model, store)
    with open(OUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(render_html(r, refresh))
    with open(OUT_DATA, "w", encoding="utf-8") as fh:
        fh.write(render_data_js(r))
    snap["history"] = (snap.get("history") or []) + [{
        "at": r["now"], "status": r["display_status"],
        "elapsed": r["elapsed"], "remaining": r["remaining"], "anomaly": r["anomaly"],
    }][-1:]
    snap["last_eval"] = r
    save_state(key, snap)
    if not quiet:
        print(f"[{r['now'][11:]}] 状态={r['display_status']}  已等={r['elapsed']}分  "
              f"预计还需={r['remaining']}分  进度={r['progress']}%"
              + ("  ⚠ 疑似提前点完成" if r["anomaly"] else ""))
    return r


# ── 真实模式 ────────────────────────────────────────────────
def run_real(order_id: str, interval: float, once: bool, refresh: int = DEFAULT_REFRESH) -> int:
    from mcd_client import McdMcpClient, McpError, envelope_data

    client = McdMcpClient()
    model = PrepModel()
    key = order_id

    while True:
        try:
            detail = envelope_data(client.call_tool("query-order", {"orderId": order_id}))
            blob = json.dumps(detail, ensure_ascii=False)
            raw_status = ""
            for k in ("orderStatus", "status"):
                marker = f'"{k}":"'
                p = blob.find(marker)
                if p >= 0:
                    raw_status = blob[p + len(marker): blob.find('"', p + len(marker))]
                    break
            store = ""
            for k in ("storeCode",):
                marker = f'"{k}":"'
                p = blob.find(marker)
                if p >= 0:
                    store = blob[p + len(marker): blob.find('"', p + len(marker))]
                    break
            pickup = ""
            for k in ("pickupCode",):
                marker = f'"{k}":"'
                p = blob.find(marker)
                if p >= 0:
                    pickup = blob[p + len(marker): blob.find('"', p + len(marker))]
                    break
            created_raw = ""
            for k in ("createTime",):
                marker = f'"{k}":"'
                p = blob.find(marker)
                if p >= 0:
                    created_raw = blob[p + len(marker): blob.find('"', p + len(marker))]
                    break
            created = datetime.now()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    created = datetime.strptime(created_raw, fmt)
                    break
                except ValueError:
                    continue

            items = []
            st = load_state(key)
            if st.get("items"):
                items = [(c, n) for c, n in st["items"]]
            if not items:
                for m in ("productName",):
                    idx = 0
                    while True:
                        p = blob.find(f'"{m}":"', idx)
                        if p < 0:
                            break
                        q = blob.find('"', p + len(m) + 4)
                        items.append((f"item{len(items)}", blob[p + len(m) + 4:q]))
                        idx = q

            if pickup:
                existing = (model.state.get("pickup_samples") or {}).get(store or "-") or []
                if not any(str(x.get("code")) == str(pickup) for x in existing):
                    qe.record_pickup(model, store or "-", pickup, created)
                    print(f"[取餐码] 已记录 {pickup}，累计采样可用于标定本店到达率 λ")

            snap = {
                "order_id": order_id, "store": store, "created_at": created,
                "now": datetime.now(), "raw_status": raw_status,
                "pickup_code": pickup,
                "items": items, "campaign": False,
                "history": st.get("history", []),
            }
            push_snapshot(snap, model, store or "-", key, quiet=False, refresh=refresh)
            if once:
                return 0
            _, code = parse_status(raw_status)
            if code in READY_CODES:
                print("订单已完成，追踪结束。")
                return 0
        except McpError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        time.sleep(interval)


# ── 演示模式 ────────────────────────────────────────────────
def run_demo(speed: float, interval: float, refresh: int = DEFAULT_REFRESH) -> int:
    """模拟一单：真实压力下的提前点完成。无需下单、无需 Token。"""
    model = PrepModel()
    store = "3450415"
    created = datetime.now() - timedelta(minutes=0)
    items = [("1440", "麦辣鸡腿汉堡"), ("4810", "薯条")]
    key = "demo-order"

    # (相对分钟数, 原始状态) —— 状态码按官方 schema：1待支付/2配餐中/6已完成
    script = [(0.0, "1"), (0.4, "2"), (1.2, "6"), (1.2, "6"), (4.5, "6")]
    est = model.estimate([Item(code=c, name=n) for c, n in items],
                         at=created, store_code=store, safe=True)
    base = float(est.breakdown.get("base_minutes") or 3.0)
    floor = max(0.7, 0.55 * base)
    print(f"演示订单：{', '.join(n for _c, n in items)}")
    print(f"基准出餐 {base:.1f} 分钟（并行折扣后），保守预估总时长 {est.minutes:.1f} 分钟，"
          f"物理下界 {floor:.1f} 分钟")
    print("模拟序列：待支付 → 配餐中 → 门店在 1.2 分钟就点了「已完成」（低于物理下界）")
    print("-" * 72)

    seen = []
    for minutes, raw in script:
        if raw in seen:
            continue
        seen.append(raw)
        snap = {
            "order_id": "DEMO-0001", "store": store,
            "created_at": created, "now": created + timedelta(minutes=minutes),
            "raw_status": raw, "pickup_code": "35062",
            "items": items, "campaign": False, "history": [],
        }
        r = push_snapshot(snap, model, store, key, quiet=False, refresh=refresh)
        if r["anomaly"]:
            print(f"    ⚠ 判定：{r['verdict']}")
            for s in r["shelf"]:
                print(f"    · {s['item']}：{s['message']}（已放置 {s['elapsed_minutes']} 分钟）")
        time.sleep(max(0.0, interval * speed))
    print("-" * 72)
    print(f"已写入：{OUT_HTML}")
    print(f"        {OUT_DATA}")
    print(f"用浏览器打开 HTML，页面每 {refresh} 秒原地更新数字（不刷新整页、不跳滚动位置）。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="order_watch", description="下单后实时追踪")
    p.add_argument("--order", help="真实订单号（走麦当劳 MCP 轮询）")
    p.add_argument("--demo", action="store_true", help="演示模式，无需下单与 Token")
    p.add_argument("--interval", type=float, default=10.0, help="MCP 轮询间隔（秒），默认 10")
    p.add_argument("--refresh", type=int, default=DEFAULT_REFRESH,
                   help=f"页面自动更新间隔（秒），默认 {DEFAULT_REFRESH}")
    p.add_argument("--speed", type=float, default=0.35, help="演示模式的节奏倍率")
    p.add_argument("--once", action="store_true", help="真实模式只查一次")
    a = p.parse_args()

    if a.demo:
        return run_demo(a.speed, a.interval, a.refresh)
    if a.order:
        return run_real(a.order, a.interval, a.once, a.refresh)
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
