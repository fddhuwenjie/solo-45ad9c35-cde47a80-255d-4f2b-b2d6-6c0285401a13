"""热态预紧力校核：变形协调求解、压缩-回弹滞回、证据缺口（初载未确认、
温度断档、材料/垫片曲线覆盖不足、单位冲突、不收敛）、接触分离/压溃/超载、
修订（替代边界/曲线须写理由）、版本差异与作业包一致性。"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.thermal import (GasketCurve, diff_cases, evaluate_case,
                         find_unit_conflicts, member_compliance_mm_per_n,
                         normalize_case, thermal_mismatch_mm)

BASE = {
    "flange_class": "PN40 DN200",
    "bolt_count": 8,
    "gasket": "缠绕垫片 304+石墨",
    "target_torque": 320.0,
    "stage_ratios": [0.3, 0.6, 1.0],
    "tolerance_pct": 5.0,
    "tool_id": "TW-1001",
    "tool_range_min": 50.0,
    "tool_range_max": 500.0,
    "calibration_valid_until": "2026-12-31",
    "start_angle_deg": 0.0,
    "clockwise": True,
    "curve_direction": "cw",
    "snug_torque": 40.0,
    "post_snug_angle_min_deg": 30.0,
    "post_snug_angle_max_deg": 120.0,
    "max_sample_interval_ms": 50.0,
    "slope_drop_limit": 5.0,
    "max_outlier_rate_pct": 25.0,
}
SEQ8 = [1, 5, 2, 6, 3, 7, 4, 8]

ALIGN_GEOM = {
    "flange_face_diameter_mm": 285.0,
    "gasket_inner_diameter_mm": 220.0,
    "gasket_outer_diameter_mm": 270.0,
    "bore_diameter_mm": 200.0,
    "max_parallelism_mm": 1.0,
    "max_radial_mismatch_mm": 2.0,
}
EDGE0 = (285.0 - 270.0) / 2.0

BOLT = {"name": "B7-Stud", "length": 150.0, "area": 353.0,
        "elastic_modulus": 206000.0, "cte": 1.2e-5,
        "prop_min_c": -50.0, "prop_max_c": 400.0}
MEMBERS = [
    {"name": "flange-near", "length": 30.0, "area": 2000.0,
     "elastic_modulus": 200000.0, "cte": 1.2e-5,
     "prop_min_c": -50.0, "prop_max_c": 400.0},
    {"name": "flange-far", "length": 30.0, "area": 2000.0,
     "elastic_modulus": 200000.0, "cte": 1.2e-5,
     "prop_min_c": -50.0, "prop_max_c": 400.0},
]
GASKET = {
    "effective_area": 3500.0, "thickness": 4.5, "cte": 1.7e-5,
    "prop_min_c": -50.0, "prop_max_c": 400.0,
    "points": [
        {"compression": 0.0, "loading_pressure": 0.0, "rebound_pressure": 0.0},
        {"compression": 0.5, "loading_pressure": 50.0, "rebound_pressure": 40.0}],
}
LIMITS = {"bolt_load_limit_kn": 180.0, "min_seating_pressure_mpa": 10.0,
          "max_gasket_pressure_mpa": 60.0,
          "max_temperature_interval_seconds": 3600.0}
REF = {"bolt_temp_c": 20.0, "member_temp_c": 20.0, "gasket_temp_c": 20.0}


def zone(zone_name, tb=20.0, tm=20.0, tg=20.0):
    return {"zone": zone_name, "bolt_temp_c": tb, "member_temp_c": tm,
            "gasket_temp_c": tg}


def thermal_payload(*, bolt_zones=None, nodes=None, **overrides):
    payload = {
        "source_type": "ultrasonic",
        "bolt": copy.deepcopy(BOLT),
        "members": copy.deepcopy(MEMBERS),
        "gasket": {"name": "spw", **copy.deepcopy(GASKET)},
        "bolt_zones": bolt_zones or ["Z1"] * 8,
        "reference": copy.deepcopy(REF), "limits": copy.deepcopy(LIMITS),
        "nodes": nodes or [
            {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
            {"at": "2026-09-13T09:00:00",
             "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 纯函数

def test_reference_node_recovers_initial_load():
    """参考温度节点（ΔT=0）热态载荷恒等于初载，垫片压缩为初始压缩。"""
    frozen = normalize_case(thermal_payload())
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert res["confirmable"] is True
    for b in res["bolts"]:
        assert b["series"][0]["thermal_load_kn"] == pytest.approx(140.0, abs=1e-3)
        assert b["series"][0]["gasket_pressure_mpa"] == pytest.approx(40.0, abs=1e-3)
        assert b["series"][0]["gasket_compression_mm"] == pytest.approx(0.4, abs=1e-3)
        assert b["series"][0]["states"] == []


def test_linear_compatibility_against_hand_calc():
    """线性段协调方程与手算一致：(F−F0)·c_s + (x−x0) = D。"""
    temps = {"bolt_temp_c": 60.0, "member_temp_c": 200.0,
             "gasket_temp_c": 200.0}
    payload = thermal_payload(nodes=[
        {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
        {"at": "2026-09-13T09:00:00", "temperatures": [zone("Z1", **{
            "tb": temps["bolt_temp_c"], "tm": temps["member_temp_c"],
            "tg": temps["gasket_temp_c"]})]}])
    frozen = normalize_case(payload)
    # 夹持件/垫片 200℃、螺栓 60℃：D>0（夹持件膨胀占优），根在加载支
    D = thermal_mismatch_mm(frozen, temps)
    # D = 1.2e-5·(60·180 − 150·40) + 1.7e-5·4.5·180
    assert D == pytest.approx(1.2e-5 * (60 * 180 - 150 * 40) + 1.7e-5 * 4.5 * 180)
    area_g = 3500.0
    c_b = 150.0 / (206000.0 * 353.0)
    c_s = c_b + member_compliance_mm_per_n(frozen)
    # 线性加载支：x−x0 = (p−p0)·0.5/50 = (F−F0)·1000/Ag·0.01
    kx_per_kn = 1000.0 / area_g * 0.01
    f_expect = 140.0 + D / (c_s * 1000.0 + kx_per_kn)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    got = res["bolts"][0]["series"][1]
    assert got["branch"] == "loading"
    assert got["thermal_load_kn"] == pytest.approx(f_expect, rel=1e-4)


def test_cooling_unloads_with_rebound_hysteresis():
    """升温加载后再降温：卸载沿平移后的回弹路径，载荷/压缩不再沿加载支原路返回。"""
    hysteresis_gasket = {
        "effective_area": 3500.0, "thickness": 4.5, "cte": 1.7e-5,
        "prop_min_c": -50.0, "prop_max_c": 400.0,
        "points": [
            {"compression": 0.0, "loading_pressure": 0.0, "rebound_pressure": 0.0},
            {"compression": 0.5, "loading_pressure": 100.0,
             "rebound_pressure": 80.0}],
    }
    payload = thermal_payload(
        gasket={"name": "hysteresis", **hysteresis_gasket},
        nodes=[
            {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
            {"at": "2026-09-13T08:30:00",
             "temperatures": [zone("Z1", tb=20.0, tm=100.0, tg=100.0)]},
            {"at": "2026-09-13T09:00:00", "temperatures": [zone("Z1")]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    heat_row, cool_row = res["bolts"][0]["series"][1:]
    # 夹持件更热 -> 继续沿加载支升高
    assert heat_row["branch"] == "loading"
    assert heat_row["thermal_load_kn"] > 140.0
    peak_p = heat_row["gasket_pressure_mpa"]
    # 回到参考温度：沿过峰值的回弹路径卸载（路径相关，非加载支原路）
    assert cool_row["branch"] == "rebound"
    assert cool_row["gasket_pressure_mpa"] < peak_p
    assert res["confirmable"] is True


def test_rebound_curve_boundary_tolerance():
    """峰值面压恰处回弹曲线上限折点时数值微扰不触发曲线覆盖缺口，求解继续。"""
    payload = thermal_payload(
        nodes=[
            {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
            {"at": "2026-09-13T09:00:00",
             "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    # 初载面压 p0=40MPa 恰为回弹曲线上限；第 2 节点小幅卸载须能沿回弹路径求解
    assert "gasket_curve_coverage" not in res["gap_reasons"]
    row = res["bolts"][0]["series"][1]
    assert row["branch"] == "rebound"
    assert row["thermal_load_kn"] > 0


def test_gasket_curve_rebound_form_passes_peak():
    curve = GasketCurve({"compression_mm": [0.0, 0.5],
                         "loading_mpa": [0.0, 50.0],
                         "rebound_mpa": [0.0, 40.0]})
    # 峰值 (x*=0.5, p*=40) 时回弹路径在 p* 给出 0.5 − x_r(40) + x_r(40) = 0.5
    assert curve.rebound_path_x(40.0, 40.0, 0.5) == pytest.approx(0.5)
    # p=20 卸载：x = 0.5 − 0.5 + 0.25
    assert curve.rebound_path_x(20.0, 40.0, 0.5) == pytest.approx(0.25)
    # 峰值压力超出回弹曲线覆盖（40）：无回弹路径
    with pytest.raises(ValueError):
        curve.rebound_path_x(10.0, 50.0, 0.5)


def test_contact_separation_when_bolt_expands_more():
    """螺栓热膨胀远大于夹持件：预紧力降至 0，标记接触分离与首次时刻。"""
    payload = thermal_payload(nodes=[
        {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
        {"at": "2026-09-13T09:00:00",
         "temperatures": [zone("Z1", tb=550.0, tm=-50.0, tg=-50.0)]}],
        bolt={**BOLT, "prop_min_c": -60.0, "prop_max_c": 600.0})
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "contact_separation" in res["blockers"]
    b1 = res["bolts"][0]
    row = b1["series"][1]
    assert "contact_separation" in row["states"]
    assert row["thermal_load_kn"] == pytest.approx(0.0, abs=1e-9)
    assert b1["first_violation_reason"] == "contact_separation"
    assert b1["first_violation_at"] == "2026-09-13T09:00:00"
    assert res["confirmable"] is False


HIGH_CURVE_GASKET = {
    "effective_area": 3500.0, "thickness": 4.5, "cte": 1.7e-5,
    "prop_min_c": -50.0, "prop_max_c": 400.0,
    "points": [
        {"compression": 0.0, "loading_pressure": 0.0, "rebound_pressure": 0.0},
        {"compression": 0.5, "loading_pressure": 100.0, "rebound_pressure": 80.0}],
}


def test_bolt_overload_on_high_differential_expansion():
    """夹持件远热于螺栓使栓载荷超过材料上限：bolt_overload 定位栓号时刻。"""
    payload = thermal_payload(
        gasket={"name": "high", **HIGH_CURVE_GASKET},
        nodes=[{"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
               {"at": "2026-09-13T08:30:00",
                "temperatures": [zone("Z1", tb=20.0, tm=380.0, tg=380.0)]},
               {"at": "2026-09-13T09:00:00",
                "temperatures": [zone("Z1", tb=20.0, tm=400.0, tg=400.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "bolt_overload" in res["violation_reasons"]
    b1 = res["bolts"][0]
    assert b1["first_violation_reason"] == "bolt_overload"
    assert b1["first_violation_at"] == "2026-09-13T08:30:00"


def test_gasket_crush_flagged():
    """需要的面压超过压溃限值（但曲线仍可解）：gasket_crush 不可确认。"""
    payload = thermal_payload(
        gasket={"name": "high", **HIGH_CURVE_GASKET},
        limits={**LIMITS, "max_gasket_pressure_mpa": 30.0},
        nodes=[{"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
               {"at": "2026-09-13T09:00:00",
                "temperatures": [zone("Z1", tb=20.0, tm=380.0, tg=380.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "gasket_crush" in res["blockers"]


def test_seal_margin_insufficient_flagged():
    """温和降温使面压跌破最小密封面压但未分离：seal_margin_insufficient。"""
    payload = thermal_payload(
        limits={**LIMITS, "min_seating_pressure_mpa": 39.5},
        nodes=[{"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
               {"at": "2026-09-13T09:00:00",
                "temperatures": [zone("Z1", tb=120.0, tm=20.0, tg=20.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "seal_margin_insufficient" in res["blockers"]
    row = res["bolts"][0]["series"][1]
    assert "seal_margin_insufficient" in row["states"]
    assert row["seal_margin_mpa"] < 0


def test_material_curve_coverage_gap():
    """部件温度超出冻结物性区间：材料曲线覆盖不足，给栓号与区间，不确认。"""
    payload = thermal_payload(nodes=[
        {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
        {"at": "2026-09-13T09:00:00",
         "temperatures": [zone("Z1", tb=450.0, tm=200.0, tg=200.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "material_curve_coverage" in res["gap_reasons"]
    g = next(g for b in res["bolts"] for g in b["gaps"]
             if g["reason"] == "material_curve_coverage")
    assert g["interval"][0] == "2026-09-13T09:00:00"
    assert "螺栓" in g["message"]
    assert res["confirmable"] is False


def test_gasket_curve_coverage_when_demanded_pressure_too_high():
    """需要面压超出压缩曲线最大折点：垫片曲线覆盖不足，禁止外推。"""
    payload = thermal_payload(nodes=[
        {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
        {"at": "2026-09-13T09:00:00",
         "temperatures": [zone("Z1", tb=20.0, tm=500.0, tg=500.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    reasons = {g["reason"] for b in res["bolts"] for g in b["gaps"]}
    assert "gasket_curve_coverage" in reasons


def test_temperature_gap_by_interval_and_missing_zone():
    """相邻节点间隔超上限记温度断档区间；节点缺分区温度按该栓记录。"""
    payload = thermal_payload(
        bolt_zones=["Z1"] * 4 + ["Z2"] * 4,
        nodes=[{"at": "2026-09-13T08:00:00",
                "temperatures": [zone("Z1"), zone("Z2")]},
               {"at": "2026-09-13T11:00:00",
                "temperatures": [zone("Z1", tb=100.0, tm=200.0, tg=200.0)]}])
    frozen = normalize_case(payload)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert "temperature_gap" in res["gap_reasons"]
    z1_bolt = res["bolts"][0]
    interval_gap = next(g for g in z1_bolt["gaps"]
                        if g["interval"] == ["2026-09-13T08:00:00",
                                             "2026-09-13T11:00:00"])
    assert interval_gap["reason"] == "temperature_gap"
    # Z2 栓在第 2 节点缺温度（行内 states 标记 temperature_missing）
    z2_series = res["bolts"][4]["series"]
    assert z2_series[1]["states"] == ["temperature_missing"]
    assert res["confirmable"] is False


def test_initial_load_missing_per_bolt():
    """已确认来源中缺某栓初载：该栓列 initial_load_missing，整案不确认。"""
    frozen = normalize_case(thermal_payload())
    loads = {str(b): 140.0 for b in range(1, 9)}
    del loads["3"]
    res = evaluate_case(frozen, loads, 8)
    b3 = next(b for b in res["bolts"] if b["bolt_no"] == 3)
    assert b3["initial_load_kn"] is None
    assert b3["gaps"][0]["reason"] == "initial_load_missing"
    assert b3["series"] == []
    assert "initial_load_missing" in res["blockers"]
    assert res["confirmable"] is False


def test_unit_conflict_blocks_entire_case():
    """单位声明冲突：只列缺口（含位置），整案不产出逐时结果。"""
    payload = thermal_payload()
    payload["bolt"]["length_unit"] = "cm"  # 其余 mm
    frozen = normalize_case(payload)
    conflicts = find_unit_conflicts(frozen)
    assert any(c["quantity"] == "length" for c in conflicts)
    res = evaluate_case(frozen, {str(b): 140.0 for b in range(1, 9)}, 8)
    assert res["evaluable"] is False
    assert "unit_conflict" in res["gap_reasons"]
    for b in res["bolts"]:
        assert b["series"] == []
        assert any(g["reason"] == "unit_conflict" for g in b["gaps"])


def test_unit_conversion_applied_consistently():
    """全部长度声明为 cm 时换算后与 mm 声明同解（无冲突）。"""
    payload_mm = thermal_payload()
    payload_cm = thermal_payload()
    payload_cm["bolt"]["length"] = 15.0
    payload_cm["bolt"]["length_unit"] = "cm"
    for m in payload_cm["members"]:
        m["length"] = 3.0
        m["length_unit"] = "cm"
    payload_cm["gasket"]["thickness"] = 0.45
    payload_cm["gasket"]["length_unit"] = "cm"
    for p in payload_cm["gasket"]["points"]:
        p["compression"] = p["compression"] / 10.0
        p["compression_unit"] = "cm"
    r_mm = evaluate_case(normalize_case(payload_mm),
                         {str(b): 140.0 for b in range(1, 9)}, 8)
    r_cm = evaluate_case(normalize_case(payload_cm),
                         {str(b): 140.0 for b in range(1, 9)}, 8)
    assert r_cm["confirmable"] is True
    a = r_mm["bolts"][0]["series"][1]
    b = r_cm["bolts"][0]["series"][1]
    assert a["thermal_load_kn"] == pytest.approx(b["thermal_load_kn"], rel=1e-9)


def test_diff_detects_frozen_param_timeline_and_load_changes():
    old_payload = thermal_payload()
    new_payload = thermal_payload(nodes=[
        {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
        {"at": "2026-09-13T08:30:00",
         "temperatures": [zone("Z1", tb=100.0, tm=200.0, tg=200.0)]},
        {"at": "2026-09-13T09:00:00",
         "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}])
    old_f = normalize_case(old_payload)
    new_f = normalize_case(new_payload)
    old = {"frozen": old_f, "source_type": "ultrasonic", "source_id": 1,
           "initial_loads": {str(b): 140.0 for b in range(1, 9)}}
    new = {"frozen": new_f, "source_type": "tensioning", "source_id": 2,
           "initial_loads": {str(b): (130.0 if b == 1 else 140.0)
                             for b in range(1, 9)},
           "change_note": "改用张拉方案初载"}
    d = diff_cases(old, new)
    assert d["param_changes"]["initial_load_source"]["to"] == \
        {"type": "tensioning", "id": 2}
    assert d["initial_load_changes_kn"]["1"] == {"from": 140.0, "to": 130.0}
    assert "2026-09-13T08:30:00" in d["timeline"]["added_nodes"]
    assert d["change_note"] == "改用张拉方案初载"


# ---------------------------------------------------------------- Pydantic 校验

def test_schema_rejects_unordered_nodes_and_curve():
    from app.schemas import ThermalCaseCreate
    # 曲线折点须升序
    bad_curve = thermal_payload()
    bad_curve["gasket"]["points"] = list(reversed(bad_curve["gasket"]["points"]))
    with pytest.raises(ValueError):
        ThermalCaseCreate(**bad_curve)
    # 节点须时刻升序
    bad_nodes = thermal_payload()
    bad_nodes["nodes"] = list(reversed(bad_nodes["nodes"]))
    with pytest.raises(ValueError):
        ThermalCaseCreate(**bad_nodes)
    # 回弹压力不得高于加载压力
    bad_rebound = thermal_payload()
    bad_rebound["gasket"]["points"][1]["rebound_pressure"] = 55.0
    with pytest.raises(ValueError):
        ThermalCaseCreate(**bad_rebound)
    # 节点缺已分配分区：请求级不再拒绝（与初载缺失等证据缺口一致），
    # 工况照常冻结，由求解器记录 temperature_gap（见端到端回归测试）
    missing_zone = thermal_payload(bolt_zones=["Z1"] * 4 + ["Z2"] * 4)
    missing_zone["nodes"][0]["temperatures"] = [zone("Z1")]
    ThermalCaseCreate(**missing_zone)  # 不抛异常


# ---------------------------------------------------------------- 端到端

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def submit_alignment(client, pid):
    points = [{"angle_deg": a, "axial_gap": 2.0, "radial_offset": 0.0,
               "gasket_edge_position": EDGE0, "bolt_free_insertion": True}
              for a in (0, 45, 90, 135, 180, 225, 270, 315)]
    r = client.post(f"/procedures/{pid}/alignment-checks",
                    json={**ALIGN_GEOM, "points": points, "operator": "预检员",
                          "measured_at": "2026-09-12T08:00:00"})
    assert r.status_code == 201, r.text


def make_approved_proc(client) -> int:
    pid = client.post("/procedures", json=BASE).json()["procedure"]["id"]
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    return pid


# 超声批次参数
L_MM, AREA, E_MOD, V0 = 150.0, 353.0, 206000.0, 5900.0
TOF0 = 2 * (L_MM / 1000.0) / V0
BATCH = {
    "length_mm": L_MM, "area_mm2": AREA, "elastic_modulus_mpa": E_MOD,
    "sound_velocity": V0, "temp_coefficient": -1.0e-4, "reference_temp_c": 20.0,
    "temp_comp_min_c": 0.0, "temp_comp_max_c": 60.0,
    "target_load_min_kn": 120.0, "target_load_max_kn": 160.0,
    "material_load_limit_kn": 180.0, "max_imbalance_pct": 15.0,
    "instrument_id": "US-77", "instrument_calibration_until": "2026-12-31",
}


def tof_for_load(load_kn: float) -> float:
    dl_mm = load_kn * 1000.0 * L_MM / (E_MOD * AREA)
    return TOF0 + dl_mm / (V0 * 500.0)


def confirm_ultrasonic(client, pid, load_kn=140.0) -> int:
    bid = client.post(f"/procedures/{pid}/measurement-batches",
                      json=BATCH).json()["batch"]["id"]
    for bolt in SEQ8:
        assert client.post(f"/measurement-batches/{bid}/baselines",
                           json={"bolt_no": bolt, "tof_s": TOF0}).status_code == 201
    assert client.post(f"/procedures/{pid}/start").status_code == 200
    for ratio in (0.3, 0.6, 1.0):
        for bolt in SEQ8:
            assert client.post(f"/procedures/{pid}/reports", json={
                "bolt_no": bolt, "tool_id": "TW-1001", "operator": "张三",
                "reported_at": "2026-09-10T09:00:00",
                "measured_torque": round(320.0 * ratio, 2)}).status_code == 201
    for bolt in SEQ8:
        assert client.post(f"/measurement-batches/{bid}/readings", json={
            "bolt_no": bolt, "tof_s": tof_for_load(load_kn), "temperature_c": 20.0,
            "operator": "赵六",
            "measured_at": "2026-09-12T12:00:00"}).status_code == 201
    assert client.post(f"/measurement-batches/{bid}/confirm").status_code == 200
    return bid


PLAN = {
    "area_mm2": 353.0, "length_mm": 150.0, "elastic_modulus_mpa": 206000.0,
    "target_load_kn": 140.0, "load_tolerance_pct": 20.0,
    "tensioner_id": "HT-01", "tensioner_count": 2,
    "hydraulic_area_mm2": 2000.0, "max_pressure_mpa": 100.0,
    "max_stroke_mm": 5.0, "min_tool_spacing": 2,
    "load_transfer_coefficient": 0.0, "min_hold_seconds": 30.0,
    "pressure_sync_tolerance_pct": 50.0, "gauge_id": "PG-1",
    "gauge_calibration_until": "2026-12-31", "stage_ratios": [1.0],
}


def confirm_tensioning(client, pid) -> int:
    tid = client.post(f"/procedures/{pid}/tensioning-plans",
                      json=PLAN).json()["plan"]["id"]
    assert client.post(f"/tensioning-plans/{tid}/approve").status_code == 200
    detail = client.get(f"/tensioning-plans/{tid}").json()
    p_set = detail["scheme"][0]["set_pressure_mpa"]
    for g in detail["scheme"][0]["groups"]:
        r = client.post(f"/tensioning-plans/{tid}/round-reports", json={
            "round_no": 1, "group_no": g["group_no"], "operator": "李四",
            "reported_at": "2026-09-12T10:00:00", "gauge_id": "PG-1",
            "hold_seconds": 60.0, "release_order": list(reversed(g["bolts"])),
            "channels": [{"bolt_no": b, "pressure_mpa": p_set, "stroke_mm": 0.3}
                         for b in g["bolts"]]})
        assert r.status_code == 201, r.text
    assert client.post(f"/tensioning-plans/{tid}/confirm").status_code == 200
    return tid


def test_create_requires_approved_procedure_and_confirmed_source(client):
    pid = client.post("/procedures", json=BASE).json()["procedure"]["id"]
    r = client.post(f"/procedures/{pid}/thermal-cases", json=thermal_payload())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_approvable_for_thermal"
    submit_alignment(client, pid)
    client.post(f"/procedures/{pid}/approve")
    # approved 但无已确认来源
    r = client.post(f"/procedures/{pid}/thermal-cases", json=thermal_payload())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "initial_load_unconfirmed"
    assert r.json()["detail"]["source_type"] == "ultrasonic"


def test_create_from_ultrasonic_freezes_loads_and_confirms(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    r = client.post(f"/procedures/{pid}/thermal-cases",
                    json=thermal_payload(source_id=bid))
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["case"]["revision"] == 1
    assert detail["case"]["source_type"] == "ultrasonic"
    assert detail["case"]["source_id"] == bid
    assert detail["initial_loads_kn"]["1"] == pytest.approx(140.0, abs=0.01)
    assert len(detail["frozen"]["gasket"]["compression_rebound_curve"]) == 2
    assert detail["result"]["confirmable"] is True
    cid = detail["case"]["id"]
    r = client.post(f"/thermal-cases/{cid}/confirm",
                    json={"reviewer": "热工", "note": "升温全程合格"})
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "confirmed"
    assert r.json()["case"]["decided_by"] == "热工"


def test_create_from_tensioning_source(client):
    pid = make_approved_proc(client)
    tid = confirm_tensioning(client, pid)
    payload = thermal_payload(source_type="tensioning", source_id=tid)
    r = client.post(f"/procedures/{pid}/thermal-cases", json=payload)
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["case"]["source_type"] == "tensioning"
    assert {int(k) for k in detail["initial_loads_kn"]} == set(range(1, 9))
    assert detail["initial_loads_kn"]["1"] == pytest.approx(140.0, abs=0.01)
    # 默认来源（不带 source_id）取最新已确认
    payload2 = thermal_payload(source_type="tensioning")
    client.post(f"/thermal-cases/{detail['case']['id']}/confirm",
                json={"reviewer": "r"})
    r2 = client.post(f"/procedures/{pid}/thermal-cases", json=payload2)
    assert r2.status_code == 201, r2.text
    assert r2.json()["case"]["source_id"] == tid


def test_source_cross_procedure_rejected(client):
    pid1 = make_approved_proc(client)
    bid1 = confirm_ultrasonic(client, pid1)
    pid2 = make_approved_proc(client)
    r = client.post(f"/procedures/{pid2}/thermal-cases",
                    json=thermal_payload(source_id=bid1))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "initial_load_unconfirmed"


def test_non_confirmable_case_stored_and_confirm_blocked(client):
    """温度断档工况照常落库 201，但确认 409 且列栓号与区间。"""
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    payload = thermal_payload(
        source_id=bid,
        nodes=[{"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
               {"at": "2026-09-13T11:00:00",
                "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}])
    r = client.post(f"/procedures/{pid}/thermal-cases", json=payload)
    assert r.status_code == 201
    cid = r.json()["case"]["id"]
    assert r.json()["result"]["confirmable"] is False
    r = client.post(f"/thermal-cases/{cid}/confirm", json={"reviewer": "r"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "thermal_case_not_confirmed"
    assert "temperature_gap" in detail["blockers"]
    flagged = detail["bolts"][0]
    assert flagged["bolt_no"] == 1
    assert flagged["gaps"][0]["interval"] == ["2026-09-13T08:00:00",
                                              "2026-09-13T11:00:00"]
    # 缺口落 thermal_gaps 留痕
    gaps = client.get(f"/thermal-cases/{cid}").json()["gaps"]
    assert all(g["reason"] == "temperature_gap" for g in gaps)
    assert len(gaps) == 8


def test_second_active_case_rejected(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    assert client.post(f"/procedures/{pid}/thermal-cases",
                       json=thermal_payload(source_id=bid)).status_code == 201
    r = client.post(f"/procedures/{pid}/thermal-cases",
                    json=thermal_payload(source_id=bid))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "active_thermal_case_exists"


def test_bolt_zone_count_mismatch_422(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    r = client.post(f"/procedures/{pid}/thermal-cases",
                    json=thermal_payload(source_id=bid, bolt_zones=["Z1"] * 7))
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "bolt_zone_count_mismatch"


def test_missing_zone_temperature_stored_as_gap_not_rejected(client):
    """温度节点缺已分配的 Z2 分区时请求级不拒绝：工况与缺口落库（201），
    列出受影响栓号与断档区间，工况不可确认；Z1 栓该节点仍正常求解。"""
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    payload = thermal_payload(
        source_id=bid,
        bolt_zones=["Z1"] * 4 + ["Z2"] * 4,
        nodes=[{"at": "2026-09-13T08:00:00",
                "temperatures": [zone("Z1"), zone("Z2")]},
               {"at": "2026-09-13T09:00:00",
                "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}])
    r = client.post(f"/procedures/{pid}/thermal-cases", json=payload)
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["result"]["confirmable"] is False
    assert "temperature_gap" in detail["result"]["blockers"]

    bolts = {b["bolt_no"]: b for b in detail["result"]["bolts"]}
    # Z2 栓（5..8）在第 2 节点缺温度：缺口定位栓号与区间
    z2_gap = next(g for g in bolts[5]["gaps"] if g["reason"] == "temperature_gap")
    assert z2_gap["interval"] == ["2026-09-13T09:00:00", "2026-09-13T09:00:00"]
    assert bolts[5]["series"][1]["states"] == ["temperature_missing"]
    # Z1 栓（1..4）该节点温度齐全，正常求解
    assert bolts[1]["series"][1]["thermal_load_kn"] is not None
    assert not any(g["reason"] == "temperature_gap" for g in bolts[1]["gaps"])

    # 缺口逐条落 thermal_gaps 留痕
    cid = detail["case"]["id"]
    stored_gaps = client.get(f"/thermal-cases/{cid}").json()["gaps"]
    z2_stored = [g for g in stored_gaps if g["bolt_no"] in range(5, 9)]
    assert all(g["reason"] == "temperature_gap" for g in z2_stored)
    assert all(g["interval"] == ["2026-09-13T09:00:00", "2026-09-13T09:00:00"]
               for g in z2_stored)

    # 确认被拒：返回 blockers 与逐栓/区间明细，工况保持 open
    r = client.post(f"/thermal-cases/{cid}/confirm", json={"reviewer": "r"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "thermal_case_not_confirmed"
    flagged = {b["bolt_no"]: b for b in r.json()["detail"]["bolts"]}
    assert 5 in flagged
    assert flagged[5]["gaps"][0]["interval"] == \
        ["2026-09-13T09:00:00", "2026-09-13T09:00:00"]
    assert client.get(f"/thermal-cases/{cid}").json()["case"]["status"] == "open"


def test_revision_requires_reason_and_change(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    cid = client.post(f"/procedures/{pid}/thermal-cases",
                      json=thermal_payload(source_id=bid)).json()["case"]["id"]
    r = client.post(f"/thermal-cases/{cid}/revisions",
                    json={"reason": "仅理由无变更"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "empty_revision"
    # 换更宽的垫片压溃限值与替代材料曲线，须写明理由
    new_gasket = thermal_payload()["gasket"]
    r = client.post(f"/thermal-cases/{cid}/revisions", json={
        "reason": "现场复核垫片为加厚型，替换压缩-回弹曲线与压溃限值",
        "gasket": new_gasket,
        "limits": {**LIMITS, "max_gasket_pressure_mpa": 80.0}})
    assert r.status_code == 201, r.text
    new = r.json()["thermal_case"]
    assert new["case"]["revision"] == 2
    assert new["case"]["parent_id"] == cid
    assert new["case"]["change_note"].startswith("现场复核")
    assert r.json()["diff"]["param_changes"]["limits"]["to"][
        "max_gasket_pressure_mpa"] == 80.0
    # 旧修订废止只读
    assert client.get(f"/thermal-cases/{cid}").json()["case"]["status"] \
        == "superseded"
    r = client.post(f"/thermal-cases/{cid}/revisions",
                    json={"reason": "x", "bolt_zones": ["Z1"] * 8})
    assert r.status_code == 409


def test_revision_diff_endpoint_and_chain(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    cid = client.post(f"/procedures/{pid}/thermal-cases",
                      json=thermal_payload(source_id=bid)).json()["case"]["id"]
    r = client.post(f"/thermal-cases/{cid}/revisions", json={
        "reason": "补测增加一个温度节点",
        "nodes": [
            {"at": "2026-09-13T08:00:00", "temperatures": [zone("Z1")]},
            {"at": "2026-09-13T08:30:00",
             "temperatures": [zone("Z1", tb=100.0, tm=200.0, tg=200.0)]},
            {"at": "2026-09-13T09:00:00",
             "temperatures": [zone("Z1", tb=120.0, tm=200.0, tg=200.0)]}]})
    new_id = r.json()["thermal_case"]["case"]["id"]
    r = client.get(f"/thermal-cases/{new_id}/diff")
    assert r.status_code == 200
    assert "2026-09-13T08:30:00" in r.json()["diff"]["timeline"]["added_nodes"]
    assert client.get(f"/thermal-cases/{cid}/diff").status_code == 409
    chain = client.get(f"/procedures/{pid}/thermal-cases").json()["thermal_cases"]
    assert [c["revision"] for c in chain] == [1, 2]
    assert chain[0]["status"] == "superseded"
    assert chain[1]["blockers"] == []


def test_package_shares_same_thermal_case(client):
    pid = make_approved_proc(client)
    bid = confirm_ultrasonic(client, pid)
    created = client.post(f"/procedures/{pid}/thermal-cases",
                          json=thermal_payload(source_id=bid)).json()
    cid = created["case"]["id"]
    package = client.get(f"/procedures/{pid}/package").json()
    detail = client.get(f"/thermal-cases/{cid}").json()
    assert package["thermal"] is not None
    assert package["thermal"]["case"] == detail["case"]
    assert package["thermal"]["frozen"] == detail["frozen"]
    assert package["thermal"]["result"] == detail["result"]
    assert package["thermal"]["initial_loads_kn"] == detail["initial_loads_kn"]
    # 无热态工况工艺为 None
    pid2 = make_approved_proc(client)
    assert client.get(f"/procedures/{pid2}/package").json()["thermal"] is None
