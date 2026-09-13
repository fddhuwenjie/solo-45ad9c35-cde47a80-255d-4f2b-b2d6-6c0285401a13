"""测试侧小规模穷举判定器（仅测试用，不参与服务代码）。

与 app.planning.schedule_plan 逐例对照：枚举 4~8 栓的全部轮内顺序与每栓工具
选择，完全镜像规划器语义——每栓每轮恰好一次、连续角距、栓位窗 × 工具窗
交集、非当前工具加换工具耗时、已完成锁定前缀原位固定。判定方式是逐位置
深度优先：在每个位置枚举 (栓, 工具) 并用角距约束剪枝，与"枚举全部排列再
逐对校验角距"等价（剪枝只移除存在相邻冲突对的部分排列），但对 8 栓也能
在毫秒级完成，可支撑成百上千个固定种子对照用例。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.planning import (BoltSite, PlanInfeasible, _required_separation,
                          circular_angle_distance, earliest_start,
                          default_min_separation, schedule_plan)


def _tool_starts(site: BoltSite, tool_windows: dict, t: datetime,
                 cur_tool: str, step_minutes: float,
                 change_minutes: float) -> list[tuple[datetime, str]]:
    """该栓在 t 之后每个允许工具的最早可行（开始时刻, 工具）。"""
    change = timedelta(minutes=change_minutes)
    out: list[tuple[datetime, str]] = []
    for tl in site.allowed_tools:
        start_t = t if tl == cur_tool else t + change
        s = earliest_start(site.windows, tool_windows.get(tl) or [],
                           start_t, step_minutes)
        if s is not None:
            out.append((s, tl))
    return out


def exhaustive_schedule(
    *,
    sites: dict[int, BoltSite],
    min_sep: float,
    tool_windows: dict[str, list],
    shift_start: datetime,
    step_minutes: float = 5.0,
    tool_change_minutes: float = 2.0,
    default_tool: str = "T1",
    stage_ratios: tuple[float, ...] = (1.0,),
    locked_steps: tuple = (),
):
    """穷举可行解。返回解列表 [(round, bolt, start_dt, tool), ...]；无解返回 None。

    locked_steps 元素为 (round_no, bolt_no, scheduled_dt, tool_id)，
    对应已完成步骤：轮内次序与时刻原位锁定，只枚举其后未完成栓。
    """
    bolt_nos = sorted(sites)
    step = timedelta(minutes=step_minutes)
    # locked_steps 按已完成（计划前缀）次序传入，必须保持该次序，不能按栓号排序
    locked = [(r, b, dt, tl) for r, b, dt, tl in locked_steps]
    locked_keys = {(r, b): (dt, tl) for r, b, dt, tl in locked}

    def search_round(round_no, remaining, prev_bolt, t, cur_tool, sol):
        if not remaining:
            if round_no < len(stage_ratios):
                # 每轮所有（非锁定）栓恰好出现一次：下一轮从全集重新开始
                # （锁定只约束它所属的轮次；多数用例仅第 1 轮有锁定前缀）
                r_locked = [x for x in locked if x[0] == round_no + 1]
                r_keys = {(r, b) for r, b, _, _ in r_locked}
                rem = [b for b in bolt_nos if (round_no + 1, b) not in r_keys]
                tt, ttool, psol = t, cur_tool, list(sol)
                pb = None
                for r, b, dt, tl in r_locked:
                    psol.append((r, b, dt, tl))
                    tt, ttool, pb = dt + step, tl, b
                return search_round(round_no + 1, rem, pb, tt, ttool, psol)
            return list(sol)
        for b in remaining:
            if prev_bolt is not None and circular_angle_distance(
                    sites[prev_bolt].angle_deg, sites[b].angle_deg
            ) < _required_separation(min_sep, sites[prev_bolt], sites[b]):
                continue
            for s, tl in _tool_starts(sites[b], tool_windows, t, cur_tool,
                                      step_minutes, tool_change_minutes):
                found = search_round(
                    round_no, [x for x in remaining if x != b], b,
                    s + step, tl, sol + [(round_no, b, s, tl)])
                if found is not None:
                    return found
        return None

    r1_locked = [x for x in locked if x[0] == 1]
    # 第 1 轮锁定步骤同样推进重排起点（时刻、工具、末栓），保持执行次序
    t0, tool0 = shift_start, default_tool
    sol0: list[tuple] = []
    pb0 = None
    for r, b, dt, tl in r1_locked:
        sol0.append((r, b, dt, tl))
        t0, tool0, pb0 = max(t0, dt + step), tl, b
    rem1 = [b for b in bolt_nos if (1, b) not in locked_keys]
    return search_round(1, rem1, pb0, t0, tool0, sol0)


def planner_result(*, bolt_count, sites, min_separation_deg, tool_windows,
                   shift_start, stage_ratios=(1.0,), locked_steps=(),
                   step_minutes=5.0, tool_change_minutes=2.0,
                   default_tool="T1"):
    """调用规划器，返回 (feasible, steps or None)。"""
    try:
        steps, _actions = schedule_plan(
            bolt_count=bolt_count, stage_ratios=list(stage_ratios),
            target_torque=100.0, sites=sites,
            min_separation_deg=min_separation_deg,
            step_minutes=step_minutes,
            tool_change_minutes=tool_change_minutes,
            shift_start=shift_start, tool_windows=tool_windows,
            default_tool=default_tool, locked_steps=locked_steps)
    except PlanInfeasible:
        return False, None
    return True, steps


def validate_planned_steps(steps, *, sites, min_sep, tool_windows, shift_start,
                           bolt_count, stage_count=1, step_minutes=5.0,
                           tool_change_minutes=2.0, default_tool="T1"):
    """独立校验规划器产出的步骤全部满足硬约束（不依赖规划器自身代码路径）。"""
    step = timedelta(minutes=step_minutes)
    change = timedelta(minutes=tool_change_minutes)
    assert len(steps) == bolt_count * stage_count
    for rnd in range(1, stage_count + 1):
        rs = [s for s in steps if s["round_no"] == rnd]
        # 唯一占用：每轮每栓恰好一次，轮内次序连续
        assert len(rs) == bolt_count
        assert sorted(s["bolt_no"] for s in rs) == list(range(1, bolt_count + 1))
        assert [s["order_in_round"] for s in rs] == list(range(1, bolt_count + 1))
        for a, b in zip(rs, rs[1:]):
            dist = circular_angle_distance(a["angle_deg"], b["angle_deg"])
            need = _required_separation(min_sep, sites[a["bolt_no"]],
                                        sites[b["bolt_no"]])
            assert dist >= need - 1e-9, (rnd, a["bolt_no"], b["bolt_no"], dist)

    def in_any(wins, lo, hi):
        if not wins:
            return True
        return any(w0 <= lo and hi <= w1 for w0, w1 in wins)

    cur_tool = default_tool
    prev_end = shift_start
    for s in steps:
        site = sites[s["bolt_no"]]
        st = datetime.fromisoformat(s["scheduled_at"])
        if s["tool_id"] != cur_tool:  # 换工具耗时计入
            assert st >= prev_end + change - timedelta(seconds=1)
        else:
            assert st >= prev_end - timedelta(seconds=1)
        assert s["tool_id"] in site.allowed_tools
        assert in_any(site.windows, st, st + step)                  # 栓位时窗
        assert in_any(tool_windows.get(s["tool_id"]) or [], st,
                      st + step)                                    # 工具时段
        cur_tool = s["tool_id"]
        prev_end = st + step
