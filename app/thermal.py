"""热态预紧力校核：温度节点上的轴向变形协调与平衡（纯函数，便于单测）。

物理模型（单栓及其贡献区，VDI 2230 思路的非线性推广）

    装配（参考温度）后逐栓初始预紧力 F0 来自已确认的超声批次或液压张拉方案。
    升温后螺栓、夹持件（法兰）与垫片自由热膨胀不同步，被螺母约束后在栓内
    重新分配载荷。以 ΔF = F − F0、附加垫片压缩 Δx_g 计：

        螺栓自由伸长：α_b·L_b·ΔT_b
        夹持件自由伸长：Σ_i α_i·L_i·ΔT_m
        垫片自由伸长：α_g·L_g·ΔT_g

    变形协调（栓长 = 夹持件叠层 + 垫片厚度）：

        (F − F0)·(1/k_b + 1/k_c) + (x_g(F) − x_g0) = D

        D = Σ_i α_i·L_i·ΔT_m + α_g·L_g·ΔT_g − α_b·L_b·ΔT_b

    其中 1/k_b = L_b/(E_b·A_b)，1/k_c = Σ L_i/(E_i·A_i)（mm/N）。
    D>0（夹持件/垫片比螺栓伸得多）螺栓被拉长、垫片被进一步压缩；
    D<0 螺栓相对伸长，预紧力下降，F≤0 即密封面接触分离。

垫片压缩—回弹曲线（滞回）

    曲线按 (压缩量 x, 加载压力 p_c, 回弹压力 p_r) 折点给出。加载沿加载支，
    卸载从历史最高压缩点 (x*, p*) 按回弹支形状平移：

        x(p) = x* − x_r(p*) + x_r(p)          （p ≤ p*）

    保证卸载路径过峰值点且在 p=0 留下残余压缩（永久变形）；重新加载沿
    回弹支回到包络后继续沿加载支。逐温度节点按时间推进、更新峰值，故
    升温—降温循环下垫片不按原路径回弹。

限值（冻结）

    螺栓超载 F > F_lim；垫片面压 p = F/A_g；
    p > p_max 压溃；p < p_min 密封裕量不足；F ≤ 0 接触分离。
    所需压力超出压缩曲线折点、温度超出材料物性区间，均为曲线覆盖不足，
    绝不外推伪造结论。

单位：内部统一 mm / mm² / MPa(=N/mm²) / kN / ℃；声明单位不一致即单位冲突
（证据缺口，只记录不计算），与对中预检的多测点单位冲突处理一致。
"""
from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------- 证据缺口 / 阻断原因
GAP_INITIAL_LOAD_MISSING = "initial_load_missing"        # 来源中缺该栓初始载荷
GAP_TEMPERATURE_GAP = "temperature_gap"                  # 温度断档（区间超窗/缺分区）
GAP_MATERIAL_CURVE = "material_curve_coverage"           # 温度超出材料物性区间
GAP_GASKET_CURVE = "gasket_curve_coverage"               # 压力/压缩超出垫片曲线
GAP_UNIT_CONFLICT = "unit_conflict"                      # 单位声明冲突
GAP_NON_CONVERGENCE = "non_convergence"                  # 节点求解不收敛

# 接触/限值越限（出现即不可确认）
V_CONTACT_SEPARATION = "contact_separation"
V_BOLT_OVERLOAD = "bolt_overload"
V_GASKET_CRUSH = "gasket_crush"
V_SEAL_LOW = "seal_margin_insufficient"

GAP_MESSAGES = {
    GAP_INITIAL_LOAD_MISSING: "已确认来源中缺少该栓逐栓初始载荷，无法建立协调方程",
    GAP_TEMPERATURE_GAP: "相邻温度节点间隔超出冻结上限或缺少该分区温度（温度断档）",
    GAP_MATERIAL_CURVE: "部件温度超出冻结材料物性曲线覆盖区间，禁止外推",
    GAP_GASKET_CURVE: "所需垫片压力/压缩超出压缩-回弹曲线折点，曲线覆盖不足",
    GAP_UNIT_CONFLICT: "同类物理量声明单位不一致（单位冲突），整案不计算",
    GAP_NON_CONVERGENCE: "该温度节点变形协调方程迭代不收敛",
}

# ---------------------------------------------------------------- 单位换算
LENGTH_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
AREA_TO_MM2 = {"mm2": 1.0, "cm2": 100.0, "m2": 1.0e6, "in2": 25.4 * 25.4}
MODULUS_TO_MPA = {"MPa": 1.0, "GPa": 1000.0, "Pa": 1.0e-6}
PRESSURE_TO_MPA = {"MPa": 1.0, "GPa": 1000.0, "Pa": 1.0e-6,
                   "psi": 0.006894757, "ksi": 6.894757}


# ---------------------------------------------------------------- 冻结输入归一化

def _part_to_mm(part: dict) -> dict:
    """单个金属部件换算到 mm/mm²/MPa，保留原声明单位用于冲突核查。"""
    lu, au, mu = (part.get("length_unit", "mm"), part.get("area_unit", "mm2"),
                  part.get("modulus_unit", "MPa"))
    return {
        "name": part["name"],
        "length_mm": part["length"] * LENGTH_TO_MM[lu],
        "area_mm2": part["area"] * AREA_TO_MM2[au],
        "modulus_mpa": part["elastic_modulus"] * MODULUS_TO_MPA[mu],
        "cte": part["cte"],
        "prop_min_c": part["prop_min_c"],
        "prop_max_c": part["prop_max_c"],
        "length_unit": lu,
        "area_unit": au,
        "modulus_unit": mu,
    }


def normalize_case(payload: dict) -> dict:
    """把请求（原始声明单位）归一化为内部冻结参数（mm/mm²/MPa）。

    同时保留各部件/曲线折点的声明单位供单位冲突核查：同类量声明不一致即
    冲突；normalize 仍完成换算，但评估遇冲突只列缺口、不产出逐时结果。
    """
    bolt = _part_to_mm(payload["bolt"])
    members = [_part_to_mm(p) for p in payload["members"]]
    g_in = payload["gasket"]
    pts = sorted(g_in["points"], key=lambda q: q["compression"])
    curve = {
        "compression_mm": [
            q["compression"] * LENGTH_TO_MM[q.get("compression_unit", "mm")]
            for q in pts],
        "loading_mpa": [
            q["loading_pressure"] * PRESSURE_TO_MPA[q.get("pressure_unit", "MPa")]
            for q in pts],
        "rebound_mpa": [
            q["rebound_pressure"] * PRESSURE_TO_MPA[q.get("pressure_unit", "MPa")]
            for q in pts],
        "compression_units": [q.get("compression_unit", "mm") for q in pts],
        "pressure_units": [q.get("pressure_unit", "MPa") for q in pts],
    }
    gasket = {
        "name": g_in.get("name") or "gasket",
        "effective_area_mm2": g_in["effective_area"]
        * AREA_TO_MM2[g_in.get("area_unit", "mm2")],
        "thickness_mm": g_in["thickness"] * LENGTH_TO_MM[g_in.get("length_unit", "mm")],
        "cte": g_in["cte"],
        "prop_min_c": g_in["prop_min_c"],
        "prop_max_c": g_in["prop_max_c"],
        "area_unit": g_in.get("area_unit", "mm2"),
        "length_unit": g_in.get("length_unit", "mm"),
        "curve": curve,
    }
    raw_nodes = sorted(payload["nodes"], key=lambda n: n["at"])
    nodes = [{
        "at": n["at"],
        "zones": {z["zone"]: {"member_temp_c": z["member_temp_c"],
                              "gasket_temp_c": z["gasket_temp_c"],
                              "bolt_temp_c": z["bolt_temp_c"]}
                  for z in n["temperatures"]},
    } for n in raw_nodes]
    return {
        "bolt": bolt,
        "members": members,
        "gasket": gasket,
        "bolt_zones": list(payload["bolt_zones"]),
        "reference": dict(payload["reference"]),
        "max_interval_seconds": float(
            payload.get("max_interval_seconds")
            or payload["limits"]["max_temperature_interval_seconds"]),
        "limits": {
            "bolt_load_limit_kn": payload["limits"]["bolt_load_limit_kn"],
            "min_seating_pressure_mpa":
                payload["limits"]["min_seating_pressure_mpa"],
            "max_gasket_pressure_mpa":
                payload["limits"]["max_gasket_pressure_mpa"],
        },
        "nodes": nodes,
        "source_type": payload.get("source_type"),
        "source_id": payload.get("source_id"),
    }


def find_unit_conflicts(frozen: dict) -> list[dict]:
    """同类物理量的声明单位须全案一致；冲突返回 [{quantity, units, positions}]。"""
    conflicts: list[dict] = []

    def check(quantity: str, declared: list[tuple[str, str]]):
        units = {u for _, u in declared}
        if len(units) > 1:
            conflicts.append({"quantity": quantity, "units": sorted(units),
                              "positions": [p for p, _ in declared]})

    parts = [("bolt", frozen["bolt"])] + [
        (f"member:{m['name']}", m) for m in frozen["members"]]
    check("length", [(p, d["length_unit"]) for p, d in parts]
          + [("gasket:thickness", frozen["gasket"]["length_unit"])])
    check("area", [(p, d["area_unit"]) for p, d in parts]
          + [("gasket:effective_area", frozen["gasket"]["area_unit"])])
    check("modulus", [(p, d["modulus_unit"]) for p, d in parts])
    cu = frozen["gasket"]["curve"]["compression_units"]
    check("curve_compression", [(f"point:{i + 1}", u) for i, u in enumerate(cu)])
    pu = frozen["gasket"]["curve"]["pressure_units"]
    check("curve_pressure", [(f"point:{i + 1}", u) for i, u in enumerate(pu)])
    return conflicts


# ---------------------------------------------------------------- 垫片曲线（折线性 + 滞回）

def _inv_linear(value: float, xs: list[float], ys: list[float]) -> float:
    """分段线性反解：已知 y 求 x；value 须在 ys 值域内（调用方先核查覆盖）。"""
    if value <= ys[0]:
        return xs[0]
    if value >= ys[-1]:
        return xs[-1]
    for i in range(1, len(ys)):
        if ys[i - 1] <= value <= ys[i]:
            if ys[i] == ys[i - 1]:
                return xs[i - 1]
            t = (value - ys[i - 1]) / (ys[i] - ys[i - 1])
            return xs[i - 1] + t * (xs[i] - xs[i - 1])
    return xs[-1]


class GasketCurve:
    """压缩-回弹曲线；x_at_pressure 为给定历史峰值下的单值压缩函数。"""

    def __init__(self, curve: dict):
        self.x = curve["compression_mm"]
        self.pc = curve["loading_mpa"]
        self.pr = curve["rebound_mpa"]

    @property
    def max_loading_p(self) -> float:
        return self.pc[-1]

    @property
    def max_rebound_p(self) -> float:
        return self.pr[-1]

    def loading_x(self, p: float) -> float:
        return _inv_linear(p, self.x, self.pc)

    def rebound_x(self, p: float) -> float:
        return _inv_linear(p, self.x, self.pr)

    def rebound_path_x(self, p: float, peak_p: float, peak_x: float) -> float:
        """卸载/再加载回弹路径压缩量：x = x* − x_r(p*) + x_r(p)。

        仅用于 p ≤ p*；峰值压力超回弹曲线覆盖时抛 ValueError（调用方记缺口）。
        """
        if peak_p > self.pr[-1] + 1e-12:
            raise ValueError("rebound_curve_exceeded")
        return peak_x - self.rebound_x(peak_p) + self.rebound_x(p)


# ---------------------------------------------------------------- 逐栓温度节点求解

def member_compliance_mm_per_n(frozen: dict) -> float:
    """夹持件叠层柔度 1/k_c = Σ L/(E·A)（mm/N）。"""
    return sum(m["length_mm"] / (m["modulus_mpa"] * m["area_mm2"])
               for m in frozen["members"])


def thermal_mismatch_mm(frozen: dict, temps: dict) -> float:
    """自由热膨胀差 D（mm）：夹持件+垫片相对螺栓的自由伸长差。"""
    ref = frozen["reference"]
    dt_m = temps["member_temp_c"] - ref["member_temp_c"]
    dt_g = temps["gasket_temp_c"] - ref["gasket_temp_c"]
    dt_b = temps["bolt_temp_c"] - ref["bolt_temp_c"]
    grow_members = sum(m["cte"] * m["length_mm"] * dt_m for m in frozen["members"])
    grow_gasket = frozen["gasket"]["cte"] * frozen["gasket"]["thickness_mm"] * dt_g
    grow_bolt = frozen["bolt"]["cte"] * frozen["bolt"]["length_mm"] * dt_b
    return grow_members + grow_gasket - grow_bolt


def _bisect_node(lo: float, hi: float, residual_at, tolerance_n: float,
                 max_iterations: int, at: str, bolt_no: int, interval: list,
                 gaps: list[dict], row: dict) -> tuple[float, float, str]:
    """在单调残差根所在支上二分；残差容差不满足记不收敛缺口（仍返回末点）。"""
    mid = (lo + hi) / 2.0
    x_mid = branch = None
    for _ in range(max_iterations):
        mid = (lo + hi) / 2.0
        r, x_mid, branch = residual_at(mid)
        if abs(r) <= tolerance_n:
            return mid, x_mid, branch
        if r > 0:
            hi = mid
        else:
            lo = mid
    r, x_mid, branch = residual_at(mid)
    if abs(r) > 1e-4:
        gaps.append({"reason": GAP_NON_CONVERGENCE, "interval": list(interval),
                     "message": f"栓 {bolt_no} 在 {at} 节点协调方程迭代 "
                                f"{max_iterations} 次残差 {round(r, 6)}mm 仍大于容差"})
        row["states"].append(GAP_NON_CONVERGENCE)
    return mid, x_mid, branch


def solve_bolt_series(frozen: dict, bolt_no: int, initial_load_kn: float,
                      series_nodes: list[dict], *,
                      max_iterations: int = 60,
                      tolerance_n: float = 1e-6) -> dict:
    """逐温度节点求解单栓热态载荷（按时间推进，垫片峰值压缩随路径更新）。

    series_nodes: [{"at", "temps": {...}|None}]（缺该分区温度时 temps=None）。
    返回逐时结果、首次越限时刻与本栓缺口（温度/物性/曲线覆盖/不收敛）。
    """
    bolt = frozen["bolt"]
    gasket = frozen["gasket"]
    area_g = gasket["effective_area_mm2"]
    limits = frozen["limits"]
    curve = GasketCurve(gasket["curve"])

    c_b = bolt["length_mm"] / (bolt["modulus_mpa"] * bolt["area_mm2"])
    c_s = c_b + member_compliance_mm_per_n(frozen)      # mm/N
    c_s_kn = c_s * 1000.0                                # mm/kN
    f_lim = limits["bolt_load_limit_kn"]
    p_min = limits["min_seating_pressure_mpa"]
    p_max = limits["max_gasket_pressure_mpa"]

    gaps: list[dict] = []
    rows: list[dict] = []
    f0 = initial_load_kn
    p0 = f0 * 1000.0 / area_g
    # x0 为装配状态（F0 沿加载支）的垫片压缩；变形协调始终锚定装配状态，不随
    # 历史峰值漂移；peak_p/peak_x 仅用于选择压缩-回弹支（温度路径相关）。
    peak_p = p0
    if p0 > curve.max_loading_p + 1e-12:
        gaps.append({"reason": GAP_GASKET_CURVE, "interval": None,
                     "message": f"栓 {bolt_no} 初始面压 {round(p0, 4)}MPa 超出压缩曲线"
                                f"上限 {curve.max_loading_p}MPa，初始压缩无法定位"})
        x0 = None
    else:
        x0 = curve.loading_x(p0)
    peak_x = x0

    def add_material_gaps(interval: list, temps: dict) -> None:
        parts = [("螺栓", bolt, temps["bolt_temp_c"]),
                 ("垫片", gasket, temps["gasket_temp_c"])]
        parts += [(f"夹持件 {m['name']}", m, temps["member_temp_c"])
                  for m in frozen["members"]]
        for label, part, t in parts:
            if not (part["prop_min_c"] <= t <= part["prop_max_c"]):
                gaps.append({"reason": GAP_MATERIAL_CURVE, "interval": list(interval),
                             "message": f"栓 {bolt_no} 在 {interval[0]} 起的区间内{label}"
                                        f"温度 {t}℃ 超出物性曲线覆盖区间 "
                                        f"[{part['prop_min_c']}, {part['prop_max_c']}]℃"})

    first_times: dict[str, str] = {}
    for i, node in enumerate(series_nodes):
        at = node["at"]
        nxt = series_nodes[i + 1]["at"] if i + 1 < len(series_nodes) else at
        interval = [at, nxt]
        temps = node.get("temps")
        if temps is None:
            gaps.append({"reason": GAP_TEMPERATURE_GAP, "interval": interval,
                         "message": f"栓 {bolt_no} 在 {at} 节点缺所属分区温度"})
            rows.append({"at": at, "temperatures": None, "thermal_load_kn": None,
                         "load_change_kn": None, "gasket_pressure_mpa": None,
                         "gasket_compression_mm": None, "seal_margin_mpa": None,
                         "seal_margin_pct": None, "branch": None,
                         "states": ["temperature_missing"]})
            continue
        add_material_gaps(interval, temps)

        row: dict = {"at": at,
                     "temperatures": {"bolt_temp_c": temps["bolt_temp_c"],
                                      "member_temp_c": temps["member_temp_c"],
                                      "gasket_temp_c": temps["gasket_temp_c"]},
                     "states": []}
        if x0 is None:
            # 初始点已在曲线外：整链路径不可信，只给温度不给力
            row.update({"thermal_load_kn": None, "load_change_kn": None,
                        "gasket_pressure_mpa": None, "gasket_compression_mm": None,
                        "seal_margin_mpa": None, "seal_margin_pct": None,
                        "branch": None})
            rows.append(row)
            continue

        d = thermal_mismatch_mm(frozen, temps)

        def res_loading(f_kn: float) -> float:
            p = f_kn * 1000.0 / area_g
            x = curve.loading_x(p)
            return (f_kn - f0) * c_s_kn + (x - x0) - d

        def res_rebound(f_kn: float) -> float:
            p = f_kn * 1000.0 / area_g
            x = curve.rebound_path_x(p, peak_p, peak_x)
            return (f_kn - f0) * c_s_kn + (x - x0) - d

        f_peak = peak_p * area_g / 1000.0
        f_max_loading = curve.max_loading_p * area_g / 1000.0

        # 峰值点（两分支连续）残差决定根在加载支还是回弹支。
        # r>容差且卸载路径覆盖峰值压力时根在回弹支；数值微扰（r≈0）按加载处理。
        r_at_peak = res_loading(f_peak)
        beyond_loading = rebound_unavailable = use_rebound = False
        if r_at_peak > 1e-7:
            try:
                res_rebound(0.0)
                use_rebound = True
            except ValueError:
                rebound_unavailable = True

        if use_rebound:
            # 根在回弹支 [0, p*]
            branch = "rebound"
            r_zero = res_rebound(0.0)
            if r_zero >= 0.0:
                # 回弹到零载荷仍不满足：密封面接触分离
                f_star = 0.0
                try:
                    x_star = curve.rebound_path_x(0.0, peak_p, peak_x)
                except ValueError:
                    x_star = peak_x
                row["states"].append(V_CONTACT_SEPARATION)
            else:
                f_star, _, branch = _bisect_node(
                    0.0, f_peak,
                    lambda f: (lambda r, x: (r, x, "rebound"))(
                        res_rebound(f), curve.rebound_path_x(
                            f * 1000.0 / area_g, peak_p, peak_x)),
                    tolerance_n, max_iterations, at, bolt_no, interval, gaps, row)
                x_star = curve.rebound_path_x(f_star * 1000.0 / area_g,
                                              peak_p, peak_x)
        else:
            # 根在加载支 [p*, p_max]（含 r=0 根即峰值，与参考节点）
            branch = "loading"
            if rebound_unavailable:
                # r>0 需卸载，但峰值压力超出回弹曲线覆盖：无回弹路径
                f_star, x_star = f_peak, peak_x
                gaps.append({"reason": GAP_GASKET_CURVE, "interval": interval,
                             "message": f"栓 {bolt_no} 在 {at} 节点载荷回落（卸载），"
                                        f"但峰值面压 {round(peak_p, 4)}MPa 超出回弹曲线"
                                        f"上限 {curve.max_rebound_p}MPa，无回弹路径"})
            elif peak_p > curve.max_loading_p + 1e-12:
                f_star, x_star = f_peak, peak_x  # 初始覆盖缺口已记录
            elif res_loading(f_max_loading) < 0.0:
                f_star = f_max_loading
                x_star = curve.loading_x(curve.max_loading_p)
                beyond_loading = True
                gaps.append({"reason": GAP_GASKET_CURVE, "interval": interval,
                             "message": f"栓 {bolt_no} 在 {at} 节点所需面压超出压缩曲线"
                                        f"上限 {curve.max_loading_p}MPa（D={round(d, 5)}mm）"})
            else:
                f_star, _, branch = _bisect_node(
                    f_peak, f_max_loading,
                    lambda f: (lambda r, x: (r, x, "loading"))(
                        res_loading(f), curve.loading_x(f * 1000.0 / area_g)),
                    tolerance_n, max_iterations, at, bolt_no, interval, gaps, row)
                x_star = curve.loading_x(f_star * 1000.0 / area_g)

        p_star = f_star * 1000.0 / area_g
        if (not beyond_loading and branch == "loading"
                and p_star > peak_p + 1e-9):
            peak_p, peak_x = p_star, x_star  # 沿加载支升高，更新历史峰值（供后续节点）

        if f_star <= 1e-9 and V_CONTACT_SEPARATION not in row["states"]:
            row["states"].append(V_CONTACT_SEPARATION)
        if f_star > f_lim + 1e-9:
            row["states"].append(V_BOLT_OVERLOAD)
        if p_star > p_max + 1e-9:
            row["states"].append(V_GASKET_CRUSH)
        if p_star < p_min - 1e-9 and V_CONTACT_SEPARATION not in row["states"]:
            row["states"].append(V_SEAL_LOW)

        margin = p_star - p_min
        row.update({
            "thermal_load_kn": round(f_star, 6),
            "load_change_kn": round(f_star - f0, 6),
            "gasket_pressure_mpa": round(p_star, 6),
            "gasket_compression_mm": round(x_star, 6),
            "seal_margin_mpa": round(margin, 6),
            "seal_margin_pct": round(margin / p_min * 100.0, 4) if p_min > 0 else None,
            "branch": branch,
        })
        for st in row["states"]:
            first_times.setdefault(st, at)
        rows.append(row)

    ordered = [s for r in rows for s in r["states"]
               if s in (V_CONTACT_SEPARATION, V_BOLT_OVERLOAD, V_GASKET_CRUSH,
                        V_SEAL_LOW, GAP_NON_CONVERGENCE)]
    first_reason = ordered[0] if ordered else None
    first_at = first_times.get(first_reason) if first_reason else None
    return {"rows": rows, "gaps": gaps, "first_violations": first_times,
            "first_violation_at": first_at, "first_violation_reason": first_reason}


# ---------------------------------------------------------------- 整案评估

def _parse_at(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def evaluate_case(frozen: dict, initial_loads: dict, bolt_count: int) -> dict:
    """整案评估：温度断档/单位冲突核查 + 逐栓逐节点求解 + 确认结论。

    initial_loads: {bolt_no(int 或 str): 载荷 kN}，取自已确认来源的冻结快照。
    缺口一律列出栓号与对应时间区间（无时间属性的区间为 null），绝不判合格。
    """
    nodes = frozen["nodes"]
    zones_of = frozen["bolt_zones"]

    # ---- 单位冲突：整案只列缺口、不产出逐时结果（同对中预检证据缺口语义）
    unit_conflicts = find_unit_conflicts(frozen)

    # ---- 温度断档（按区间）：相邻节点间隔超冻结上限即整区间断档
    interval_gaps: list[dict] = []
    for i in range(len(nodes) - 1):
        t0, t1 = _parse_at(nodes[i]["at"]), _parse_at(nodes[i + 1]["at"])
        seconds = (t1 - t0).total_seconds()
        if seconds > frozen["max_interval_seconds"] + 1e-9:
            interval_gaps.append({
                "from": nodes[i]["at"], "to": nodes[i + 1]["at"],
                "seconds": seconds, "limit": frozen["max_interval_seconds"]})

    bolt_views: list[dict] = []
    all_gap_reasons: set[str] = set()
    all_violation_reasons: set[str] = set()
    if unit_conflicts:
        all_gap_reasons.add(GAP_UNIT_CONFLICT)

    for bolt_no in range(1, bolt_count + 1):
        zone = zones_of[bolt_no - 1] if bolt_no - 1 < len(zones_of) else None
        f0 = initial_loads.get(str(bolt_no), initial_loads.get(bolt_no))
        gaps: list[dict] = []
        rows: list[dict] = []
        first_times: dict[str, str] = {}
        first_at = first_reason = None

        if zone is None:
            gaps.append({"reason": GAP_TEMPERATURE_GAP, "interval": None,
                         "message": f"栓 {bolt_no} 未分配温度分区"})
        if f0 is None:
            gaps.append({"reason": GAP_INITIAL_LOAD_MISSING, "interval": None,
                         "message": GAP_MESSAGES[GAP_INITIAL_LOAD_MISSING]
                                    + f"（栓 {bolt_no}）"})

        if unit_conflicts:
            for c in unit_conflicts:
                gaps.append({"reason": GAP_UNIT_CONFLICT, "interval": None,
                             "message": f"{c['quantity']} 单位冲突：{c['units']}"
                                        f"（出现于 {', '.join(c['positions'])}）"})
        elif f0 is not None and zone is not None:
            series_nodes: list[dict] = []
            for i, n in enumerate(nodes):
                z = n["zones"].get(zone)
                if z is None:
                    series_nodes.append({"at": n["at"], "temps": None})
                    gaps.append({"reason": GAP_TEMPERATURE_GAP,
                                 "interval": [n["at"],
                                              nodes[i + 1]["at"] if i + 1 < len(nodes)
                                              else n["at"]],
                                 "message": f"节点 {n['at']} 缺分区 {zone} 温度"})
                else:
                    series_nodes.append({"at": n["at"], "temps": z})
            for ig in interval_gaps:
                gaps.append({"reason": GAP_TEMPERATURE_GAP,
                             "interval": [ig["from"], ig["to"]],
                             "message": f"区间 {ig['from']} ~ {ig['to']} 间隔 "
                                        f"{ig['seconds']}s 超过温度断档上限 "
                                        f"{ig['limit']}s"})
            sol = solve_bolt_series(frozen, bolt_no, float(f0), series_nodes)
            rows, bolt_gaps = sol["rows"], sol["gaps"]
            first_times, first_at = sol["first_violations"], sol["first_violation_at"]
            first_reason = sol["first_violation_reason"]
            gaps.extend(bolt_gaps)

        gap_reasons = sorted({g["reason"] for g in gaps})
        violations = sorted({s for r in rows for s in r.get("states", [])
                             if s in (V_CONTACT_SEPARATION, V_BOLT_OVERLOAD,
                                      V_GASKET_CRUSH, V_SEAL_LOW)})
        all_gap_reasons.update(gap_reasons)
        all_violation_reasons.update(violations)
        p0 = (float(f0) * 1000.0 / frozen["gasket"]["effective_area_mm2"]
              if f0 is not None else None)
        bolt_views.append({
            "bolt_no": bolt_no,
            "zone": zone,
            "initial_load_kn": float(f0) if f0 is not None else None,
            "initial_gasket_pressure_mpa": round(p0, 6) if p0 is not None else None,
            "gaps": gaps,
            "series": rows,
            "first_violations": first_times,
            "first_violation_at": first_at,
            "first_violation_reason": first_reason,
        })

    blockers = sorted(all_gap_reasons | all_violation_reasons)
    return {
        "evaluable": not unit_conflicts,
        "confirmable": not blockers,
        "blockers": blockers,
        "gap_reasons": sorted(all_gap_reasons),
        "violation_reasons": sorted(all_violation_reasons),
        "reference_temperatures_c": frozen["reference"],
        "limits": frozen["limits"],
        "temperature_nodes": [{"at": n["at"], "zones": sorted(n["zones"].keys())}
                              for n in nodes],
        "temperature_intervals": [{"interval": [g["from"], g["to"]],
                                   "seconds": g["seconds"],
                                   "limit": g["limit"]} for g in interval_gaps],
        "bolts": bolt_views,
    }


# ---------------------------------------------------------------- 版本差异

def _short_part(p: dict) -> dict:
    return {k: p[k] for k in ("name", "length_mm", "area_mm2", "modulus_mpa",
                              "cte", "prop_min_c", "prop_max_c")}


def diff_cases(old: dict, new: dict) -> dict:
    """相邻修订差异：冻结参数、温度时序、初始载荷来源与逐栓初载变化。"""
    param_changes: dict[str, dict] = {}
    if _short_part(old["frozen"]["bolt"]) != _short_part(new["frozen"]["bolt"]):
        param_changes["bolt"] = {"from": _short_part(old["frozen"]["bolt"]),
                                 "to": _short_part(new["frozen"]["bolt"])}
    old_m = [_short_part(m) for m in old["frozen"]["members"]]
    new_m = [_short_part(m) for m in new["frozen"]["members"]]
    if old_m != new_m:
        param_changes["members"] = {"from": old_m, "to": new_m}

    def gasket_brief(f: dict) -> dict:
        g = f["gasket"]
        return {"name": g["name"], "effective_area_mm2": g["effective_area_mm2"],
                "thickness_mm": g["thickness_mm"], "cte": g["cte"],
                "prop_min_c": g["prop_min_c"], "prop_max_c": g["prop_max_c"],
                "curve_points": len(g["curve"]["compression_mm"])}

    if gasket_brief(old["frozen"]) != gasket_brief(new["frozen"]):
        param_changes["gasket"] = {"from": gasket_brief(old["frozen"]),
                                   "to": gasket_brief(new["frozen"])}
    for field in ("limits", "reference", "max_interval_seconds", "bolt_zones"):
        if old["frozen"][field] != new["frozen"][field]:
            param_changes[field] = {"from": old["frozen"][field],
                                    "to": new["frozen"][field]}
    if (old.get("source_type"), old.get("source_id")) != \
            (new.get("source_type"), new.get("source_id")):
        param_changes["initial_load_source"] = {
            "from": {"type": old.get("source_type"), "id": old.get("source_id")},
            "to": {"type": new.get("source_type"), "id": new.get("source_id")}}

    old_init = {int(k): v for k, v in (old.get("initial_loads") or {}).items()}
    new_init = {int(k): v for k, v in (new.get("initial_loads") or {}).items()}
    load_changes = {str(b): {"from": old_init.get(b), "to": new_init.get(b)}
                    for b in sorted(set(old_init) | set(new_init))
                    if old_init.get(b) != new_init.get(b)}

    old_nodes = {n["at"]: n["zones"] for n in old["frozen"]["nodes"]}
    new_nodes = {n["at"]: n["zones"] for n in new["frozen"]["nodes"]}
    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    changed = []
    for at in sorted(set(old_nodes) & set(new_nodes)):
        if old_nodes[at] != new_nodes[at]:
            changed.append({"at": at, "from": old_nodes[at], "to": new_nodes[at]})
    return {
        "param_changes": param_changes,
        "initial_load_changes_kn": load_changes,
        "timeline": {"added_nodes": added, "removed_nodes": removed,
                     "changed_nodes": changed},
        "change_note": new.get("change_note"),
    }
