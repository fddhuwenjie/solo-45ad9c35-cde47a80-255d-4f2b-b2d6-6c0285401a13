"""液压张拉执行端到端：建案冻结、分轮换位、分组回传校验、确认门禁、
人工改组/中断重排修订、采纳超声实测、版本差异与作业包一致性。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.tensioning import (build_scheme, flatten_groups, load_for_pressure_kn,
                            predicted_residual_kn, predicted_stroke_mm,
                            pressure_for_load_mpa, required_applied_load_kn,
                            residual_band_kn)

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

# 张拉冻结参数：F_target=140kN±10%，λ=0.15，A_h=2000mm²
# => 末轮设定泵压 140/0.85/2000*1000 ≈ 82.353MPa，预测行程 ≈ 0.34mm
PLAN = {
    "area_mm2": 353.0,
    "length_mm": 150.0,
    "elastic_modulus_mpa": 206000.0,
    "target_load_kn": 140.0,
    "load_tolerance_pct": 10.0,
    "tensioner_id": "HT-01",
    "tensioner_count": 2,
    "hydraulic_area_mm2": 2000.0,
    "max_pressure_mpa": 100.0,
    "max_stroke_mm": 5.0,
    "min_tool_spacing": 2,
    "load_transfer_coefficient": 0.15,
    "min_hold_seconds": 30.0,
    "pressure_sync_tolerance_pct": 5.0,
    "gauge_id": "PG-1",
    "gauge_calibration_until": "2026-12-31",
    "stage_ratios": [0.5, 1.0],
}
P1 = 70.0 / 1.7    # 第 1 轮目标残余 70kN 对应通道压力 ≈ 41.176MPa
P2 = 140.0 / 1.7   # 第 2 轮目标残余 140kN 对应通道压力 ≈ 82.353MPa

# 超声批次冻结参数（采纳超声实测用）
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
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    return pid


def make_plan(client, pid=None, **overrides) -> tuple[int, int]:
    """建 approved 工艺 + 张拉方案（open），返回 (pid, plan_id)。"""
    if pid is None:
        pid = make_approved_proc(client)
    r = client.post(f"/procedures/{pid}/tensioning-plans", json={**PLAN, **overrides})
    assert r.status_code == 201, r.text
    return pid, r.json()["plan"]["id"]


def make_approved_plan(client, **overrides) -> tuple[int, int]:
    pid, tid = make_plan(client, **overrides)
    r = client.post(f"/tensioning-plans/{tid}/approve")
    assert r.status_code == 200, r.text
    return pid, tid


def group_payload(round_no, group_no, bolts, pressure, **kw):
    """构造一组合格回传：各通道同压、行程 0.3mm、保压 60s、逆序卸压。"""
    body = {
        "round_no": round_no, "group_no": group_no,
        "operator": "李四", "reported_at": "2026-09-12T10:00:00",
        "gauge_id": "PG-1", "hold_seconds": 60.0,
        "release_order": list(reversed(bolts)),
        "channels": [{"bolt_no": b, "pressure_mpa": pressure, "stroke_mm": 0.3}
                     for b in bolts],
    }
    body.update(kw)
    return body


def scheme_groups(client, tid, round_no):
    detail = client.get(f"/tensioning-plans/{tid}").json()
    rd = next(r for r in detail["scheme"] if r["round_no"] == round_no)
    return [(g["group_no"], g["bolts"]) for g in rd["groups"]]


def report_all(client, tid):
    """按方案顺序回传全部组（各通道压力取当轮设定值）。"""
    detail = client.get(f"/tensioning-plans/{tid}").json()
    for rd in detail["scheme"]:
        pressure = P1 if rd["round_no"] == 1 else P2
        for g in rd["groups"]:
            r = client.post(f"/tensioning-plans/{tid}/round-reports",
                            json=group_payload(rd["round_no"], g["group_no"],
                                               g["bolts"], pressure))
            assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 纯函数

def test_load_pressure_roundtrip():
    applied = required_applied_load_kn(140.0, 1.0, 0.15)
    assert applied == pytest.approx(164.705882, rel=1e-6)
    p = pressure_for_load_mpa(applied, 2000.0)
    assert p == pytest.approx(82.352941, rel=1e-6)
    assert load_for_pressure_kn(p, 2000.0) == pytest.approx(applied)
    assert predicted_residual_kn(applied, 0.15) == pytest.approx(140.0)
    assert predicted_stroke_mm(applied, 150.0, 206000.0, 353.0) \
        == pytest.approx(0.339741, rel=1e-4)
    assert residual_band_kn(140.0, 1.0, 10.0) == (126.0, 154.0)


def test_scheme_full_coverage_spacing_and_rotation():
    """每轮全覆盖且每栓恰好一次；组内间隔达标；逐轮换位（组归属不同）。"""
    rounds = build_scheme(8, [0.5, 0.6, 1.0], 2, 2)
    memberships = []
    for rd in rounds:
        bolts = [b for g in rd["groups"] for b in g["bolts"]]
        assert sorted(bolts) == list(range(1, 9))
        for g in rd["groups"]:
            assert len(g["bolts"]) <= 2
            if len(g["bolts"]) == 2:
                a, b = g["bolts"]
                d = min(abs(a - b) % 8, 8 - abs(a - b) % 8)
                assert d >= 2
        memberships.append({tuple(sorted(g["bolts"])) for g in rd["groups"]})
    # 换位：各轮组归属不完全相同
    assert len({frozenset(m) for m in memberships}) > 1


def test_scheme_prefix_stable_regroup():
    """同参数重排复现剩余组（中断只重排未完成组）；改参数仅影响未完成组。"""
    locked = [{"round_no": 1, "group_no": 1, "bolts": [1, 5]}]
    base = build_scheme(8, [0.5, 1.0], 2, 2)
    regen = build_scheme(8, [0.5, 1.0], 2, 2, locked_groups=locked)
    assert [g["bolts"] for g in regen[0]["groups"]] \
        == [g["bolts"] for g in base[0]["groups"]]
    assert regen[0]["groups"][0]["locked"] is True
    bigger = build_scheme(8, [0.5, 1.0], 4, 2, locked_groups=locked)
    assert bigger[0]["groups"][0]["bolts"] == [1, 5]  # 已完成组原位锁定
    assert bigger[0]["groups"][0]["locked"] is True
    assert sorted(b for g in bigger[0]["groups"] for b in g["bolts"]) == list(range(1, 9))


# ---------------------------------------------------------------- 建案与批准

def test_create_requires_approved_procedure(client):
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    r = client.post(f"/procedures/{pid}/tensioning-plans", json=PLAN)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_approvable_for_tensioning"


def test_create_generates_scheme_and_freezes_params(client):
    pid, tid = make_plan(client)
    detail = client.get(f"/tensioning-plans/{tid}").json()
    assert detail["plan"]["status"] == "open"
    assert detail["plan"]["target_load_kn"] == 140.0
    assert detail["plan"]["gauge_id"] == "PG-1"
    assert [g["bolts"] for g in detail["scheme"][0]["groups"]] \
        == [[1, 5], [2, 6], [3, 7], [4, 8]]
    assert detail["scheme"][0]["set_pressure_mpa"] == pytest.approx(41.1765, abs=1e-3)
    assert detail["scheme"][1]["residual_band_kn"] == [126.0, 154.0]
    # 逐轮换位：第 2 轮组归属与第 1 轮不同
    assert [g["bolts"] for g in detail["scheme"][1]["groups"]] \
        != [g["bolts"] for g in detail["scheme"][0]["groups"]]


def test_create_rejects_infeasible_capacity_and_stroke(client):
    pid = make_approved_proc(client)
    r = client.post(f"/procedures/{pid}/tensioning-plans",
                    json={**PLAN, "max_pressure_mpa": 50.0})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "tensioning_infeasible"
    assert any("pressure" in c["problems"] for c in detail["conflicts"])
    r = client.post(f"/procedures/{pid}/tensioning-plans",
                    json={**PLAN, "max_stroke_mm": 0.1})
    assert r.status_code == 409
    assert any("stroke" in c["problems"] for c in r.json()["detail"]["conflicts"])


def test_create_rejects_second_active_plan(client):
    pid, tid = make_plan(client)
    r = client.post(f"/procedures/{pid}/tensioning-plans", json=PLAN)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "active_plan_exists"


def test_approve_snapshot_and_report_window(client):
    pid, tid = make_plan(client)
    # 未批准禁止回传
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "plan_not_approved"
    r = client.post(f"/tensioning-plans/{tid}/approve")
    assert r.status_code == 200
    detail = r.json()
    assert detail["plan"]["status"] == "approved"
    assert detail["plan"]["approved_at"] is not None
    # 重复批准拒绝
    assert client.post(f"/tensioning-plans/{tid}/approve").status_code == 409


# ---------------------------------------------------------------- 回传校验

def test_full_flow_report_and_confirm(client):
    pid, tid = make_approved_plan(client)
    report_all(client, tid)
    detail = client.get(f"/tensioning-plans/{tid}").json()
    assert all(g["status"] == "done" for rd in detail["scheme"] for g in rd["groups"])
    # 逐栓换算：施加载荷 ≈ 164.71kN，预测残余 ≈ 140kN
    last = detail["reports"][-1]
    assert last["channels"][0]["applied_load_kn"] == pytest.approx(164.705882, abs=1e-3)
    assert last["channels"][0]["residual_load_kn"] == pytest.approx(140.0, abs=1e-3)
    r = client.post(f"/tensioning-plans/{tid}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["status"] == "confirmed"
    # 确认后只读
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "plan_read_only"


def test_confirm_blocked_by_incomplete_coverage(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1))
    assert r.status_code == 201
    r = client.post(f"/tensioning-plans/{tid}/confirm")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "tensioning_not_confirmed"
    assert "incomplete_coverage" in detail["blockers"]
    assert {"round_no": 1, "group_no": 2, "bolts": [2, 6]} \
        in detail["evaluation"]["missing_groups"]


def test_report_out_of_sequence(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 2, [2, 6], P1))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "out_of_sequence"
    assert detail["expected_bolts"] == [1, 5]


def test_report_coverage_conflict(client):
    pid, tid = make_approved_plan(client)
    # 通道含他组栓 6，缺本组栓 5
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 6], P1))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "coverage_conflict"
    assert detail["bolt_no"] == 6
    assert detail["missing_bolts"] == [5]
    assert detail["extra_bolts"] == [6]
    # 异常留痕
    anomalies = client.get(f"/procedures/{pid}/package").json()["anomalies"]
    assert anomalies[-1]["reason"] == "coverage_conflict"


def test_report_gauge_mismatch_and_calibration_expired(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1, gauge_id="PG-9"))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "gauge_mismatch"
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1,
                                       reported_at="2027-01-05T09:00:00"))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "calibration_expired"


def test_report_hold_insufficient_and_release_order(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1, hold_seconds=10.0))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "hold_insufficient"
    assert detail["allowed_interval"] == [30.0, None]
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1, release_order=[5]))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "release_order_invalid"


def test_report_stroke_and_pressure_capacity(client):
    pid, tid = make_approved_plan(client)
    payload = group_payload(1, 1, [1, 5], P1)
    payload["channels"][1]["stroke_mm"] = 6.0
    r = client.post(f"/tensioning-plans/{tid}/round-reports", json=payload)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "stroke_exceeded"
    assert detail["bolt_no"] == 5
    assert detail["allowed_interval"] == [0, 5.0]
    payload = group_payload(1, 1, [1, 5], 120.0)
    r = client.post(f"/tensioning-plans/{tid}/round-reports", json=payload)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "pressure_over_capacity"
    assert detail["allowed_interval"] == [0, 100.0]


def test_report_pressure_out_of_sync(client):
    pid, tid = make_approved_plan(client)
    payload = group_payload(1, 1, [1, 5], P1)
    payload["channels"][1]["pressure_mpa"] = 45.0  # 极差 ≈8.9% > 5%
    r = client.post(f"/tensioning-plans/{tid}/round-reports", json=payload)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "pressure_out_of_sync"
    assert detail["bolt_no"] == 5
    assert detail["min_bolt_no"] == 1
    assert detail["allowed_interval"] == [0, 5.0]


def test_report_residual_out_of_tolerance(client):
    pid, tid = make_approved_plan(client)
    # 末轮第 1 组（第 2 轮第 1 组为 [5,2]）：压力 60MPa -> 残余 102kN < 126kN
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(2, 1, [5, 2], 60.0))
    assert r.status_code == 409  # 跳组：须先完成第 1 轮
    assert r.json()["detail"]["reason"] == "out_of_sequence"
    for group_no, bolts in enumerate(([1, 5], [2, 6], [3, 7], [4, 8]), start=1):
        assert client.post(f"/tensioning-plans/{tid}/round-reports",
                           json=group_payload(1, group_no, bolts, P1)
                           ).status_code == 201
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(2, 1, [5, 2], 60.0))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "residual_out_of_tolerance"
    assert detail["bolt_no"] == 5
    assert detail["allowed_interval"] == [126.0, 154.0]
    assert detail["residual_load_kn"] == pytest.approx(102.0)


# ---------------------------------------------------------------- 修订：人工改组与中断重排

def test_revision_requires_reason_and_change(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/revisions",
                    json={"tensioner_count": 4})
    assert r.status_code == 422  # 缺理由
    r = client.post(f"/tensioning-plans/{tid}/revisions", json={"reason": "无变更"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "empty_revision"


def test_revision_regroups_only_unfinished_groups(client):
    """执行中断（第 1 轮第 1 组已完成）后改组：已完成组原位锁定，只重排未完成组。"""
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1))
    assert r.status_code == 201
    r = client.post(f"/tensioning-plans/{tid}/revisions",
                    json={"reason": "现场增援两台拉伸器，改四机同步",
                          "tensioner_count": 4})
    assert r.status_code == 201, r.text
    new = r.json()["tensioning_plan"]
    assert new["plan"]["revision"] == 2
    assert new["plan"]["parent_id"] == tid
    assert new["plan"]["status"] == "approved"  # 继承批准状态（批准快照随修订）
    assert new["plan"]["change_note"] == "现场增援两台拉伸器，改四机同步"
    # 已完成组原位锁定；第 1 轮剩余栓重排为 4 机组
    groups1 = [(g["group_no"], g["bolts"], g["locked"], g["status"])
               for g in new["scheme"][0]["groups"]]
    assert groups1[0] == (1, [1, 5], True, "done")
    assert [b for _, bolts, _, _ in groups1[1:] for b in bolts] == [2, 6, 4, 8, 3, 7]
    # 旧修订废止、只读
    old = client.get(f"/tensioning-plans/{tid}").json()
    assert old["plan"]["status"] == "superseded"
    r = client.post(f"/tensioning-plans/{tid}/round-reports",
                    json=group_payload(1, 2, [2, 6], P1))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "plan_read_only"
    # 新修订从第 1 轮第 2 组继续（重排后的 4 机组）
    new_tid = new["plan"]["id"]
    r = client.post(f"/tensioning-plans/{new_tid}/round-reports",
                    json=group_payload(1, 2, [2, 6, 4, 8], P1))
    assert r.status_code == 201, r.text
    assert r.json()["next_group"]["bolts"] == [3, 7]


def test_revision_diff_and_chain(client):
    pid, tid = make_approved_plan(client)
    assert client.post(f"/tensioning-plans/{tid}/round-reports",
                       json=group_payload(1, 1, [1, 5], P1)).status_code == 201
    r = client.post(f"/tensioning-plans/{tid}/revisions",
                    json={"reason": "改四机同步", "tensioner_count": 4})
    new_tid = r.json()["tensioning_plan"]["plan"]["id"]
    r = client.get(f"/tensioning-plans/{new_tid}/diff")
    assert r.status_code == 200
    diff = r.json()["diff"]
    assert diff["param_changes"]["tensioner_count"] == {"from": 2, "to": 4}
    assert diff["change_note"] == "改四机同步"
    round1 = next(rd for rd in diff["round_changes"] if rd["round_no"] == 1)
    assert 1 in round1["unchanged_groups"]  # 已完成组未变
    # 首版无差异可比
    assert client.get(f"/tensioning-plans/{tid}/diff").status_code == 409
    # 修订链逐版保留
    chain = client.get(f"/procedures/{pid}/tensioning-plans").json()
    assert [p["revision"] for p in chain["tensioning_plans"]] == [1, 2]
    assert chain["tensioning_plans"][0]["status"] == "superseded"


def test_revision_infeasible_params_rejected(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/revisions",
                    json={"reason": "换小泵", "max_pressure_mpa": 50.0})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "tensioning_infeasible"
    # 当前修订不受影响
    assert client.get(f"/tensioning-plans/{tid}").json()["plan"]["status"] == "approved"


# ---------------------------------------------------------------- 采纳超声实测

def _confirm_ultrasonic_batch(client, pid, load_kn=140.0) -> int:
    """走完超声批次：建批 -> 基线 -> 扭矩完工 -> 复测 -> 确认，返回批次 id。"""
    r = client.post(f"/procedures/{pid}/measurement-batches", json=BATCH)
    assert r.status_code == 201, r.text
    bid = r.json()["batch"]["id"]
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
            "operator": "赵六", "measured_at": "2026-09-12T12:00:00"}).status_code == 201
    r = client.post(f"/measurement-batches/{bid}/confirm")
    assert r.status_code == 200, r.text
    return bid


def test_adopt_ultrasonic_requires_reason_and_confirmed_batch(client):
    pid, tid = make_approved_plan(client)
    r = client.post(f"/tensioning-plans/{tid}/adopt-ultrasonic",
                    json={"batch_id": 1})
    assert r.status_code == 422  # 缺理由
    r = client.post(f"/tensioning-plans/{tid}/adopt-ultrasonic",
                    json={"reason": "以实测为准"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "no_confirmed_batch"


def test_adopt_ultrasonic_derives_revision_and_confirms(client):
    pid, tid = make_approved_plan(client)
    bid = _confirm_ultrasonic_batch(client, pid, load_kn=140.0)
    r = client.post(f"/tensioning-plans/{tid}/adopt-ultrasonic",
                    json={"reason": "终检以超声实测预紧力为准"})
    assert r.status_code == 201, r.text
    new = r.json()["tensioning_plan"]
    assert new["plan"]["revision"] == 2
    assert new["plan"]["ultrasonic_batch_id"] == bid
    assert r.json()["adopted_loads_kn"]["1"] == pytest.approx(140.0, abs=0.01)
    new_tid = new["plan"]["id"]
    # 工艺已 completed：回传窗口已关（procedure_window_closed）
    r = client.post(f"/tensioning-plans/{new_tid}/round-reports",
                    json=group_payload(1, 1, [1, 5], P1))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "procedure_window_closed"
    # 评估以超声实测为证据来源，实测 140kN 落在 [126,154] -> 可确认
    detail = client.get(f"/tensioning-plans/{new_tid}").json()
    assert detail["evaluation"]["evidence_source"] == "ultrasonic"
    assert "residual_out_of_tolerance" not in detail["evaluation"]["blockers"]
    # 但未回传组仍是覆盖缺口；补一条工艺内回传窗口不可行时，
    # 此处仅验证超声证据本身不阻断确认（覆盖缺口另行消除）
    assert "incomplete_coverage" in detail["evaluation"]["blockers"]


def test_adopt_ultrasonic_full_confirm(client):
    """回传完成后采纳超声：确认以实测值为准，全链路透出同一方案与结果。"""
    pid, tid = make_approved_plan(client)
    report_all(client, tid)
    bid = _confirm_ultrasonic_batch(client, pid, load_kn=140.0)
    r = client.post(f"/tensioning-plans/{tid}/adopt-ultrasonic",
                    json={"reason": "以超声实测复核残余预紧力"})
    assert r.status_code == 201, r.text
    new_tid = r.json()["tensioning_plan"]["plan"]["id"]
    r = client.post(f"/tensioning-plans/{new_tid}/confirm")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["plan"]["status"] == "confirmed"
    assert detail["evaluation"]["evidence_source"] == "ultrasonic"
    bolt1 = next(b for b in detail["evaluation"]["bolt_results"] if b["bolt_no"] == 1)
    assert bolt1["source"] == "ultrasonic"
    assert bolt1["residual_load_kn"] == pytest.approx(140.0, abs=0.01)


# ---------------------------------------------------------------- 作业包一致性

def test_package_shares_same_plan_and_results(client):
    pid, tid = make_approved_plan(client)
    assert client.post(f"/tensioning-plans/{tid}/round-reports",
                       json=group_payload(1, 1, [1, 5], P1)).status_code == 201
    package = client.get(f"/procedures/{pid}/package").json()
    detail = client.get(f"/tensioning-plans/{tid}").json()
    assert package["tensioning"] is not None
    # 作业包与方案详情共用同一张拉方案与结果
    assert package["tensioning"]["plan"] == detail["plan"]
    assert package["tensioning"]["scheme"] == detail["scheme"]
    assert package["tensioning"]["evaluation"] == detail["evaluation"]
    assert package["tensioning"]["reports"] == detail["reports"]
    # 无方案工艺为 None
    pid2 = make_approved_proc(client)
    assert client.get(f"/procedures/{pid2}/package").json()["tensioning"] is None
