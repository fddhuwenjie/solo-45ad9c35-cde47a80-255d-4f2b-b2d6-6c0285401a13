"""超声伸长复核端到端：建批冻结、基线、复测、证据缺口、重测/排除、确认、补拧派生。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.ultrasonic import (compensated_sound_velocity, elongation_mm,
                            evaluate_batch, evaluate_reading)

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

# 对中预检冻结几何（PN40 DN200 示例）
ALIGN_GEOM = {
    "flange_face_diameter_mm": 285.0,
    "gasket_inner_diameter_mm": 220.0,
    "gasket_outer_diameter_mm": 270.0,
    "bore_diameter_mm": 200.0,
    "max_parallelism_mm": 1.0,
    "max_radial_mismatch_mm": 2.0,
}
EDGE0 = (285.0 - 270.0) / 2.0


def submit_alignment(client, pid):
    """提交一次通过的 8 方位对中预检（均匀 2mm 间隙、完全对中）。"""
    points = [{"angle_deg": a, "axial_gap": 2.0, "radial_offset": 0.0,
               "gasket_edge_position": EDGE0, "bolt_free_insertion": True}
              for a in (0, 45, 90, 135, 180, 225, 270, 315)]
    r = client.post(f"/procedures/{pid}/alignment-checks",
                    json={**ALIGN_GEOM, "points": points, "operator": "预检员",
                          "measured_at": "2026-09-12T08:00:00"})
    assert r.status_code == 201, r.text
    return r.json()["alignment_check"]

# 冻结参数
L_MM = 150.0
AREA = 353.0
E_MOD = 206000.0
V0 = 5900.0
ALPHA = -1.0e-4
T_REF = 20.0
TOF0 = 2 * (L_MM / 1000.0) / V0          # 未承载基线飞行时间 ≈ 5.0847e-5 s

BATCH = {
    "length_mm": L_MM,
    "area_mm2": AREA,
    "elastic_modulus_mpa": E_MOD,
    "sound_velocity": V0,
    "temp_coefficient": ALPHA,
    "reference_temp_c": T_REF,
    "temp_comp_min_c": 0.0,
    "temp_comp_max_c": 60.0,
    "target_load_min_kn": 120.0,
    "target_load_max_kn": 160.0,
    "material_load_limit_kn": 180.0,
    "max_imbalance_pct": 15.0,
    "instrument_id": "US-77",
    "instrument_calibration_until": "2026-12-31",
}


def tof_for_load(load_kn: float, temp: float = T_REF) -> float:
    """反推给定预紧力（kN）在给定温度下的复测飞行时间。"""
    vt = compensated_sound_velocity(V0, ALPHA, T_REF, temp)
    dl_mm = load_kn * 1000.0 * L_MM / (E_MOD * AREA)
    return TOF0 * V0 / vt + dl_mm / (vt * 500.0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def make_approved(client) -> int:
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    return pid


def start_and_complete(client, pid):
    assert client.post(f"/procedures/{pid}/start").status_code == 200
    for ratio in (0.3, 0.6, 1.0):
        target = round(320.0 * ratio, 2)
        for bolt in SEQ8:
            r = client.post(f"/procedures/{pid}/reports", json={
                "bolt_no": bolt, "tool_id": "TW-1001", "operator": "张三",
                "reported_at": "2026-09-10T09:00:00", "measured_torque": target})
            assert r.status_code == 201, r.text


def make_batch(client, pid, **overrides):
    r = client.post(f"/procedures/{pid}/measurement-batches", json={**BATCH, **overrides})
    assert r.status_code == 201, r.text
    return r.json()["batch"]["id"]


def baselines_all(client, bid, tof=TOF0):
    for bolt in range(1, 9):
        r = client.post(f"/measurement-batches/{bid}/baselines",
                        json={"bolt_no": bolt, "tof_s": tof})
        assert r.status_code == 201, r.text


def readings_all(client, bid, loads, *, temp=T_REF, ts="2026-09-12T10:00:00"):
    for bolt in range(1, 9):
        r = client.post(f"/measurement-batches/{bid}/readings", json={
            "bolt_no": bolt, "tof_s": tof_for_load(loads[bolt], temp),
            "temperature_c": temp, "operator": "王五", "measured_at": ts})
        assert r.status_code == 201, r.text
        yield bolt, r


# ------------------------------------------------------------ 物理换算单测

def test_elongation_physics_handcalc():
    batch = {"sound_velocity": V0, "temp_coefficient": ALPHA,
             "reference_temp_c": T_REF, "length_mm": L_MM}
    # 140 kN 期望伸长 ≈ 0.2888 mm，时差 ≈ 9.79e-8 s
    tof = tof_for_load(140.0)
    dl = elongation_mm(batch, TOF0, tof, T_REF)
    assert dl == pytest.approx(140_000 * L_MM / (E_MOD * AREA), rel=1e-9)
    load = E_MOD * AREA * dl / L_MM / 1000.0
    assert load == pytest.approx(140.0, rel=1e-9)


def test_temperature_compensation_neutralizes_tof_shift():
    """同载荷在 40℃ 下因声速下降需更长 tof；补偿后伸长量一致。"""
    tof20 = tof_for_load(140.0, 20.0)
    tof40 = tof_for_load(140.0, 40.0)
    assert tof40 > tof20
    batch = {"sound_velocity": V0, "temp_coefficient": ALPHA,
             "reference_temp_c": T_REF, "length_mm": L_MM}
    d20 = elongation_mm(batch, TOF0, tof20, 20.0)
    d40 = elongation_mm(batch, TOF0, tof40, 40.0)
    assert d40 == pytest.approx(d20, rel=1e-9)


def test_diametral_imbalance_detection():
    frozen = {**{k: BATCH[k] for k in (
        "length_mm", "area_mm2", "elastic_modulus_mpa", "sound_velocity",
        "temp_coefficient", "reference_temp_c", "temp_comp_min_c", "temp_comp_max_c",
        "target_load_min_kn", "target_load_max_kn", "material_load_limit_kn",
        "max_imbalance_pct", "instrument_calibration_until")},
        "bolt_count": 8, "scope_bolts": list(range(1, 9)),
        "locked_bolts": [], "locked_results": {},
        "instrument_calibration_until": "2026-12-31"}
    # 直接构造读数：3 号栓 150 kN，对径 7 号 100 kN -> 不平衡 40% > 15%
    loads = {1: 140, 2: 140, 3: 150, 4: 140, 5: 140, 6: 140, 7: 100, 8: 140}
    baselines = [{"bolt_no": b, "tof_s": TOF0} for b in range(1, 9)]
    readings = [{"id": b, "bolt_no": b, "tof_s": tof_for_load(loads[b]),
                 "temperature_c": T_REF, "operator": "x",
                 "measured_at": "2026-09-12T10:00:00", "excluded": False,
                 "amendment_note": None, "supersedes": None} for b in range(1, 9)]
    v = evaluate_batch(frozen, baselines, readings)
    pair_37 = [d for d in v.diametral if d["bolt_a"] == 3][0]
    assert pair_37["imbalance_pct"] == pytest.approx(40.0, abs=0.01)
    assert "diametral_imbalance" in v.blockers
    assert v.confirmed is False
    assert v.dispersion_cv_pct is not None and v.dispersion_cv_pct > 0


# ------------------------------------------------------------ 建批冻结

def test_batch_requires_approved(client):
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    r = client.post(f"/procedures/{pid}/measurement-batches", json=BATCH)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_approvable_for_measurement"


def test_batch_freezes_params_and_rejects_bad_band(client):
    pid = make_approved(client)
    r = client.post(f"/procedures/{pid}/measurement-batches",
                    json={**BATCH, "target_load_min_kn": 170.0})
    assert r.status_code == 422  # 目标带高于材料上限
    r = client.post(f"/procedures/{pid}/measurement-batches",
                    json={**BATCH, "temp_comp_max_c": -10.0})
    assert r.status_code == 422
    bid = make_batch(client, pid)
    detail = client.get(f"/measurement-batches/{bid}").json()
    assert detail["batch"]["length_mm"] == L_MM
    assert detail["batch"]["revision"] == 1
    assert detail["batch"]["scope_bolts"] == list(range(1, 9))
    # 同一工艺只允许一个开放批次
    assert client.post(f"/procedures/{pid}/measurement-batches", json=BATCH).status_code == 409


def test_baseline_window_and_uniqueness(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    # 批准后即可提交基线
    assert client.post(f"/measurement-batches/{bid}/baselines",
                       json={"bolt_no": 1, "tof_s": TOF0}).status_code == 201
    # 重复基线拒绝（不覆盖）
    r = client.post(f"/measurement-batches/{bid}/baselines",
                    json={"bolt_no": 1, "tof_s": TOF0 + 1e-9})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "baseline_exists"
    # 完工后基线窗口关闭
    start_and_complete(client, pid)
    r = client.post(f"/measurement-batches/{bid}/baselines",
                    json={"bolt_no": 2, "tof_s": TOF0})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "baseline_window_closed"


def test_reading_requires_completed(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(140), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_ready_for_remeasurement"


# ------------------------------------------------------------ 证据缺口

def _gap_case(client, **batch_overrides):
    pid = make_approved(client)
    bid = make_batch(client, pid, **batch_overrides)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    return pid, bid


def test_gap_baseline_missing_recorded_not_confirmed(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    # 只录 1..7 基线，8 号缺基线
    for bolt in range(1, 8):
        client.post(f"/measurement-batches/{bid}/baselines",
                    json={"bolt_no": bolt, "tof_s": TOF0})
    start_and_complete(client, pid)
    for bolt in range(1, 9):
        client.post(f"/measurement-batches/{bid}/readings", json={
            "bolt_no": bolt, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
            "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    detail = client.get(f"/measurement-batches/{bid}").json()
    b8 = [b for b in detail["verdict"]["bolts"] if b["bolt_no"] == 8][0]
    assert "baseline_missing" in b8["gaps"]
    assert b8["load_kn"] is None
    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 409
    assert "evidence_gaps" in r.json()["detail"]["blockers"]


def test_gap_calibration_expired(client):
    pid, bid = _gap_case(client, instrument_calibration_until="2026-08-31")
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.status_code == 201  # 缺口照记
    assert r.json()["valid"] is False
    assert "calibration_expired" in r.json()["bolt_result"]["gaps"]


def test_gap_temperature_out_of_range(client):
    pid, bid = _gap_case(client)
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(140.0, 70.0), "temperature_c": 70.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.json()["valid"] is False
    assert "temperature_out_of_range" in r.json()["bolt_result"]["gaps"]


def test_gap_non_positive_elongation(client):
    pid, bid = _gap_case(client)
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": TOF0 - 1e-8, "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.json()["valid"] is False
    assert "non_positive_elongation" in r.json()["bolt_result"]["gaps"]


def test_gap_load_over_material_limit(client):
    pid, bid = _gap_case(client)
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(200.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.json()["valid"] is False
    assert "load_over_material_limit" in r.json()["bolt_result"]["gaps"]


# ------------------------------------------------------------ 确认与指标

def test_full_confirm_flow(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    list(readings_all(client, bid, loads))

    detail = client.get(f"/measurement-batches/{bid}").json()
    for b in detail["verdict"]["bolts"]:
        assert b["load_kn"] == pytest.approx(140.0, abs=1e-3)
        assert b["in_target_band"] is True
        assert b["deviation_pct"] == pytest.approx(0.0, abs=1e-6)
    assert detail["verdict"]["dispersion_cv_pct"] == 0.0
    assert detail["verdict"]["max_imbalance_pct"] == 0.0

    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"
    assert r.json()["confirmed_revision"] == 1
    # 已确认批次只读
    assert client.post(
        f"/measurement-batches/{bid}/readings", json={
            "bolt_no": 1, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
            "operator": "x", "measured_at": "2026-09-12T11:00:00"}).status_code == 409


def test_target_band_violation_blocks(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    loads[3] = 100.0  # 低于目标带，对径 7 号仍 140 -> 不平衡同时超标
    list(readings_all(client, bid, loads))
    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 409
    blockers = r.json()["detail"]["blockers"]
    assert "target_band_exceeded" in blockers
    assert "diametral_imbalance" in blockers


# ------------------------------------------------------------ 重测与排除（留痕）

def test_retest_keeps_original_and_bumps_revision(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    r = client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(100.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    assert r.json()["bolt_result"]["in_target_band"] is False
    r = client.post(f"/measurement-batches/{bid}/retests", json={
        "bolt_no": 1, "tof_s": tof_for_load(140.0), "temperature_c": 22.0,
        "operator": "赵六", "measured_at": "2026-09-12T12:00:00",
        "reason": "首测耦合剂涂抹不均，重新耦合后复测"})
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2
    assert r.json()["bolt_result"]["in_target_band"] is True
    detail = client.get(f"/measurement-batches/{bid}").json()
    readings = detail["readings"]
    assert len(readings) == 2                       # 原值保留
    assert readings[0]["id"] != readings[1]["id"]
    assert readings[1]["supersedes"] == readings[0]["id"]
    assert readings[0]["tof_s"] != readings[1]["tof_s"]
    assert "重测" in readings[1]["amendment_note"]


def test_exclude_requires_reason_and_retest(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 2, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    r = client.post(f"/measurement-batches/{bid}/exclusions",
                    json={"bolt_no": 2, "reason": "探头波形异常，读数不可信"})
    assert r.status_code == 201
    assert r.json()["revision"] == 2
    detail = client.get(f"/measurement-batches/{bid}").json()
    excluded = [x for x in detail["readings"] if x["bolt_no"] == 2][0]
    assert excluded["excluded"] is True             # 原值保留
    assert "排除" in excluded["amendment_note"]
    # 确认被 reading_excluded 阻断
    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 409
    assert "evidence_gaps" in r.json()["detail"]["blockers"]
    # 重测补证
    r = client.post(f"/measurement-batches/{bid}/retests", json={
        "bolt_no": 2, "tof_s": tof_for_load(142.0), "temperature_c": 21.0,
        "operator": "赵六", "measured_at": "2026-09-12T13:00:00",
        "reason": "排除异常波形读数后重测"})
    assert r.status_code == 201 and r.json()["revision"] == 3
    assert r.json()["bolt_result"]["gaps"] == []


# ------------------------------------------------------------ 补拧派生

def test_failed_batch_derives_rework_locking_good_bolts(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    loads[3] = 105.0  # 3 号栓偏低
    list(readings_all(client, bid, loads))
    assert client.post(f"/measurement-batches/{bid}/confirm").status_code == 409

    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["target_bolts"] == [3]
    assert body["locked_bolts"] == [1, 2, 4, 5, 6, 7, 8]
    rw_pid = body["rework_procedure"]["id"]
    assert body["rework_procedure"]["stage_ratios"] == [1.0]
    assert body["rework_procedure"]["parent_id"] == pid
    assert body["plan"] == [
        {"round_no": 1, "order_in_round": 1, "bolt_no": 3,
         "ratio": 1.0, "target_torque": 320.0,
         "tool_id": "TW-1001", "scheduled_at": None, "angle_deg": 90.0}]
    # 源批次废止
    assert client.get(f"/measurement-batches/{bid}").json()["batch"]["status"] == "superseded"

    # 锁定栓回传被拒
    submit_alignment(client, rw_pid)
    assert client.post(f"/procedures/{rw_pid}/approve").status_code == 200
    r = client.post(f"/procedures/{rw_pid}/start")
    assert r.status_code == 200
    r = client.post(f"/procedures/{rw_pid}/reports", json={
        "bolt_no": 5, "tool_id": "TW-1001", "operator": "张三",
        "reported_at": "2026-09-12T15:00:00", "measured_torque": 320.0})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "bolt_locked"

    # 新测量批次继承冻结参数与锁定结果，范围仅 3 号栓
    r = client.post(f"/procedures/{rw_pid}/measurement-batches", json=BATCH)
    assert r.status_code == 201, r.text
    rbid = r.json()["batch"]["id"]
    assert r.json()["batch"]["scope_bolts"] == [3]
    assert set(r.json()["batch"]["locked_bolts"]) == {1, 2, 4, 5, 6, 7, 8}
    assert r.json()["batch"]["derived_from_batch_id"] == bid
    # 锁定栓不在范围：不能提交基线/复测
    r = client.post(f"/measurement-batches/{rbid}/baselines",
                    json={"bolt_no": 5, "tof_s": TOF0})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "bolt_out_of_scope"
    client.post(f"/measurement-batches/{rbid}/baselines",
                json={"bolt_no": 3, "tof_s": TOF0})

    # 3 号栓补拧到 140 kN，整圈（含锁定栓）复核通过
    r = client.post(f"/procedures/{rw_pid}/reports", json={
        "bolt_no": 3, "tool_id": "TW-1001", "operator": "张三",
        "reported_at": "2026-09-12T15:10:00", "measured_torque": 320.0})
    assert r.status_code == 201 and r.json()["status"] == "completed"
    r = client.post(f"/measurement-batches/{rbid}/readings", json={
        "bolt_no": 3, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "赵六", "measured_at": "2026-09-12T15:20:00"})
    assert r.status_code == 201 and r.json()["valid"] is True
    detail = client.get(f"/measurement-batches/{rbid}").json()
    assert len(detail["verdict"]["bolts"]) == 8
    assert {b["bolt_no"] for b in detail["verdict"]["bolts"] if b["locked"]} == {
        1, 2, 4, 5, 6, 7, 8}
    assert all(b["in_target_band"] for b in detail["verdict"]["bolts"])
    assert detail["verdict"]["max_imbalance_pct"] == 0.0
    r = client.post(f"/measurement-batches/{rbid}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    # 作业包与 SVG 引用同一测量版本
    pkg = client.get(f"/procedures/{rw_pid}/package").json()
    assert pkg["measurement"]["batch_id"] == rbid
    assert pkg["measurement"]["revision"] == 1
    svg = client.get(f"/procedures/{rw_pid}/diagram.svg").text
    assert f"批次 #{rbid} r1" in svg
    assert "<svg" in svg


def test_derive_rework_nothing_when_all_good(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    list(readings_all(client, bid, {b: 140.0 for b in range(1, 9)}))
    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "nothing_to_rework"


# ------------------------------------------------------------ 状态与前置条件

def test_retest_and_exclude_require_existing_reading(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    r = client.post(f"/measurement-batches/{bid}/retests", json={
        "bolt_no": 1, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "赵六", "measured_at": "2026-09-12T12:00:00", "reason": "x"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "no_reading_to_retest"
    r = client.post(f"/measurement-batches/{bid}/exclusions",
                    json={"bolt_no": 1, "reason": "x"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "no_reading_to_exclude"


def test_duplicate_reading_requires_retest_with_reason(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    payload = {"bolt_no": 4, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
               "operator": "王五", "measured_at": "2026-09-12T10:00:00"}
    assert client.post(f"/measurement-batches/{bid}/readings", json=payload).status_code == 201
    r = client.post(f"/measurement-batches/{bid}/readings", json=payload)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "reading_exists_use_retest"
    # 无理由重测在 Pydantic 层即 422
    r = client.post(f"/measurement-batches/{bid}/retests", json=payload)
    assert r.status_code == 422


def test_duplicate_exclusion_rejected(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 2, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    client.post(f"/measurement-batches/{bid}/exclusions",
                json={"bolt_no": 2, "reason": "波形异常"})
    r = client.post(f"/measurement-batches/{bid}/exclusions",
                    json={"bolt_no": 2, "reason": "再次排除"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "reading_already_excluded"


def test_confirm_and_rework_on_confirmed_batch_rejected(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    list(readings_all(client, bid, {b: 140.0 for b in range(1, 9)}))
    assert client.post(f"/measurement-batches/{bid}/confirm").status_code == 200
    assert client.post(f"/measurement-batches/{bid}/confirm").status_code == 409
    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 409 and "batch_confirmed" in r.json()["detail"]["reason"]


def test_package_references_measurement_version(client):
    pid = make_approved(client)
    pkg_before = client.get(f"/procedures/{pid}/package").json()
    assert pkg_before["measurement"] is None
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    list(readings_all(client, bid, {b: 140.0 for b in range(1, 9)}))
    # 确认前：开放批次
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["measurement"]["batch_id"] == bid
    assert pkg["measurement"]["status"] == "open"
    client.post(f"/measurement-batches/{bid}/confirm")
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["measurement"]["status"] == "confirmed"
    assert pkg["measurement"]["confirmed_revision"] == 1
    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert f"批次 #{bid} r1" in svg and "已确认" in svg


def test_evidence_gaps_persisted(client):
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    # 仅提交 1 号栓一条超材料上限读数
    client.post(f"/measurement-batches/{bid}/readings", json={
        "bolt_no": 1, "tof_s": tof_for_load(200.0), "temperature_c": 20.0,
        "operator": "王五", "measured_at": "2026-09-12T10:00:00"})
    detail = client.get(f"/measurement-batches/{bid}").json()
    gaps = detail["verdict"]["evidence_gaps"]
    reasons = {g["bolt_no"]: g["reasons"] for g in gaps}
    assert "load_over_material_limit" in reasons[1]
    assert reasons[2] == ["reading_missing"]
    assert all(g["messages"] for g in gaps)


def test_rework_plan_respects_cross_sequence(client):
    """补拧计划按交叉顺序过滤锁定栓：8 栓仅补 3、7 时顺序为 3,7（对径，合法）。"""
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    loads[3] = loads[7] = 105.0  # 一对对径栓偏低
    list(readings_all(client, bid, loads))
    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 201, r.text
    assert r.json()["target_bolts"] == [3, 7]
    assert [s["bolt_no"] for s in r.json()["plan"]] == [3, 7]
    assert [s["order_in_round"] for s in r.json()["plan"]] == [1, 2]


def test_imbalance_only_failure_derives_rework(client):
    """回归：各栓均在目标带 120~160 kN 内，仅 1/5 对径不平衡 28.57% 超限。

    旧逻辑只按逐栓目标带选补拧栓 -> 误报 nothing_to_rework；
    新逻辑须选出较低载荷栓 1（120 kN）补拧，锁定其余 7 栓。
    """
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    loads[1] = 120.0  # 1 号栓目标带下沿
    loads[5] = 160.0  # 对径 5 号栓目标带上沿 -> 不平衡 |120-160|/140 = 28.5714%
    list(readings_all(client, bid, loads))

    # 确认失败：唯一阻断因素是对径不平衡（无证据缺口、无超目标带）
    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["blockers"] == ["diametral_imbalance"]
    pair_15 = [d for d in r.json()["detail"]["verdict"]["diametral_imbalance"]
               if d["bolt_a"] == 1][0]
    assert pair_15["imbalance_pct"] == pytest.approx(28.5714, abs=1e-4)

    # 派生补拧：不再 nothing_to_rework，较低载荷栓 1 被选中
    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["target_bolts"] == [1]
    assert body["locked_bolts"] == [2, 3, 4, 5, 6, 7, 8]
    assert "1" in body["rework_reasons"]
    assert any("diametral_imbalance:1/5" in rsn
               for rsn in body["rework_reasons"]["1"])
    assert [s["bolt_no"] for s in body["plan"]] == [1]


def test_imbalance_picks_lower_bolt_and_full_rework_chain(client):
    """2/6 对 125 vs 155（21.4%）：补拧 2 号栓到带内中部后整圈复核通过。"""
    pid = make_approved(client)
    bid = make_batch(client, pid)
    baselines_all(client, bid)
    start_and_complete(client, pid)
    loads = {b: 140.0 for b in range(1, 9)}
    loads[2], loads[6] = 125.0, 155.0
    list(readings_all(client, bid, loads))
    r = client.post(f"/measurement-batches/{bid}/derive-rework")
    assert r.status_code == 201
    assert r.json()["target_bolts"] == [2]

    rw_pid = r.json()["rework_procedure"]["id"]
    submit_alignment(client, rw_pid)
    client.post(f"/procedures/{rw_pid}/approve")
    client.post(f"/procedures/{rw_pid}/start")

    # 补拧前（in_progress）建立批次并采集补拧栓基线
    r = client.post(f"/procedures/{rw_pid}/measurement-batches", json=BATCH)
    rbid = r.json()["batch"]["id"]
    assert r.json()["batch"]["scope_bolts"] == [2]
    assert client.post(f"/measurement-batches/{rbid}/baselines",
                       json={"bolt_no": 2, "tof_s": TOF0}).status_code == 201

    # 末轮补拧
    rep = client.post(f"/procedures/{rw_pid}/reports", json={
        "bolt_no": 2, "tool_id": "TW-1001", "operator": "张三",
        "reported_at": "2026-09-12T15:00:00", "measured_torque": 320.0})
    assert rep.status_code == 201 and rep.json()["status"] == "completed"

    r = client.post(f"/measurement-batches/{rbid}/readings", json={
        "bolt_no": 2, "tof_s": tof_for_load(140.0), "temperature_c": 20.0,
        "operator": "赵六", "measured_at": "2026-09-12T15:20:00"})
    assert r.status_code == 201 and r.json()["valid"] is True
    v = client.get(f"/measurement-batches/{rbid}").json()["verdict"]
    # 2 号栓补拧到 140，与 6 号（155）不平衡 10.17% < 15%，全部入带 -> 可确认
    assert v["confirmed"] is True
    assert v["blockers"] == []
    assert client.post(f"/measurement-batches/{rbid}/confirm").status_code == 200
