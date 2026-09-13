"""受限栓位规划器的最坏无解分支压力回归与穷举正确性对照。

背景：朴素回溯对"交替密集团"无解输入是指数级——n=12 即 >5s、n=24 不结束。
规划器现在带两层只裁可证无解分支的剪枝：

1. 角距结构必要条件（孤立栓 / 兼容图不连通 / 割点裂 ≥3 块 / 互斥团超过
   线性序可间隔数），首次回溯前仅在轮首运行（保住对径贪心快速路径）；
2. 失败状态记忆化：相同 (剩余栓集合, 上一栓, 工具) 在更晚时刻必然失败。

本模块：
- 24/48/96 栓的确定性无解压力回归：固定种子 20260913，2 秒硬超时保护，
  并断言诊断契约（plan_infeasible / 受阻栓位 / min_separation_deg 放宽建议）；
- 4~8 栓固定种子的穷举判定器对照（顺序 × 工具选项、实际方位、角区、
  栓位窗口、工具时段、已完成锁定前缀），可行性结论须一致，可行解逐例
  独立校验唯一占用、角距、时窗交集、换工具耗时与轮内次序；
- 结构剪枝证书的针对性单测（证书命中必须真的无解，防止误判可行性）。

性能基线（本机，seed 20260913，20 次取中位，2026-09 测量）：
  n=24  约 2.1 ms（优化前 >2 s 不返回）
  n=48  约 7.2 ms（优化前不结束）
  n=96  约 27 ms （优化前不结束）
  可行规则网格 n=96 约 87 ms（剪枝武装前直接贪心命中，行为不变）。
"""
from __future__ import annotations

import os
import random
import signal
import time
from datetime import datetime, timedelta

import pytest

from app.planning import (BoltSite, PlanInfeasible, _required_separation,
                          _structural_dead_end, circular_angle_distance,
                          default_min_separation, expand_angles, schedule_plan)

from _planner_oracle import (exhaustive_schedule, planner_result,
                             validate_planned_steps)

SHIFT = datetime(2026, 9, 13, 8, 0)
STEP_MIN = 5.0
CHANGE_MIN = 2.0
# 明确的超时保护：契约要求三种规模在 2s 内给出无解结论；测试硬限 2s。
HARD_TIMEOUT_S = float(os.environ.get("PLANNER_STRESS_TIMEOUT_S", "2.0"))
# 断言用更宽的工程余量（基线最大 28ms），只在百倍退化时失败，避免机器抖动。
ASSERT_BUDGET_S = 1.0
STRESS_SEED = 20260913


class _Timeout(Exception):
    """压力用例硬超时（规划器未在保护时间内返回结论）。"""


def _alarm(signum, frame):  # pragma: no cover - 仅在性能回归时触发
    raise _Timeout()


# ------------------------------------------------------------ 压力输入生成

def alternating_cluster_sites(n: int, seed: int = STRESS_SEED,
                              ) -> tuple[dict[int, BoltSite], float]:
    """确定性"交替密集团"无解输入。

    k = n/2+1 个栓挤在约 6° 圆弧内（两两角距 <30° 互斥），其余 m = n/2-1
    个栓散布在 90°~270° 作为间隔栓。任何线性序中互斥团需 k-1 = m+1 个
    团外栓分隔而轮内只有 m 个 ⇒ 数学上无解；对径优先贪心要到深层才暴露
    冲突，朴素回溯须枚举指数级排列才能判定。

    固定种子只给团内角方位加亚度级抖动（<0.01°），保证可重复且不改变
    两两角距 <30° 的互斥性质；角区半宽为 0，最小角间隔 30°。
    """
    rng = random.Random(seed + n)
    k = n // 2 + 1
    m = n - k
    assert k - 1 > m
    sites: dict[int, BoltSite] = {}
    span = 6.0
    for i in range(k):
        jitter = rng.uniform(-0.005, 0.005)
        sites[i + 1] = BoltSite(i + 1, i * (span / max(k - 1, 1)) + jitter,
                                [], ["T1"])
    # 间隔栓散布在 [90°, 270°]：彼此间隔 >30°（n≤96 时 m≤47，间隔≥3.9°
    # 不必然互斥），且与团首栓角距 ≥84°，仅用于"可被贪心选中后深层失败"。
    for j in range(m):
        ang = 90.0 + j * (180.0 / max(m, 1))
        sites[k + 1 + j] = BoltSite(k + 1 + j, ang, [], ["T1"])
    # 校验团的互斥性质，保证用例本身有效
    clique = list(range(1, k + 1))
    for i, a in enumerate(clique):
        for b in clique[i + 1:]:
            assert circular_angle_distance(sites[a].angle_deg,
                                           sites[b].angle_deg) < 30.0
    return sites, 30.0


def _run_alternating(n: int) -> tuple[float, PlanInfeasible]:
    sites, min_sep = alternating_cluster_sites(n)
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT_S)
    start = time.perf_counter()
    try:
        with pytest.raises(PlanInfeasible) as ei:
            schedule_plan(
                bolt_count=n, stage_ratios=[1.0], target_torque=100.0,
                sites=sites, min_separation_deg=min_sep,
                step_minutes=STEP_MIN, tool_change_minutes=CHANGE_MIN,
                shift_start=SHIFT, tool_windows={}, default_tool="T1")
    except _Timeout:  # pragma: no cover - 性能回归
        pytest.fail(f"{n} 栓交替密集团无解分支未在 {HARD_TIMEOUT_S}s 内返回结论")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return time.perf_counter() - start, ei.value


# ------------------------------------------------------------ 24/48/96 压力回归

@pytest.mark.parametrize("n", [24, 48, 96])
def test_alternating_cluster_infeasible_within_timeout(n):
    """确定性交替密集团无解输入：2s 硬超时保护内判定无解，诊断契约不变。"""
    elapsed, exc = _run_alternating(n)
    assert elapsed < ASSERT_BUDGET_S, (
        f"n={n} 用时 {elapsed*1000:.0f}ms，超出工程预算 {ASSERT_BUDGET_S*1000:.0f}ms"
        "（基线 ≤28ms；硬超时 2s）")
    detail = exc.as_detail()
    assert detail["reason"] == "plan_infeasible"
    assert detail["round_no"] == 1
    assert detail["blocked_bolts"]
    # 受阻栓位沿用角距契约
    assert all("angular_separation" in b["reasons"]
               for b in detail["blocked_bolts"])
    # 放宽建议契约：给出 min_separation_deg 建议值（≤ 团内最大对角距，
    # 即按该值至少最远的一对互斥栓不再冲突；best-effort 放宽量而非保证可行）
    rel = next(r for r in detail["relaxations"]
               if r.get("constraint") == "min_separation_deg")
    sites, min_sep = alternating_cluster_sites(n)
    clique = [k for k in range(1, n // 2 + 2)]
    max_pair = max(circular_angle_distance(sites[a].angle_deg, sites[b].angle_deg)
                   for i, a in enumerate(clique) for b in clique[i + 1:])
    assert 0 < rel["suggested_value"] <= 30.0
    # 亚度级抖动下建议值取团内实际最大角距（≤ 标称跨度 6°）
    assert rel["suggested_value"] <= max_pair + 0.01


def test_feasible_96_grid_unchanged_fast_path():
    """96 栓规则网格可行解：对径贪心直接命中，剪枝不改变序列与耗时级别。"""
    n = 96
    angles = expand_angles(n, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["T1"]) for k in range(1, n + 1)}
    start = time.perf_counter()
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT_S)
    try:
        steps, _ = schedule_plan(
            bolt_count=n, stage_ratios=[1.0], target_torque=100.0, sites=sites,
            min_separation_deg=None, step_minutes=STEP_MIN,
            tool_change_minutes=CHANGE_MIN, shift_start=SHIFT,
            tool_windows={}, default_tool="T1")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    elapsed = time.perf_counter() - start
    assert elapsed < ASSERT_BUDGET_S
    assert [s["bolt_no"] for s in steps] == [
        b for k in range(1, n // 2 + 1) for b in (k, k + n // 2)]


# ------------------------------------------------------------ 结构剪枝证书单测

def _compat(sites, min_sep, bolts):
    """角距兼容集：两栓同轮相邻不违反最小间隔/角区。"""
    return {b: {x for x in bolts if x != b
                and circular_angle_distance(sites[b].angle_deg,
                                            sites[x].angle_deg)
                >= _required_separation(min_sep, sites[b], sites[x])}
            for b in bolts}


def test_certificate_independent_set_is_truly_infeasible():
    """互斥团证书命中的情形必须真的无解（穷举对照）。"""
    n = 8
    angles = {1: 0.0, 2: 5.0, 3: 10.0, 4: 15.0, 5: 100.0,
              6: 150.0, 7: 200.0, 8: 250.0}  # 4 团 + 4 间隔：边界可行
    # 改为 5 团 + 3 间隔 => 无解
    angles[5] = 20.0
    sites = {k: BoltSite(k, a, [], ["T1"]) for k, a in angles.items()}
    cert = _structural_dead_end(list(range(1, n + 1)), None, sites, 30.0,
                                _compat(sites, 30.0, list(range(1, n + 1))))
    assert cert is not None and cert[0] == "independent_set"
    assert exhaustive_schedule(
        sites=sites, min_sep=30.0, tool_windows={}, shift_start=SHIFT) is None
    with pytest.raises(PlanInfeasible):
        schedule_plan(bolt_count=n, stage_ratios=[1.0], target_torque=100.0,
                      sites=sites, min_separation_deg=30.0,
                      step_minutes=STEP_MIN, tool_change_minutes=CHANGE_MIN,
                      shift_start=SHIFT, tool_windows={}, default_tool="T1")


def test_certificate_absent_on_feasible_cases():
    """4 栓经典反例与规则网格可行时不得误开结构死路证书。"""
    ce = {1: 50.0, 2: 200.0, 3: 310.0, 4: 320.0}
    sites = {k: BoltSite(k, a, [], ["T1"]) for k, a in ce.items()}
    assert _structural_dead_end([1, 2, 3, 4], None, sites, 60.0,
                                _compat(sites, 60.0, [1, 2, 3, 4])) is None
    steps, _ = schedule_plan(
        bolt_count=4, stage_ratios=[1.0], target_torque=100.0, sites=sites,
        min_separation_deg=60.0, step_minutes=STEP_MIN,
        tool_change_minutes=CHANGE_MIN, shift_start=SHIFT,
        tool_windows={}, default_tool="T1")
    assert [s["bolt_no"] for s in steps] == [1, 3, 2, 4]

    n = 8
    angles = expand_angles(n, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["T1"]) for k in range(1, n + 1)}
    assert _structural_dead_end(list(range(1, 9)), None, sites,
                                default_min_separation(n),
                                _compat(sites, default_min_separation(n),
                                        list(range(1, 9)))) is None


# ------------------------------------------------------------ 固定种子穷举对照

def _random_scenario(seed: int):
    """固定种子生成实际方位/角区/栓位窗口/工具时段组合（测试侧）。"""
    rng = random.Random(seed)
    n = rng.choice([4, 5, 6, 7, 8])
    tools = ["T1"] + (["T2"] if rng.random() < 0.5 else [])
    sa = 360.0 / n
    angles = {k: ((k - 1) * sa + rng.uniform(-sa * 0.3, sa * 0.3)) % 360.0
              for k in range(1, n + 1)}
    if rng.random() < 0.25:  # 小密集团：制造角距无解分支
        for b in rng.sample(range(1, n + 1), rng.randint(2, min(n, 4))):
            angles[b] = rng.uniform(0, 12.0)
    msi = rng.choice([None, 30.0, 45.0, 60.0, 90.0,
                      360.0 / n + 1e-6, 360.0 / n + 10.0])
    sites: dict[int, BoltSite] = {}
    tool_windows: dict[str, list] = {}
    for k in range(1, n + 1):
        clearance = rng.choice([0.0, 0.0, 0.0, rng.uniform(0, 10)])
        allowed = tools
        if len(tools) > 1 and rng.random() < 0.3:
            allowed = [rng.choice(tools)]
        wins = []
        r = rng.random()
        if r < 0.15:
            wins = [(SHIFT + timedelta(minutes=rng.choice([5, 15, 30])),
                     SHIFT + timedelta(hours=8))]
        elif r < 0.22:  # 起点前结束 => 顺序无关无解
            wins = [(SHIFT - timedelta(hours=3), SHIFT - timedelta(hours=2))]
        elif r < 0.32:
            o = rng.choice([0, 10, 20])
            wins = [(SHIFT + timedelta(minutes=o),
                     SHIFT + timedelta(minutes=o + 30))]
        sites[k] = BoltSite(k, angles[k], wins, allowed, clearance)
    if "T2" in tools and rng.random() < 0.5:
        tool_windows["T1"] = [
            (SHIFT + timedelta(minutes=rng.choice([0, 20, 40])),
             SHIFT + timedelta(hours=8))]
        if rng.random() < 0.3:  # 批准工具时段提前结束
            tool_windows["T1"] = [(SHIFT - timedelta(hours=2),
                                   SHIFT - timedelta(hours=1))]
    return n, sites, msi, tool_windows


# 固定种子集合：覆盖 4~8 栓、可行/无解、等待/换工具/窗错位（可重复，不随机抽样）
ORACLE_SEEDS = list(range(7001, 7041)) + list(range(8100, 8121))


@pytest.mark.parametrize("seed", ORACLE_SEEDS)
def test_planner_matches_exhaustive_oracle(seed):
    """规划器可行性结论与穷举判定器逐例一致；可行解独立校验全部硬约束。"""
    n, sites, msi, tool_windows = _random_scenario(seed)
    min_sep = msi if msi is not None else default_min_separation(n)
    oracle = exhaustive_schedule(
        sites=sites, min_sep=min_sep, tool_windows=tool_windows,
        shift_start=SHIFT, step_minutes=STEP_MIN,
        tool_change_minutes=CHANGE_MIN, default_tool="T1")
    feasible, steps = planner_result(
        bolt_count=n, sites=sites, min_separation_deg=msi,
        tool_windows=tool_windows, shift_start=SHIFT)
    assert feasible is (oracle is not None), (seed, n, feasible, oracle is not None)
    if feasible:
        validate_planned_steps(
            steps, sites=sites, min_sep=min_sep, tool_windows=tool_windows,
            shift_start=SHIFT, bolt_count=n, step_minutes=STEP_MIN,
            tool_change_minutes=CHANGE_MIN, default_tool="T1")


def test_multi_round_schedules_each_round_consistently():
    """三轮（0.3/0.6/1.0）情形：每轮独立满足全部硬约束，穷举判定器一致。"""
    # 多轮只测角距结构（全时段窗/单工具）：窗口重复使用问题不干扰结论
    n = 6
    angles = expand_angles(n, 15.0, True)
    rng = random.Random(7777)
    for k in angles:  # 轻度扰动，保持可解/难解均由穷举判定
        angles[k] = (angles[k] + rng.uniform(-8, 8)) % 360
    msi = 45.0
    sites = {k: BoltSite(k, angles[k], [], ["T1"]) for k in range(1, n + 1)}
    ratios = (0.3, 0.6, 1.0)
    oracle = exhaustive_schedule(
        sites=sites, min_sep=msi, tool_windows={},
        shift_start=SHIFT, step_minutes=STEP_MIN,
        tool_change_minutes=CHANGE_MIN, default_tool="T1",
        stage_ratios=ratios)
    feasible, steps = planner_result(
        bolt_count=n, sites=sites, min_separation_deg=msi,
        tool_windows={}, shift_start=SHIFT, stage_ratios=ratios)
    assert feasible is (oracle is not None)
    if feasible:
        validate_planned_steps(
            steps, sites=sites, min_sep=msi, tool_windows={},
            shift_start=SHIFT, bolt_count=n, stage_count=3,
            step_minutes=STEP_MIN, tool_change_minutes=CHANGE_MIN,
            default_tool="T1")
        assert [s["ratio"] for s in steps] == (
            [0.3] * n + [0.6] * n + [1.0] * n)


# ------------------------------------------------------------ 锁定前缀（修订）对照

LOCKED_SEEDS = list(range(9200, 9225))


@pytest.mark.parametrize("seed", LOCKED_SEEDS)
def test_planner_matches_oracle_with_locked_prefix(seed):
    """已完成前缀锁定：穷举对照可行性，且锁定步骤原位不被重排/改时刻。"""
    rng = random.Random(seed)
    n = rng.choice([4, 5, 6, 7, 8])
    sa = 360.0 / n
    angles = {k: ((k - 1) * sa + rng.uniform(-sa * 0.2, sa * 0.2)) % 360.0
              for k in range(1, n + 1)}
    msi = rng.choice([None, 30.0, 60.0, 360.0 / n + 1e-6])
    sites = {k: BoltSite(k, angles[k], [], ["T1"]) for k in range(1, n + 1)}
    base_feasible, base = planner_result(
        bolt_count=n, sites=sites, min_separation_deg=msi,
        tool_windows={}, shift_start=SHIFT)
    if not base_feasible:
        pytest.skip("基准即无解，另由无解用例覆盖")
    lock_count = rng.randint(1, n - 2)
    locked_steps = [dict(base[i]) for i in range(lock_count)]
    locked_tuples = tuple(
        (s["round_no"], s["bolt_no"],
         datetime.fromisoformat(s["scheduled_at"]), s["tool_id"])
        for s in locked_steps)
    # 扰动剩余栓位（后置窗口），仍保持可解或制造无解
    remaining_bolts = [s["bolt_no"] for s in base[lock_count:]]
    if rng.random() < 0.6:
        b = rng.choice(remaining_bolts)
        sites[b].windows = [(SHIFT + timedelta(minutes=rng.choice([0, 20, 60])),
                             SHIFT + timedelta(hours=8))]
    min_sep = msi if msi is not None else default_min_separation(n)
    oracle = exhaustive_schedule(
        sites=sites, min_sep=min_sep, tool_windows={}, shift_start=SHIFT,
        step_minutes=STEP_MIN, tool_change_minutes=CHANGE_MIN,
        default_tool="T1", locked_steps=locked_tuples)
    try:
        steps, _ = schedule_plan(
            bolt_count=n, stage_ratios=[1.0], target_torque=100.0, sites=sites,
            min_separation_deg=msi, step_minutes=STEP_MIN,
            tool_change_minutes=CHANGE_MIN, shift_start=SHIFT,
            tool_windows={}, default_tool="T1", locked_steps=locked_steps)
        feasible = True
    except PlanInfeasible:
        feasible = False
        steps = None
    assert feasible is (oracle is not None), seed
    if feasible:
        # 锁定步骤原位、原时刻、原工具、原轮内次序
        assert [dict(s) for s in steps[:lock_count]] == locked_steps
        validate_planned_steps(
            steps, sites=sites, min_sep=min_sep, tool_windows={},
            shift_start=SHIFT, bolt_count=n, step_minutes=STEP_MIN,
            tool_change_minutes=CHANGE_MIN, default_tool="T1")
