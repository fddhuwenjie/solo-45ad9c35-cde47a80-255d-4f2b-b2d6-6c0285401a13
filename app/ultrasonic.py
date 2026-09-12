"""超声伸长复核：时差 -> 伸长量 -> 预紧力（纯函数，便于单测）。

物理模型（脉冲回波，单程有效长度）：

    L(t) = v(t) · tof(t) / 2

基线（未承载、温度 t0）：L_eff = v0 · tof0 / 2，批次冻结的螺栓有效长度即由此得出。
承载后温度变化 Δt = t - t0，声速按批次冻结的温度系数线性补偿：

    v(t) = v0 · (1 + alpha · Δt)

伸长量：ΔL = L(t) - L_eff = (v(t)·tof - v0·tof0) / 2
预紧力：F = E · A · ΔL / L_eff

单位：长度 mm，声速 m/s（内部统一换算），时间 s，力 N，温度 ℃，
弹性模量 MPa（=N/mm²），声速温度系数 1/℃。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

# 证据缺口（任一存在即该栓结果无效，不能据此确认批次）
GAP_BASELINE_MISSING = "baseline_missing"        # 基线飞行时间缺失
GAP_CALIBRATION_EXPIRED = "calibration_expired"  # 仪器校准过期
GAP_TEMPERATURE_OUT_OF_RANGE = "temperature_out_of_range"  # 温度超出补偿范围
GAP_NON_POSITIVE_ELONGATION = "non_positive_elongation"    # 伸长非正（未承载/读数异常）
GAP_LOAD_OVER_MATERIAL_LIMIT = "load_over_material_limit"  # 载荷超过材料上限
GAP_READING_EXCLUDED = "reading_excluded"        # 当前读数已被排除
GAP_READING_MISSING = "reading_missing"          # 从未提交复测

GAP_MESSAGES = {
    GAP_BASELINE_MISSING: "缺少基线飞行时间，无法计算伸长量",
    GAP_CALIBRATION_EXPIRED: "复测时刻晚于仪器校准有效期（含当日）",
    GAP_TEMPERATURE_OUT_OF_RANGE: "温度超出冻结的声速补偿范围",
    GAP_NON_POSITIVE_ELONGATION: "计算伸长量非正，读数不能确认承载",
    GAP_LOAD_OVER_MATERIAL_LIMIT: "换算预紧力超过材料允许上限",
    GAP_READING_EXCLUDED: "该读数已按理由排除，原值保留但不参与确认",
    GAP_READING_MISSING: "尚无复测读数",
}


def compensated_sound_velocity(v0: float, alpha: float, ref_temp: float,
                               temp: float) -> float:
    """线性温度补偿后的声速 v(t) = v0·(1 + α·(t - t0))。"""
    return v0 * (1.0 + alpha * (temp - ref_temp))


def pulse_echo_length(tof: float, velocity: float) -> float:
    """脉冲回波飞行时间换算单程长度：L = v·tof/2。

    velocity 单位 m/s，tof 单位 s，返回长度单位 m。
    """
    return velocity * tof / 2.0


def elongation_mm(batch: dict, baseline_tof: float, tof: float, temp: float) -> float:
    """基线/复测飞行时差换算伸长量（mm）。

    脉冲回波 L = v·tof/2（m）；长度差 ×1000 为 mm，系数 1/2 合并为 ×500。
    batch 须含 sound_velocity、temp_coefficient、reference_temp_c、length_mm。
    """
    v0 = batch["sound_velocity"]
    v_t = compensated_sound_velocity(
        v0, batch["temp_coefficient"], batch["reference_temp_c"], temp)
    return (v_t * tof - v0 * baseline_tof) * 500.0


def _reading_view(reading: dict, *, gaps: list[str], elongation_mm: float | None,
                  load_kn: float | None, deviation_pct: float | None,
                  in_band: bool) -> dict:
    return {
        "reading_id": reading["id"],
        "bolt_no": reading["bolt_no"],
        "gaps": gaps,
        "elongation_mm": round(elongation_mm, 6) if elongation_mm is not None else None,
        "load_kn": load_kn,
        "deviation_pct": deviation_pct,
        "in_target_band": in_band,
        "operator": reading["operator"],
        "measured_at": reading["measured_at"],
        "temperature_c": reading["temperature_c"],
        "amendment_note": reading.get("amendment_note"),
        "supersedes": reading.get("supersedes"),
        "excluded": bool(reading.get("excluded")),
    }


def evaluate_reading(batch: dict, baseline: dict | None, reading: dict) -> dict:
    """评估单条复测读数：证据缺口、伸长量、预紧力、逐栓偏差。

    伸长量/载荷在存在阻断性缺口（缺基线/温度越界/校准过期）时为 None；
    reading.excluded 为真时只给 reading_excluded 缺口，原值保留展示。
    """
    if reading.get("excluded"):
        return _reading_view(reading, gaps=[GAP_READING_EXCLUDED], elongation_mm=None,
                             load_kn=None, deviation_pct=None, in_band=False)

    gaps: list[str] = []
    measure_date = date.fromisoformat(reading["measured_at"][:10])
    calib_until = date.fromisoformat(batch["instrument_calibration_until"])
    if measure_date > calib_until:
        gaps.append(GAP_CALIBRATION_EXPIRED)

    if not (batch["temp_comp_min_c"] <= reading["temperature_c"]
            <= batch["temp_comp_max_c"]):
        gaps.append(GAP_TEMPERATURE_OUT_OF_RANGE)

    if baseline is None:
        gaps.append(GAP_BASELINE_MISSING)

    elongation = load_kn = deviation = None
    in_band = False
    if baseline is not None:
        elongation = elongation_mm(
            batch, baseline["tof_s"], reading["tof_s"], reading["temperature_c"])
        # 同时回显基线温度供核对；伸长非正仍记录计算值作为证据
        if elongation <= 0:
            gaps.append(GAP_NON_POSITIVE_ELONGATION)
        else:
            load_n = (batch["elastic_modulus_mpa"] * batch["area_mm2"]
                      * elongation / batch["length_mm"])
            load_kn = round(load_n / 1000.0, 6)
            if load_n > batch["material_load_limit_kn"] * 1000.0:
                gaps.append(GAP_LOAD_OVER_MATERIAL_LIMIT)
            lo, hi = batch["target_load_min_kn"], batch["target_load_max_kn"]
            in_band = lo <= load_kn <= hi
            target_mid = (lo + hi) / 2.0
            deviation = round((load_kn - target_mid) / target_mid * 100.0, 4)

    return _reading_view(reading, gaps=gaps, elongation_mm=elongation,
                         load_kn=load_kn, deviation_pct=deviation, in_band=in_band)


@dataclass
class BatchVerdict:
    """整圈复核结论。confirmed 要求全部螺栓有效、入目标带且对径不平衡达标。"""

    bolt_results: list[dict]
    gaps: list[dict]                 # [{bolt_no, reasons[]}]
    loads_kn: list[float]
    dispersion_cv_pct: float | None  # 整圈变异系数（标准差/均值）
    max_deviation_pct: float | None  # 相对目标带中值的最大逐栓偏差绝对值
    diametral: list[dict]            # 对径不平衡
    max_imbalance_pct: float | None
    imbalance_limit_pct: float
    target_band: tuple[float, float]
    confirmed: bool
    blockers: list[str]


def evaluate_batch(batch: dict, baselines: list[dict], readings: list[dict]) -> BatchVerdict:
    """汇总批次复核结果：逐栓偏差、整圈离散度、对径不平衡与确认结论。

    每栓取最新一条读数（含已排除）；缺基线/缺读数/被排除均记为证据缺口。
    readings 按 id 升序（后写为新），baselines 每栓至多一条。
    补拧批次中 locked 螺栓不重测，直接沿用 batch["locked_results"] 的合格
    结果快照，与范围螺栓一起参与整圈离散度与对径不平衡评估。
    """
    n = batch["bolt_count"]
    scope = set(batch.get("scope_bolts") or list(range(1, n + 1)))
    locked = set(batch.get("locked_bolts") or [])
    locked_results = batch.get("locked_results") or {}
    base_of = {b["bolt_no"]: b for b in baselines}
    latest_of: dict[int, dict] = {}
    for r in readings:
        latest_of[r["bolt_no"]] = r  # 升序覆盖：保留最新（含已排除）

    bolt_results: list[dict] = []
    gap_entries: list[dict] = []
    loads: list[float] = []
    for bolt in range(1, n + 1):
        if bolt in locked:
            snap = locked_results.get(str(bolt)) or locked_results.get(bolt)
            res = dict(snap) if snap else {
                "reading_id": None, "bolt_no": bolt,
                "gaps": ["locked_result_missing"],
                "elongation_mm": None, "load_kn": None, "deviation_pct": None,
                "in_target_band": False, "operator": None, "measured_at": None,
                "temperature_c": None, "amendment_note": None,
                "supersedes": None, "excluded": False,
            }
            res["locked"] = True
        elif bolt in scope:
            reading = latest_of.get(bolt)
            if reading is None:
                res = {
                    "reading_id": None, "bolt_no": bolt,
                    "gaps": [GAP_READING_MISSING],
                    "elongation_mm": None, "load_kn": None, "deviation_pct": None,
                    "in_target_band": False, "operator": None, "measured_at": None,
                    "temperature_c": None, "amendment_note": None,
                    "supersedes": None, "excluded": False,
                }
            else:
                res = evaluate_reading(batch, base_of.get(bolt), reading)
            res["locked"] = False
        else:  # 既不在补拧范围也未锁定：结构性缺口
            res = {
                "reading_id": None, "bolt_no": bolt,
                "gaps": [GAP_READING_MISSING],
                "elongation_mm": None, "load_kn": None, "deviation_pct": None,
                "in_target_band": False, "operator": None, "measured_at": None,
                "temperature_c": None, "amendment_note": None,
                "supersedes": None, "excluded": False, "locked": False,
            }
        bolt_results.append(res)
        if res["gaps"]:
            gap_entries.append({"bolt_no": bolt, "reasons": res["gaps"]})
        elif res["load_kn"] is not None:
            loads.append(res["load_kn"])

    blockers: list[str] = []
    if gap_entries:
        blockers.append("evidence_gaps")
    if any(not r["in_target_band"] and not r["gaps"] for r in bolt_results):
        blockers.append("target_band_exceeded")

    # 整圈离散度（变异系数）与最大逐栓偏差
    cv = max_dev = None
    if loads:
        mean = sum(loads) / len(loads)
        if mean > 0:
            std = math.sqrt(sum((x - mean) ** 2 for x in loads) / len(loads))
            cv = round(std / mean * 100.0, 4)
    deviations = [abs(r["deviation_pct"]) for r in bolt_results
                  if r["deviation_pct"] is not None]
    if deviations:
        max_dev = max(deviations)

    # 对径不平衡：bolt 与 bolt+n/2 配对，|F_a-F_b|/均值×100%
    half = n // 2
    diametral: list[dict] = []
    res_of = {r["bolt_no"]: r for r in bolt_results}
    imbalance_over = False
    for bolt in range(1, half + 1):
        opposite = bolt + half
        fa = res_of[bolt]["load_kn"]
        fb = res_of[opposite]["load_kn"]
        imb = None
        if fa is not None and fb is not None:
            mean2 = (fa + fb) / 2.0
            if mean2 > 0:
                imb = round(abs(fa - fb) / mean2 * 100.0, 4)
                if imb > batch["max_imbalance_pct"]:
                    imbalance_over = True
        diametral.append({
            "bolt_a": bolt, "bolt_b": opposite,
            "load_a_kn": fa, "load_b_kn": fb, "imbalance_pct": imb,
        })
    if imbalance_over:
        blockers.append("diametral_imbalance")

    imbs = [d["imbalance_pct"] for d in diametral if d["imbalance_pct"] is not None]
    max_imb = max(imbs) if imbs else None
    confirmed = not blockers and len(loads) == n

    return BatchVerdict(
        bolt_results=bolt_results,
        gaps=gap_entries,
        loads_kn=loads,
        dispersion_cv_pct=cv,
        max_deviation_pct=max_dev,
        diametral=diametral,
        max_imbalance_pct=max_imb,
        imbalance_limit_pct=batch["max_imbalance_pct"],
        target_band=(batch["target_load_min_kn"], batch["target_load_max_kn"]),
        confirmed=confirmed,
        blockers=blockers,
    )
