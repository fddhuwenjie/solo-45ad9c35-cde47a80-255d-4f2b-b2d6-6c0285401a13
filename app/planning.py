"""受限栓位施工规划：实际方位、时间窗、允许工具与角区约束下的分轮排序（纯函数）。

现场脚手架、管托或扳手反力臂会挡住部分栓位，等分圆周的固定交叉序列可能排出
当班无法执行的步骤；临时跳栓又会破坏分轮受力。本模块在分轮递增框架内为每一轮
排出可执行顺序：

- 每轮所有（非补拧锁定）螺栓恰好出现一次——不得用跳过螺栓伪造可行方案；
- 同轮连续两步的圆周角距 ≥ max(最小角间隔, 两栓套筒/反力臂角区半宽之和)；
- 轮内顺序用带回溯的深度优先搜索确定：对径优先（与上一栓最接近 180°）
  只是选序偏好，贪心走不通时回溯尝试其他候选，全部候选顺序
  （满足角距、角区、时间窗、工具时段与已完成前缀）都失败才判定无解；
- 紧固时刻须落在栓位可操作时间窗与所用工具可用时段的交集内；
  当前时刻无可行栓时生成等待动作，备用工具更早可用时生成换工具动作；
- 修订时已完成步骤原位锁定（时刻、工具、轮内次序不变），只重排未完成步骤。

无解时抛出 PlanInfeasible，携带首个冲突轮次、受阻栓位与最少需解除的限制；
与顺序无关的时间窗/工具失效（排程起点前已全部结束）优先于搜索死胡同诊断。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

_EPS = timedelta(seconds=1)  # 时刻比较容差（吸收秒级舍入）


@dataclass
class BoltSite:
    """单个栓位的现场约束（默认值已展开）。"""

    bolt_no: int
    angle_deg: float                         # 实际方位（0=正上方，顺时针为正）
    windows: list[tuple[datetime, datetime]]  # 可操作时间窗；空 = 全时段可操作
    allowed_tools: list[str]                  # 允许工具（已展开，默认 = [批准工具]）
    clearance_deg: float = 0.0                # 套筒/反力臂所需角区半宽（自栓位向两侧）


class PlanInfeasible(Exception):
    """规划无解：首个冲突轮次、受阻栓位与最少需解除的限制。"""

    def __init__(self, round_no: int, blocked_bolts: list[dict],
                 relaxations: list[dict], message: str):
        super().__init__(message)
        self.round_no = round_no
        self.blocked_bolts = blocked_bolts
        self.relaxations = relaxations
        self.message = message

    def as_detail(self) -> dict:
        return {
            "reason": "plan_infeasible",
            "message": self.message,
            "round_no": self.round_no,
            "blocked_bolts": self.blocked_bolts,
            "relaxations": self.relaxations,
        }


def expand_angles(n: int, start_angle_deg: float, clockwise: bool,
                  overrides: dict[int, float | None] | None = None) -> dict[int, float]:
    """每栓实际方位：登记值优先，缺省按规则圆周展开（与圆周 SVG 同一公式）。"""
    step = 360.0 / n
    angles: dict[int, float] = {}
    for k in range(1, n + 1):
        base = start_angle_deg + (k - 1) * step
        angles[k] = (base if clockwise else -base) % 360.0
    for bolt_no, ang in (overrides or {}).items():
        if ang is not None:
            angles[bolt_no] = ang % 360.0
    return angles


def circular_angle_distance(a: float, b: float) -> float:
    """圆周最短角距（度，0~180）。"""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def default_min_separation(n: int) -> float:
    """缺省最小角间隔：复现"同轮连续步骤不得圆周相邻"（略大于一格）。"""
    return 360.0 / n + 1e-6


def earliest_start(bolt_windows: list[tuple[datetime, datetime]],
                   tool_windows: list[tuple[datetime, datetime]],
                   t: datetime, step_minutes: float) -> datetime | None:
    """[s, s+step] 同时落入某栓位窗与某工具窗的最早 s >= t；无窗一侧视为不限。"""
    step = timedelta(minutes=step_minutes)
    best: datetime | None = None
    for a0, a1 in bolt_windows or [(None, None)]:
        for b0, b1 in tool_windows or [(None, None)]:
            lo = max(x for x in (a0, b0, t) if x is not None)
            hi = min((x for x in (a1, b1) if x is not None), default=None)
            if hi is None or lo + step <= hi:
                if best is None or lo < best:
                    best = lo
    return best


def _earliest_with_tool(site: BoltSite, tool_windows: dict[str, list],
                        t: datetime, cur_tool: str, step_minutes: float,
                        change_minutes: float) -> tuple[datetime | None, str | None]:
    """该栓在当前时刻的最早可行（开始时刻, 工具）；换工具需额外耗时。"""
    best: tuple[datetime, str] | None = None
    for tl in site.allowed_tools:
        start_t = t if tl == cur_tool else t + timedelta(minutes=change_minutes)
        s = earliest_start(site.windows, tool_windows.get(tl) or [], start_t, step_minutes)
        if s is not None and (best is None or s < best[0]):
            best = (s, tl)
    return best if best else (None, None)


def _tool_options(site: BoltSite, tool_windows: dict[str, list],
                  t: datetime, cur_tool: str, step_minutes: float,
                  change_minutes: float) -> list[tuple[datetime, str]]:
    """该栓每个允许工具的最早可行时刻，按（开始时刻、优先当前工具）排序。"""
    options: list[tuple[datetime, str]] = []
    for tl in site.allowed_tools:
        start_t = t if tl == cur_tool else t + timedelta(minutes=change_minutes)
        s = earliest_start(site.windows, tool_windows.get(tl) or [], start_t, step_minutes)
        if s is not None:
            options.append((s, tl))
    options.sort(key=lambda o: (o[0], o[1] != cur_tool))
    return options


def _required_separation(min_sep: float, a: BoltSite, b: BoltSite) -> float:
    """连续两步所需角距：最小角间隔与两栓角区半宽之和取大。"""
    return max(min_sep, a.clearance_deg + b.clearance_deg)


def _diagnose_angular(prev_bolt: int, remaining: list[int],
                      sites: dict[int, BoltSite], min_sep: float,
                      ) -> tuple[list[dict], list[dict]]:
    """角间隔无解诊断：受阻栓位 + 最少需解除的限制（降最小间隔或减角区）。"""
    prev = sites[prev_bolt]
    blocked: list[dict] = []
    for b in remaining:
        s = sites[b]
        dist = circular_angle_distance(prev.angle_deg, s.angle_deg)
        need = _required_separation(min_sep, prev, s)
        blocked.append({
            "bolt_no": b,
            "angle_deg": s.angle_deg,
            "reasons": ["angular_separation"],
            "detail": f"与上一栓 {prev_bolt} 角距 {round(dist, 2)}° < 所需 "
                      f"{round(need, 2)}°（最小间隔 {round(min_sep, 2)}°，"
                      f"角区 {prev.clearance_deg}°+{s.clearance_deg}°）",
        })
    relaxations: list[dict] = []
    # 仅保留角区约束（最小间隔视为 0）时能放行的最远栓
    best_dist = 0.0
    for b in remaining:
        s = sites[b]
        dist = circular_angle_distance(prev.angle_deg, s.angle_deg)
        if dist >= prev.clearance_deg + s.clearance_deg:
            best_dist = max(best_dist, dist)
    if best_dist > 0:
        relaxations.append({
            "constraint": "min_separation_deg",
            "suggested_value": round(best_dist, 2),
            "detail": f"将最小角间隔降至 ≤{round(best_dist, 2)}° 即可放行"
                      f"与栓 {prev_bolt} 角距最大的受阻栓位",
        })
    else:
        worst = max(remaining, key=lambda b: sites[b].clearance_deg)
        relaxations.append({
            "constraint": "clearance_deg",
            "bolt_no": worst,
            "detail": "栓位角区（套筒/反力臂）相互重叠，需减小所需角区"
                      f"（如栓 {worst}）或改用无反力臂工具",
        })
    return blocked, relaxations


def _diagnose_time(cands: list[int], sites: dict[int, BoltSite],
                   tool_windows: dict[str, list], t: datetime,
                   step_minutes: float, change_minutes: float, cur_tool: str,
                   ) -> tuple[list[dict], list[dict]]:
    """时间/工具无解诊断：受阻栓位 + 每栓最少需解除的一项限制。"""
    step = timedelta(minutes=step_minutes)
    blocked: list[dict] = []
    relaxations: list[dict] = []
    for b in cands:
        s = sites[b]
        reasons: list[str] = []
        if s.windows and all(w1 < t + step for _, w1 in s.windows):
            reasons.append("time_window")
        for tl in s.allowed_tools:
            tws = tool_windows.get(tl) or []
            start_t = t if tl == cur_tool else t + timedelta(minutes=change_minutes)
            if tws and all(w1 < start_t + step for _, w1 in tws):
                reasons.append(f"tool_availability:{tl}")
        if not reasons:
            reasons.append("time_window")  # 两侧窗均未过期但交集为空（窗错位）
        blocked.append({
            "bolt_no": b,
            "angle_deg": s.angle_deg,
            "reasons": reasons,
            "detail": f"栓 {b} 在当前时刻之后无可行的时间窗/工具时段交集"
                      f"（允许工具 {s.allowed_tools}）",
        })
        # 最少解除测试：单项解除后该栓即可行才列入（先栓级、再工具级）
        if _earliest_with_tool(replace(s, windows=[]), tool_windows, t, cur_tool,
                               step_minutes, change_minutes)[0] is not None:
            relaxations.append({
                "constraint": "time_window", "bolt_no": b,
                "detail": f"解除或延长栓 {b} 的可操作时间窗",
            })
            continue
        free_tools = {tl: [] for tl in s.allowed_tools}
        if _earliest_with_tool(s, free_tools, t, cur_tool,
                               step_minutes, change_minutes)[0] is not None:
            relaxations.append({
                "constraint": "tool_availability", "bolt_no": b,
                "detail": f"延长工具 {s.allowed_tools} 的可用时段",
            })
            continue
        relaxations.append({
            "constraints": ["time_window", "tool_availability"], "bolt_no": b,
            "detail": f"栓 {b} 需同时解除时间窗与工具可用时段限制",
        })
    return blocked, relaxations


class _DeadEnd:
    """搜索过程中记录的最深死胡同（剩余栓最少），供无解诊断。"""

    def __init__(self) -> None:
        self.remaining: list[int] | None = None
        self.prev_bolt: int | None = None
        self.kind: str | None = None          # "angular" | "time"
        self.cands: list[int] = []
        self.t: datetime | None = None
        self.tool: str | None = None

    def update(self, remaining: list[int], prev_bolt: int | None, kind: str,
               cands: list[int], t: datetime, tool: str) -> None:
        if self.remaining is None or len(remaining) < len(self.remaining):
            self.remaining = list(remaining)
            self.prev_bolt = prev_bolt
            self.kind = kind
            self.cands = list(cands)
            self.t = t
            self.tool = tool


def _search_round(*, round_no: int, ratio: float, target_torque: float,
                  remaining: list[int], prev_bolt: int | None, t: datetime,
                  tool: str, order: int, sites: dict[int, BoltSite],
                  min_sep: float, step: timedelta, change: timedelta,
                  tool_windows: dict[str, list], step_minutes: float,
                  tool_change_minutes: float, dead_end: _DeadEnd,
                  ) -> tuple[list[dict], list[dict], datetime, str] | None:
    """轮内深度优先搜索：对径优先只是选序偏好，失败即回溯尝试其他候选。

    返回 (steps, actions, 结束时刻, 结束工具)；全部候选顺序均不可行时返回 None，
    最深的死胡同已写入 dead_end。
    """
    if not remaining:
        return ([], [], t, tool)
    cands = [
        b for b in remaining
        if prev_bolt is None
        or circular_angle_distance(sites[prev_bolt].angle_deg, sites[b].angle_deg)
        >= _required_separation(min_sep, sites[prev_bolt], sites[b])
    ]
    if not cands:
        dead_end.update(remaining, prev_bolt, "angular", [], t, tool)
        return None
    # 对径优先：与上一栓角距最接近 180° 者优先，并列按栓号
    cands.sort(key=lambda b: (
        abs(circular_angle_distance(sites[prev_bolt].angle_deg,
                                    sites[b].angle_deg) - 180.0)
        if prev_bolt is not None else 0.0, b))
    # 分支排序：当前工具立即可行的候选优先（对径序），其余按最早开始时刻
    immediate: list[tuple[int, datetime, str]] = []
    deferred: list[tuple[datetime, int, int, str]] = []
    for rank, b in enumerate(cands):
        for s, tl in _tool_options(sites[b], tool_windows, t, tool,
                                   step_minutes, tool_change_minutes):
            if tl == tool and s <= t + _EPS:
                immediate.append((b, s, tl))
            else:
                deferred.append((s, rank, b, tl))
    deferred.sort(key=lambda x: (x[0], x[1]))
    branches: list[tuple[int, datetime, str]] = (
        [(b, s, tl) for b, s, tl in immediate]
        + [(b, s, tl) for s, _rank, b, tl in deferred])
    for b, s, tl in branches:
        sub = _search_round(
            round_no=round_no, ratio=ratio, target_torque=target_torque,
            remaining=[x for x in remaining if x != b], prev_bolt=b,
            t=s + step, tool=tl, order=order + 1, sites=sites, min_sep=min_sep,
            step=step, change=change, tool_windows=tool_windows,
            step_minutes=step_minutes, tool_change_minutes=tool_change_minutes,
            dead_end=dead_end)
        if sub is None:
            continue
        sub_steps, sub_actions, end_t, end_tool = sub
        acts: list[dict] = []
        eff = s - (change if tl != tool else timedelta(0))
        if eff > t + _EPS:
            acts.append({
                "kind": "wait",
                "from": t.isoformat(timespec="seconds"),
                "until": eff.isoformat(timespec="seconds"),
                "minutes": round((eff - t).total_seconds() / 60.0, 2),
                "reason": "等待栓位时间窗或工具可用时段",
            })
        if tl != tool:
            acts.append({
                "kind": "tool_change", "from_tool": tool, "to_tool": tl,
                "at": eff.isoformat(timespec="seconds"),
                "minutes": tool_change_minutes,
            })
        step_dict = {
            "round_no": round_no,
            "order_in_round": order + 1,
            "bolt_no": b,
            "ratio": ratio,
            "target_torque": round(target_torque * ratio, 2),
            "tool_id": tl,
            "scheduled_at": s.isoformat(timespec="seconds"),
            "angle_deg": sites[b].angle_deg,
        }
        return ([step_dict] + sub_steps, acts + sub_actions, end_t, end_tool)
    # 所有候选顺序均失败：本层也是死胡同（更深的失败优先保留）
    dead_end.update(remaining, prev_bolt, "time", cands, t, tool)
    return None


def schedule_plan(*, bolt_count: int, stage_ratios: list[float], target_torque: float,
                  sites: dict[int, BoltSite], min_separation_deg: float | None,
                  step_minutes: float, tool_change_minutes: float,
                  shift_start: datetime, tool_windows: dict[str, list] | None,
                  default_tool: str, locked_steps: list[dict] | tuple = (),
                  locked_bolts: set[int] | frozenset[int] = frozenset(),
                  ) -> tuple[list[dict], list[dict]]:
    """排出全部轮次的紧固步骤与等待/换工具动作。

    locked_steps：已完成的计划步骤（修订时传入），原位锁定、不重排；
    locked_bolts：补拧锁定螺栓，不生成步骤。
    返回 (steps, actions)；steps 元素兼容 build_plan 字段并增加
    tool_id / scheduled_at / angle_deg。
    轮内顺序用带回溯的搜索确定：对径优先仅为选序偏好，全部候选顺序
    （满足角距、角区、时间窗、工具时段与已完成前缀）都失败才判定无解。
    """
    min_sep = (min_separation_deg if min_separation_deg is not None
               else default_min_separation(bolt_count))
    tool_windows = tool_windows or {}
    step = timedelta(minutes=step_minutes)
    change = timedelta(minutes=tool_change_minutes)

    steps: list[dict] = [dict(s) for s in locked_steps]
    actions: list[dict] = []
    locked_keys = {(s["round_no"], s["bolt_no"]) for s in locked_steps}
    all_bolts = [b for b in range(1, bolt_count + 1) if b not in locked_bolts]

    t = shift_start
    tool = default_tool
    for s in locked_steps:  # 已完成步骤的时刻/工具延续为重排起点
        tool = s.get("tool_id") or tool
        sat = s.get("scheduled_at")
        if sat:
            t = max(t, datetime.fromisoformat(sat) + step)

    for round_no, ratio in enumerate(stage_ratios, start=1):
        round_locked = [s for s in locked_steps if s["round_no"] == round_no]
        prev_bolt = round_locked[-1]["bolt_no"] if round_locked else None
        remaining = [b for b in all_bolts if (round_no, b) not in locked_keys]
        order = max((s["order_in_round"] for s in round_locked), default=0)
        dead_end = _DeadEnd()
        result = _search_round(
            round_no=round_no, ratio=ratio, target_torque=target_torque,
            remaining=remaining, prev_bolt=prev_bolt, t=t, tool=tool,
            order=order, sites=sites, min_sep=min_sep, step=step, change=change,
            tool_windows=tool_windows, step_minutes=step_minutes,
            tool_change_minutes=tool_change_minutes, dead_end=dead_end)
        if result is None:
            # 先查与顺序无关的不可行栓：其时间窗/允许工具时段在排程起点前
            # 已全部结束，任何候选顺序都无法放行——直接作为根因诊断，
            # 不被搜索过程中产生的角间隔假象掩盖
            orderless = [
                b for b in remaining
                if _earliest_with_tool(sites[b], tool_windows, t, default_tool,
                                       step_minutes, tool_change_minutes)[0] is None
            ]
            if orderless:
                blocked, relax = _diagnose_time(
                    orderless, sites, tool_windows, t,
                    step_minutes, tool_change_minutes, default_tool)
                raise PlanInfeasible(
                    round_no, blocked, relax,
                    f"第 {round_no} 轮：栓 {orderless} 的时间窗/允许工具时段"
                    "在排程起点前已全部结束，任何顺序都无法放行，"
                    "拒绝跳过螺栓伪造方案")
            if dead_end.kind == "angular":
                blocked, relax = _diagnose_angular(
                    dead_end.prev_bolt, dead_end.remaining, sites, min_sep)
                message = (f"第 {round_no} 轮：栓 {dead_end.prev_bolt} 之后无任何栓位"
                           f"满足最小角间隔/角区约束（剩余 {len(dead_end.remaining)} 栓），"
                           "已搜索全部候选顺序，拒绝跳过螺栓伪造方案")
            else:
                blocked, relax = _diagnose_time(
                    dead_end.cands, sites, tool_windows, dead_end.t,
                    step_minutes, tool_change_minutes, dead_end.tool)
                message = (f"第 {round_no} 轮：候选栓位的时间窗/工具可用时段均不可行，"
                           "已搜索全部候选顺序，拒绝跳过螺栓伪造方案")
            raise PlanInfeasible(round_no, blocked, relax, message)
        round_steps, round_actions, t, tool = result
        steps.extend(round_steps)
        actions.extend(round_actions)
    return steps, actions
