"""液压张拉执行：压力 -> 施加载荷 -> 预测残余预紧力（纯函数，便于单测）。

物理模型（液压拉伸器）：

    施加载荷（拉伸器对螺栓施加的拉力）：
        F_pull = p · A_h            （p 泵压 MPa = N/mm²，A_h 液压有效面积 mm²）

    卸压后载荷转移（螺母贴合、螺纹嵌入、垫片回弹），按冻结的载荷转移系数 λ：
        F_res = F_pull · (1 − λ)    （预测残余预紧力）

    因此每轮目标残余 ρ·F_target 所需的设定泵压：
        p_set = ρ · F_target / (1 − λ) / A_h

    行程预测（螺栓在施加载荷下的弹性伸长）：
        ΔL = F_pull · L_eff / (E · A_s)

单位：力 kN（内部换算 N），压力 MPa，长度/行程 mm，弹性模量 MPa（=N/mm²）。

分轮换位方案：每轮覆盖全部螺栓；同组螺栓用同一泵源同步加压，组内任意两栓
圆周间隔 ≥ 冻结的最小机具间隔（防相邻机具相撞）；组大小 ≤ 拉伸器数量。
轮内分组按"交叉序列逐轮旋转 + 贪心间距过滤"生成——旋转使各轮组归属与
执行次序不同（换位），让卸压载荷转移的影响在全周均布；贪心对已完成组
前缀稳定：执行中断派生修订时，已完成组原位锁定，仅未完成组重排。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .sequencing import circular_distance, cross_sequence

# ---------------------------------------------------------------- 拒绝原因
REJ_PLAN_NOT_APPROVED = "plan_not_approved"          # 方案未批准（或无活动修订）
REJ_PLAN_READ_ONLY = "plan_read_only"                # 已确认/已废止，只读
REJ_PROCEDURE_WINDOW = "procedure_window_closed"     # 工艺状态不允许回传
REJ_OUT_OF_SEQUENCE = "out_of_sequence"              # 未按方案组序回传
REJ_COVERAGE_CONFLICT = "coverage_conflict"          # 覆盖冲突：通道与计划组不符
REJ_GAUGE_MISMATCH = "gauge_mismatch"                # 压力表与冻结表号不符
REJ_CALIBRATION_EXPIRED = "calibration_expired"      # 压力表校准失效
REJ_HOLD_INSUFFICIENT = "hold_insufficient"          # 保压不足
REJ_RELEASE_ORDER_INVALID = "release_order_invalid"  # 卸压次序未覆盖全组
REJ_STROKE_EXCEEDED = "stroke_exceeded"              # 机具超行程
REJ_PRESSURE_OVER_CAPACITY = "pressure_over_capacity"  # 超拉伸器能力
REJ_PRESSURE_OUT_OF_SYNC = "pressure_out_of_sync"    # 组内压力不同步
REJ_RESIDUAL_OUT_OF_TOLERANCE = "residual_out_of_tolerance"  # 残余预紧力超差


@dataclass
class TensionRejection:
    """回传拒绝：定位栓号与原始区间（冻结限值/目标带），由路由层写入 anomalies。"""

    reason: str
    message: str
    bolt_no: int | None
    allowed_interval: list | None = None      # 原始区间（冻结限值或目标带）
    extra: dict | None = None

    def as_detail(self) -> dict:
        detail = {
            "reason": self.reason,
            "message": self.message,
            "bolt_no": self.bolt_no,
        }
        if self.allowed_interval is not None:
            detail["allowed_interval"] = self.allowed_interval
        if self.extra:
            detail.update(self.extra)
        return detail


# ---------------------------------------------------------------- 换算

def required_applied_load_kn(target_load_kn: float, ratio: float,
                             transfer: float) -> float:
    """本轮需施加的拉伸载荷：F_pull = ρ·F_target / (1 − λ)。"""
    return target_load_kn * ratio / (1.0 - transfer)


def pressure_for_load_mpa(load_kn: float, hydraulic_area_mm2: float) -> float:
    """施加载荷换算泵压：p = F / A_h（kN→N）。"""
    return load_kn * 1000.0 / hydraulic_area_mm2


def load_for_pressure_kn(pressure_mpa: float, hydraulic_area_mm2: float) -> float:
    """泵压换算施加载荷：F = p · A_h（N→kN）。"""
    return pressure_mpa * hydraulic_area_mm2 / 1000.0


def predicted_residual_kn(applied_load_kn: float, transfer: float) -> float:
    """预测残余预紧力：F_res = F_pull · (1 − λ)。"""
    return applied_load_kn * (1.0 - transfer)


def predicted_stroke_mm(load_kn: float, length_mm: float, modulus_mpa: float,
                        area_mm2: float) -> float:
    """施加载荷下的螺栓弹性伸长（行程需求）：ΔL = F·L / (E·A)。"""
    return load_kn * 1000.0 * length_mm / (modulus_mpa * area_mm2)


def residual_band_kn(target_load_kn: float, ratio: float,
                     tolerance_pct: float) -> tuple[float, float]:
    """本轮残余预紧力允许区间 [ρ·F·(1−偏差), ρ·F·(1+偏差)]（精确值）。"""
    target = target_load_kn * ratio
    return (target * (1 - tolerance_pct / 100.0),
            target * (1 + tolerance_pct / 100.0))


# ---------------------------------------------------------------- 分轮换位方案

def _greedy_groups(ordered: list[int], size: int, min_spacing: int,
                   bolt_count: int) -> list[list[int]]:
    """按候选顺序贪心分组：取队首，依序吸收与组内各栓间隔均 ≥ min_spacing 的栓。

    对已完成组前缀稳定：从候选序列中删去前 j 个完整组的栓后重跑，结果即
    原第 j+1..k 组——中断修订只重排未完成组时方案可确定复现。
    """
    groups: list[list[int]] = []
    queue = list(ordered)
    while queue:
        group = [queue.pop(0)]
        rest: list[int] = []
        for b in queue:
            if (len(group) < size
                    and all(circular_distance(b, g, bolt_count) >= min_spacing
                            for g in group)):
                group.append(b)
            else:
                rest.append(b)
        groups.append(group)
        queue = rest
    return groups


def build_scheme(bolt_count: int, stage_ratios: list[float], tensioner_count: int,
                 min_tool_spacing: int, *,
                 locked_groups: list[dict] | None = None) -> list[dict]:
    """生成分轮换位方案：每轮全部螺栓恰好出现一次，分组同步加压。

    换位：第 r 轮（0 起）候选顺序取交叉序列旋转 r 位，各轮组归属与执行
    次序随之变化；locked_groups（已完成组）原位保留，仅对剩余栓重排，
    新组号接续该轮已锁定组号。
    返回 [{"round_no", "ratio", "groups": [{"group_no", "bolts"}]}]。
    """
    seq = cross_sequence(bolt_count)
    locked_groups = locked_groups or []
    rounds: list[dict] = []
    for r_idx, ratio in enumerate(stage_ratios):
        round_no = r_idx + 1
        locked = sorted((g for g in locked_groups if g["round_no"] == round_no),
                        key=lambda g: g["group_no"])
        locked_bolts = {b for g in locked for b in g["bolts"]}
        rot = r_idx % bolt_count
        rotated = seq[rot:] + seq[:rot]
        remaining = [b for b in rotated if b not in locked_bolts]
        groups = [dict(group_no=g["group_no"], bolts=list(g["bolts"]), locked=True)
                  for g in locked]
        next_no = max((g["group_no"] for g in locked), default=0) + 1
        for bolts in _greedy_groups(remaining, tensioner_count, min_tool_spacing,
                                    bolt_count):
            groups.append({"group_no": next_no, "bolts": bolts, "locked": False})
            next_no += 1
        rounds.append({"round_no": round_no, "ratio": ratio, "groups": groups})
    return rounds


def scheme_setpoints(plan: dict, rounds: list[dict]) -> list[dict]:
    """为方案每轮补设定泵压/施加载荷/预测行程（展示与回传判定共用同一换算）。"""
    out: list[dict] = []
    for rd in rounds:
        ratio = rd["ratio"]
        applied = required_applied_load_kn(plan["target_load_kn"], ratio,
                                           plan["load_transfer_coefficient"])
        lo, hi = residual_band_kn(plan["target_load_kn"], ratio,
                                  plan["load_tolerance_pct"])
        out.append({
            **rd,
            "set_pressure_mpa": round(pressure_for_load_mpa(
                applied, plan["hydraulic_area_mm2"]), 4),
            "applied_load_kn": round(applied, 4),
            "predicted_stroke_mm": round(predicted_stroke_mm(
                applied, plan["length_mm"], plan["elastic_modulus_mpa"],
                plan["area_mm2"]), 4),
            "residual_band_kn": [round(lo, 4), round(hi, 4)],
        })
    return out


def find_infeasible_rounds(plan: dict) -> list[dict]:
    """逐轮预检：设定泵压不得超拉伸器能力，预测行程不得超最大行程（精确边界）。

    无交集的轮次任何回传都不可能合格，须在创建/批准前拒绝；payload 中
    数值舍入到 4 位小数仅用于展示，判断用精确值。
    """
    conflicts: list[dict] = []
    for ratio in plan["stage_ratios"]:
        applied = required_applied_load_kn(plan["target_load_kn"], ratio,
                                           plan["load_transfer_coefficient"])
        p_set = pressure_for_load_mpa(applied, plan["hydraulic_area_mm2"])
        stroke = predicted_stroke_mm(applied, plan["length_mm"],
                                     plan["elastic_modulus_mpa"], plan["area_mm2"])
        problems: list[str] = []
        if p_set > plan["max_pressure_mpa"]:
            problems.append("pressure")
        if stroke > plan["max_stroke_mm"]:
            problems.append("stroke")
        if problems:
            conflicts.append({
                "round_ratio": ratio,
                "problems": problems,
                "required_pressure_mpa": round(p_set, 4),
                "max_pressure_mpa": plan["max_pressure_mpa"],
                "required_stroke_mm": round(stroke, 4),
                "max_stroke_mm": plan["max_stroke_mm"],
            })
    return conflicts


def flatten_groups(rounds: list[dict]) -> list[dict]:
    """方案执行顺序展开：[(round_no, group_no, bolts)]，按轮、组号升序。"""
    out: list[dict] = []
    for rd in sorted(rounds, key=lambda r: r["round_no"]):
        for g in sorted(rd["groups"], key=lambda g: g["group_no"]):
            out.append({"round_no": rd["round_no"], "ratio": rd["ratio"],
                        "group_no": g["group_no"], "bolts": list(g["bolts"])})
    return out


# ---------------------------------------------------------------- 回传校验

def validate_round_report(plan: dict, rounds: list[dict], done: list[dict],
                          report, proc_status: str) -> TensionRejection | None:
    """校验一条分组回传。任一规则不满足即拒绝并定位栓号与原始区间。

    校验顺序：方案/工艺状态 -> 组序 -> 覆盖冲突 -> 压力表 -> 校准 ->
    保压 -> 卸压次序 -> 超行程 -> 超能力 -> 压力不同步 -> 残余预紧力超差。
    plan 为冻结参数（stage_ratios 已解析为 list）；done 为已接受回传（按 id 升序）。
    """
    status = plan["status"]
    if status in ("confirmed", "superseded"):
        return TensionRejection(
            REJ_PLAN_READ_ONLY,
            f"张拉方案修订 {plan['revision']} 已 {status}，只读；"
            "新作业须派生修订或新建方案", None)
    if status != "approved":
        return TensionRejection(
            REJ_PLAN_NOT_APPROVED,
            f"张拉方案修订 {plan['revision']} 尚未批准（状态 {status}），禁止回传",
            None)
    if proc_status not in ("approved", "in_progress"):
        return TensionRejection(
            REJ_PROCEDURE_WINDOW,
            f"工艺状态 {proc_status}：张拉回传仅在 approved/in_progress 阶段接收",
            None)

    sequence = flatten_groups(rounds)
    if len(done) >= len(sequence):
        return TensionRejection(
            REJ_OUT_OF_SEQUENCE, "全部轮次分组均已完成，禁止重复回传", None,
            extra={"completed_groups": len(done)})
    expected = sequence[len(done)]
    if (report.round_no, report.group_no) != (expected["round_no"],
                                              expected["group_no"]):
        return TensionRejection(
            REJ_OUT_OF_SEQUENCE,
            f"跳组：下一组应为第 {expected['round_no']} 轮第 {expected['group_no']} 组"
            f"（栓 {expected['bolts']}），实际回传第 {report.round_no} 轮"
            f"第 {report.group_no} 组",
            None,
            extra={"expected_round_no": expected["round_no"],
                   "expected_group_no": expected["group_no"],
                   "expected_bolts": expected["bolts"]})

    # 覆盖冲突：回传通道必须与计划组栓号完全一致（不缺、不多、不重）
    planned = set(expected["bolts"])
    reported = [c.bolt_no for c in report.channels]
    reported_set = set(reported)
    duplicated = sorted({b for b in reported if reported.count(b) > 1})
    missing = sorted(planned - reported_set)
    extra = sorted(reported_set - planned)
    if duplicated or missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"计划栓 {missing} 未回传")
        if extra:
            parts.append(f"栓 {extra} 不属于本组（覆盖他组栓位）")
        if duplicated:
            parts.append(f"栓 {duplicated} 通道重复")
        located = (extra or missing or duplicated)[0]
        return TensionRejection(
            REJ_COVERAGE_CONFLICT,
            f"覆盖冲突：第 {report.round_no} 轮第 {report.group_no} 组计划栓"
            f" {sorted(planned)}，" + "；".join(parts),
            located,
            extra={"planned_bolts": sorted(planned), "missing_bolts": missing,
                   "extra_bolts": extra, "duplicated_bolts": duplicated})

    if report.gauge_id != plan["gauge_id"]:
        return TensionRejection(
            REJ_GAUGE_MISMATCH,
            f"回传压力表 {report.gauge_id} 与方案冻结表号 {plan['gauge_id']} 不一致；"
            "更换压力表须派生修订并说明理由", None)

    valid_until = date.fromisoformat(plan["gauge_calibration_until"])
    if report.reported_at.date() > valid_until:
        return TensionRejection(
            REJ_CALIBRATION_EXPIRED,
            f"压力表 {report.gauge_id} 校准有效期至 {valid_until}，"
            f"回传时刻 {report.reported_at.date()} 已过期，本组作业无效",
            None)

    if report.hold_seconds < plan["min_hold_seconds"]:
        return TensionRejection(
            REJ_HOLD_INSUFFICIENT,
            f"保压 {report.hold_seconds}s 不足冻结下限 {plan['min_hold_seconds']}s，"
            "载荷转移未稳定，本组作业无效",
            None,
            allowed_interval=[plan["min_hold_seconds"], None])

    if sorted(report.release_order) != sorted(planned):
        return TensionRejection(
            REJ_RELEASE_ORDER_INVALID,
            f"卸压次序 {report.release_order} 未恰好覆盖本组栓 {sorted(planned)}",
            None,
            extra={"planned_bolts": sorted(planned)})

    # 逐通道：超行程 / 超能力（定位栓号与原始区间）
    for c in report.channels:
        if c.stroke_mm > plan["max_stroke_mm"]:
            return TensionRejection(
                REJ_STROKE_EXCEEDED,
                f"栓 {c.bolt_no} 活塞行程 {c.stroke_mm}mm 超拉伸器最大行程 "
                f"{plan['max_stroke_mm']}mm（机具超行程，载荷读数不可信）",
                c.bolt_no,
                allowed_interval=[0, plan["max_stroke_mm"]])
        if c.pressure_mpa > plan["max_pressure_mpa"]:
            return TensionRejection(
                REJ_PRESSURE_OVER_CAPACITY,
                f"栓 {c.bolt_no} 通道压力 {c.pressure_mpa}MPa 超拉伸器能力 "
                f"{plan['max_pressure_mpa']}MPa",
                c.bolt_no,
                allowed_interval=[0, plan["max_pressure_mpa"]])

    # 组内压力不同步：极差/均值超冻结同步允差（泵压记录正常≠各栓受力一致）
    pressures = {c.bolt_no: c.pressure_mpa for c in report.channels}
    if len(pressures) > 1:
        p_max_b = max(pressures, key=pressures.get)
        p_min_b = min(pressures, key=pressures.get)
        mean_p = sum(pressures.values()) / len(pressures)
        spread_pct = ((pressures[p_max_b] - pressures[p_min_b]) / mean_p * 100.0
                      if mean_p > 0 else 0.0)
        if spread_pct > plan["pressure_sync_tolerance_pct"]:
            return TensionRejection(
                REJ_PRESSURE_OUT_OF_SYNC,
                f"组内压力不同步：栓 {p_max_b}（{pressures[p_max_b]}MPa）与栓 "
                f"{p_min_b}（{pressures[p_min_b]}MPa）极差 {round(spread_pct, 2)}% "
                f"超同步允差 {plan['pressure_sync_tolerance_pct']}%",
                p_max_b,
                allowed_interval=[0, plan["pressure_sync_tolerance_pct"]],
                extra={"spread_pct": round(spread_pct, 4),
                       "max_bolt_no": p_max_b, "min_bolt_no": p_min_b})

    # 逐栓残余预紧力超差（原始区间 = 本轮目标带）
    lo, hi = residual_band_kn(plan["target_load_kn"], expected["ratio"],
                              plan["load_tolerance_pct"])
    for c in report.channels:
        applied = load_for_pressure_kn(c.pressure_mpa, plan["hydraulic_area_mm2"])
        residual = predicted_residual_kn(applied, plan["load_transfer_coefficient"])
        if not (lo <= residual <= hi):
            return TensionRejection(
                REJ_RESIDUAL_OUT_OF_TOLERANCE,
                f"栓 {c.bolt_no} 预测残余预紧力 {round(residual, 4)}kN 超出第 "
                f"{expected['round_no']} 轮目标带 [{round(lo, 4)}, {round(hi, 4)}]kN"
                f"（通道压力 {c.pressure_mpa}MPa 换算施加 {round(applied, 4)}kN，"
                f"载荷转移系数 {plan['load_transfer_coefficient']}）",
                c.bolt_no,
                allowed_interval=[round(lo, 4), round(hi, 4)],
                extra={"residual_load_kn": round(residual, 6),
                       "applied_load_kn": round(applied, 6)})
    return None


def channel_results(plan: dict, report) -> list[dict]:
    """回传通道换算结果：逐栓施加载荷与预测残余预紧力（落库与响应共用）。"""
    out: list[dict] = []
    for c in report.channels:
        applied = load_for_pressure_kn(c.pressure_mpa, plan["hydraulic_area_mm2"])
        out.append({
            "bolt_no": c.bolt_no,
            "pressure_mpa": c.pressure_mpa,
            "stroke_mm": c.stroke_mm,
            "applied_load_kn": round(applied, 6),
            "residual_load_kn": round(predicted_residual_kn(
                applied, plan["load_transfer_coefficient"]), 6),
        })
    return out


# ---------------------------------------------------------------- 整案评估

def evaluate_plan(plan: dict, rounds: list[dict], reports: list[dict],
                  channels_by_report: dict[int, list[dict]]) -> dict:
    """汇总方案执行结论：逐组状态、逐栓末轮残余预紧力与确认阻断项。

    采纳超声实测的修订以超声快照为残余预紧力证据（source=ultrasonic），
    否则以末轮回传换算的预测值（source=predicted）。
    """
    sequence = flatten_groups(rounds)
    done_keys = {(r["round_no"], r["group_no"]) for r in reports}
    group_views: list[dict] = []
    missing: list[dict] = []
    for step in sequence:
        key = (step["round_no"], step["group_no"])
        rep = next((r for r in reports
                    if (r["round_no"], r["group_no"]) == key), None)
        if rep is None:
            missing.append({"round_no": step["round_no"],
                            "group_no": step["group_no"], "bolts": step["bolts"]})
            group_views.append({**step, "status": "pending", "report_id": None})
        else:
            group_views.append({**step, "status": "done",
                                "report_id": rep["id"],
                                "channels": channels_by_report.get(rep["id"], [])})

    final_ratio = plan["stage_ratios"][-1]
    lo, hi = residual_band_kn(plan["target_load_kn"], final_ratio,
                              plan["load_tolerance_pct"])
    final_round_no = len(plan["stage_ratios"])
    ultrasonic = plan.get("ultrasonic_snapshot") or {}
    bolt_results: list[dict] = []
    out_of_band: list[dict] = []
    for step in sequence:
        if step["round_no"] != final_round_no:
            continue
        rep = next((r for r in reports
                    if (r["round_no"], r["group_no"])
                    == (step["round_no"], step["group_no"])), None)
        chans = channels_by_report.get(rep["id"], []) if rep else []
        by_bolt = {c["bolt_no"]: c for c in chans}
        for bolt in step["bolts"]:
            entry: dict = {"bolt_no": bolt,
                           "residual_band_kn": [round(lo, 4), round(hi, 4)]}
            snap = ultrasonic.get(str(bolt))
            if snap is not None:
                load = snap.get("load_kn")
                entry.update(source="ultrasonic", residual_load_kn=load,
                             in_band=(load is not None and lo <= load <= hi))
            elif bolt in by_bolt:
                load = by_bolt[bolt]["residual_load_kn"]
                entry.update(source="predicted", residual_load_kn=load,
                             in_band=lo <= load <= hi)
            else:
                entry.update(source="predicted", residual_load_kn=None,
                             in_band=False)
            if not entry["in_band"]:
                out_of_band.append({"bolt_no": bolt,
                                    "residual_load_kn": entry["residual_load_kn"],
                                    "allowed_interval": [round(lo, 4),
                                                         round(hi, 4)]})
            bolt_results.append(entry)

    blockers: list[str] = []
    if missing:
        blockers.append("incomplete_coverage")
    if out_of_band:
        blockers.append("residual_out_of_tolerance")
    return {
        "groups": group_views,
        "missing_groups": missing,
        "bolt_results": sorted(bolt_results, key=lambda b: b["bolt_no"]),
        "out_of_band_bolts": out_of_band,
        "final_residual_band_kn": [round(lo, 4), round(hi, 4)],
        "evidence_source": "ultrasonic" if ultrasonic else "predicted",
        "blockers": blockers,
        "confirmable": not blockers,
    }


# ---------------------------------------------------------------- 版本差异

def diff_schemes(old_plan: dict, old_rounds: list[dict],
                 new_plan: dict, new_rounds: list[dict]) -> dict:
    """相邻修订差异：冻结参数变化、逐轮分组变化（锁定组原位保留）与超声采纳。"""
    frozen_fields = (
        "area_mm2", "length_mm", "elastic_modulus_mpa", "target_load_kn",
        "load_tolerance_pct", "tensioner_id", "tensioner_count",
        "hydraulic_area_mm2", "max_pressure_mpa", "max_stroke_mm",
        "min_tool_spacing", "load_transfer_coefficient", "min_hold_seconds",
        "pressure_sync_tolerance_pct", "gauge_id", "gauge_calibration_until",
    )
    param_changes = {
        f: {"from": old_plan[f], "to": new_plan[f]}
        for f in frozen_fields if old_plan[f] != new_plan[f]
    }
    if old_plan["stage_ratios"] != new_plan["stage_ratios"]:
        param_changes["stage_ratios"] = {"from": old_plan["stage_ratios"],
                                         "to": new_plan["stage_ratios"]}

    round_diffs: list[dict] = []
    old_by_round = {r["round_no"]: r for r in old_rounds}
    for rd in new_rounds:
        old_rd = old_by_round.get(rd["round_no"])
        old_groups = {g["group_no"]: sorted(g["bolts"])
                      for g in (old_rd["groups"] if old_rd else [])}
        new_groups = {g["group_no"]: sorted(g["bolts"]) for g in rd["groups"]}
        unchanged = sorted(no for no in new_groups
                           if old_groups.get(no) == new_groups[no])
        regrouped = sorted(no for no in new_groups
                           if no in old_groups and old_groups[no] != new_groups[no])
        added = sorted(no for no in new_groups if no not in old_groups)
        removed = sorted(no for no in old_groups if no not in new_groups)
        if regrouped or added or removed:
            round_diffs.append({
                "round_no": rd["round_no"],
                "unchanged_groups": unchanged,
                "regrouped_groups": regrouped,
                "added_groups": added,
                "removed_groups": removed,
                "groups": [{"group_no": g["group_no"], "bolts": g["bolts"],
                            "locked": g.get("locked", False)}
                           for g in rd["groups"]],
            })
    return {
        "param_changes": param_changes,
        "round_changes": round_diffs,
        "ultrasonic_batch": (new_plan.get("ultrasonic_batch_id")
                             if new_plan.get("ultrasonic_batch_id")
                             != old_plan.get("ultrasonic_batch_id") else None),
        "change_note": new_plan.get("change_note"),
    }
