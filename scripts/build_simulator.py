"""生成 `simulator.html` —— 单文件、零依赖、零 Token 的"课间取餐模拟器"

设计要点
--------
Token 只用于**取数**（调 MCP），**计算本身不需要 Token**。
所以把架构拆成三层：

    数据（Python 侧固化：菜单/分类/基准/营养/作息/门店配置）
        ↓  内联进 HTML
    算法（浏览器侧实时跑：六系数、打分权重、选择规则）
        ↓
    交互（用户改时间/课间时长/预算 → 立即重算）

这样"没有 Token 的路人"拿到的**不是一个结果快照，而是一个能自己算的工具**。

分类与基准时长在 Python 里算一次后固化（保证与 CLI 完全一致），
浏览器只负责真正的"实时"部分：时段系数、课间脉冲、打分、选品。

用法：
    python scripts/build_simulator.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from break_window import (  # noqa: E402
    CAMPAIGN_PULSE,
    PARALLEL_DISCOUNT,
    Item,
    PrepModel,
    parse_menu,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "simulator.html")
SNAPSHOT = os.path.join(HERE, "sample_data")

CAMPAIGN_KEYWORDS = ("上新", "联名", "限时", "第二份", "促销", "抢购")


def load_json(name: str):
    with open(os.path.join(SNAPSHOT, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_nutrition() -> dict:
    path = os.path.join(SNAPSHOT, "nutrition.json")
    if not os.path.exists(path):
        return {}
    raw = load_json("nutrition.json")
    text = raw if isinstance(raw, str) else str(raw.get("data", ""))
    table: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line or line.startswith("[") or "productName" in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        try:
            table[parts[0]] = {
                "kcal": float(parts[3]), "protein": float(parts[4]),
                "fat": float(parts[5]), "carbs": float(parts[6]),
                "sodium": float(parts[7]), "calcium": float(parts[8]),
            }
        except ValueError:
            continue
    return table


def build_payload() -> dict:
    model = PrepModel()
    menu_raw = load_json("menu.json")
    items = parse_menu(menu_raw if isinstance(menu_raw, dict) else {})

    meals = []
    for it in items:
        meals.append({
            "c": it.code, "n": it.name, "p": round(it.price, 1),
            "cat": it.category, "b": it.base_minutes,
            "cp": it.completeness,
        })

    campaign = load_json("campaign.json")
    ctext = json.dumps(campaign, ensure_ascii=False)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    idx = ctext.find(today)
    camp_active = bool(idx >= 0 and any(k in ctext[idx: idx + 1200] for k in CAMPAIGN_KEYWORDS))

    sched = model.schedule or {}
    return {
        "meals": meals,
        "nutrition": load_nutrition(),
        "profiles": sched.get("profiles") or {},
        "window_rules": sched.get("window_rules") or {},
        "store_hours": sched.get("store_hours") or {},
        "walking": (sched.get("walking") or {}).get("round_trip_minutes", 4),
        "queue": sched.get("queue") or {},
        "school": sched.get("school", ""),
        "term": sched.get("term", ""),
        "meta": load_json("meta.json"),
        "campaign_active": camp_active,
        "consts": {
            "campaign_pulse": CAMPAIGN_PULSE,
            "parallel_discount": PARALLEL_DISCOUNT,
            "time_factors": [
                [a, b, f, l] for a, b, f, l in
                [("07:30", "09:00", 1.15, "早高峰"), ("09:00", "11:30", 1.00, "上午课时段"),
                 ("11:30", "13:10", 1.55, "午高峰"), ("13:10", "14:15", 1.20, "午后"),
                 ("14:15", "16:40", 0.95, "下午课时段"), ("16:40", "18:30", 1.50, "放学高峰"),
                 ("18:30", "21:45", 1.30, "夜市时段"), ("21:45", "23:00", 1.10, "宵夜时段")]
            ],
            "closed_factor": 0.60,
        },
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<title>课间取餐模拟器 · 麦麦课间窗口</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1a1d21;line-height:1.65;
     padding:clamp(14px,3vw,26px) clamp(12px,2.6vw,20px) 40px}
.wrap{max-width:760px;margin:0 auto}
.card{background:#fff;border:1px solid #e6e8ec;border-radius:14px;
      padding:clamp(15px,3.4vw,22px);margin-bottom:13px}
h1{font-size:clamp(17px,4.3vw,21px);font-weight:750;margin-bottom:5px}
.sub{font-size:12.5px;color:#5b6270}
h2{font-size:13.5px;font-weight:700;margin-bottom:12px;display:flex;gap:8px;align-items:center}
h2::before{content:"";width:3px;height:14px;background:#c0392b;border-radius:2px}
.ctrl{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:12px}
label{display:block;font-size:12px;color:#5b6270;margin-bottom:5px}
input[type=time],input[type=number],select{width:100%;padding:10px 11px;font-size:15px;
  border:1px solid #dfe3e8;border-radius:9px;background:#fff;font-family:inherit;color:#1a1d21}
input:focus,select:focus{outline:2px solid #c0392b33;border-color:#c0392b}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.chip{background:#f1f3f6;border:1px solid #e2e5ea;border-radius:999px;padding:3px 10px;font-size:12px;color:#39404d}
.chip.hi{background:#fff7ed;border-color:#f3ddc0;color:#8a4b12}
.verdict{border-radius:11px;padding:13px 15px;font-size:13.5px;margin:13px 0;border:1px solid}
.verdict.ok{background:#f1f8f3;border-color:#cfe6d6;color:#1d5c37}
.verdict.warn{background:#fff7ed;border-color:#f3ddc0;color:#8a4b12}
.rec{border:1px solid #e6e8ec;border-radius:11px;padding:13px 15px;margin-bottom:10px;position:relative}
.rec .rk{position:absolute;top:11px;right:13px;font-size:11px;color:#9aa1ad;font-weight:600}
.rec .nm{font-size:14.5px;font-weight:650;padding-right:24px}
.rec .pr{font-size:12.5px;color:#c0392b;font-weight:650;margin-top:3px}
.rec .wt{font-size:12.5px;color:#4a5160;margin-top:8px;padding-top:8px;border-top:1px dashed #e9ebef}
.rec .wt b{font-size:15.5px;color:#1a1d21;font-variant-numeric:tabular-nums}
.tag{display:inline-block;background:#f1f3f6;border-radius:5px;padding:1px 6px;font-size:11px;color:#4a5160;margin-top:5px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 5px;border-bottom:1px solid #eef0f3}
th{color:#6b7280;font-weight:600;font-size:11.5px}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.tiny{font-size:11.5px;color:#9aa1ad;margin-top:9px;line-height:1.8}
.warnbox{background:#fffbeb;border:1px solid #f5e2ac;border-radius:10px;
         padding:11px 13px;font-size:12px;color:#7a5c12;line-height:1.8;margin-top:11px}
footer{text-align:center;font-size:11px;color:#9aa1ad;margin-top:20px;line-height:1.9}
</style></head><body><div class="wrap">

<div class="card">
  <h1>课间取餐模拟器</h1>
  <div class="sub" id="subtitle"></div>
  <div class="chips" id="metaChips"></div>
</div>

<div class="card">
  <h2>改参数，立刻重算</h2>
  <div class="ctrl">
    <div>
      <label>当前时间</label>
      <input type="time" id="atTime" value="10:05">
    </div>
    <div>
      <label>课间时长（分钟）</label>
      <select id="breakSel"></select>
    </div>
    <div>
      <label>预算上限（元，0=不限）</label>
      <input type="number" id="budget" value="0" min="0" step="1">
    </div>
    <div>
      <label>推荐条数</label>
      <select id="topK">
        <option>3</option><option>5</option><option>8</option>
      </select>
    </div>
  </div>
  <div class="chips" id="winChips"></div>
</div>

<div class="card">
  <h2>结论</h2>
  <div class="verdict" id="verdict">—</div>
  <div id="recs"></div>
  <div class="warnbox" id="caveat"></div>
</div>

<div class="card">
  <h2>算的依据</h2>
  <table id="breakdown"></table>
  <div class="tiny" id="formula"></div>
</div>

<footer>
  <span id="footMeta"></span><br>
  本页在<b>你的浏览器里实时计算</b>，零网络请求、零 Token、零依赖。<br>
  参赛作品演示，非麦当劳官方产品。
</footer>

</div>
<script>
const DATA = __PAYLOAD__;

/* ───────── 数据侧（Python 已固化，浏览器不重复推导） ───────── */
const MEALS = DATA.meals;
const NUT = DATA.nutrition;
const RULES = DATA.window_rules || {};
const CONST = DATA.consts;
const WALK = DATA.walking;
const QCFG = DATA.queue || {};
const CAMPAIGN = DATA.campaign_active;

/* ───────── 工具 ───────── */
const hhmm2min = s => { const p = String(s).split(':'); return (+p[0]) * 60 + (+p[1]); };
const fmt = (n, d = 1) => Number(n).toFixed(d);

/* ───────── 作息：按星期选 profile，并从 periods 推导课间窗口 ───────── */
function profileFor(date) {
  const wd = date.getDay() === 0 ? 6 : date.getDay() - 1;   // JS 周日=0 → 我们的 6
  for (const k in DATA.profiles) {
    const p = DATA.profiles[k];
    if ((p.days || []).includes(wd)) return p;
  }
  return { label: '未知', periods: [] };
}

function dayWindows(date) {
  const prof = profileFor(date);
  const ps = (prof.periods || []).slice().sort((a, b) => hhmm2min(a.start) - hhmm2min(b.start));
  const minGap = RULES.min_gap_minutes ?? 5;
  const shortMax = RULES.short_max_minutes ?? 12;
  const longMin = RULES.long_min_minutes ?? 25;
  const mealMin = RULES.meal_min_minutes ?? 45;
  const out = [];
  for (let i = 0; i < ps.length - 1; i++) {
    const gap = hhmm2min(ps[i + 1].start) - hhmm2min(ps[i].end);
    if (gap < minGap) continue;
    let kind = gap >= mealMin ? 'meal' : (gap >= longMin ? 'long' : 'short');
    out.push({ start: ps[i].end, end: ps[i + 1].start, minutes: gap, kind,
               label: ps[i].name + ' → ' + ps[i + 1].name });
  }
  return out;
}

function currentWindow(now) {
  const hm = now.getHours() * 60 + now.getMinutes();
  for (const w of dayWindows(now)) {
    if (hm >= hhmm2min(w.start) && hm < hhmm2min(w.end)) {
      return { w, remaining: hhmm2min(w.end) - hm };
    }
  }
  return { w: null, remaining: 0 };
}

/* ───────── 系数 ───────── */
function timeFactor(now) {
  const hm = now.getHours() * 60 + now.getMinutes();
  for (const [a, b, f, l] of CONST.time_factors) {
    if (hm >= hhmm2min(a) && hm < hhmm2min(b)) return { f, l };
  }
  return { f: CONST.closed_factor, l: '非营业时段' };
}

function breakFactor(now, w) {
  if (!w) return { f: 1.0, inBreak: false };
  const pulse = (RULES.pulse || {});
  const map = { short: pulse.short ?? 1.35, long: pulse.long ?? 1.20, meal: pulse.meal ?? 1.10 };
  return { f: map[w.kind] ?? 1.0, inBreak: true };
}

function scoreWeights(budget) {
  const tight = Math.max(0, Math.min(1, (30 - budget) / 18));
  const wSpeed = 0.15 + 0.35 * tight;
  const wComplete = 0.05 + 0.30 * (1 - tight);
  const wNut = 0.20 + 0.10 * (1 - tight);
  const wPrice = 1 - wSpeed - wComplete - wNut;
  return { speed: wSpeed, completeness: wComplete, nutrition: wNut, price: wPrice, tightness: tight };
}

/* ───────── 预估（与 Python 端同一公式） ───────── */
function baseMinutes(items) {
  // 最慢的串行打底，其余按并行折扣叠加
  const per = items.map(m => m.b);
  if (!per.length) return 0;
  const sorted = per.slice().sort((a, b) => b - a);
  const longest = sorted[0];
  const rest = sorted.slice(1).reduce((s, x) => s + x, 0);
  return longest + rest * CONST.parallel_discount;
}

function estimate(items, now, w, safe) {
  const base = baseMinutes(items);
  const tf = timeFactor(now);
  const bf = breakFactor(now, w);
  const kStore = 1.0;                       // 单机无校准历史 → 恒为 1，且 safe 口径下 >=1
  const minutes = base * tf.f * bf.f * (CAMPAIGN ? CONST.campaign_pulse : 1) * kStore;
  return { minutes, base, tf, bf, kStore,
           breakdown: { base, kTime: tf.f, kTimeLabel: tf.l,
                        kBreak: bf.f, kind: w ? w.kind : null,
                        kCampaign: CAMPAIGN ? CONST.campaign_pulse : 1, kStore } };
}

/* ───────── 营养 / 价格 / 速度 打分 ───────── */
function nutritionScore(name) {
  const n = NUT[name];
  if (!n || !n.kcal) return 0.60;                 // 未知 ≠ 差
  let s;
  const k = n.kcal;
  if (k >= 350 && k <= 650) s = 1.00;
  else if (k >= 250) s = 0.85;
  else if (k > 650 && k <= 900) s = 0.70;
  else if (k > 900) s = 0.45;
  else s = 0.60;
  if (n.sodium > 1000) s -= 0.15;
  if (n.protein >= 20) s += 0.05;
  return Math.max(0.30, Math.min(1.00, s));
}

function priceScore(p) {
  if (!p) return 0.50;
  if (p <= 30) return 1.00;
  if (p <= 45) return 0.85;
  if (p <= 60) return 0.65;
  return 0.45;
}

function recommend(items, budgetMin, now, w, budgetMoney, topK) {
  const wt = scoreWeights(budgetMin);
  const rows = items.map(m => {
    const e = estimate([m], now, w, true);
    const fit = e.minutes <= budgetMin;
    const speed = Math.max(0, 1 - e.minutes / Math.max(budgetMin * 2, 1));
    const nut = nutritionScore(m.n);
    const pri = priceScore(m.p);
    let score = wt.speed * speed + wt.completeness * m.cp + wt.nutrition * nut + wt.price * pri;
    if (!fit) score *= 0.35;
    if (budgetMoney > 0 && m.p > budgetMoney) { score *= 0.25; }
    return { m, wait: e.minutes, fit, score, nut, pri, speed };
  });
  rows.sort((a, b) => (b.fit - a.fit) || (b.score - a.score));

  // 第一轮只取正餐（完整度 >= 0.5），不足再用饮品/小食补
  const cap = c => (c === '饮品' ? 1 : 2);
  const picked = [], used = {};
  const take = list => {
    for (const r of list) {
      if (picked.length >= topK) return;
      if (!r.fit) continue;
      const c = r.m.cat;
      if ((used[c] || 0) >= cap(c)) continue;
      picked.push(r); used[c] = (used[c] || 0) + 1;
    }
  };
  take(rows.filter(r => r.m.cp >= 0.50));
  if (picked.length < topK) take(rows);
  return { picked, weights: wt, all: rows };
}

/* ───────── 在制订单数 L = λW ───────── */
function inSystemCount(now, waitMin) {
  const peak = QCFG.peak_rate_per_min ?? 1.2;
  const peakK = QCFG.peak_time_factor ?? 1.55;
  const src = String(QCFG.rate_source || 'default').toLowerCase();
  const tf = timeFactor(now);
  const lam = peakK > 0 ? peak * (tf.f / peakK) : peak;
  return { L: Math.max(1, Math.round(lam * waitMin)), lam,
           conf: src === 'manager' ? 'medium' : 'low',
           basis: src === 'manager' ? '门店给定峰值 × 时段系数' : '内置默认峰值 × 时段系数（未标定）' };
}

/* ───────── 渲染 ───────── */
const KIND_TXT = { short: '小课间', long: '大课间', meal: '饭点' };
const $ = id => document.getElementById(id);

function render() {
  const [hh, mm] = $('atTime').value.split(':').map(Number);
  const now = new Date();
  now.setHours(hh || 0, mm || 0, 0, 0);

  const prof = profileFor(now);
  const wins = dayWindows(now);
  const cw = currentWindow(now);

  // 课间时长下拉：当前窗口 + 常见值
  const sel = $('breakSel');
  const prev = sel.value;
  const opts = [];
  if (cw.w) {
    // 默认用「剩余」而不是窗口全长 —— 用户关心的是"我现在还剩几分钟"
    const left = cw.remaining > 0 ? cw.remaining : cw.w.minutes;
    opts.push({ v: left, t: `当前窗口剩余 ${left} 分（${cw.w.start}–${cw.w.end} · ${KIND_TXT[cw.w.kind]}）` });
  }
  [10, 15, 20, 25, 30, 45, 60, 90].forEach(v => opts.push({ v, t: `${v} 分钟` }));
  sel.innerHTML = opts.map(o => `<option value="${o.v}">${o.t}</option>`).join('');
  sel.value = prev || (opts[0] ? opts[0].v : 10);
  if (!sel.value) sel.value = 10;

  const breakMin = parseFloat(sel.value) || 10;
  const budgetMin = Math.max(1, breakMin - WALK);
  const money = parseFloat($('budget').value) || 0;
  const topK = parseInt($('topK').value, 10) || 3;

  $('subtitle').textContent = `${DATA.school} · ${DATA.term} · 数据快照抓取于 ${DATA.meta.captured_at}`;
  $('metaChips').innerHTML = [
    `<span class="chip">在售餐品 ${MEALS.length} 项</span>`,
    `<span class="chip">步行往返 ${WALK} 分钟</span>`,
    `<span class="chip ${CAMPAIGN ? 'hi' : ''}">${CAMPAIGN ? '当日有促销/上新 ×' + CONST.campaign_pulse : '当日无活动'}</span>`,
    `<span class="chip hi">离线运行 · 零 Token</span>`,
  ].join('');

  const winChips = [];
  winChips.push(`<span class="chip">适用作息 ${prof.label}</span>`);
  if (cw.w) winChips.push(`<span class="chip hi">当前 ${cw.w.start}–${cw.w.end} · ${KIND_TXT[cw.w.kind]} · 剩 ${cw.remaining} 分</span>`);
  else winChips.push('<span class="chip">当前不在课间窗口</span>');
  winChips.push(`<span class="chip">课间 ${breakMin} 分 − 步行 ${WALK} 分 = 可等 ${budgetMin} 分</span>`);
  $('winChips').innerHTML = winChips.join('');

  const { picked, weights, all } = recommend(MEALS, budgetMin, now, cw.w, money, topK);

  // 结论
  const v = $('verdict');
  if (!picked.length) {
    v.className = 'verdict warn';
    v.textContent = '这个时间窗口内没有餐品来得及做好。建议提前一节课点，或换到时间更长的窗口。';
    $('recs').innerHTML = '';
  } else {
    const best = picked[0];
    const over = all.filter(r => !r.fit).length;
    v.className = 'verdict ok';
    v.textContent = `推荐「${best.m.n}」，约 ${fmt(best.wait)} 分钟能拿到，${budgetMin} 分钟内来得及`
      + `（共 ${all.length} 项在售，其中 ${over} 项本窗口内做不完，已排除）`;
    $('recs').innerHTML = picked.map((r, i) => `
      <div class="rec">
        <div class="rk">#${i + 1}</div>
        <div class="nm">${r.m.n}</div>
        <div class="pr">¥${fmt(r.m.p)}</div>
        <span class="tag">${r.m.cat} · 基准 ${fmt(r.m.b)} 分</span>
        <div class="wt">约 <b>${fmt(r.wait)}</b> 分钟能拿到　
          <span style="color:#9aa1ad">得分 ${fmt(r.score, 3)}</span></div>
      </div>`).join('');
  }

  const q = inSystemCount(now, picked.length ? picked[0].wait : (all[0] ? all[0].wait : budgetMin));
  $('caveat').innerHTML =
    `<b>不能显示"前方还有几个人"</b>：麦当劳门店不按取餐号顺序出餐（店员原话「取餐编号不等于取餐顺序」）。<br>`
    + `能算的是<b>门店当前约 ${q.L} 单在做</b>（L = λ × W：λ≈${fmt(q.lam, 3)} 单/分 × ${fmt(picked.length ? picked[0].wait : budgetMin)} 分），`
    + `依据「${q.basis}」，置信度 <b>${q.conf}</b>。`;

  // 依据表
  const e = picked.length ? estimate([picked[0].m], now, cw.w, true) : null;
  const bd = e ? e.breakdown : null;
  $('breakdown').innerHTML = bd ? `
    <tr><th>项</th><th style="text-align:right">值</th><th>说明</th></tr>
    <tr><td>基准出餐</td><td class="n">${fmt(bd.base, 2)} 分</td><td>按餐品工序分类</td></tr>
    <tr><td>时段系数</td><td class="n">${fmt(bd.kTime, 2)}</td><td>${bd.kTimeLabel}</td></tr>
    <tr><td>课间脉冲</td><td class="n">${fmt(bd.kBreak, 2)}</td><td>${cw.w ? KIND_TXT[cw.w.kind] : '不在窗口内'}</td></tr>
    <tr><td>促销系数</td><td class="n">${fmt(bd.kCampaign, 2)}</td><td>${CAMPAIGN ? '当日有活动' : '当日无活动'}</td></tr>
    <tr><td>门店系数</td><td class="n">${fmt(bd.kStore, 2)}</td><td>单机无校准历史，恒为 1</td></tr>
    <tr><td><b>合计</b></td><td class="n"><b>${fmt(e.minutes, 2)}</b></td><td>分钟</td></tr>
  ` : '<tr><td class="tiny">无推荐项</td></tr>';

  $('formula').innerHTML =
    `T_wait = 基准出餐 × 时段系数 × 课间脉冲 × 促销系数 × 门店系数（× 负载）<br>`
    + `打分权重（随窗口松紧自适应）：速度 ${fmt(weights.speed, 3)}｜完整度 ${fmt(weights.completeness, 3)}`
    + `｜营养 ${fmt(weights.nutrition, 3)}｜价格 ${fmt(weights.price, 3)}（松紧度 ${fmt(weights.tightness, 2)}）`;

  $('footMeta').textContent =
    `${DATA.meta.server.name} v${DATA.meta.server.version} · 快照抓取于 ${DATA.meta.captured_at} · `
    + `与服务端版本 ${DATA.meta.protocol}`;
}

['atTime', 'breakSel', 'budget', 'topK'].forEach(id => {
  const el = $(id);
  el.addEventListener('input', render);
  el.addEventListener('change', render);
});
render();
</script>
</body></html>
"""


def main() -> int:
    payload = build_payload()
    doc = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"已生成 {OUT}")
    print(f"  餐品 {len(payload['meals'])} 项，营养 {len(payload['nutrition'])} 条")
    print(f"  文件大小 {os.path.getsize(OUT)/1024:.1f} KB（单文件，零依赖，零 Token）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
