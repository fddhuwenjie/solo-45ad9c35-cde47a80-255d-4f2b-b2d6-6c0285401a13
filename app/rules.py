"""回传校验规则（纯函数，便于单测）。

任一规则不满足即拒绝推进并指出涉事螺栓；所有拒绝由路由层写入 anomalies。
校验顺序：状态 -> 栓号合法 -> 工具一致 -> 校准有效期 -> 量程 ->
（补拧分支 | 跳步/同轮重复 -> 同轮相邻 -> 扭矩超差）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .sequencing import circular_distance
from .schemas import TorqueReport


@dataclass
class Rejection:
    reason: str
    message: str
    bolt_no: int | None
    expected_bolt_no: int | None = None

    def as_detail(self) -> dict:
        return {
            "reason": self.reason,
            "message": self.message,
            "bolt_no": self.bolt_no,
            "expected_bolt_no": self.expected_bolt_no,
        }


def allowed_interval(target: float, tolerance_pct: float) -> tuple[float, float]:
    """允许区间 [target*(1-偏差), target*(1+偏差)]，精确值。

    前置可行性校验与回传偏差判断共用同一边界计算；任何舍入只允许
    出现在展示层，不得参与可行性/合格性判断。
    """
    return (target * (1 - tolerance_pct / 100), target * (1 + tolerance_pct / 100))


def find_infeasible_rounds(
    target_torque: float,
    stage_ratios: list[float],
    tolerance_pct: float,
    tool_range_min: float,
    tool_range_max: float,
) -> list[dict]:
    """逐轮检查允许区间与工具量程是否有交集（精确边界判断）。

    无交集的轮次任何回传都不可能合格，须在批准前拒绝。返回冲突轮次列表；
    区间在 payload 中舍入到 4 位小数仅用于展示，不影响判断。
    """
    conflicts: list[dict] = []
    for round_no, ratio in enumerate(stage_ratios, start=1):
        target = round(target_torque * ratio, 2)  # 与 build_plan 的轮目标一致
        lo, hi = allowed_interval(target, tolerance_pct)
        if hi < tool_range_min or lo > tool_range_max:
            conflicts.append(
                {
                    "round_no": round_no,
                    "ratio": ratio,
                    "target_torque": target,
                    "allowed_interval": [round(lo, 4), round(hi, 4)],
                    "tool_range": [tool_range_min, tool_range_max],
                }
            )
    return conflicts


def _check_tolerance(proc: dict, report: TorqueReport, target: float) -> Rejection | None:
    lo, hi = allowed_interval(target, proc["tolerance_pct"])
    if not (lo <= report.measured_torque <= hi):
        dev_pct = abs(report.measured_torque - target) / target * 100
        return Rejection(
            "torque_out_of_tolerance",
            f"螺栓 {report.bolt_no} 实测 {report.measured_torque} N·m，"
            f"目标 {target} N·m，偏差 {dev_pct:.2f}% 超过允许 ±{proc['tolerance_pct']}%",
            report.bolt_no,
        )
    return None


def validate_report(
    proc: dict,
    plan: list[dict],
    done: list[dict],
    report: TorqueReport,
    rework_origin: dict | None = None,
) -> Rejection | None:
    """校验一条回传。proc 中 calibration_valid_until 须为 date，stage_ratios 为 list。"""
    status = proc["status"]

    if status == "archived":
        return Rejection("archived", "工艺已封存，禁止任何回传", report.bolt_no)
    if status in ("draft", "approved"):
        return Rejection("not_started", f"工艺状态为 {status}，尚未开工，禁止回传", report.bolt_no)

    if not (1 <= report.bolt_no <= proc["bolt_count"]):
        return Rejection(
            "unknown_bolt",
            f"螺栓 {report.bolt_no} 不存在（共 {proc['bolt_count']} 栓）",
            report.bolt_no,
        )

    # 补拧批次：锁定的合格螺栓不再作业，也不接受补拧回传
    locked = proc.get("locked_bolts") or set()
    if report.bolt_no in locked:
        return Rejection(
            "bolt_locked",
            f"螺栓 {report.bolt_no} 在补拧作业包中已锁定为合格，禁止重新紧固；"
            "如需变更须另行派生工艺",
            report.bolt_no,
        )

    # 计划步骤锁定工具：现场计划可为不同栓位安排不同工具（换工具动作）；
    # 无现场计划时即批准工具
    if report.rework_of is not None and rework_origin is not None:
        step = next((s for s in plan
                     if s["round_no"] == rework_origin["round_no"]
                     and s["bolt_no"] == report.bolt_no), None)
    else:
        step = plan[len(done)] if len(done) < len(plan) else None
    expected_tool = (step or {}).get("tool_id") or proc["tool_id"]
    if report.tool_id != expected_tool:
        return Rejection(
            "tool_mismatch",
            f"回传工具 {report.tool_id} 与计划步骤锁定工具 {expected_tool} 不一致；"
            "更换工具须派生新版本，现场工具变化须派生计划修订",
            report.bolt_no,
        )

    # 校准有效期（含当日）
    valid_until: date = proc["calibration_valid_until"]
    if report.reported_at.date() > valid_until:
        return Rejection(
            "calibration_expired",
            f"工具 {report.tool_id} 校准有效期至 {valid_until}，"
            f"回传时刻 {report.reported_at.date()} 已过期，作业无效",
            report.bolt_no,
        )

    # 工具越量程：量程内读数才有效
    if not (proc["tool_range_min"] <= report.measured_torque <= proc["tool_range_max"]):
        return Rejection(
            "tool_out_of_range",
            f"实测扭矩 {report.measured_torque} N·m 超出工具量程 "
            f"[{proc['tool_range_min']}, {proc['tool_range_max']}] N·m",
            report.bolt_no,
        )

    # ---- 补拧分支：不推进顺序、不覆盖原记录 ----
    if report.rework_of is not None:
        if rework_origin is None:
            return Rejection(
                "rework_target_missing",
                f"补拧指向的原记录 {report.rework_of} 不存在",
                report.bolt_no,
            )
        if rework_origin["bolt_no"] != report.bolt_no:
            return Rejection(
                "rework_bolt_mismatch",
                f"补拧螺栓 {report.bolt_no} 与原记录螺栓 "
                f"{rework_origin['bolt_no']} 不一致",
                report.bolt_no,
            )
        target = round(
            proc["target_torque"] * proc["stage_ratios"][rework_origin["round_no"] - 1], 2
        )
        return _check_tolerance(proc, report, target)

    # ---- 正常推进分支 ----
    if status != "in_progress":
        return Rejection(
            "not_in_progress",
            f"工艺状态为 {status}，常规回传须处于 in_progress；如需补拧请指定 rework_of",
            report.bolt_no,
        )

    progress = len(done)
    if progress >= len(plan):
        return Rejection("already_complete", "全部螺栓各轮次均已完成", report.bolt_no)

    expected = plan[progress]
    current_round = expected["round_no"]

    # 同一螺栓在本轮已有记录
    if any(r["round_no"] == current_round and r["bolt_no"] == report.bolt_no for r in done):
        return Rejection(
            "duplicate_in_round",
            f"螺栓 {report.bolt_no} 在第 {current_round} 轮已有记录，禁止重复回传；"
            "如需补拧请指定 rework_of 指向原记录",
            report.bolt_no,
            expected["bolt_no"],
        )

    # 跳步：必须严格按计划顺序推进
    if report.bolt_no != expected["bolt_no"]:
        return Rejection(
            "out_of_sequence",
            f"跳步：第 {current_round} 轮下一栓应为 {expected['bolt_no']}"
            f"（轮内第 {expected['order_in_round']} 位），实际回传 {report.bolt_no}",
            report.bolt_no,
            expected["bolt_no"],
        )

    # 同轮连续紧固相邻螺栓（现场计划由规划器按实际方位保证角间隔，
    # 不再套用规则圆周的栓号相邻规则；无现场计划时该检查不变）
    if done and done[-1]["round_no"] == current_round and not proc.get("site_plan"):
        prev_bolt = done[-1]["bolt_no"]
        if circular_distance(prev_bolt, report.bolt_no, proc["bolt_count"]) == 1:
            return Rejection(
                "adjacent_in_round",
                f"螺栓 {report.bolt_no} 与上一栓 {prev_bolt} 同轮相邻，"
                "禁止同轮连续紧固相邻螺栓",
                report.bolt_no,
            )

    return _check_tolerance(proc, report, expected["target_torque"])
