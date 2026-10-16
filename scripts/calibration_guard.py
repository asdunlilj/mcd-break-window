"""校准数据完整性守卫（calibration guard）

────────────────────────────────────────────────────────────────────
为什么需要这个模块
────────────────────────────────────────────────────────────────────
本项目的自校准依赖 query-order 的 orderStatus 变化来测量真实等待时长：

    orderStatus: 1 待支付 / 2 配餐中 / 3 配送中 / 4 已完成 / 5 已取消
                 / 6 已评价 / 10 餐厅确认配餐中

但 orderStatus **是门店员工手动操作的状态机，不是传感器**。

已知现实问题（门店经理反馈）：
    门店在压单时会先把单子全部点"完成"以清空操作屏，
    因此 orderStatus=4 的时刻可能早于餐品真实交付时刻。

这不是随机噪声，而是**系统性偏差**：
    1. 偏差方向单一 —— 只会让"实测等待"偏短，模型因此变得过于乐观
    2. 偏差与负载正相关 —— 压单越严重，提前点完成的幅度越大

第 2 点最关键：它使误差与自变量（高峰负载）相关，属于**内生性污染**。
后果是模型会在"高峰"这一最需要准确的场景下给出最乐观的预估，
从而把学生推向迟到——恰好击穿本项目的核心价值。

────────────────────────────────────────────────────────────────────
五道防线（纵深防御，逐层可审计）
────────────────────────────────────────────────────────────────────
R1 物理下界   —— 低于物理不可能时长的样本直接丢弃
R2 状态跳变   —— 首次观测即"已完成"且间隔过短的样本丢弃
R3 批量清屏   —— 同门店短窗口内样本聚集时判定为清屏，整批丢弃
R4 负载单调性 —— 实测等待与负载显著负相关时，冻结自动校准
R5 保守单向   —— 判定用包络永不允许比未校准基线更乐观

R1-R4 处理"训练数据是否可信"，R5 处理"即使数据可信也不能更乐观"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------- 阈值常量

# R1：任何热食餐品的最短可交付时间不可能低于 0.7 分钟
MIN_ABS_MINUTES = 0.7
# R1：相对物理下界 —— 实测不得低于基准出餐时长的这个比例
PHYSICAL_FLOOR_RATIO = 0.55

# R2：首观测即"已完成"时，允许的最短间隔
MIN_FIRST_OBSERVE_SECONDS = 90

# R3：批量清屏判定窗口与最少聚集样本数
BATCH_WINDOW_SECONDS = 90
BATCH_MIN_SAMPLES = 3

# R4：负载单调性检验的样本下限与相关系数阈值
LOAD_MONOTONIC_MIN_SAMPLES = 8
LOAD_MONOTONIC_RHO = -0.30

# R5：门店系数硬钳制区间（下限 <1 允许乐观，但仅用于排序，见 estimate(safe=)）
STORE_FACTOR_LO = 0.85
STORE_FACTOR_HI = 2.50

# 校准样本来源权重：用户确认 > 状态机
SOURCE_TRUST = {"user": 1.0, "status": 0.4, "status_full": 0.0}


@dataclass
class Verdict:
    """单条样本的准入判定结果。"""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    trust: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "trust": round(self.trust, 2), "reasons": self.reasons}


# ---------------------------------------------------------------- R1 物理下界

def physical_floor(base_minutes: float) -> float:
    """给定基准出餐时长，返回物理上不可能低于的下界（分钟）。"""
    return max(MIN_ABS_MINUTES, PHYSICAL_FLOOR_RATIO * max(base_minutes, 0.0))


# ---------------------------------------------------------------- R3 批量清屏

def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def detect_batch_clear(
    samples: Sequence[dict[str, Any]],
    store: str,
    at: datetime,
    window_seconds: int = BATCH_WINDOW_SECONDS,
    min_samples: int = BATCH_MIN_SAMPLES,
) -> list[str]:
    """同一门店在短窗口内出现多条校准样本 → 判定为批量清屏。

    原理：真实交付是逐单发生的，人手不可能在 90 秒内真实完成 3 单以上并逐一交付；
    出现聚集说明是操作屏批量点击所致。
    """
    hits: list[str] = []
    lo = at - timedelta(seconds=window_seconds)
    for sample in samples:
        if sample.get("store") != store:
            continue
        ts = _parse_ts(sample.get("at", ""))
        if ts is None:
            continue
        if ts >= lo:
            hits.append(sample.get("at", ""))
    if len(hits) >= min_samples:
        return [
            f"R3 批量清屏：该门店在 {window_seconds} 秒内出现 {len(hits)} 条校准样本，"
            f"判定为操作屏批量点击，整批丢弃"
        ]
    return []


# ---------------------------------------------------------------- R4 负载单调性

def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman 秩相关（无第三方依赖）。"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def check_load_monotonicity(
    samples: Sequence[dict[str, Any]],
    store: str,
    min_samples: int = LOAD_MONOTONIC_MIN_SAMPLES,
    rho_threshold: float = LOAD_MONOTONIC_RHO,
) -> tuple[bool, float | None, str]:
    """检验「实测等待」是否随「负载」单调不减。

    理论上，负载越高真实等待必然越长。若实测呈现显著负相关，
    说明实测值被外部因素（批量点完成）系统性压短，数据不可用于校准。
    """
    pairs = [
        (float(s.get("load", 0)), float(s.get("actual", 0)))
        for s in samples
        if s.get("store") == store and s.get("source") != "status_full"
    ]
    if len(pairs) < min_samples:
        return True, None, f"负载单调性：样本 {len(pairs)} 条，未达检验下限 {min_samples}，跳过"
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rho = _spearman(xs, ys)
    if rho is None:
        return True, None, "负载单调性：数据无秩变化，跳过"
    if rho < rho_threshold:
        return False, rho, (
            f"R4 负载单调性违背：实测等待与负载的秩相关 ρ={rho:.2f}（阈值 {rho_threshold}），"
            f"说明实测被系统性压短，**冻结自动校准**"
        )
    return True, rho, f"负载单调性正常：ρ={rho:.2f}"


# ---------------------------------------------------------------- 综合判定

def audit_sample(
    sample: dict[str, Any],
    base_minutes: float,
    recent_samples: Sequence[dict[str, Any]],
) -> Verdict:
    """对单条校准样本执行 R1/R2/R3 准入判定。"""
    reasons: list[str] = []
    actual = float(sample.get("actual", 0.0))
    source = str(sample.get("source", "status"))

    # R1 物理下界
    floor = physical_floor(base_minutes)
    if actual < floor:
        reasons.append(
            f"R1 物理下界：实测 {actual:.2f} 分钟 < 下界 {floor:.2f} 分钟"
            f"（基准 {base_minutes:.2f} × {PHYSICAL_FLOOR_RATIO}），物理不可能，丢弃"
        )
        return Verdict(False, reasons, 0.0)

    # R2 状态跳变
    if source != "user" and float(sample.get("first_observed_seconds", 999)) < MIN_FIRST_OBSERVE_SECONDS:
        reasons.append(
            f"R2 状态跳变：首次观测即已完成且间隔 "
            f"{sample.get('first_observed_seconds')}s < {MIN_FIRST_OBSERVE_SECONDS}s，丢弃"
        )
        return Verdict(False, reasons, 0.0)

    # R3 批量清屏
    at = _parse_ts(sample.get("at", ""))
    if at is not None:
        batch = detect_batch_clear(recent_samples, str(sample.get("store", "-")), at)
        if batch:
            reasons.extend(batch)
            return Verdict(False, reasons, 0.0)

    trust = SOURCE_TRUST.get(source, 0.4)
    reasons.append(
        f"准入通过：实测 {actual:.2f} 分钟 ≥ 物理下界 {floor:.2f} 分钟，"
        f"来源={source}（信任权重 {trust}）"
    )
    return Verdict(True, reasons, trust)


# ---------------------------------------------------------------- R5 钳制

def clamp_store_factor(value: float) -> tuple[float, str | None]:
    if value < STORE_FACTOR_LO:
        return STORE_FACTOR_LO, f"门店系数 {value:.3f} 低于下限 {STORE_FACTOR_LO}，已钳制"
    if value > STORE_FACTOR_HI:
        return STORE_FACTOR_HI, f"门店系数 {value:.3f} 高于上限 {STORE_FACTOR_HI}，已钳制"
    return value, None
