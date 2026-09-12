"""扭矩-转角轨迹分析：单位统一、跨零展开、贴合点定位与缺陷判定（纯函数）。

数字扳手终值合格并不代表过程合格：套筒打滑、螺纹咬伤、垫片突然就位都藏在
扭矩-转角轨迹里。不同设备的单位、角度零点与采样频率不同，分析前先统一单位
（s / N·m / deg），按批准旋向展开跨零角度并换算为旋向转角（>=0 递增），再：

1. 缺陷判定（任一命中即不得进入复核结论，并返回具体区间）：
   时标倒退、点数不足、采样间隔超限、读数越出工具量程、角度反转、
   贴合点无法定位、提前达峰、分段斜率突降；
2. 指标计算：贴合点、贴合后转角、峰值扭矩、扭矩功（梯形积分）、分段斜率；
3. 离群判定：可用轨迹的贴合后转角越出批准范围即为离群（不判废，
   计入整圈离群率，由 evaluate_curve_review 汇总）。
"""
from __future__ import annotations

import math

MIN_POINTS = 10            # 构成有效轨迹的最少采样点数
SEGMENTS = 4               # 贴合后转角的分段数（分段斜率）
REVERSAL_TOL_DEG = 0.05    # 角度反转判定容差（吸收编码器噪声）
EARLY_PEAK_DROP_PCT = 2.0  # 峰值后回落超过该百分比判提前达峰

TIME_TO_S = {"s": 1.0, "ms": 1e-3}
TORQUE_TO_NM = {"Nm": 1.0, "Nmm": 1e-3, "lbfft": 1.3558179483314004}
ANGLE_TO_DEG = {"deg": 1.0, "rev": 360.0, "rad": 180.0 / math.pi}

# 轨迹缺陷（任一存在即该修订不可用，不得进入复核结论）
D_TIME_REGRESSION = "time_regression"                # 时标倒退
D_INSUFFICIENT_POINTS = "insufficient_points"        # 点数不足
D_SAMPLING_INTERVAL = "sampling_interval_exceeded"   # 采样间隔超过锁定上限
D_OUT_OF_RANGE = "reading_out_of_range"              # 读数越出工具量程
D_ANGLE_REVERSAL = "angle_reversal"                  # 角度反转（反旋向回退）
D_SNUG_NOT_REACHED = "snug_not_reached"              # 贴合点无法定位
D_EARLY_PEAK = "early_peak"                          # 提前达峰
D_SLOPE_COLLAPSE = "slope_collapse"                  # 分段斜率突降

DEFECT_MESSAGES = {
    D_TIME_REGRESSION: "时标倒退，采样时间轴非单调",
    D_INSUFFICIENT_POINTS: "采样点数不足，无法构成有效轨迹",
    D_SAMPLING_INTERVAL: "采样间隔超过批准锁定的上限",
    D_OUT_OF_RANGE: "扭矩读数越出工具量程",
    D_ANGLE_REVERSAL: "转角出现反旋向回退",
    D_SNUG_NOT_REACHED: "扭矩未达到贴合扭矩，无法定位贴合点",
    D_EARLY_PEAK: "峰值扭矩提前出现，终值明显回落",
    D_SLOPE_COLLAPSE: "分段斜率突降超过锁定限值",
}


def _defect(reason: str, interval: dict) -> dict:
    return {"reason": reason, "message": DEFECT_MESSAGES[reason], "interval": interval}


def unwrap_angles(angles_deg: list[float], direction: str) -> list[float]:
    """展开跨零角度并换算为旋向转角（首点为 0，沿旋向递增为正）。

    相邻采样跨越角度零点（如 cw 下 350°→10°）时按 ±360° 修正偏移；
    ccw 旋向取负，使两种旋向的转角都可直接比较（消除设备角度零点差异）。
    """
    unwrapped = [angles_deg[0]]
    offset = 0.0
    for prev, cur in zip(angles_deg, angles_deg[1:]):
        d = cur - prev
        if d < -180.0:
            offset += 360.0
        elif d > 180.0:
            offset -= 360.0
        unwrapped.append(cur + offset)
    base = unwrapped[0]
    sign = 1.0 if direction == "cw" else -1.0
    return [sign * (u - base) for u in unwrapped]


def locate_snug(torques: list[float], snug_torque: float) -> int | None:
    """自动贴合点：扭矩首次达到贴合扭矩的采样索引；未达到返回 None。"""
    for i, t in enumerate(torques):
        if t >= snug_torque:
            return i
    return None


def _work_j(torques: list[float], rotation: list[float]) -> float:
    """扭矩功：梯形积分 Σ(T_i+T_{i+1})/2·Δθ；deg 换算 rad 后为焦耳。"""
    w = 0.0
    for i in range(1, len(torques)):
        w += 0.5 * (torques[i] + torques[i - 1]) * (rotation[i] - rotation[i - 1])
    return w * math.pi / 180.0


def segment_slopes(rotation: list[float], torques: list[float],
                   start_idx: int, n_seg: int = SEGMENTS) -> list[dict]:
    """贴合后转角均分为 n_seg 段，逐段端点法求斜率 (N·m)/deg。

    段内不足两点或转角无推进时斜率为 None（不参与突降判定）。
    """
    r0, r1 = rotation[start_idx], rotation[-1]
    if r1 - r0 <= 0:
        return []
    width = (r1 - r0) / n_seg
    segs: list[dict] = []
    for k in range(n_seg):
        lo, hi = r0 + k * width, r0 + (k + 1) * width
        idxs = [i for i in range(start_idx, len(rotation))
                if lo <= rotation[i] < hi or (k == n_seg - 1 and rotation[i] == r1)]
        slope = None
        if len(idxs) >= 2 and rotation[idxs[-1]] > rotation[idxs[0]]:
            slope = round((torques[idxs[-1]] - torques[idxs[0]])
                          / (rotation[idxs[-1]] - rotation[idxs[0]]), 6)
        segs.append({"segment": k + 1, "start_deg": round(lo, 4),
                     "end_deg": round(hi, 4), "slope_nm_per_deg": slope,
                     "points": len(idxs)})
    return segs


def _out_of_range_runs(torques: list[float], tool_max: float) -> list[tuple[int, int]]:
    """越出工具量程（负值或超过量程上限）的连续索引区间。"""
    runs: list[tuple[int, int]] = []
    start = None
    for i, t in enumerate(torques):
        bad = t < 0 or t > tool_max
        if bad and start is None:
            start = i
        elif not bad and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(torques) - 1))
    return runs


def analyze_curve(proc: dict, points: list[dict], *, time_unit: str,
                  torque_unit: str, angle_unit: str,
                  snug_override: int | None = None) -> dict:
    """分析一条扭矩-转角轨迹。proc 须含批准锁定的曲线参数与 tool_range_max。

    points 为原始提交点 [{"t", "torque", "angle"}]（提交单位）；返回指标与
    缺陷列表（含具体区间）。defects 非空即该修订不可用。
    """
    tf = TIME_TO_S[time_unit]
    qf = TORQUE_TO_NM[torque_unit]
    af = ANGLE_TO_DEG[angle_unit]
    ts = [p["t"] * tf for p in points]
    tq = [p["torque"] * qf for p in points]
    rotation = unwrap_angles([p["angle"] * af for p in points], proc["curve_direction"])
    n = len(points)
    defects: list[dict] = []

    if n < MIN_POINTS:
        defects.append(_defect(D_INSUFFICIENT_POINTS,
                               {"point_count": n, "min_points": MIN_POINTS}))
        return _result(proc, ts, tq, rotation, defects)

    # 时标倒退 / 采样间隔超限
    max_dt = proc["max_sample_interval_ms"] / 1000.0
    for i in range(1, n):
        dt = ts[i] - ts[i - 1]
        if dt < 0:
            defects.append(_defect(D_TIME_REGRESSION, {
                "point_index": i,
                "t_s": [round(ts[i - 1], 6), round(ts[i], 6)]}))
        elif dt > max_dt:
            defects.append(_defect(D_SAMPLING_INTERVAL, {
                "point_index": i,
                "t_s": [round(ts[i - 1], 6), round(ts[i], 6)],
                "interval_s": round(dt, 6), "max_interval_s": max_dt}))

    # 读数越出工具量程（负值或超过量程上限），按连续区间返回
    tool_max = proc["tool_range_max"]
    for a, b in _out_of_range_runs(tq, tool_max):
        defects.append(_defect(D_OUT_OF_RANGE, {
            "start_index": a, "end_index": b,
            "min_torque_nm": round(min(tq[a:b + 1]), 4),
            "max_torque_nm": round(max(tq[a:b + 1]), 4),
            "tool_range_max_nm": tool_max}))

    # 角度反转（反旋向回退超过容差）
    for i in range(1, n):
        if rotation[i] < rotation[i - 1] - REVERSAL_TOL_DEG:
            defects.append(_defect(D_ANGLE_REVERSAL, {
                "point_index": i,
                "rotation_deg": [round(rotation[i - 1], 4), round(rotation[i], 4)]}))

    # 贴合点（人工修订优先，否则自动定位）
    if snug_override is not None and 0 <= snug_override < n:
        snug_idx, snug_source = snug_override, "manual"
    else:
        snug_idx, snug_source = locate_snug(tq, proc["snug_torque"]), "auto"
    if snug_idx is None:
        defects.append(_defect(D_SNUG_NOT_REACHED, {
            "snug_torque_nm": proc["snug_torque"],
            "peak_torque_nm": round(max(tq), 4)}))

    # 峰值与提前达峰（峰值后终值回落超过容差）
    peak_idx = max(range(n), key=lambda i: tq[i])
    peak = tq[peak_idx]
    if peak_idx < n - 1 and peak - tq[-1] > EARLY_PEAK_DROP_PCT / 100.0 * peak:
        defects.append(_defect(D_EARLY_PEAK, {
            "peak_index": peak_idx, "end_index": n - 1,
            "peak_torque_nm": round(peak, 4),
            "final_torque_nm": round(tq[-1], 4)}))

    # 分段斜率与斜率突降
    segments: list[dict] = []
    if snug_idx is not None:
        segments = segment_slopes(rotation, tq, snug_idx)
        for prev, cur in zip(segments, segments[1:]):
            s0, s1 = prev["slope_nm_per_deg"], cur["slope_nm_per_deg"]
            if (s0 is not None and s1 is not None
                    and s1 < s0 - proc["slope_drop_limit"]):
                defects.append(_defect(D_SLOPE_COLLAPSE, {
                    "segment": cur["segment"],
                    "rotation_deg": [prev["start_deg"], cur["end_deg"]],
                    "slope_prev_nm_per_deg": s0,
                    "slope_nm_per_deg": s1,
                    "drop_limit": proc["slope_drop_limit"]}))

    return _result(proc, ts, tq, rotation, defects, snug_idx=snug_idx,
                   snug_source=snug_source if snug_idx is not None else None,
                   peak_idx=peak_idx, segments=segments)


def _result(proc: dict, ts: list[float], tq: list[float], rotation: list[float],
            defects: list[dict], *, snug_idx: int | None = None,
            snug_source: str | None = None, peak_idx: int | None = None,
            segments: list[dict] | None = None) -> dict:
    n = len(ts)
    if peak_idx is None and n:
        peak_idx = max(range(n), key=lambda i: tq[i])
    post_snug = in_range = work_post = None
    if snug_idx is not None and n >= 2:
        post_snug = round(rotation[-1] - rotation[snug_idx], 4)
        in_range = bool(proc["post_snug_angle_min_deg"] <= post_snug
                        <= proc["post_snug_angle_max_deg"])
        work_post = round(_work_j(tq[snug_idx:], rotation[snug_idx:]), 4)
    return {
        "point_count": n,
        "duration_s": round(ts[-1] - ts[0], 6) if n >= 2 else 0.0,
        "direction": proc["curve_direction"],
        "snug_torque_nm": proc["snug_torque"],
        "snug_index": snug_idx,
        "snug_source": snug_source,
        "rotation_total_deg": round(rotation[-1], 4) if n else None,
        "post_snug_angle_deg": post_snug,
        "post_snug_in_range": in_range,
        "peak_torque_nm": round(tq[peak_idx], 4) if peak_idx is not None else None,
        "peak_index": peak_idx,
        "final_torque_nm": round(tq[-1], 4) if n else None,
        "work_total_j": round(_work_j(tq, rotation), 4) if n >= 2 else None,
        "work_post_snug_j": work_post,
        "segment_slopes": segments or [],
        "defects": defects,
    }


def evaluate_curve_review(proc: dict, plan: list[dict], views: list[dict]) -> dict:
    """整圈轨迹复核：终轮每栓须有可用轨迹，且整圈离群率不超批准上限。

    views 为每栓当前采用修订的视图 [{bolt_no, curve_id, revision, record_id,
    usable, analysis}]。离群 = 可用但贴合后转角越出批准范围。
    """
    final_round = max(s["round_no"] for s in plan)
    required = sorted({s["bolt_no"] for s in plan if s["round_no"] == final_round})
    by_bolt = {v["bolt_no"]: v for v in views}
    bolts: list[dict] = []
    missing: list[int] = []
    unusable: list[int] = []
    outliers: list[int] = []
    for b in required:
        v = by_bolt.get(b)
        if v is None:
            bolts.append({"bolt_no": b, "state": "missing", "curve_id": None,
                          "revision": None, "record_id": None, "usable": False,
                          "outlier": False, "post_snug_angle_deg": None,
                          "defects": []})
            missing.append(b)
            continue
        a = v["analysis"]
        outlier = bool(v["usable"] and a.get("post_snug_in_range") is False)
        if not v["usable"]:
            state = "unusable"
            unusable.append(b)
        elif outlier:
            state = "outlier"
            outliers.append(b)
        else:
            state = "ok"
        bolts.append({"bolt_no": b, "state": state, "curve_id": v["curve_id"],
                      "revision": v["revision"], "record_id": v["record_id"],
                      "usable": v["usable"], "outlier": outlier,
                      "post_snug_angle_deg": a.get("post_snug_angle_deg"),
                      "defects": a.get("defects", [])})
    rate = round(len(outliers) / len(required) * 100.0, 4) if required else 0.0
    blockers: list[str] = []
    if missing:
        blockers.append("curve_missing")
    if unusable:
        blockers.append("curve_unusable")
    if rate > proc["max_outlier_rate_pct"]:
        blockers.append("outlier_rate_exceeded")
    return {
        "final_round": final_round,
        "required_bolts": required,
        "bolts": bolts,
        "missing_bolts": missing,
        "unusable_bolts": unusable,
        "outlier_bolts": outliers,
        "usable_count": sum(1 for x in bolts if x["usable"]),
        "outlier_rate_pct": rate,
        "max_outlier_rate_pct": proc["max_outlier_rate_pct"],
        "passed": not blockers,
        "blockers": blockers,
    }


def format_defect(defect: dict) -> str:
    """缺陷简述（含具体区间），供 SVG 与作业包展示。"""
    reason = defect["reason"]
    iv = defect.get("interval") or {}
    msg = DEFECT_MESSAGES.get(reason, reason)
    if reason == D_TIME_REGRESSION:
        return f"{msg}：点{iv['point_index']} 时标 {iv['t_s'][0]}→{iv['t_s'][1]}s"
    if reason == D_SAMPLING_INTERVAL:
        return (f"{msg}：点{iv['point_index']} 间隔 {iv['interval_s']}s"
                f"＞上限 {iv['max_interval_s']}s")
    if reason == D_OUT_OF_RANGE:
        return (f"{msg}：点{iv['start_index']}–{iv['end_index']} "
                f"读数 {iv['min_torque_nm']}~{iv['max_torque_nm']} N·m")
    if reason == D_ANGLE_REVERSAL:
        return (f"{msg}：点{iv['point_index']} 转角 "
                f"{iv['rotation_deg'][0]}→{iv['rotation_deg'][1]}°")
    if reason == D_EARLY_PEAK:
        return (f"{msg}：峰 {iv['peak_torque_nm']} N·m@点{iv['peak_index']}，"
                f"末值 {iv['final_torque_nm']} N·m")
    if reason == D_SLOPE_COLLAPSE:
        return (f"{msg}：段{iv['segment']}（{iv['rotation_deg'][0]}–"
                f"{iv['rotation_deg'][1]}°）斜率 {iv['slope_prev_nm_per_deg']}→"
                f"{iv['slope_nm_per_deg']} (N·m)/°")
    if reason == D_INSUFFICIENT_POINTS:
        return f"{msg}：{iv['point_count']}＜{iv['min_points']} 点"
    if reason == D_SNUG_NOT_REACHED:
        return (f"{msg}：贴合 {iv['snug_torque_nm']} N·m，"
                f"峰值仅 {iv['peak_torque_nm']} N·m")
    return msg
