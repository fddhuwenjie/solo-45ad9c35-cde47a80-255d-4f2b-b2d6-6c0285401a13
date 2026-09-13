"""紧固件摩擦批次标定：贴合后单调加载段拟合、扭矩系数统计与推荐扭矩窗口（纯函数）。

同一套工艺改用镀层螺栓、另一批螺母或新开封润滑剂后，旧扭矩系数未必仍适用；
照抄目标扭矩，实测预紧力可能整圈偏高或离散。本模块对台架标定数据（逐点
扭矩-转角-轴向载荷）做评估：

1. 逐装配次只取**贴合后的单调加载段**：贴合点（扭矩首次达到贴合阈值，或人工
   指定）之后，扭矩与轴向载荷均单调不减（容差内）的连续区间；段内最小二乘
   拟合 T = a·F + b，在目标载荷点给出扭矩系数

       K = T(F_target) / (F_target · d)        （T 取拟合值）

   其中 T[N·m]、F[kN]、d[mm]——1 kN·mm 恰等于 1 N·m，K 无量纲。

2. 批内统计：全部合格装配次在目标载荷点的 K 均值/标准差/变异系数（批内离散度）、
   逐试样重复装配漂移（末次相对首次的 K 变化率）、可用载荷区间（各合格段
   载荷覆盖的交集），并反算推荐扭矩窗口

       T_window = [K_min·d·F_target, K_max·d·F_target]

   （该批紧固件达到同一目标载荷实际需要的扭矩范围），对照工艺工具量程。

3. 草稿阻断项（任一存在即不可确认，标定停在草稿）：
   试样数量不足、批次覆盖不足（试样批次归属不符/装配次数覆盖不全）、
   测量通道校准失效、曲线回退、几何不一致、异常值剔除无理由、
   推荐窗口越出工具量程。

单位：内部统一 N·m / kN / deg；声明单位在提交级字段给出（逐点读数共用）。
"""
from __future__ import annotations

from datetime import date, datetime

from .curve import ANGLE_TO_DEG, TORQUE_TO_NM

MIN_POINTS = 5               # 构成有效装配次曲线的最少采样点数
MIN_SEGMENT_POINTS = 3       # 贴合后单调加载段拟合所需最少点数
REGRESSION_TOL_NM = 1e-6     # 扭矩回退判定容差（吸收传感器噪声）
REGRESSION_TOL_KN = 1e-6     # 载荷回退判定容差
GEOMETRY_TOL_REL = 1e-6      # 试样直径与冻结公称直径的一致性相对容差

LOAD_TO_KN = {"kN": 1.0, "N": 1e-3, "lbf": 0.0044482216152605}

# ---------------------------------------------------------------- 草稿阻断项
B_INSUFFICIENT_SPECIMENS = "insufficient_specimens"        # 合格试样数量不足
B_BATCH_COVERAGE = "batch_coverage_insufficient"           # 批次/装配次数覆盖不足
B_CHANNEL_CAL_EXPIRED = "channel_calibration_expired"      # 测量通道校准失效
B_CURVE_REGRESSION = "curve_regression"                    # 曲线回退
B_GEOMETRY_INCONSISTENT = "geometry_inconsistent"          # 几何不一致
B_UNJUSTIFIED_EXCLUSION = "outlier_exclusion_unjustified"  # 异常值剔除无理由
B_WINDOW_OUT_OF_TOOL = "torque_window_out_of_tool_range"   # 推荐窗口越出工具量程

BLOCKER_MESSAGES = {
    B_INSUFFICIENT_SPECIMENS: "合格试样数量低于冻结下限，批统计不具代表性",
    B_BATCH_COVERAGE: "试样批次归属与冻结批次身份不符，或装配次数覆盖不全",
    B_CHANNEL_CAL_EXPIRED: "测量通道（扭矩/载荷）校准有效期已过，读数不可信",
    B_CURVE_REGRESSION: "贴合后加载段出现扭矩/载荷回退，曲线非单调",
    B_GEOMETRY_INCONSISTENT: "试样声明直径与冻结公称直径不一致",
    B_UNJUSTIFIED_EXCLUSION: "存在未注明理由的异常值剔除（试样或装配次）",
    B_WINDOW_OUT_OF_TOOL: "推荐扭矩窗口越出工艺工具量程，任何设定都无法保证合格",
}

# 装配次曲线缺陷（使该装配次不可用；curve_regression 同时是草稿阻断项）
D_INSUFFICIENT_POINTS = "insufficient_points"          # 点数不足
D_SNUG_NOT_REACHED = "snug_not_reached"                # 贴合点无法定位
D_SEGMENT_TOO_SHORT = "segment_too_short"              # 单调加载段点数不足
D_CURVE_REGRESSION = B_CURVE_REGRESSION                # 曲线回退
D_TARGET_NOT_COVERED = "target_load_not_covered"       # 单调段未覆盖目标载荷
D_OUT_OF_CHANNEL_RANGE = "reading_out_of_channel_range"  # 读数越出通道量程

DEFECT_MESSAGES = {
    D_INSUFFICIENT_POINTS: "采样点数不足，无法构成有效标定曲线",
    D_SNUG_NOT_REACHED: "扭矩未达到贴合阈值，无法定位贴合点",
    D_SEGMENT_TOO_SHORT: "贴合后单调加载段点数不足，无法拟合",
    D_CURVE_REGRESSION: "贴合后加载段出现扭矩/载荷回退",
    D_TARGET_NOT_COVERED: "单调加载段最大载荷未覆盖目标载荷点",
    D_OUT_OF_CHANNEL_RANGE: "扭矩/载荷读数越出测量通道量程",
}


def _defect(reason: str, interval: dict) -> dict:
    return {"reason": reason, "message": DEFECT_MESSAGES[reason],
            "interval": interval}


# ---------------------------------------------------------------- 冻结输入归一化

def normalize_frozen(payload: dict, *, tool_range_min_nm: float,
                     tool_range_max_nm: float) -> dict:
    """把建版请求归一化为内部冻结参数（N·m/kN/deg），并快照工艺工具量程。

    通道量程按声明单位换算到内部单位；批次身份五元组原样冻结（一致性判定
    只做等值比较，不做单位换算）。
    """
    tf = TORQUE_TO_NM[payload.get("torque_unit", "Nm")]
    lf = LOAD_TO_KN[payload.get("load_unit", "kN")]
    tc, lc = payload["torque_channel"], payload["load_channel"]
    return {
        "identity": dict(payload["identity"]),
        "nominal_diameter_mm": payload["nominal_diameter_mm"],
        "min_specimens": payload["min_specimens"],
        "assemblies_per_specimen": payload["assemblies_per_specimen"],
        "target_load_kn": payload["target_load"] * lf,
        "load_tolerance_pct": payload["load_tolerance_pct"],
        "snug_torque_nm": payload["snug_torque"] * tf,
        "units": {"torque_unit": payload.get("torque_unit", "Nm"),
                  "load_unit": payload.get("load_unit", "kN"),
                  "angle_unit": payload.get("angle_unit", "deg")},
        "torque_channel": {
            "channel_id": tc["channel_id"],
            "range_min_nm": tc["range_min"] * tf,
            "range_max_nm": tc["range_max"] * tf,
            "calibration_until": tc["calibration_valid_until"],
        },
        "load_channel": {
            "channel_id": lc["channel_id"],
            "range_min_kn": lc["range_min"] * lf,
            "range_max_kn": lc["range_max"] * lf,
            "calibration_until": lc["calibration_valid_until"],
        },
        "tool_range_nm": [tool_range_min_nm, tool_range_max_nm],
    }


# ---------------------------------------------------------------- 逐装配次分析

def locate_seating(torques: list[float], snug_torque_nm: float) -> int | None:
    """自动贴合点：扭矩首次达到贴合阈值的采样索引；未达到返回 None。"""
    for i, t in enumerate(torques):
        if t >= snug_torque_nm:
            return i
    return None


def monotonic_segment(torques: list[float], loads: list[float],
                      start_idx: int) -> tuple[int, int | None]:
    """贴合后的单调加载段：自 start_idx 起扭矩与载荷均单调不减的最长前缀。

    返回 (末索引, 回退索引)；回退索引为首个回退点（扭矩或载荷下降超容差），
    无回退为 None。段含起点与末索引。
    """
    end = start_idx
    regression = None
    for i in range(start_idx + 1, len(torques)):
        if (torques[i] < torques[i - 1] - REGRESSION_TOL_NM
                or loads[i] < loads[i - 1] - REGRESSION_TOL_KN):
            regression = i
            break
        end = i
    return end, regression


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float, float | None]:
    """最小二乘拟合 y = a·x + b；返回 (斜率, 截距, R²)。x 无离散度时斜率取 0。"""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, my, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    a = sxy / sxx
    b = my - a * mx
    sst = sum((y - my) ** 2 for y in ys)
    r2 = None
    if sst > 0:
        sse = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys))
        r2 = max(0.0, 1.0 - sse / sst)
    return a, b, r2


def _parse_day(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def analyze_assembly(frozen: dict, assembly: dict, *,
                     seating_override: int | None = None) -> dict:
    """分析单个装配次：单位换算→贴合点→单调段→拟合→目标载荷点扭矩系数。

    assembly 为原始提交 {"assembly_index", "measured_at", "points",
    "excluded", "exclusion_reason"}（提交单位）；返回指标与缺陷列表
    （defects 非空即该装配次不可用）。被排除的装配次不参与分析。
    """
    idx = assembly["assembly_index"]
    out = {"assembly_index": idx, "measured_at": assembly["measured_at"],
           "excluded": bool(assembly.get("excluded")),
           "exclusion_reason": assembly.get("exclusion_reason")}
    if out["excluded"]:
        return {**out, "usable": False, "defects": [], "fit": None,
                "seating": None, "segment": None, "k_at_target": None,
                "torque_at_target_nm": None, "usable_load_range_kn": None}

    tf = TORQUE_TO_NM[frozen["units"]["torque_unit"]]
    lf = LOAD_TO_KN[frozen["units"]["load_unit"]]
    af = ANGLE_TO_DEG[frozen["units"]["angle_unit"]]
    points = assembly["points"]
    torques = [p["torque"] * tf for p in points]
    loads = [p["load"] * lf for p in points]
    angles = [p["angle"] * af for p in points]
    n = len(points)
    defects: list[dict] = []

    # 测量通道校准失效（含当日有效）：测量日期晚于任一通道校准有效期
    measured_day = _parse_day(assembly["measured_at"])
    expired: list[str] = []
    for key, label in (("torque_channel", "扭矩"), ("load_channel", "载荷")):
        ch = frozen[key]
        if measured_day > _parse_day(ch["calibration_until"]):
            expired.append(f"{label}通道 {ch['channel_id']}"
                           f"（校准至 {ch['calibration_until']}）")
    channel_expired = bool(expired)

    # 读数越出通道量程（按连续区间返回）
    tq_ch, ld_ch = frozen["torque_channel"], frozen["load_channel"]
    for a, b in _out_of_range_runs(torques, tq_ch["range_min_nm"],
                                   tq_ch["range_max_nm"]):
        defects.append(_defect(D_OUT_OF_CHANNEL_RANGE, {
            "channel": tq_ch["channel_id"], "quantity": "torque",
            "start_index": a, "end_index": b,
            "min_torque_nm": round(min(torques[a:b + 1]), 4),
            "max_torque_nm": round(max(torques[a:b + 1]), 4),
            "range_nm": [round(tq_ch["range_min_nm"], 4),
                         round(tq_ch["range_max_nm"], 4)]}))
    for a, b in _out_of_range_runs(loads, ld_ch["range_min_kn"],
                                   ld_ch["range_max_kn"]):
        defects.append(_defect(D_OUT_OF_CHANNEL_RANGE, {
            "channel": ld_ch["channel_id"], "quantity": "load",
            "start_index": a, "end_index": b,
            "min_load_kn": round(min(loads[a:b + 1]), 4),
            "max_load_kn": round(max(loads[a:b + 1]), 4),
            "range_kn": [round(ld_ch["range_min_kn"], 4),
                         round(ld_ch["range_max_kn"], 4)]}))

    if n < MIN_POINTS:
        defects.append(_defect(D_INSUFFICIENT_POINTS,
                               {"point_count": n, "min_points": MIN_POINTS}))
        return _assembly_result(out, defects, channel_expired)

    # 贴合点（人工修订优先，否则自动定位）
    if seating_override is not None and 0 <= seating_override < n:
        snug_idx, snug_source = seating_override, "manual"
    else:
        snug_idx = locate_seating(torques, frozen["snug_torque_nm"])
        snug_source = "auto"
    if snug_idx is None:
        defects.append(_defect(D_SNUG_NOT_REACHED, {
            "snug_torque_nm": round(frozen["snug_torque_nm"], 4),
            "peak_torque_nm": round(max(torques), 4)}))
        return _assembly_result(out, defects, channel_expired)

    # 贴合后的单调加载段（曲线回退即段终止并记缺陷）
    end_idx, regression_idx = monotonic_segment(torques, loads, snug_idx)
    if regression_idx is not None:
        defects.append(_defect(D_CURVE_REGRESSION, {
            "point_index": regression_idx,
            "torque_nm": [round(torques[regression_idx - 1], 4),
                          round(torques[regression_idx], 4)],
            "load_kn": [round(loads[regression_idx - 1], 4),
                        round(loads[regression_idx], 4)]}))

    seg_t = torques[snug_idx:end_idx + 1]
    seg_f = loads[snug_idx:end_idx + 1]
    if len(seg_t) < MIN_SEGMENT_POINTS:
        defects.append(_defect(D_SEGMENT_TOO_SHORT, {
            "segment_points": len(seg_t),
            "min_segment_points": MIN_SEGMENT_POINTS}))
        return _assembly_result(out, defects, channel_expired,
                                snug_idx=snug_idx, snug_source=snug_source,
                                end_idx=end_idx)

    slope, intercept, r2 = _linfit(seg_f, seg_t)
    f_lo, f_hi = seg_f[0], seg_f[-1]
    target = frozen["target_load_kn"]
    if not (f_lo - REGRESSION_TOL_KN <= target <= f_hi + REGRESSION_TOL_KN):
        defects.append(_defect(D_TARGET_NOT_COVERED, {
            "target_load_kn": round(target, 4),
            "segment_load_range_kn": [round(f_lo, 4), round(f_hi, 4)]}))
    d = frozen["nominal_diameter_mm"]
    t_at_target = slope * target + intercept
    fit = {"slope_nm_per_kn": round(slope, 6),
           "intercept_nm": round(intercept, 6),
           "r_squared": round(r2, 6) if r2 is not None else None,
           "points": len(seg_t)}
    return _assembly_result(
        out, defects, channel_expired, snug_idx=snug_idx,
        snug_source=snug_source, end_idx=end_idx, fit=fit,
        torque_at_target=round(t_at_target, 4),
        k_at_target=round(t_at_target / (target * d), 6),
        load_range=[round(f_lo, 4), round(f_hi, 4)],
        angle_span=round(angles[end_idx] - angles[snug_idx], 4))


def _out_of_range_runs(values: list[float], lo: float,
                       hi: float) -> list[tuple[int, int]]:
    """越出量程（低于下限或超过上限）的连续索引区间。"""
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(values):
        bad = v < lo or v > hi
        if bad and start is None:
            start = i
        elif not bad and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(values) - 1))
    return runs


def _assembly_result(out: dict, defects: list[dict], channel_expired: bool, *,
                     snug_idx: int | None = None, snug_source: str | None = None,
                     end_idx: int | None = None, fit: dict | None = None,
                     torque_at_target: float | None = None,
                     k_at_target: float | None = None,
                     load_range: list[float] | None = None,
                     angle_span: float | None = None) -> dict:
    segment = None
    if snug_idx is not None and end_idx is not None:
        segment = {"start_index": snug_idx, "end_index": end_idx,
                   "points": end_idx - snug_idx + 1}
    return {**out,
            "usable": not defects and not channel_expired,
            "defects": defects,
            "channel_calibration_expired": channel_expired,
            "seating": ({"index": snug_idx, "source": snug_source}
                        if snug_idx is not None else None),
            "segment": segment,
            "post_seating_angle_deg": angle_span,
            "fit": fit,
            "torque_at_target_nm": torque_at_target,
            "k_at_target": k_at_target,
            "usable_load_range_kn": load_range}


# ---------------------------------------------------------------- 整版评估

def evaluate_calibration(frozen: dict, payload: dict) -> dict:
    """整版评估：逐试样/装配次分析 + 批统计 + 推荐扭矩窗口 + 草稿阻断项。

    payload 为原始提交 {"specimens": [...], "seating_overrides": {...}}；
    人工决定（贴合点覆盖、排除）随 payload 冻结。阻断项非空即不可确认，
    标定停在草稿。
    """
    overrides = payload.get("seating_overrides") or {}
    required_assemblies = list(range(1, frozen["assemblies_per_specimen"] + 1))
    identity = frozen["identity"]
    d_nom = frozen["nominal_diameter_mm"]

    specimen_views: list[dict] = []
    blockers: set[str] = set()
    gap_items: list[dict] = []

    for spec in payload["specimens"]:
        sno = spec["specimen_no"]
        excluded = bool(spec.get("excluded"))
        reason = spec.get("exclusion_reason")
        gaps: list[dict] = []

        # 异常值剔除无理由（试样级）
        if excluded and not (reason or "").strip():
            blockers.add(B_UNJUSTIFIED_EXCLUSION)
            gaps.append({"reason": B_UNJUSTIFIED_EXCLUSION,
                         "message": f"试样 {sno} 被剔除但未注明理由"})

        # 几何一致性：声明直径与冻结公称直径
        geometry_ok = abs(spec["diameter_mm"] - d_nom) <= GEOMETRY_TOL_REL * d_nom
        if not geometry_ok and not excluded:
            blockers.add(B_GEOMETRY_INCONSISTENT)
            gaps.append({"reason": B_GEOMETRY_INCONSISTENT,
                         "message": f"试样 {sno} 声明直径 {spec['diameter_mm']}mm "
                                    f"与冻结公称直径 {d_nom}mm 不一致"})

        # 批次归属：试样批次须与冻结批次身份一致（表面处理/润滑状态为
        # 过程状态，试样级只核对实物批次）
        batch_ok = True
        for key, label in (("bolt_batch", "螺栓批次"),
                           ("nut_batch", "螺母批次"),
                           ("lubricant_batch", "润滑剂批次")):
            if spec.get(key) != identity.get(key):
                batch_ok = False
                if not excluded:
                    blockers.add(B_BATCH_COVERAGE)
                    gaps.append({"reason": B_BATCH_COVERAGE,
                                 "message": f"试样 {sno} 的{label} {spec.get(key)!r} "
                                            f"与冻结批次 {identity.get(key)!r} 不符"})

        # 逐装配次分析（人工贴合点覆盖随版冻结）
        assemblies: list[dict] = []
        present: set[int] = set()
        for asm in spec["assemblies"]:
            key = f"{sno}#{asm['assembly_index']}"
            view = analyze_assembly(frozen, asm,
                                    seating_override=overrides.get(key))
            if view["excluded"]:
                if not (view["exclusion_reason"] or "").strip():
                    blockers.add(B_UNJUSTIFIED_EXCLUSION)
                    gaps.append({"reason": B_UNJUSTIFIED_EXCLUSION,
                                 "message": f"试样 {sno} 第 {asm['assembly_index']} "
                                            "次装配被剔除但未注明理由"})
            else:
                present.add(asm["assembly_index"])
                if view["channel_calibration_expired"]:
                    blockers.add(B_CHANNEL_CAL_EXPIRED)
                    gaps.append({"reason": B_CHANNEL_CAL_EXPIRED,
                                 "message": f"试样 {sno} 第 {asm['assembly_index']} "
                                            "次装配测量日晚于测量通道校准有效期"})
                if any(df["reason"] == D_CURVE_REGRESSION for df in view["defects"]):
                    blockers.add(B_CURVE_REGRESSION)
                    gaps.append({"reason": B_CURVE_REGRESSION,
                                 "message": f"试样 {sno} 第 {asm['assembly_index']} "
                                            "次装配贴合后出现曲线回退"})
            assemblies.append(view)

        # 装配次数覆盖：未排除的装配次须覆盖全部冻结次数
        missing = [i for i in required_assemblies if i not in present]
        if missing and not excluded:
            blockers.add(B_BATCH_COVERAGE)
            gaps.append({"reason": B_BATCH_COVERAGE,
                         "message": f"试样 {sno} 缺第 {missing} 次装配，"
                                    f"装配次数覆盖不全（要求 {required_assemblies}）"})

        accepted = (not excluded and geometry_ok and batch_ok and not missing
                    and all(a["usable"] for a in assemblies if not a["excluded"]))
        specimen_views.append({
            "specimen_no": sno, "diameter_mm": spec["diameter_mm"],
            "excluded": excluded, "exclusion_reason": reason,
            "geometry_ok": geometry_ok, "batch_ok": batch_ok,
            "missing_assemblies": missing,
            "accepted": accepted, "assemblies": assemblies, "gaps": gaps,
        })
        gap_items.extend({**g, "specimen_no": sno} for g in gaps)

    accepted_specs = [s for s in specimen_views if s["accepted"]]
    if len(accepted_specs) < frozen["min_specimens"]:
        blockers.add(B_INSUFFICIENT_SPECIMENS)
        gap_items.append({"reason": B_INSUFFICIENT_SPECIMENS, "specimen_no": None,
                          "message": f"合格试样 {len(accepted_specs)} 个，低于冻结下限 "
                                     f"{frozen['min_specimens']} 个"})

    stats = _batch_stats(frozen, accepted_specs)
    window = _recommended_window(frozen, stats)
    if (window is not None and not window["within_tool_range"]):
        blockers.add(B_WINDOW_OUT_OF_TOOL)
        gap_items.append({"reason": B_WINDOW_OUT_OF_TOOL, "specimen_no": None,
                          "message": f"推荐扭矩窗口 {window['window_nm']} N·m 越出"
                                     f"工具量程 {window['tool_range_nm']} N·m"})

    return {
        "confirmable": not blockers,
        "blockers": sorted(blockers),
        "blocker_messages": [BLOCKER_MESSAGES[b] for b in sorted(blockers)],
        "gaps": gap_items,
        "specimens": specimen_views,
        "accepted_specimen_count": len(accepted_specs),
        "required_specimen_count": frozen["min_specimens"],
        "stats": stats,
        "recommended_torque": window,
    }


def _batch_stats(frozen: dict, accepted_specs: list[dict]) -> dict:
    """批内统计：目标载荷点 K 的离散度、重复装配漂移与可用载荷区间。"""
    target = frozen["target_load_kn"]
    tol = frozen["load_tolerance_pct"]
    k_all: list[float] = []
    per_specimen: list[dict] = []
    drifts: list[dict] = []
    lo_bound: float | None = None
    hi_bound: float | None = None

    for spec in accepted_specs:
        runs = [a for a in spec["assemblies"] if a["usable"]]
        runs.sort(key=lambda a: a["assembly_index"])
        ks = [a["k_at_target"] for a in runs]
        k_all.extend(ks)
        per_specimen.append({"specimen_no": spec["specimen_no"],
                             "assemblies_used": [a["assembly_index"] for a in runs],
                             "k_values": ks,
                             "k_mean": round(sum(ks) / len(ks), 6)})
        if len(ks) >= 2:  # 重复装配漂移：末次相对首次的 K 变化率
            first, last = ks[0], ks[-1]
            drifts.append({"specimen_no": spec["specimen_no"],
                           "k_first": first, "k_last": last,
                           "drift_pct": round((last - first) / first * 100.0, 4)})
        for a in runs:
            lo, hi = a["usable_load_range_kn"]
            lo_bound = lo if lo_bound is None else max(lo_bound, lo)
            hi_bound = hi if hi_bound is None else min(hi_bound, hi)

    n = len(k_all)
    if n:
        mean = sum(k_all) / n
        var = (sum((k - mean) ** 2 for k in k_all) / (n - 1)) if n >= 2 else 0.0
        std = var ** 0.5
        stats = {
            "target_load_kn": round(target, 4),
            "target_load_band_kn": [round(target * (1 - tol / 100.0), 4),
                                    round(target * (1 + tol / 100.0), 4)],
            "sample_count": n,
            "k_mean": round(mean, 6),
            "k_std": round(std, 6),
            "k_cv_pct": round(std / mean * 100.0, 4) if mean > 0 else None,
            "k_min": round(min(k_all), 6),
            "k_max": round(max(k_all), 6),
            "per_specimen": per_specimen,
            "assembly_drift": {
                "per_specimen": drifts,
                "max_abs_drift_pct": (round(max(abs(d["drift_pct"]) for d in drifts), 4)
                                      if drifts else None),
            },
            "usable_load_range_kn": ([round(lo_bound, 4), round(hi_bound, 4)]
                                     if lo_bound is not None else None),
        }
    else:
        stats = {
            "target_load_kn": round(target, 4),
            "target_load_band_kn": [round(target * (1 - tol / 100.0), 4),
                                    round(target * (1 + tol / 100.0), 4)],
            "sample_count": 0, "k_mean": None, "k_std": None, "k_cv_pct": None,
            "k_min": None, "k_max": None, "per_specimen": [],
            "assembly_drift": {"per_specimen": [], "max_abs_drift_pct": None},
            "usable_load_range_kn": None,
        }
    return stats


def _recommended_window(frozen: dict, stats: dict) -> dict | None:
    """推荐扭矩窗口：该批紧固件达到目标载荷实际需要的扭矩范围。

    T = K·d·F（1 kN·mm = 1 N·m，K 无量纲）；窗口取批内 K 极值在目标载荷点
    的扭矩范围，名义设定取 K 均值。窗口任一端越出工艺工具量程即不可确认。
    """
    if not stats["sample_count"]:
        return None
    d = frozen["nominal_diameter_mm"]
    target = stats["target_load_kn"]
    t_lo = stats["k_min"] * d * target
    t_hi = stats["k_max"] * d * target
    t_nom = stats["k_mean"] * d * target
    tool_lo, tool_hi = frozen["tool_range_nm"]
    within = tool_lo <= t_lo and t_hi <= tool_hi
    return {
        "window_nm": [round(t_lo, 4), round(t_hi, 4)],
        "nominal_nm": round(t_nom, 4),
        "k_min": stats["k_min"], "k_max": stats["k_max"],
        "k_mean": stats["k_mean"],
        "diameter_mm": d, "target_load_kn": target,
        "tool_range_nm": [tool_lo, tool_hi],
        "within_tool_range": within,
    }


# ---------------------------------------------------------------- 版本差异

def _asm_key(specimen_no: str, assembly_index: int) -> str:
    return f"{specimen_no}#{assembly_index}"


def _payload_signature(payload: dict) -> dict:
    """试样数据签名：逐试样/装配次的点数与剔除状态（版本差异用）。"""
    sig: dict[str, dict] = {}
    for spec in payload["specimens"]:
        sig[spec["specimen_no"]] = {
            "diameter_mm": spec["diameter_mm"],
            "excluded": bool(spec.get("excluded")),
            "assemblies": {a["assembly_index"]: len(a["points"])
                           for a in spec["assemblies"]},
        }
    return sig


def diff_calibrations(old: dict, new: dict) -> dict:
    """相邻修订差异：冻结参数、试样数据、人工决定（贴合点/剔除）与结果变化。

    old/new 为 {"frozen", "payload", "analysis"} 视图；原始点、拟合参数与
    人工决定保留在各版本详情中，差异只给索引与摘要。
    """
    param_changes: dict[str, dict] = {}
    for field in ("nominal_diameter_mm", "min_specimens", "assemblies_per_specimen",
                  "target_load_kn", "load_tolerance_pct", "snug_torque_nm",
                  "torque_channel", "load_channel", "tool_range_nm"):
        if old["frozen"][field] != new["frozen"][field]:
            param_changes[field] = {"from": old["frozen"][field],
                                    "to": new["frozen"][field]}
    if old["frozen"]["identity"] != new["frozen"]["identity"]:
        param_changes["identity"] = {"from": old["frozen"]["identity"],
                                     "to": new["frozen"]["identity"]}

    old_sig = _payload_signature(old["payload"])
    new_sig = _payload_signature(new["payload"])
    specimen_changes = {
        "added": sorted(set(new_sig) - set(old_sig)),
        "removed": sorted(set(old_sig) - set(new_sig)),
        "changed": sorted(
            s for s in set(old_sig) & set(new_sig) if old_sig[s] != new_sig[s]),
    }

    old_ov = old["payload"].get("seating_overrides") or {}
    new_ov = new["payload"].get("seating_overrides") or {}
    seating_changes = {
        k: {"from": old_ov.get(k), "to": new_ov.get(k)}
        for k in sorted(set(old_ov) | set(new_ov)) if old_ov.get(k) != new_ov.get(k)
    }

    def exclusions(payload: dict) -> dict:
        out: dict[str, str | None] = {}
        for spec in payload["specimens"]:
            if spec.get("excluded"):
                out[spec["specimen_no"]] = spec.get("exclusion_reason")
            for a in spec["assemblies"]:
                if a.get("excluded"):
                    out[_asm_key(spec["specimen_no"], a["assembly_index"])] = \
                        a.get("exclusion_reason")
        return out

    old_ex, new_ex = exclusions(old["payload"]), exclusions(new["payload"])
    exclusion_changes = {
        "added": {k: new_ex[k] for k in sorted(set(new_ex) - set(old_ex))},
        "removed": sorted(set(old_ex) - set(new_ex)),
        "reason_changed": {k: {"from": old_ex[k], "to": new_ex[k]}
                           for k in sorted(set(old_ex) & set(new_ex))
                           if old_ex[k] != new_ex[k]},
    }

    old_s, new_s = old["analysis"]["stats"], new["analysis"]["stats"]
    result_changes = {
        "k_mean": {"from": old_s["k_mean"], "to": new_s["k_mean"]},
        "k_cv_pct": {"from": old_s["k_cv_pct"], "to": new_s["k_cv_pct"]},
        "max_abs_drift_pct": {
            "from": old_s["assembly_drift"]["max_abs_drift_pct"],
            "to": new_s["assembly_drift"]["max_abs_drift_pct"]},
        "recommended_window_nm": {
            "from": (old["analysis"]["recommended_torque"] or {}).get("window_nm"),
            "to": (new["analysis"]["recommended_torque"] or {}).get("window_nm")},
        "blockers": {"from": old["analysis"]["blockers"],
                     "to": new["analysis"]["blockers"]},
    }
    return {
        "param_changes": param_changes,
        "specimen_changes": specimen_changes,
        "decision_changes": {"seating_overrides": seating_changes,
                             "exclusions": exclusion_changes},
        "result_changes": result_changes,
        "change_note": new.get("change_note"),
    }
