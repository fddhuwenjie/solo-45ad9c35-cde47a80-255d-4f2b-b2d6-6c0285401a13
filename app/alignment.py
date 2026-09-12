"""装配对中预检：两法兰面相对倾斜、径向错边与垫片对中的拟合判定（纯函数）。

终拧扭矩与超声预紧力合格，并不能暴露"螺栓未受力即被强行拉拢"的装配缺陷：
管口附加应力、垫片偏心只存在于紧固之前的自由状态。预检在批准/开工前冻结
法兰与垫片几何及限值，接收按方位分布的测点，拟合：

1. 间隙面（两法兰面的相对倾斜）
       gap(θ) = c + a_x·sinθ + a_y·cosθ
   θ 为测点方位角（0=正上方，顺时针为正，与工艺编号方位一致）。
   平行度 = max−min = 2·|a|（全圆周拟合极值 c±|a|），
   倾角 = atan(|a|)，最大间隙方位 atan2(a_x, a_y)。
2. 径向偏移（活动法兰中心相对固定法兰中心的位移向量 t）
       radial(θ) = t_x·sinθ + t_y·cosθ
   径向错边 = |t|，径向跳动 TIR = 2|t|。
3. 垫片外缘位置 e_o（自法兰外缘向内量到垫片外缘）。垫片中心位移 g 时，
   u 侧垫片外缘随垫片远离该侧法兰外缘，向内量距增大：
       e_o(θ) = (R − r_o) + g_x·sinθ + g_y·cosθ
   垫片偏心 = |g|；垫片居中余量：
     流道侧（内缘不得侵入内孔）: (Gi − Db)/2 − |g|
     法兰面侧（外缘不得越出法兰面）: (D − Go)/2 − |g|
   侵入流道判据：|g| > (Gi − Db)/2。

测点方位重复、覆盖弧段不足、单位不一致或几何自相矛盾时**只列证据缺口**，
不产出伪造的拟合结论；超限、垫片侵入流道或需要螺栓强行拉拢时列为阻断项。
单位：除测点自带 length_unit 外，内部全部换算为 mm。
"""
from __future__ import annotations

import math

# 长度单位 -> mm
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}

MIN_POINTS = 4              # 至少四个按方位分布的测点
ARC_GAP_LIMIT_DEG = 180.0   # 最大空弧 > 180° 视为测点全部落在半圆内，覆盖不足
ANGLE_MATCH_TOL_DEG = 5.0   # 版本差异中测点方位匹配容差

# ------------------------------------------------------------ 证据缺口（只列证据，不判合格）
GAP_DUPLICATE_AZIMUTH = "duplicate_azimuth"            # 测点方位重复
GAP_INSUFFICIENT_ARC = "insufficient_arc_coverage"     # 覆盖弧段不足（空弧 > 180°）
GAP_INCONSISTENT_UNITS = "inconsistent_units"          # 测点之间长度单位不一致
GAP_NEGATIVE_GAP = "negative_axial_gap"                # 轴向间隙为负（两面自相交叠）
GAP_GASKET_OUTSIDE_FACE = "gasket_outside_face"        # 垫片边缘越出法兰面/内缘越过对侧边
GAP_RADIAL_OFFSET_IMPOSSIBLE = "radial_offset_impossible"  # 径向偏移大到几何不可能

GAP_MESSAGES = {
    GAP_DUPLICATE_AZIMUTH: "存在重复方位的测点，同一方位不得重复布点",
    GAP_INSUFFICIENT_ARC: "测点覆盖弧段不足：最大空弧超过 180°，测点全部落在半圆内",
    GAP_INCONSISTENT_UNITS: "各测点长度单位不一致，无法在同一几何模型下拟合",
    GAP_NEGATIVE_GAP: "轴向间隙为负：自由状态下两法兰面不应交叠，几何自相矛盾",
    GAP_GASKET_OUTSIDE_FACE: "垫片边缘位置越出法兰面（外缘超出法兰外缘或内缘越过对侧）",
    GAP_RADIAL_OFFSET_IMPOSSIBLE: "径向偏移绝对值超过法兰面半径，两法兰不可能如此错边",
}

# ------------------------------------------------------------ 阻断项（阻止工艺批准与开工）
BL_PARALLELISM = "parallelism_exceeded"          # 平行度超限
BL_RADIAL = "radial_mismatch_exceeded"           # 径向错边超限
BL_GASKET_INTRUSION = "gasket_intrusion"         # 垫片侵入流道
BL_FORCED_PULL = "forced_pull_required"          # 须靠螺栓强行拉拢

BLOCKER_MESSAGES = {
    BL_PARALLELISM: "法兰面平行度超过冻结限值，强制贴合将产生附加管口应力",
    BL_RADIAL: "径向错边超过冻结限值，两法兰中心不对中",
    BL_GASKET_INTRUSION: "垫片内缘侵入法兰内孔（流道），垫片偏心或选型错误",
    BL_FORCED_PULL: "存在螺栓不能自由穿入或法兰面已局部接触，须靠螺栓强行拉拢",
}


def _norm_angle(deg: float) -> float:
    return deg % 360.0


def _azimuth(x: float, y: float) -> float:
    """向量 (x,y) 在 u(θ)=(sinθ, cosθ) 约定下的方位角（0=上方，顺时针）。"""
    return math.degrees(math.atan2(x, y)) % 360.0


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """高斯消元解小线性方程组（最小二乘法方程）；奇异返回 None。"""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        pivot_val = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pivot_val
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor:
                for j in range(col, n + 1):
                    a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _least_squares(rows: list[list[float]], ys: list[float], dim: int) -> list[float] | None:
    """求 y ≈ X·b 的最小二乘解（法方程）。"""
    ata = [[0.0] * dim for _ in range(dim)]
    aty = [0.0] * dim
    for row, y in zip(rows, ys):
        for i in range(dim):
            aty[i] += row[i] * y
            for j in range(dim):
                ata[i][j] += row[i] * row[j]
    return _solve_linear(ata, aty)


def _residual_rms(rows: list[list[float]], ys: list[float], beta: list[float]) -> float:
    total = 0.0
    for row, y in zip(rows, ys):
        pred = sum(b * x for b, x in zip(beta, row))
        total += (y - pred) ** 2
    return math.sqrt(total / len(ys))


def _fit_plane(points: list[dict], key: str, with_mean: bool) -> dict | None:
    """拟合 y(θ) = b_x·sinθ + b_y·cosθ [+ c]，返回系数与残差 RMS。"""
    dim = 3 if with_mean else 2
    rows, ys = [], []
    for p in points:
        theta = math.radians(p["angle_deg"])
        row = [math.sin(theta), math.cos(theta)] + ([1.0] if with_mean else [])
        rows.append(row)
        ys.append(p[key])
    beta = _least_squares(rows, ys, dim)
    if beta is None:
        return None
    return {"beta": beta, "rms": round(_residual_rms(rows, ys, beta), 6)}


def _gap_entry(reason: str, *, angle: float | None = None, detail: dict | None = None) -> dict:
    return {
        "scope": "point" if angle is not None else "structure",
        "angle_deg": round(angle, 3) if angle is not None else None,
        "reason": reason,
        "message": GAP_MESSAGES[reason],
        "detail": detail or {},
    }


def analyze_alignment(frozen: dict, points: list[dict]) -> dict:
    """评估一次装配对中预检。

    frozen 键：flange_face_diameter_mm / gasket_inner_diameter_mm /
    gasket_outer_diameter_mm / bore_diameter_mm /
    max_parallelism_mm / max_radial_mismatch_mm。
    points 每项：angle_deg、axial_gap、radial_offset、gasket_edge_position、
    bolt_free_insertion、length_unit（原始单位）。

    返回结构证据缺口、逐测点换算值、拟合指标与阻断项；evaluable 为 False 时
    不产出拟合指标（证据不足，绝不伪造合格结论）。
    """
    D = float(frozen["flange_face_diameter_mm"])
    Gi = float(frozen["gasket_inner_diameter_mm"])
    Go = float(frozen["gasket_outer_diameter_mm"])
    Db = float(frozen["bore_diameter_mm"])
    par_limit = float(frozen["max_parallelism_mm"])
    rad_limit = float(frozen["max_radial_mismatch_mm"])
    R, ri, ro = D / 2.0, Gi / 2.0, Go / 2.0

    converted: list[dict] = []
    seen: dict[float, float] = {}
    units: set[str] = set()
    gaps: list[dict] = []
    duplicate_angles: list[float] = []

    for raw in points:
        unit = raw.get("length_unit", "mm")
        factor = UNIT_TO_MM[unit]
        units.add(unit)
        angle = _norm_angle(float(raw["angle_deg"]))
        point = {
            "angle_deg": round(angle, 6),
            "axial_gap_mm": float(raw["axial_gap"]) * factor,
            "radial_offset_mm": float(raw["radial_offset"]) * factor,
            "gasket_edge_position_mm": float(raw["gasket_edge_position"]) * factor,
            "bolt_free_insertion": bool(raw["bolt_free_insertion"]),
            "length_unit": unit,
        }
        converted.append(point)

        if angle in seen:
            duplicate_angles.append(angle)
            gaps.append(_gap_entry(
                GAP_DUPLICATE_AZIMUTH, angle=angle,
                detail={"first_seen_angle_deg": round(seen[angle], 3)}))
        else:
            seen[angle] = angle

    # 单位一致性（逐测点仍按各自单位换算，故逐点几何矛盾仍可识别）
    if len(units) > 1:
        gaps.append(_gap_entry(GAP_INCONSISTENT_UNITS, detail={"units": sorted(units)}))

    # 覆盖弧段：相邻方位空弧（含绕回），最大空弧 > 180° 即全部落在半圆内
    angles_sorted = sorted({p["angle_deg"] for p in converted})
    max_empty_arc = 360.0
    if len(angles_sorted) >= 2:
        arcs = [angles_sorted[i + 1] - angles_sorted[i]
                for i in range(len(angles_sorted) - 1)]
        arcs.append(360.0 - angles_sorted[-1] + angles_sorted[0])
        max_empty_arc = max(arcs)
    elif len(angles_sorted) == 1:
        max_empty_arc = 360.0
    if max_empty_arc > ARC_GAP_LIMIT_DEG + 1e-9:
        gaps.append(_gap_entry(
            GAP_INSUFFICIENT_ARC,
            detail={"max_empty_arc_deg": round(max_empty_arc, 3),
                    "limit_deg": ARC_GAP_LIMIT_DEG}))

    # 逐测点几何自相矛盾（不依赖整圈拟合）
    for p in converted:
        if p["axial_gap_mm"] < 0:
            gaps.append(_gap_entry(
                GAP_NEGATIVE_GAP, angle=p["angle_deg"],
                detail={"axial_gap_mm": round(p["axial_gap_mm"], 6)}))
        edge = p["gasket_edge_position_mm"]
        # 外缘位置自法兰外缘向内量：<0 表示垫片外缘越过法兰外缘；
        # > R+ri 表示内缘越过对侧法兰边缘（两者都是自相矛盾的读数）
        if edge < 0 or edge > R + ri + 1e-9:
            gaps.append(_gap_entry(
                GAP_GASKET_OUTSIDE_FACE, angle=p["angle_deg"],
                detail={"gasket_edge_position_mm": round(edge, 6),
                        "outer_flange_edge_mm": 0.0,
                        "far_side_inner_limit_mm": round(R + ri, 6)}))
        if abs(p["radial_offset_mm"]) > R + 1e-9:
            gaps.append(_gap_entry(
                GAP_RADIAL_OFFSET_IMPOSSIBLE, angle=p["angle_deg"],
                detail={"radial_offset_mm": round(p["radial_offset_mm"], 6),
                        "face_radius_mm": R}))

    # 方位重复 / 覆盖不足 / 单位不一致 / 几何自相矛盾均属证据缺口：
    # 存在任一原始缺口时不做整圈拟合，只列证据缺口，绝不据矛盾数据出具限值结论。
    evaluable = not gaps

    metrics: dict | None = None
    blockers: list[dict] = []

    if evaluable:
        # 1) 间隙面：c + a·u
        gap_fit = _fit_plane(converted, "axial_gap_mm", with_mean=True)
        ax, ay, c_mean = gap_fit["beta"]
        grad = math.hypot(ax, ay)
        gap_max = c_mean + grad
        gap_min = c_mean - grad
        parallelism = 2.0 * grad

        # 2) 径向偏移：t·u
        rad_fit = _fit_plane(converted, "radial_offset_mm", with_mean=False)
        tx, ty = rad_fit["beta"]
        mismatch = math.hypot(tx, ty)

        # 3) 垫片外缘：垫片中心相对法兰中心位移 g 时，
        #    e_o(θ) = (R − r_o) + g·u（u 侧垫片外缘更靠内，自外缘向内的量距增大）。
        #    同心基准 (R−r_o) 先扣除；平面拟合给出垫片偏心向量与方位，
        #    两侧居中余量直接取测点极值（最坏方位的直接证据，刚性平移下与拟合相等）。
        edge_rows = [
            {**p, "_edge_centered": p["gasket_edge_position_mm"] - (R - ro)}
            for p in converted
        ]
        gas_fit = _fit_plane(edge_rows, "_edge_centered", with_mean=False)
        gx, gy = gas_fit["beta"]
        eccentricity = math.hypot(gx, gy)
        edge_min = min(p["gasket_edge_position_mm"] for p in converted)
        edge_max = max(p["gasket_edge_position_mm"] for p in converted)
        e0 = R - ro
        inner_margin = ri - Db / 2.0 - (edge_max - e0)
        outer_margin = edge_min

        metrics = {
            "gap_max_mm": round(gap_max, 6),
            "gap_min_mm": round(gap_min, 6),
            "parallelism_mm": round(parallelism, 6),
            "parallelism_limit_mm": par_limit,
            "tilt_deg": round(math.degrees(math.atan(grad)), 6),
            "tilt_azimuth_deg": round(_azimuth(ax, ay), 3) if grad > 1e-12 else None,
            "gap_fit_residual_rms_mm": gap_fit["rms"],
            "radial_mismatch_mm": round(mismatch, 6),
            "radial_tir_mm": round(2.0 * mismatch, 6),
            "radial_limit_mm": rad_limit,
            "radial_azimuth_deg": round(_azimuth(tx, ty), 3) if mismatch > 1e-12 else None,
            "radial_fit_residual_rms_mm": rad_fit["rms"],
            "gasket_eccentricity_mm": round(eccentricity, 6),
            "gasket_azimuth_deg": round(_azimuth(gx, gy), 3) if eccentricity > 1e-12 else None,
            "gasket_inner_margin_mm": round(inner_margin, 6),
            "gasket_outer_margin_mm": round(outer_margin, 6),
            "gasket_fit_residual_rms_mm": gas_fit["rms"],
        }

        if parallelism > par_limit + 1e-9:
            blockers.append({
                "reason": BL_PARALLELISM,
                "message": BLOCKER_MESSAGES[BL_PARALLELISM],
                "detail": {"parallelism_mm": metrics["parallelism_mm"],
                           "limit_mm": par_limit,
                           "gap_max_mm": metrics["gap_max_mm"],
                           "gap_min_mm": metrics["gap_min_mm"]},
            })
        if mismatch > rad_limit + 1e-9:
            blockers.append({
                "reason": BL_RADIAL,
                "message": BLOCKER_MESSAGES[BL_RADIAL],
                "detail": {"radial_mismatch_mm": metrics["radial_mismatch_mm"],
                           "radial_tir_mm": metrics["radial_tir_mm"],
                           "limit_mm": rad_limit},
            })
        if inner_margin < -1e-9:
            blockers.append({
                "reason": BL_GASKET_INTRUSION,
                "message": BLOCKER_MESSAGES[BL_GASKET_INTRUSION],
                "detail": {"gasket_eccentricity_mm": metrics["gasket_eccentricity_mm"],
                           "concentric_inner_clearance_mm": round((Gi - Db) / 2.0, 6),
                           "gasket_inner_margin_mm": metrics["gasket_inner_margin_mm"]},
            })

    # 强行拉拢的直接证据不依赖拟合：任何方位螺栓不能自由穿入；
    # 拟合可评估时，全周最小间隙 ≤ 0（局部已接触）同样意味着拉拢。
    not_free = [p["angle_deg"] for p in converted if not p["bolt_free_insertion"]]
    touch = (metrics is not None and metrics["gap_min_mm"] <= 1e-9)
    if not_free or touch:
        blockers.append({
            "reason": BL_FORCED_PULL,
            "message": BLOCKER_MESSAGES[BL_FORCED_PULL],
            "detail": {
                "bolts_not_free_at_angles_deg": [round(a, 3) for a in not_free],
                "local_contact": touch,
                "gap_min_mm": metrics["gap_min_mm"] if metrics else None,
            },
        })

    return {
        "frozen": {
            "flange_face_diameter_mm": D,
            "gasket_inner_diameter_mm": Gi,
            "gasket_outer_diameter_mm": Go,
            "bore_diameter_mm": Db,
            "max_parallelism_mm": par_limit,
            "max_radial_mismatch_mm": rad_limit,
        },
        "point_count": len(converted),
        "points": converted,
        "structure": {
            "min_points": MIN_POINTS,
            "max_empty_arc_deg": round(max_empty_arc, 3),
            "arc_gap_limit_deg": ARC_GAP_LIMIT_DEG,
            "units": sorted(units),
            "duplicate_angles_deg": [round(a, 3) for a in duplicate_angles],
        },
        "evaluable": evaluable,
        "metrics": metrics,
        "evidence_gaps": gaps,
        "blockers": blockers,
        "passed": evaluable and not gaps and not blockers,
    }


# ---------------------------------------------------------------- 版本差异

def diff_analyses(previous: dict, current: dict,
                  tol_deg: float = ANGLE_MATCH_TOL_DEG) -> dict:
    """对比两次预检：冻结参数、按方位匹配的测点与拟合指标差异。

    测点按圆周最近方位配对（容差 tol_deg）；配不上的记为新增/移除方位。
    输入为两次 analyze_alignment 的结果。
    """
    def _angular_diff(a: float, b: float) -> float:
        d = abs(a - b) % 360.0
        return min(d, 360.0 - d)

    prev_pts = {p["angle_deg"]: p for p in previous["points"]}
    cur_pts = {p["angle_deg"]: p for p in current["points"]}
    matched_cur: set[float] = set()
    matched: list[dict] = []
    for ang_a, pa in prev_pts.items():
        candidates = [( _angular_diff(ang_a, ang_b), ang_b)
                      for ang_b in cur_pts if ang_b not in matched_cur]
        candidates = [c for c in candidates if c[0] <= tol_deg]
        if not candidates:
            continue
        d_ang, ang_b = min(candidates, key=lambda c: c[0])
        pb = cur_pts[ang_b]
        matched_cur.add(ang_b)
        matched.append({
            "angle_prev_deg": round(ang_a, 3),
            "angle_current_deg": round(ang_b, 3),
            "angular_difference_deg": round(d_ang, 3),
            "axial_gap_delta_mm": round(pb["axial_gap_mm"] - pa["axial_gap_mm"], 6),
            "radial_offset_delta_mm": round(
                pb["radial_offset_mm"] - pa["radial_offset_mm"], 6),
            "gasket_edge_delta_mm": round(
                pb["gasket_edge_position_mm"] - pa["gasket_edge_position_mm"], 6),
            "bolt_free_insertion_changed":
                pa["bolt_free_insertion"] != pb["bolt_free_insertion"],
            "bolt_free_insertion": pb["bolt_free_insertion"],
        })

    frozen_fields = (
        "flange_face_diameter_mm", "gasket_inner_diameter_mm",
        "gasket_outer_diameter_mm", "bore_diameter_mm",
        "max_parallelism_mm", "max_radial_mismatch_mm")
    frozen_changes = [
        {"field": f, "from": previous["frozen"][f], "to": current["frozen"][f]}
        for f in frozen_fields if previous["frozen"][f] != current["frozen"][f]
    ]

    metric_fields = (
        "gap_max_mm", "gap_min_mm", "parallelism_mm", "tilt_deg",
        "radial_mismatch_mm", "radial_tir_mm", "gasket_eccentricity_mm",
        "gasket_inner_margin_mm", "gasket_outer_margin_mm")
    metric_changes: dict[str, list] = {}
    if previous["metrics"] and current["metrics"]:
        for f in metric_fields:
            metric_changes[f] = [previous["metrics"][f], current["metrics"][f]]

    return {
        "frozen_changes": frozen_changes,
        "points_matched": matched,
        "points_added_angles_deg": [
            round(a, 3) for a in cur_pts if a not in matched_cur],
        "points_removed_angles_deg": [
            round(a, 3) for a in prev_pts
            if all(a != m["angle_prev_deg"] for m in matched)],
        "metric_changes": metric_changes,
        "passed_transition": [previous["passed"], current["passed"]],
    }
