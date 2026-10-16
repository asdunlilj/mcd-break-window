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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from calibration_guard import (
    audit_sample,
    check_load_monotonicity,
    clamp_store_factor,
)

STATE_PATH = os.path.join(os.path.expanduser("~"), ".mcd-break-window", "state.json")
DEFAULT_SCHEDULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "campus_schedule.json"
)

# 未配置 window_rules.pulse 时的兜底脉冲系数
WINDOW_PULSE_FALLBACK = {"short": 1.35, "long": 1.20, "meal": 1.10}

# 课间窗口类型的中文说明，用于输出
WINDOW_KIND_TEXT = {
    "short": "小课间（仅快餐品可行）",
    "long": "大课间（可选常规餐品）",
    "meal": "餐段（时间充裕）",
}


def _hm(value: str) -> datetime:
    return datetime.strptime(value, "%H:%M")


def _load_schedule(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class ClassWindow:
    """课程之间的空档窗口，由作息表的 periods 自动推导。"""

    start: str
    end: str
    minutes: int
    kind: str          # short / long / meal
    label: str         # 如「第一节课 → 第二节课」

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start, "end": self.end,
            "minutes": self.minutes, "kind": self.kind,
            "kind_text": WINDOW_KIND_TEXT.get(self.kind, self.kind),
            "label": self.label,
        }

# ---------------------------------------------------------------- 出餐基准表

# 规则按顺序匹配，首个命中生效。数值来自门店出餐工序拆解的经验基线，
# 并通过 calibration 逐步修正（见 PrepModel.calibrate）。
#
# ⚠️ 顺序很重要：**组合餐必须排在最前**。
#    否则「龙焰鸡腿堡三件套」会先命中「鸡腿堡」规则而被当成单品鸡肉汉堡，
#    导致基准时长与类别都错（这是早期版本的缺陷，已修复）。
BASE_RULES: list[tuple[tuple[str, ...], float, str]] = [
    (("三件套", "四件套", "套餐", "随心选", "随心配", "双人餐", "小食盘",
      "口福", "拼盘", "分享餐", "乐园", "亲子", "精选单人餐"), 3.8, "组合餐"),
    (("可乐", "雪碧", "芬达", "怡泉", "柠檬红茶", "苹果汁", "纯悦", "鲜萃咖啡",
      "锡兰红茶", "牛奶", "奶铁", "美式", "卡布奇诺", "玛奇朵", "豆浆",
      "橙橙", "柠柠", "果汁", "燕麦"), 0.8, "饮品"),
    (("圆筒", "新地", "麦旋风", "冰淇淋", "派", "薯条", "玉米杯", "苹果片",
      "薯饼", "莓"), 1.8, "小食甜品"),
    (("鸡块", "鸡翅", "V翅", "鸡排", "脆汁鸡", "鸡柳", "肉骨鸡", "鸡架"), 2.9, "炸鸡"),
    (("卷",), 2.6, "卷类"),
    (("安格斯",), 4.2, "安格斯厚牛堡"),
    (("麦辣鸡腿堡", "板烧", "麦香鸡", "鸡肉堡", "鸡腿堡"), 3.2, "鸡肉汉堡"),
    (("巨无霸", "吉士汉堡", "麦香鱼", "鳕鱼堡", "牛堡", "高达", "不素之霸"), 3.5, "牛肉/鱼汉堡"),
]
DEFAULT_BASE = 3.0

# 正餐完整度：用于让「时间充裕的窗口」推荐吃得饱的组合，而非永远推荐最快的单品
COMPLETENESS_BY_CATEGORY = {
    "组合餐": 1.00,
    "安格斯厚牛堡": 0.70,
    "鸡肉汉堡": 0.60,
    "牛肉/鱼汉堡": 0.60,
    "卷类": 0.50,
    "炸鸡": 0.50,
    "小食甜品": 0.30,
    "饮品": 0.10,
}
DEFAULT_COMPLETENESS = 0.45

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

    @property
    def completeness(self) -> float:
        """正餐完整度 0.10（仅饮品）～ 1.00（完整套餐）。"""
        return COMPLETENESS_BY_CATEGORY.get(self.category, DEFAULT_COMPLETENESS)


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

    def __init__(self, state_path: str = STATE_PATH, schedule: dict[str, Any] | None = None,
                 schedule_path: str = DEFAULT_SCHEDULE_PATH) -> None:
        self.state_path = state_path
        if schedule is None:
            schedule = _load_schedule(schedule_path)
        self.schedule = schedule or {}
        self.state = self._load_state()

    # ---------- 状态持久化 ----------

    def _load_state(self) -> dict[str, Any]:
        default = {
            "samples": [], "store_factor": {}, "category_delta": {},
            "frozen": False, "frozen_reason": "", "rejected": [],
        }
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

    # ---------- 作息表驱动的课间窗口 ----------

    def profile_for(self, at: datetime) -> dict[str, Any]:
        """按星期几选择作息 profile（周一至周四 / 周五周六 / 周日）。"""
        wd = at.weekday()
        for _key, prof in (self.schedule.get("profiles") or {}).items():
            if wd in (prof.get("days") or []):
                return prof
        return {"label": "未知", "periods": []}

    def day_windows(self, at: datetime) -> list[ClassWindow]:
        """从作息表的相邻课程空档推导出当天的所有课间窗口。"""
        prof = self.profile_for(at)
        periods = sorted(prof.get("periods") or [], key=lambda p: p["start"])
        rules = self.schedule.get("window_rules") or {}
        min_gap = int(rules.get("min_gap_minutes", 5))
        short_max = int(rules.get("short_max_minutes", 12))
        long_min = int(rules.get("long_min_minutes", 25))
        meal_min = int(rules.get("meal_min_minutes", 45))

        windows: list[ClassWindow] = []
        for prev, nxt in zip(periods, periods[1:]):
            gap = int((_hm(nxt["start"]) - _hm(prev["end"])).total_seconds() // 60)
            if gap < min_gap:
                continue
            if gap >= meal_min:
                kind = "meal"
            elif gap >= long_min:
                kind = "long"
            elif gap <= short_max:
                kind = "short"
            else:
                kind = "short" if gap <= short_max else "long"
            windows.append(ClassWindow(
                start=prev["end"], end=nxt["start"], minutes=gap, kind=kind,
                label=f"{prev['name']} → {nxt['name']}",
            ))
        return windows

    def current_window(self, at: datetime) -> tuple[ClassWindow | None, int]:
        """返回当前所处课间窗口与剩余分钟数；不在任何窗口内则返回 (None, 0)。"""
        hhmm = at.strftime("%H:%M")
        for w in self.day_windows(at):
            if w.start <= hhmm < w.end:
                remaining = int((_hm(w.end) - _hm(hhmm)).total_seconds() // 60)
                return w, remaining
        return None, 0

    def store_open(self, at: datetime) -> bool:
        hours = self.schedule.get("store_hours") or {}
        hhmm = at.strftime("%H:%M")
        return hours.get("open", "00:00") <= hhmm < hours.get("close", "23:59")

    def break_factor(self, at: datetime) -> tuple[float, bool]:
        """课间脉冲：按窗口类型取值（小课间压力最大）。"""
        window, _remaining = self.current_window(at)
        if window is None:
            return 1.0, False
        pulses = (self.schedule.get("window_rules") or {}).get("pulse") or {}
        factor = pulses.get(window.kind, WINDOW_PULSE_FALLBACK.get(window.kind, 1.0))
        return float(factor), True

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
        safe: bool = False,
    ) -> Estimate:
        """预估等待时长。

        safe=False → 点估计，用于**排序**（允许乐观，让推荐有区分度）
        safe=True  → 保守包络，用于**「课间是否来得及」的判定**。
                     强制 K_store ≥ 1.0，即校准只能让判定更保守，永不允许更乐观。

        这是 R5「保守单向」防线的落地：即使校准数据被批量点完成污染而把
        门店系数压低，判定口径也不会因此变得乐观。
        """
        at = at or datetime.now()
        base, detail = self.base_minutes(items)
        k_time, time_label = self.time_factor(at)
        k_break, in_break = self.break_factor(at)
        window, window_remaining = self.current_window(at)
        k_campaign = CAMPAIGN_PULSE if campaign_active else 1.0
        raw_store = self.store_factor(store_code)
        k_store = max(1.0, raw_store) if safe else raw_store
        k_load, recent_count = self.load_factor(at, store_code)

        minutes = base * k_time * k_break * k_campaign * k_store * k_load

        # 置信区间：样本越多越窄，从 ±35% 收敛到 ±15%
        n = len(self.state["samples"])
        spread = max(0.15, 0.35 - 0.002 * n)
        breakdown = {
            "envelope": "safe" if safe else "point",
            "base_minutes": round(base, 2),
            "time_factor": {"value": k_time, "label": time_label},
            "break_pulse": {"value": k_break, "in_break": in_break},
            "break_window": (
                {**window.as_dict(), "remaining_minutes": window_remaining} if window else None
            ),
            "campaign_factor": k_campaign,
            "store_factor": round(k_store, 3),
            "store_factor_raw": round(raw_store, 3),
            "load_factor": {"value": round(k_load, 3), "recent_orders": recent_count},
            "items": detail,
            "calibration_samples": n,
            "calibration_frozen": bool(self.state.get("frozen")),
        }
        return Estimate(minutes, minutes * (1 - spread), minutes * (1 + spread), breakdown)

    # ---------- 校准（带完整性守卫） ----------

    def calibrate(
        self,
        store_code: str,
        predicted: float,
        actual: float,
        at: datetime | None = None,
        base_minutes: float | None = None,
        source: str = "status",
        load: int = 0,
        first_observed_seconds: float = 999.0,
    ) -> dict[str, Any]:
        """回填一次真实耗时，并执行 R1–R4 完整性守卫。

        source 取值：
            "user"   —— 用户亲口/按键确认"已取到餐"，最可信
            "status" —— 由 query-order 的 orderStatus 变化推断，可能被批量点完成污染
        """
        at = at or datetime.now()
        sample: dict[str, Any] = {
            "store": store_code,
            "at": at.isoformat(),
            "predicted": round(predicted, 2),
            "actual": round(actual, 2),
            "base_minutes": round(base_minutes if base_minutes is not None else predicted, 2),
            "source": source,
            "load": load,
            "first_observed_seconds": first_observed_seconds,
        }

        # R1 / R2 / R3 准入判定
        verdict = audit_sample(sample, sample["base_minutes"], self.state["samples"])
        if not verdict.accepted:
            self.state["rejected"].append({**sample, "reasons": verdict.reasons})
            self.save()
            return {
                "accepted": False,
                "guard": verdict.as_dict(),
                "samples_total": len(self.state["samples"]),
                "rejected_total": len(self.state["rejected"]),
                "store_factor": self.store_factor(store_code),
            }

        sample["trust"] = verdict.trust
        self.state["samples"].append(sample)

        # R4 负载单调性检验
        ok, rho, message = check_load_monotonicity(self.state["samples"], store_code)
        if not ok:
            self.state["frozen"] = True
            self.state["frozen_reason"] = message
            self.save()
            return {
                "accepted": True,
                "guard": verdict.as_dict(),
                "frozen": True,
                "frozen_reason": message,
                "spearman_rho": rho,
                "samples_total": len(self.state["samples"]),
                "store_factor": self.store_factor(store_code),
            }

        # 信任加权更新门店系数
        recent = [
            s for s in self.state["samples"]
            if s["store"] == store_code and s.get("predicted", 0) > 0
        ][-30:]
        num = sum(s["actual"] * s.get("trust", 0.4) for s in recent)
        den = sum(s["predicted"] * s.get("trust", 0.4) for s in recent)
        note = None
        if den > 0:
            target = num / den
            current = self.store_factor(store_code)
            merged = current * 0.7 + target * 0.3
            merged, note = clamp_store_factor(merged)
            self.state["store_factor"][store_code] = round(merged, 4)
        self.save()

        return {
            "accepted": True,
            "guard": verdict.as_dict(),
            "frozen": False,
            "spearman_rho": rho,
            "monotonicity_note": message,
            "samples_total": len(self.state["samples"]),
            "rejected_total": len(self.state["rejected"]),
            "store_factor": self.store_factor(store_code),
            "clamp_note": note,
        }

    def unfreeze(self, reason: str = "人工复核后解除") -> dict[str, Any]:
        """人工复核确认数据可信后，解除校准冻结。"""
        self.state["frozen"] = False
        self.state["frozen_reason"] = f"已解除（{reason}）"
        self.save()
        return {"frozen": False, "reason": self.state["frozen_reason"]}

    def integrity_report(self) -> dict[str, Any]:
        """输出校准数据完整性报告，供审计。"""
        samples = self.state["samples"]
        rejected = self.state["rejected"]
        by_source: dict[str, int] = {}
        for s in samples:
            by_source[s.get("source", "unknown")] = by_source.get(s.get("source", "unknown"), 0) + 1
        return {
            "samples_accepted": len(samples),
            "samples_rejected": len(rejected),
            "accepted_by_source": by_source,
            "frozen": bool(self.state.get("frozen")),
            "frozen_reason": self.state.get("frozen_reason", ""),
            "rejected_detail": [
                {"at": r.get("at"), "actual": r.get("actual"), "reasons": r.get("reasons", [])}
                for r in rejected[-10:]
            ],
        }


# ---------------------------------------------------------------- 推荐引擎

# 选择阶段的每类别上限：饮品单独限 1 项，避免"两杯可乐"式的冗余推荐
CATEGORY_CAP = {"饮品": 1}
DEFAULT_CATEGORY_CAP = 2


def _cap_for(category: str) -> int:
    return CATEGORY_CAP.get(category, DEFAULT_CATEGORY_CAP)


@dataclass
class Suggestion:
    item: Item
    wait_minutes: float
    fits: bool
    score: float
    reasons: list[str]
    safe_wait_minutes: float = 0.0
    guard_note: str = ""


def score_weights(budget_minutes: float) -> dict[str, float]:
    """打分权重随窗口松紧自适应。

    这是修复一处真实排序缺陷的产物。早期版本把速度权重写死为 0.55，
    导致「28 分钟的大课间」仍然只推荐最快的那三个单品——因为同一批餐品
    在速度、价格、营养上都同时占优，排序对权重不敏感，无论怎么调顺序都不变。

    修正后的四个维度，关键在于引入**正餐完整度**（completeness）：
        紧窗口（≤12 分钟）：速度主导（0.50），完整度几乎不计（0.05）
                            —— 来不及的餐品毫无意义
        宽窗口（≥30 分钟）：完整度升到 0.35，速度降到 0.15
                            —— 既然都来得及，就该选吃得饱的正餐
    """
    tight = max(0.0, min(1.0, (30.0 - budget_minutes) / 18.0))
    w_speed = 0.15 + 0.35 * tight
    w_complete = 0.05 + 0.30 * (1.0 - tight)
    w_nut = 0.20 + 0.10 * (1.0 - tight)
    w_price = 1.0 - w_speed - w_complete - w_nut
    return {
        "speed": round(w_speed, 3),
        "completeness": round(w_complete, 3),
        "nutrition": round(w_nut, 3),
        "price": round(w_price, 3),
        "tightness": round(tight, 3),
    }


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
    w = score_weights(budget_minutes)

    for item in items:
        est = model.estimate([item], at=at, store_code=store_code, campaign_active=campaign_active)
        safe_est = model.estimate([item], at=at, store_code=store_code,
                                  campaign_active=campaign_active, safe=True)
        wait = est.minutes
        safe_wait = safe_est.minutes

        # R5：判定口径用保守包络，永不允许比未校准基线更乐观
        fits = safe_wait <= budget_minutes
        reasons: list[str] = []
        if fits:
            reasons.append(
                f"保守预估 {safe_wait:.1f} 分钟可取（点估计 {wait:.1f} 分钟），"
                f"在 {budget_minutes:.0f} 分钟课间窗口内"
            )
        else:
            reasons.append(
                f"保守预估 {safe_wait:.1f} 分钟，超出课间窗口 {safe_wait - budget_minutes:.1f} 分钟"
                + (f"（点估计 {wait:.1f} 分钟）" if wait < safe_wait else "")
            )

        # 出餐速度分（越快越高）
        speed = max(0.0, 1.0 - wait / max(budget_minutes * 2, 1.0))        # 营养分：以「有能量数据 + 蛋白占比合理」为准，纯饮品/甜品不给高分
        # 营养分：连续打分，且**无数据取中性值**
        # ⚠️ 早期版本对"不在营养表里的餐品"直接给 0.3，造成巨大悬崖——
        #    那实际上是在奖励"数据可得性"而非"营养质量"，是设计缺陷。
        nut = nutrition.get(item.name, {})
        kcal = nut.get("energyKcal", 0.0)
        protein = nut.get("protein", 0.0)
        sodium = nut.get("sodium", 0.0)
        if not kcal:
            nut_score = 0.60          # 未知 ≠ 差，取中性
        else:
            if 350 <= kcal <= 650:
                nut_score = 1.00      # 正餐合理区间
            elif 250 <= kcal < 350:
                nut_score = 0.85
            elif 650 < kcal <= 900:
                nut_score = 0.70
            elif kcal > 900:
                nut_score = 0.45      # 单份超 900 kcal，作为课间餐偏重
            else:
                nut_score = 0.60      # 热量过低，多为饮品/小食，不算正餐
            if sodium > 1000:
                nut_score -= 0.15     # 单份钠超 1000mg 偏高
            if protein >= 20:
                nut_score += 0.05     # 蛋白充足、更抗饿
            nut_score = max(0.30, min(1.00, nut_score))
        # 价格分：课间餐的价格敏感度有限，只对明显偏高的做温和惩罚。
        # （早期版本把 15–35 元以外一律给 0.6，导致套餐被不成比例地压低。）
        p = item.price
        if not p:
            price_score = 0.50
        elif p <= 30:
            price_score = 1.00
        elif p <= 45:
            price_score = 0.85
        elif p <= 60:
            price_score = 0.65
        else:
            price_score = 0.45

        score = (
            w["speed"] * speed
            + w["completeness"] * item.completeness
            + w["nutrition"] * nut_score
            + w["price"] * price_score
        )
        if not fits:
            score *= 0.35  # 来不及的强惩罚

        guard_note = ""
        if model.state.get("frozen"):
            guard_note = "校准已冻结（疑似批量点完成污染），当前使用未校准基线"

        results.append(Suggestion(item, wait, fits, score, reasons,
                                  safe_wait_minutes=safe_wait, guard_note=guard_note))

    results.sort(key=lambda s: (-s.fits, -s.score))

    picked: list[Suggestion] = []
    per_category: dict[str, int] = {}

    def _select(candidates: list[Suggestion]) -> None:
        """按得分顺序挑选，每类别有数量上限（多样性与削峰双重考虑）。"""
        for cand in candidates:
            if len(picked) >= top_k:
                return
            if not cand.fits:
                continue
            cat = cand.item.category
            if per_category.get(cat, 0) >= _cap_for(cat):
                continue
            picked.append(cand)
            per_category[cat] = per_category.get(cat, 0) + 1

    # 第一轮：只从**正餐**中挑（完整度 ≥ 0.50，即汉堡/卷/炸鸡/安格斯/组合餐）——
    # 课间推荐首先要保证"吃得饱"。门槛若放到 0.30，小食甜品（恰好 0.30）会被放行，
    # 结果是 10 分钟小课间的前两名变成麦旋风 + 薯饼，这不是一顿饭。
    _select([s for s in results if s.item.completeness >= 0.50])
    # 第二轮：正餐不足 top_k 时，才用饮品/小食补齐
    if len(picked) < top_k:
        _select(results)

    if picked:
        return picked

    feasible = [s for s in results if s.fits][:top_k]
    return feasible or results[:top_k]


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
    env = "保守包络（用于判定）" if b.get("envelope") == "safe" else "点估计（用于排序）"
    lines = [
        f"口径：{env}",
        f"预计等待：{estimate.minutes:.1f} 分钟（区间 {estimate.low:.1f}–{estimate.high:.1f}）",
        f"  基准出餐 {b['base_minutes']} 分钟",
        f"  时段系数 {b['time_factor']['value']}（{b['time_factor']['label']}）",
        f"  课间脉冲 {b['break_pulse']['value']}"
        + (
            f"（{b['break_window']['kind_text']}：{b['break_window']['label']}"
            f"，窗口 {b['break_window']['start']}–{b['break_window']['end']}"
            f"，剩余 {b['break_window']['remaining_minutes']} 分钟）"
            if b.get("break_window")
            else "（当前不在课间窗口内）"
        ),
        f"  促销系数 {b['campaign_factor']}",
        f"  门店系数 {b['store_factor']}"
        + (f"（原始值 {b['store_factor_raw']}）" if b.get("store_factor_raw") != b["store_factor"] else "")
        + f"，校准样本 {b['calibration_samples']} 条"
        + ("，**校准已冻结**" if b.get("calibration_frozen") else ""),
        f"  负载系数 {b['load_factor']['value']}（近 15 分钟 {b['load_factor']['recent_orders']} 单）",
    ]
    return "\n".join(lines)
