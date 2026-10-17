"""麦当劳官方时间标准（可核查出处）

本模块只收录**官方公开发布**的时间标准，不含任何推测值。
每一条都标注了来源与可靠性，便于审计。

用途：
1. 赏味倒计时 —— 判断餐品是否已过官方最佳赏味时限
2. 出餐时长的**品类级锚点** —— 校准 T_base，而不是凭空编造

⚠️ 重要边界：官方**只发布到品类级**（薯条、汉堡），
   没有 35 个餐品各自的出品时长表。未覆盖的品类一律返回 None，
   调用方必须显示"未知"，**不得填一个看起来合理的数字**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimeStandard:
    """单条官方时间标准。"""

    key: str
    label: str
    minutes: float | None          # 单一值（分钟）
    minutes_range: tuple[float, float] | None  # 区间（分钟）
    category: str                  # holding（赏味/持有） / making（制作） / e2e（端到端）
    source: str
    reliability: str               # 高 / 中

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label,
            "minutes": self.minutes,
            "minutes_range": list(self.minutes_range) if self.minutes_range else None,
            "category": self.category,
            "source": self.source, "reliability": self.reliability,
        }


# ── 官方标准表 ────────────────────────────────────────────────
STANDARDS: list[TimeStandard] = [
    TimeStandard(
        key="fries_holding", label="薯条起锅后最佳赏味", minutes=7.0,
        minutes_range=None, category="holding",
        source="麦当劳中国官网「因为热爱，尽善而行」/ 麦当劳台湾官网 / 麦当劳塞浦路斯官网 FAQ（超时丢弃不售）",
        reliability="高",
    ),
    TimeStandard(
        key="burger_holding", label="汉堡制作后可售时限", minutes=25.0,
        minutes_range=(20.0, 30.0), category="holding",
        source="法治日报引述麦当劳 QSC&V 标准（超期丢弃）",
        reliability="中",
    ),
    TimeStandard(
        key="burger_making", label="汉堡点餐后现制", minutes=1.0,
        minutes_range=None, category="making",
        source="麦当劳中国官网新闻「所有汉堡均在消费者点餐之后 60 秒内新鲜制作」",
        reliability="高",
    ),
    TimeStandard(
        key="beef_patty", label="牛肉饼双面煎炉煎制", minutes=37 / 60,
        minutes_range=None, category="making",
        source="麦当劳中国官网新闻「烹饪时间严格控制在 37 秒以上」",
        reliability="高",
    ),
    TimeStandard(
        key="e2e_cn", label="中国端到端（点餐到取餐）", minutes=2.5,
        minutes_range=None, category="e2e",
        source="麦当劳中国 CEO 专访「中国不到 150 秒」（全球标准 210 秒）",
        reliability="中",
    ),
]

_BY_KEY = {s.key: s for s in STANDARDS}


def get(key: str) -> TimeStandard | None:
    return _BY_KEY.get(key)


# ── 餐品名 → 赏味时限映射 ─────────────────────────────────────
# 只映射官方明确发布的两类；其余一律不映射（返回 None）
HOLDING_RULES: list[tuple[tuple[str, ...], str]] = [
    (("薯条", "薯饼", "脆薯"), "fries_holding"),
    (("堡", "麦满分", "吉士汉堡", "巨无霸", "麦香鱼", "板烧", "安格斯", "不素之霸",
      "双笋卷", "卷"), "burger_holding"),
]


def holding_key(item_name: str) -> str | None:
    """返回该餐品适用的官方赏味标准 key；官方未覆盖则返回 None。"""
    for keywords, key in HOLDING_RULES:
        if any(k in item_name for k in keywords):
            return key
    return None


def holding_limit_minutes(item_name: str) -> float | None:
    """返回该餐品的官方赏味上限（分钟）。未覆盖返回 None，调用方须显示"未知"。"""
    key = holding_key(item_name)
    if not key:
        return None
    std = _BY_KEY.get(key)
    if not std:
        return None
    if std.minutes_range:
        return std.minutes_range[1]  # 取上限（最宽松）作为告警阈值，避免误报
    return std.minutes


def shelf_life_status(elapsed_minutes: float, item_names: list[str]) -> list[dict[str, Any]]:
    """按官方赏味标准评估已放置时长的风险。

    返回每个餐品的告警项；未覆盖的品类 status 为 unknown，**不给出任何数字**。
    """
    out: list[dict[str, Any]] = []
    for name in item_names:
        limit = holding_limit_minutes(name)
        key = holding_key(name)
        if limit is None:
            out.append({
                "item": name, "status": "unknown", "limit_minutes": None,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "message": "官方未发布该品类的赏味时限，无法判断",
                "source": "",
            })
            continue
        ratio = elapsed_minutes / limit if limit else 0
        if ratio >= 1.0:
            status, msg = "expired", f"已超过官方赏味上限（{limit:.0f} 分钟）"
        elif ratio >= 0.8:
            status, msg = "warning", f"接近官方赏味上限（{limit:.0f} 分钟），建议尽快取餐"
        else:
            status, msg = "ok", f"在官方赏味时限内（上限 {limit:.0f} 分钟）"
        std = _BY_KEY.get(key or "")
        out.append({
            "item": name, "status": status, "limit_minutes": limit,
            "elapsed_minutes": round(elapsed_minutes, 1),
            "message": msg,
            "source": std.source if std else "",
        })
    return out


def standards_table() -> list[dict[str, Any]]:
    return [s.as_dict() for s in STANDARDS]
