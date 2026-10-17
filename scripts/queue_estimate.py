"""在制订单数估算（Little's Law）

────────────────────────────────────────────────────────────────
为什么不能说「前方还有 N 人」
────────────────────────────────────────────────────────────────
麦当劳门店**不按取餐编号顺序出餐**。门店店员的公开说明是：

    「取餐编号不等于取餐顺序」——「如果有人只点一个苹果派就可以先给啊，不必按照顺序」
    （2022 年台湾麦当劳店员受访，多家媒体转载）

所以「前方排了 N 个人」在麦当劳门店里**不成立** —— 没有严格的 FIFO 队列。

────────────────────────────────────────────────────────────────
那能算什么：门店当前"在制订单数"
────────────────────────────────────────────────────────────────
利特尔法则（Little's Law）：**L = λ × W**，它只要求系统处于稳态，
**不要求先进先出**。所以：

    L = 门店当前在制订单数（约几单正在做）
    λ = 到达率（每分钟新增几单）
    W = 每单从下单到取走的平均时长（本项目模型已能算）

这个数字对学生同样有"体感"，而且它是**可验证的**——问店员"现在手上大概几单"就能核对。

────────────────────────────────────────────────────────────────
λ 从哪来（三档，按可信度递减）
────────────────────────────────────────────────────────────────
1. **取餐码实测标定（高）**：同一门店两次下单，
   `λ = (pickupCode₂ − pickupCode₁) / (t₂ − t₁)`
   取餐码是门店流水号（实测样例 35062），随总单量递增，差值即期间新增单量。
2. **门店给定峰值（中）**：经理给出「高峰期 10 分钟大约几单」，写入配置。
3. **默认值 × 时段系数（低）**：`λ = peak_rate × (K_time / 1.55)`，未标定时使用，
   **必须在界面上标注"未标定"**。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

DEFAULT_PEAK_RATE_PER_MIN = 1.2   # 单/分钟，参考 QSR 排队研究量级，未标定时使用
DEFAULT_PEAK_TIME_FACTOR = 1.55   # 与 break_window.TIME_FACTORS 的午高峰一致


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# ── 取餐码标定 ────────────────────────────────────────────────
def record_pickup(model, store: str, code: str, at: datetime) -> dict[str, Any]:
    """记录一次取餐码采样。返回当前该店的标定状态。"""
    try:
        num = int(str(code).strip())
    except (TypeError, ValueError):
        return {"recorded": False, "reason": "取餐码不是纯数字，无法用于标定"}

    samples = model.state.setdefault("pickup_samples", {})
    bucket = samples.setdefault(store, [])
    bucket.append({"code": num, "at": at.isoformat()})
    # 只保留最近 20 条
    samples[store] = bucket[-20:]
    model.save()
    lam = pickup_lambda(model, store)
    return {
        "recorded": True,
        "samples": len(samples[store]),
        "lambda_per_min": round(lam, 3) if lam else None,
    }


def pickup_lambda(model, store: str) -> float | None:
    """用取餐码差值估到达率。样本 <2 或数据异常时返回 None。"""
    bucket = (model.state.get("pickup_samples") or {}).get(store) or []
    if len(bucket) < 2:
        return None
    first, last = bucket[0], bucket[-1]
    t0, t1 = _parse(first.get("at", "")), _parse(last.get("at", ""))
    if not t0 or not t1:
        return None
    dt_min = (t1 - t0).total_seconds() / 60.0
    d_code = last.get("code", 0) - first.get("code", 0)
    if dt_min <= 0 or d_code <= 0:
        return None
    # 合理性护栏：单店每分钟超过 10 单或低于 0.02 单，视为异常（可能是换日/跨店）
    lam = d_code / dt_min
    if not (0.02 <= lam <= 10.0):
        return None
    return lam


# ── 到达率（三档） ────────────────────────────────────────────
def arrival_rate(model, store: str, at: datetime) -> dict[str, Any]:
    lam = pickup_lambda(model, store)
    if lam:
        return {
            "lambda_per_min": round(lam, 3),
            "basis": "取餐码实测标定",
            "confidence": "high",
            "note": f"由本店取餐码差值推算（{len((model.state.get('pickup_samples') or {}).get(store) or [])} 次采样）",
        }

    q = (model.schedule or {}).get("queue") or {}
    peak = float(q.get("peak_rate_per_min", DEFAULT_PEAK_RATE_PER_MIN))
    peak_k = float(q.get("peak_time_factor", DEFAULT_PEAK_TIME_FACTOR))
    k_time, label = model.time_factor(at)
    lam = peak * (k_time / peak_k) if peak_k > 0 else peak
    # 只有配置里明确声明来源为门店提供时，才算"中置信度"；
    # 默认值一律按"未标定"处理，避免把内置常量伪装成门店数据。
    src = str(q.get("rate_source", "default")).lower()
    if src == "manager":
        basis, conf = "门店给定峰值 × 时段系数", "medium"
    else:
        basis, conf = "默认峰值 × 时段系数（未标定）", "low"
    return {
        "lambda_per_min": round(lam, 3),
        "basis": basis,
        "confidence": conf,
        "note": f"{label} K_time={k_time}，峰值 {peak} 单/分"
                + ("（门店提供）" if src == "manager" else "（内置默认值，未经本店标定）"),
    }


def estimate_queue(model, store: str, at: datetime, wait_minutes: float) -> dict[str, Any]:
    """估算门店当前在制订单数 L = λ × W。"""
    a = arrival_rate(model, store, at)
    lam = a["lambda_per_min"]
    in_system = max(1, round(lam * wait_minutes))
    return {
        "in_system": in_system,
        "lambda_per_min": lam,
        "wait_minutes": round(wait_minutes, 1),
        "basis": a["basis"],
        "confidence": a["confidence"],
        "note": a["note"],
        "formula": "L = λ × W",
        "caveat": "麦当劳不按取餐号顺序出餐，因此这是「门店当前约有几单在做」，不是「你前面排了几个人」",
    }
