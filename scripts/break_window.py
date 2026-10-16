"""麦麦课间窗口 —— 取餐等待预估与快取推荐核心

设计前提（重要）：
    麦当劳 MCP **不提供**任何出餐/排队时长的实时信号（已对 35 个 Tool 全量核查）。
    因此等待时长必须由本项目自行建模，并利用 query-order 的订单状态
    （1待支付 / 2配餐中 / 3配送中 / 4已完成 / 5已取消 / 10餐厅确认配餐中）
    实测「下单 → 配餐完成」的真实耗时，回填校准，使预估越用越准。

模型（可解释、可审计）：
    T_wait = T_base(餐品) × K_time(时段) × K_break(课间脉冲)
             × K_campaign(促销) × K_store(门店) × K_load(近期负载)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

STATE_PATH = os.path.join(os.path.expanduser("~"), ".mcd-break-window", "state.json")

# ---------------------------------------------------------------- 出餐基准表

# 规则按顺序匹配，首个命中生效。数值来自门店出餐工序拆解的经验基线，
# 并通过 calibration 逐步修正（见 PrepModel.calibrate）。
BASE_RULES: list[tuple[tuple[str, ...], float, str]] = [
    (("可乐", "雪碧", "芬达", "怡泉", "柠檬红茶", "苹果汁", "纯悦", "鲜萃咖啡",
      "锡兰红茶", "牛奶", "奶铁", "美式", "卡布奇诺", "玛奇朵", "豆浆",
      "橙橙", "柠柠", "果汁", "燕麦"), 0.8, "饮品"),
    (("圆筒", "新地", "麦旋风", "冰淇淋", "派", "薯条", "玉米杯", "苹果片",
      "薯饼", "莓"), 1.8, "小食甜品"),
    (("鸡块", "鸡翅", "V翅", "鸡排", "脆汁鸡", "鸡柳", "肉骨鸡", "鸡架"), 2.9, "炸鸡"),
    (("卷",), 2.6, "卷类"),
    (("麦辣鸡腿堡", "板烧", "麦香鸡", "鸡肉堡", "鸡腿堡"), 3.2, "鸡肉汉堡"),
    (("安格斯",), 4.2, "安格斯厚牛堡"),
    (("巨无霸", "吉士汉堡", "麦香鱼", "鳕鱼堡", "牛堡", "高达", "不素之霸"), 3.5, "牛肉/鱼汉堡"),
    (("三件套", "四件套", "套餐", "随心选", "随心配", "双人餐", "小食盘",
      "口福", "拼盘", "分享餐", "乐园", "亲子", "精选单人餐"), 3.8, "组合餐"),
]
DEFAULT_BASE = 3.0

# 时段系数：校园门店作息（可在 campus_schedule.json 中覆盖）
TIME_FACTORS: list[tuple[str, str, float, str]] = [
    ("07:30", "09:00", 1.15, "早高峰"),
    ("09:00", "11:30", 1.00, "上午课时段"),
    ("11:30", "13:10", 1.55, "午高峰"),
    ("13:10", "14:15", 1.20, "午后"),
    ("14:15", "16:40", 0.95, "下午课时段"),
    ("16:40", "18:30", 1.50, "放学高峰"),
    ("18:30", "21:45", 1.30, "夜市时段"),
    ("21:45", "23:00", 1.10, "宵夜时段"),
]
CLOSED_FACTOR = 0.60

# 课间脉冲：课间窗口内学生集中到店，出餐压力短时抬升
BREAK_PULSE = 1.35

# 有进行中的促销/上新活动时的叠加系数
CAMPAIGN_PULSE = 1.25

# 单笔订单的并行上限：同一订单内多个餐品的制作是部分并行的
PARALLEL_DISCOUNT = 0.75


@dataclass
class Item:
    code: str
    name: str
    quantity: int = 1
    price: float = 0.0

    @property
    def category(self) -> str:
        for keywords, _base, label in BASE_RULES:
            if any(k in self.name for k in keywords):
                return label
        return "其他"

    @property
    def base_minutes(self) -> float:
        for keywords, base, _label in BASE_RULES:
            if any(k in self.name for k in keywords):
                return base
        return DEFAULT_BASE


@dataclass
class Estimate:
    minutes: float
    low: float
    high: float
    breakdown: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimate_minutes": round(self.minutes, 1),
            "range_minutes": [round(self.low, 1), round(self.high, 1)],
            "breakdown": self.breakdown,
        }


class PrepModel:
    """可校准的出餐等待预估模型。"""

    def __init__(self, state_path: str = STATE_PATH, schedule: dict[str, Any] | None = None) -> None:
        self.state_path = state_path
        self.schedule = schedule or {}
        self.state = self._load_state()

    # ---------- 状态持久化 ----------

    def _load_state(self) -> dict[str, Any]:
        default = {"samples": [], "store_factor": {}, "category_delta": {}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for key, value in default.items():
                data.setdefault(key, value)
            return data
        except (OSError, json.JSONDecodeError):
            return default

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)

    # ---------- 系数 ----------

    def time_factor(self, at: datetime) -> tuple[float, str]:
        hhmm = at.strftime("%H:%M")
        for start, end, factor, label in TIME_FACTORS:
            if start <= hhmm < end:
                return factor, label
        return CLOSED_FACTOR, "非营业时段"

    def break_windows(self) -> list[tuple[str, str]]:
        raw = self.schedule.get("break_windows")
        if raw:
            return [(w["start"], w["end"]) for w in raw]
        return [("09:40", "09:50"), ("14:40", "14:50")]

    def break_factor(self, at: datetime) -> tuple[float, bool]:
        hhmm = at.strftime("%H:%M")
        for start, end in self.break_windows():
            lo = (datetime.strptime(start, "%H:%M") - timedelta(minutes=5)).strftime("%H:%M")
            hi = (datetime.strptime(end, "%H:%M") + timedelta(minutes=15)).strftime("%H:%M")
            if lo <= hhmm <= hi:
                return BREAK_PULSE, True
        return 1.0, False

    def store_factor(self, store_code: str) -> float:
        return float(self.state["store_factor"].get(store_code, 1.0))

    def load_factor(self, at: datetime, store_code: str) -> tuple[float, int]:
        """近 15 分钟该门店成交样本数 → 负载修正。"""
        cutoff = at - timedelta(minutes=15)
        recent = [
            s for s in self.state["samples"]
            if s.get("store") == store_code
            and datetime.fromisoformat(s["at"]) >= cutoff
        ]
        n = len(recent)
        # 每 3 单抬升 4%，上限 1.3
        return min(1.0 + 0.04 * (n // 3), 1.3), n

    # ---------- 预估 ----------

    def base_minutes(self, items: Iterable[Item]) -> tuple[float, list[dict[str, Any]]]:
        detail: list[dict[str, Any]] = []
        per_item: list[float] = []
        for item in items:
            minutes = item.base_minutes * item.quantity
            per_item.append(minutes)
            detail.append({
                "code": item.code, "name": item.name,
                "category": item.category, "quantity": item.quantity,
                "base_minutes": round(minutes, 2),
            })
        if not per_item:
            return 0.0, detail
        # 最慢的一样串行打底，其余按并行折扣叠加
        longest = max(per_item)
        rest = sum(sorted(per_item)[:-1])
        return longest + rest * PARALLEL_DISCOUNT, detail

    def estimate(
        self,
        items: list[Item],
        at: datetime | None = None,
        store_code: str = "-",
        campaign_active: bool = False,
    ) -> Estimate:
        at = at or datetime.now()
        base, detail = self.base_minutes(items)
        k_time, time_label = self.time_factor(at)
        k_break, in_break = self.break_factor(at)
        k_campaign = CAMPAIGN_PULSE if campaign_active else 1.0
        k_store = self.store_factor(store_code)
        k_load, recent_count = self.load_factor(at, store_code)

        minutes = base * k_time * k_break * k_campaign * k_store * k_load

        # 置信区间：样本越多越窄，从 ±35% 收敛到 ±15%
        n = len(self.state["samples"])
        spread = max(0.15, 0.35 - 0.002 * n)
        breakdown = {
            "base_minutes": round(base, 2),
            "time_factor": {"value": k_time, "label": time_label},
            "break_pulse": {"value": k_break, "in_break": in_break},
            "campaign_factor": k_campaign,
            "store_factor": round(k_store, 3),
            "load_factor": {"value": round(k_load, 3), "recent_orders": recent_count},
            "items": detail,
            "calibration_samples": n,
        }
        return Estimate(minutes, minutes * (1 - spread), minutes * (1 + spread), breakdown)

    # ---------- 校准 ----------

    def calibrate(self, store_code: str, predicted: float, actual: float, at: datetime | None = None) -> dict[str, Any]:
        """回填一次真实耗时（来自 query-order 的配餐完成时刻）。"""
        at = at or datetime.now()
        self.state["samples"].append({
            "store": store_code, "at": at.isoformat(),
            "predicted": round(predicted, 2), "actual": round(actual, 2),
        })
        samples = [s for s in self.state["samples"] if s["store"] == store_code][-30:]
        if samples:
            ratios = [s["actual"] / s["predicted"] for s in samples if s["predicted"] > 0]
            if ratios:
                # 指数滑动，避免单次异常样本把系数带偏
                target = sum(ratios) / len(ratios)
                current = self.store_factor(store_code)
                self.state["store_factor"][store_code] = round(current * 0.7 + target * 0.3, 4)
        self.save()
        return {
            "samples_total": len(self.state["samples"]),
            "store_factor": self.store_factor(store_code),
        }


# ---------------------------------------------------------------- 推荐引擎

@dataclass
class Suggestion:
    item: Item
    wait_minutes: float
    fits: bool
    score: float
    reasons: list[str]


def recommend(
    model: PrepModel,
    items: list[Item],
    budget_minutes: float,
    at: datetime | None = None,
    store_code: str = "-",
    campaign_active: bool = False,
    nutrition: dict[str, dict[str, float]] | None = None,
    top_k: int = 3,
) -> list[Suggestion]:
    """在「课间预算」内推荐出餐快、营养合理、价格适中的餐品。"""
    at = at or datetime.now()
    nutrition = nutrition or {}
    results: list[Suggestion] = []

    for item in items:
        est = model.estimate([item], at=at, store_code=store_code, campaign_active=campaign_active)
        wait = est.minutes
        fits = wait <= budget_minutes
        reasons: list[str] = []
        if fits:
            reasons.append(f"预计 {wait:.1f} 分钟可取，在 {budget_minutes:.0f} 分钟课间窗口内")
        else:
            reasons.append(f"预计 {wait:.1f} 分钟，超出课间窗口 {wait - budget_minutes:.1f} 分钟")

        # 出餐速度分（越快越高）
        speed = max(0.0, 1.0 - wait / max(budget_minutes * 2, 1.0))
        # 营养分：以「有能量数据 + 蛋白占比合理」为准，纯饮品/甜品不给高分
        nut = nutrition.get(item.name, {})
        kcal = nut.get("energyKcal", 0.0)
        protein = nut.get("protein", 0.0)
        if kcal and 250 <= kcal <= 700:
            nut_score = 0.8 + min(protein / 40.0, 0.2)
        elif kcal:
            nut_score = 0.45
        else:
            nut_score = 0.3
        # 价格分：单价 15-35 元为校园最优区间
        if 15 <= item.price <= 35:
            price_score = 1.0
        elif item.price:
            price_score = 0.6
        else:
            price_score = 0.5

        score = 0.55 * speed + 0.25 * nut_score + 0.20 * price_score
        if not fits:
            score *= 0.35  # 来不及的强惩罚

        results.append(Suggestion(item, wait, fits, score, reasons))

    results.sort(key=lambda s: (-s.fits, -s.score))
    feasible = [s for s in results if s.fits][:top_k]
    if feasible:
        return feasible
    return results[:top_k]


# ---------------------------------------------------------------- 工具函数

def load_nutrition(path: str) -> dict[str, dict[str, float]]:
    """解析 list-nutrition-foods 的表格文本 → {餐品名: 指标}。"""
    table: dict[str, dict[str, float]] = {}
    if not os.path.exists(path):
        return table
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line or line.startswith("[") or "productName" in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        try:
            table[parts[0]] = {
                "energyKj": float(parts[2]), "energyKcal": float(parts[3]),
                "protein": float(parts[4]), "fat": float(parts[5]),
                "carbohydrate": float(parts[6]), "sodium": float(parts[7]),
                "calcium": float(parts[8]),
            }
        except ValueError:
            continue
    return table


def parse_menu(raw: dict[str, Any]) -> list[Item]:
    """把 query-meals 的返回解析成 Item 列表（去重、保序）。"""
    data = raw.get("data", raw)
    detail = data.get("meals", {}) or {}
    seen: set[str] = set()
    items: list[Item] = []
    for category in data.get("categories", []) or []:
        for meal in category.get("meals", []) or []:
            code = meal.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            info = detail.get(code, {}) or {}
            name = info.get("name") or code
            try:
                price = float(info.get("currentPrice") or 0)
            except (TypeError, ValueError):
                price = 0.0
            items.append(Item(code=code, name=name, price=price))
    return items


def humanize(estimate: Estimate) -> str:
    b = estimate.breakdown
    lines = [
        f"预计等待：{estimate.minutes:.1f} 分钟（区间 {estimate.low:.1f}–{estimate.high:.1f}）",
        f"  基准出餐 {b['base_minutes']} 分钟",
        f"  时段系数 {b['time_factor']['value']}（{b['time_factor']['label']}）",
        f"  课间脉冲 {b['break_pulse']['value']}"
        + ("（当前处于课间窗口）" if b["break_pulse"]["in_break"] else ""),
        f"  促销系数 {b['campaign_factor']}",
        f"  门店系数 {b['store_factor']}（校准样本 {b['calibration_samples']} 条）",
        f"  负载系数 {b['load_factor']['value']}（近 15 分钟 {b['load_factor']['recent_orders']} 单）",
    ]
    return "\n".join(lines)
