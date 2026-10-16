"""校准完整性守卫的可复现验证（无需 pytest，直接运行）

    python tests/test_guard.py

本脚本用**具体数字**演示「门店压单时批量点完成」这一污染场景下，
五道防线各自如何生效，以及模型最终会不会因此变得乐观。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from break_window import PrepModel  # noqa: E402
from calibration_guard import (  # noqa: E402
    audit_sample,
    check_load_monotonicity,
    clamp_store_factor,
    detect_batch_clear,
    physical_floor,
)

PASS, FAIL = "  [PASS]", "  [FAIL]"
_failures = 0


def check(condition: bool, label: str) -> None:
    global _failures
    print((PASS if condition else FAIL) + " " + label)
    if not condition:
        _failures += 1


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mcd-guard-")

    # ────────────────────────────────────────────────────────────────
    section("R1 物理下界：不可能这么快的样本必须丢弃")

    base = 3.2  # 麦辣鸡腿汉堡的基准出餐时长
    floor = physical_floor(base)
    print(f"基准出餐 {base} 分钟 → 物理下界 {floor:.2f} 分钟")
    print(f"（任何厨房都不可能在此时间内完成现制汉堡）")

    verdict = audit_sample(
        {"store": "T", "at": datetime.now().isoformat(), "actual": 0.5,
         "base_minutes": base, "source": "status", "first_observed_seconds": 999},
        base, [],
    )
    print(f"样本 actual=0.50 分钟 → accepted={verdict.accepted}")
    for reason in verdict.reasons:
        print(f"    {reason}")
    check(not verdict.accepted, "0.50 分钟的样本被 R1 拒绝")

    verdict_ok = audit_sample(
        {"store": "T", "at": datetime.now().isoformat(), "actual": 4.5,
         "base_minutes": base, "source": "status", "first_observed_seconds": 999},
        base, [],
    )
    check(verdict_ok.accepted, "4.50 分钟的合理样本被接纳")

    # ────────────────────────────────────────────────────────────────
    section("R2 状态跳变：首次观测即已完成且间隔过短，可疑")

    verdict = audit_sample(
        {"store": "T", "at": datetime.now().isoformat(), "actual": 3.0,
         "base_minutes": base, "source": "status", "first_observed_seconds": 30},
        base, [],
    )
    print(f"样本 first_observed_seconds=30 → accepted={verdict.accepted}")
    for reason in verdict.reasons:
        print(f"    {reason}")
    check(not verdict.accepted, "首观测 30 秒即完成的样本被 R2 拒绝")

    verdict_user = audit_sample(
        {"store": "T", "at": datetime.now().isoformat(), "actual": 3.0,
         "base_minutes": base, "source": "user", "first_observed_seconds": 30},
        base, [],
    )
    check(verdict_user.accepted, "用户确认口径不受 R2 限制（真值优先）")

    # ────────────────────────────────────────────────────────────────
    section("R3 批量清屏：同门店短窗口内样本聚集 = 操作屏批量点击")

    now = datetime.now()
    from datetime import timedelta

    clustered = [
        {"store": "T", "at": (now - timedelta(seconds=s)).isoformat()}
        for s in (10, 40, 70)
    ]
    notes = detect_batch_clear(clustered, "T", now)
    print(f"90 秒内出现 3 条样本 → 判定结果：{notes or '未触发'}")
    check(bool(notes), "3 条聚集样本触发 R3")

    sparse = [
        {"store": "T", "at": (now - timedelta(seconds=s)).isoformat()}
        for s in (10, 300, 900)
    ]
    check(not detect_batch_clear(sparse, "T", now), "分散样本不触发 R3（避免误杀）")

    # ────────────────────────────────────────────────────────────────
    section("R4 负载单调性：实测等待随负载下降 → 数据被压短，冻结校准")

    # 真实世界里负载越高等待越长；这里故意构造反向数据模拟污染
    poisoned = [
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 1, "actual": 8.0},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 3, "actual": 6.5},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 5, "actual": 5.0},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 7, "actual": 4.0},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 9, "actual": 3.0},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 11, "actual": 2.5},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 13, "actual": 2.0},
        {"store": "T", "at": now.isoformat(), "source": "status", "load": 15, "actual": 1.8},
    ]
    ok, rho, message = check_load_monotonicity(poisoned, "T")
    print(f"ρ = {rho}，通过 = {ok}")
    print(f"    {message}")
    check(not ok, "负载-等待负相关被 R4 识别并冻结")

    healthy = [
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 1, "actual": 2.0},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 3, "actual": 3.2},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 5, "actual": 4.1},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 7, "actual": 5.6},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 9, "actual": 6.8},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 11, "actual": 7.9},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 13, "actual": 9.2},
        {"store": "T", "at": now.isoformat(), "source": "user", "load": 15, "actual": 10.5},
    ]
    ok2, rho2, _ = check_load_monotonicity(healthy, "T")
    check(ok2, f"健康的正相关数据通过检验（ρ={rho2:.2f}）")

    # ────────────────────────────────────────────────────────────────
    section("R5 保守单向：即使校准把门店系数压低，判定口径也不会变乐观")

    model = PrepModel(state_path=os.path.join(tmp, "state.json"))
    from break_window import Item

    item = Item(code="1440", name="麦辣鸡腿汉堡", price=22.5)

    # 人为把门店系数压到 0.6（模拟被批量点完成污染后的结果）
    model.state["store_factor"]["T"] = 0.60

    point = model.estimate([item], at=datetime.now(), store_code="T")
    safe = model.estimate([item], at=datetime.now(), store_code="T", safe=True)

    print(f"门店系数原始值  : {point.breakdown['store_factor_raw']}")
    print(f"点估计口径 K_store: {point.breakdown['store_factor']} → {point.minutes:.2f} 分钟（用于排序）")
    print(f"判定口径 K_store: {safe.breakdown['store_factor']} → {safe.minutes:.2f} 分钟（用于判定）")
    check(safe.minutes > point.minutes,
          "保守包络严格大于点估计（污染无法让判定变乐观）")

    clamped, note = clamp_store_factor(0.30)
    print(f"钳制 0.30 → {clamped}（{note}）")
    check(clamped >= 0.85, "R5 硬钳制下限生效")

    # ────────────────────────────────────────────────────────────────
    section("端到端：模拟门店压单 → 观察模型是否仍然保守")

    model2 = PrepModel(state_path=os.path.join(tmp, "state2.json"))
    print("场景：真实等待 8 分钟，但门店在高峰把订单提前点成完成，状态机给出 3 分钟。\n")

    # 状态机口径的污染样本（连续多条，且都远低于物理下界或集中出现）
    for i, actual in enumerate([3.0, 2.8, 3.2]):
        result = model2.calibrate(
            "T", predicted=6.0, actual=actual, base_minutes=3.2,
            source="status", load=12, first_observed_seconds=40,
        )
        print(f"  污染样本 #{i + 1} actual={actual} → accepted={result['accepted']}"
              f" | reasons={result['guard']['reasons'][0][:46]}…")

    # 用户确认口径的真值
    result = model2.calibrate(
        "T", predicted=6.0, actual=8.4, base_minutes=3.2,
        source="user", load=12, first_observed_seconds=999,
    )
    print(f"\n  用户确认真值 actual=8.4 → accepted={result['accepted']}")
    print(f"  更新后门店系数 = {result.get('store_factor')}")

    report = model2.integrity_report()
    print(f"\n  完整性报告：采纳 {report['samples_accepted']} 条，"
          f"丢弃 {report['samples_rejected']} 条，冻结 {report['frozen']}")

    safe2 = model2.estimate([item], at=datetime.now(), store_code="T", safe=True)
    check(safe2.breakdown["store_factor"] >= 1.0,
          "污染样本未把判定口径压到 1.0 以下")

    # ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    if _failures == 0:
        print("全部断言通过：五道防线均按预期生效。")
    else:
        print(f"有 {_failures} 项断言失败，需要检查。")
    print("=" * 72)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
