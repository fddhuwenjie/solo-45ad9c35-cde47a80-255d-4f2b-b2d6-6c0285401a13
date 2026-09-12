"""装配对中预检：拟合纯函数、证据缺口、阻断项、版本留痕、门禁与作业包/SVG 共用版本。"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.alignment import (BL_FORCED_PULL, BL_GASKET_INTRUSION, BL_PARALLELISM,
                           BL_RADIAL, GAP_DUPLICATE_AZIMUTH, GAP_INCONSISTENT_UNITS,
                           GAP_INSUFFICIENT_ARC, GAP_NEGATIVE_GAP,
                           GAP_GASKET_OUTSIDE_FACE, GAP_RADIAL_OFFSET_IMPOSSIBLE,
                           analyze_alignment, diff_analyses)
from app.db import init_db
from app.main import app

FROZEN = {
    "flange_face_diameter_mm": 285.0,
    "gasket_inner_diameter_mm": 220.0,
    "gasket_outer_diameter_mm": 270.0,
    "bore_diameter_mm": 200.0,
    "max_parallelism_mm": 1.0,
    "max_radial_mismatch_mm": 2.0,
}
EDGE0 = 7.5  # (D - Go)/2 同心时自外缘到垫片外缘
ANGLES8 = (0, 45, 90, 135, 180, 225, 270, 315)

PROC = {
    "flange_class": "PN40 DN200", "bolt_count": 8, "gasket": "缠绕垫片 304+石墨",
    "target_torque": 320.0, "stage_ratios": [0.3, 0.6, 1.0], "tolerance_pct": 5.0,
    "tool_id": "TW-1001", "tool_range_min": 50.0, "tool_range_max": 500.0,
    "calibration_valid_until": "2026-12-31", "start_angle_deg": 0.0, "clockwise": True,
    "curve_direction": "cw", "snug_torque": 40.0,
    "post_snug_angle_min_deg": 30.0, "post_snug_angle_max_deg": 120.0,
    "max_sample_interval_ms": 50.0, "slope_drop_limit": 5.0,
    "max_outlier_rate_pct": 25.0,
}


def pt(angle, *, gap=2.0, offset=0.0, edge=EDGE0, free=True, unit="mm"):
    return {"angle_deg": angle, "axial_gap": gap, "radial_offset": offset,
            "gasket_edge_position": edge, "bolt_free_insertion": free,
            "length_unit": unit}


def points8(**kw):
    return [pt(a, **kw) for a in ANGLES8]


def analysis(points, frozen=FROZEN):
    return analyze_alignment(frozen, points)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def make_draft(client) -> int:
    r = client.post("/procedures", json=PROC)
    assert r.status_code == 201, r.text
    return r.json()["procedure"]["id"]


def check_body(points, *, frozen=FROZEN, reason=None):
    body = {**frozen, "points": points, "operator": "预检员",
            "measured_at": "2026-09-12T08:00:00"}
    if reason is not None:
        body["adjustment_reason"] = reason
    return body


def submit(client, pid, points, *, frozen=FROZEN, reason=None, expected=201):
    r = client.post(f"/procedures/{pid}/alignment-checks",
                    json=check_body(points, frozen=frozen, reason=reason))
    assert r.status_code == expected, r.text
    return r


# ------------------------------------------------------------ 拟合指标（手算）

def test_perfect_concentric_passes():
    r = analysis(points8())
    assert r["passed"] is True and r["evaluable"] is True
    assert r["evidence_gaps"] == [] and r["blockers"] == []
    m = r["metrics"]
    assert m["gap_max_mm"] == pytest.approx(2.0)
    assert m["gap_min_mm"] == pytest.approx(2.0)
    assert m["parallelism_mm"] == pytest.approx(0.0, abs=1e-9)
    assert m["tilt_deg"] == pytest.approx(0.0, abs=1e-9)
    assert m["tilt_azimuth_deg"] is None
    assert m["radial_mismatch_mm"] == pytest.approx(0.0, abs=1e-9)
    assert m["radial_tir_mm"] == pytest.approx(0.0, abs=1e-9)
    assert m["gasket_eccentricity_mm"] == pytest.approx(0.0, abs=1e-9)
    assert m["gasket_inner_margin_mm"] == pytest.approx(10.0)
    assert m["gasket_outer_margin_mm"] == pytest.approx(7.5)
    assert m["gasket_fit_residual_rms_mm"] == pytest.approx(0.0, abs=1e-9)


def test_tilt_plane_fit_handcalc():
    pts = [pt(a, gap=2.0 + 0.2 * math.sin(math.radians(a))
                       + 0.1 * math.cos(math.radians(a))) for a in ANGLES8]
    m = analysis(pts)["metrics"]
    grad = math.hypot(0.2, 0.1)
    assert m["parallelism_mm"] == pytest.approx(2 * grad, abs=1e-6)
    assert m["gap_max_mm"] == pytest.approx(2 + grad, abs=1e-6)
    assert m["gap_min_mm"] == pytest.approx(2 - grad, abs=1e-6)
    assert m["tilt_azimuth_deg"] == pytest.approx(
        math.degrees(math.atan2(0.2, 0.1)) % 360, abs=1e-3)
    assert m["tilt_deg"] == pytest.approx(math.degrees(math.atan(grad)), abs=1e-6)
    assert m["gap_fit_residual_rms_mm"] == pytest.approx(0.0, abs=1e-9)


def test_radial_and_gasket_vector_fit():
    pts = [pt(a,
              offset=0.3 * math.sin(math.radians(a)) + 0.1 * math.cos(math.radians(a)),
              edge=EDGE0 + 0.5 * math.sin(math.radians(a))
                   + 0.2 * math.cos(math.radians(a)))
           for a in ANGLES8]
    m = analysis(pts)["metrics"]
    assert m["radial_mismatch_mm"] == pytest.approx(math.hypot(0.3, 0.1), abs=1e-6)
    assert m["radial_tir_mm"] == pytest.approx(2 * math.hypot(0.3, 0.1), abs=1e-6)
    assert m["radial_azimuth_deg"] == pytest.approx(
        math.degrees(math.atan2(0.3, 0.1)) % 360, abs=1e-3)
    assert m["gasket_eccentricity_mm"] == pytest.approx(math.hypot(0.5, 0.2), abs=1e-6)
    assert m["gasket_azimuth_deg"] == pytest.approx(
        math.degrees(math.atan2(0.5, 0.2)) % 360, abs=1e-3)
    # 全圆周最坏方位按拟合偏心量计算（不取离散测点极值）
    shift_max = math.hypot(0.5, 0.2)
    assert m["gasket_inner_margin_mm"] == pytest.approx(10.0 - shift_max, abs=1e-6)
    assert m["gasket_outer_margin_mm"] == pytest.approx(EDGE0 - shift_max, abs=1e-6)
    assert m["gasket_sizing_offset_mm"] == pytest.approx(0.0, abs=1e-9)


def test_four_points_quarter_circle_is_minimum():
    r = analysis([pt(0), pt(90), pt(180), pt(270)])
    assert r["evaluable"] and r["passed"]
    assert r["point_count"] == 4


# ------------------------------------------------------------ 证据缺口：只列证据

def test_duplicate_azimuth_is_gap_not_rejection():
    r = analysis([pt(0), pt(0), pt(90), pt(180), pt(270)])
    assert r["evaluable"] is False and r["metrics"] is None
    reasons = {g["reason"] for g in r["evidence_gaps"]}
    assert GAP_DUPLICATE_AZIMUTH in reasons
    dup = next(g for g in r["evidence_gaps"] if g["reason"] == GAP_DUPLICATE_AZIMUTH)
    assert dup["scope"] == "point" and dup["angle_deg"] == 0.0


def test_insufficient_arc_coverage_gap():
    # 四个点全落在 0~90 半圆内：最大空弧 270°
    r = analysis([pt(0), pt(20), pt(60), pt(90)])
    assert r["evaluable"] is False and r["metrics"] is None
    gap = next(g for g in r["evidence_gaps"] if g["reason"] == GAP_INSUFFICIENT_ARC)
    assert gap["scope"] == "structure"
    assert gap["detail"]["max_empty_arc_deg"] == pytest.approx(270.0)


def test_quarter_spaced_points_cover_full_circle():
    r = analysis([pt(45), pt(135), pt(225), pt(315)])
    assert not [g for g in r["evidence_gaps"] if g["reason"] == GAP_INSUFFICIENT_ARC]


def test_inconsistent_units_gap():
    pts = points8()
    pts[0]["length_unit"] = "cm"
    pts[0]["axial_gap"] = 0.2  # 0.2 cm == 2 mm，数值经换算仍一致
    pts[0]["radial_offset"] = 0.0
    pts[0]["gasket_edge_position"] = 0.75
    r = analysis(pts)
    assert r["evaluable"] is False and r["metrics"] is None
    gap = next(g for g in r["evidence_gaps"] if g["reason"] == GAP_INCONSISTENT_UNITS)
    assert set(gap["detail"]["units"]) == {"cm", "mm"}


def test_negative_axial_gap_is_geometric_contradiction():
    pts = points8()
    pts[3]["axial_gap"] = -0.5
    r = analysis(pts)
    assert r["evaluable"] is False and r["metrics"] is None
    gap = next(g for g in r["evidence_gaps"] if g["reason"] == GAP_NEGATIVE_GAP)
    assert gap["angle_deg"] == 135 and gap["detail"]["axial_gap_mm"] == -0.5


def test_gasket_outside_face_gap():
    pts = points8()
    pts[2]["gasket_edge_position"] = -1.0  # 外缘已越过法兰外缘
    r = analysis(pts)
    assert r["evaluable"] is False
    assert any(g["reason"] == GAP_GASKET_OUTSIDE_FACE
               and g["angle_deg"] == 90 for g in r["evidence_gaps"])


def test_undersized_concentric_gasket_is_not_contradiction():
    """垫片整体偏小（同心、各方位量距一致偏大但不侵入）：非矛盾也非阻断。

    常数项 k 吸收同心尺寸偏差：偏心为 0，两侧余量按名义同心间隙计，
    整体偏小只体现在 gasket_sizing_offset_mm 与残差（此处拟合完全吸收，RMS=0）。
    """
    pts = points8(edge=16.0)
    r = analysis(pts)
    assert r["passed"] is True and r["evidence_gaps"] == []
    m = r["metrics"]
    assert m["gasket_eccentricity_mm"] == pytest.approx(0.0, abs=1e-9)
    assert m["gasket_sizing_offset_mm"] == pytest.approx(8.5)
    assert m["gasket_inner_margin_mm"] == pytest.approx(10.0)
    assert m["gasket_outer_margin_mm"] == pytest.approx(7.5)
    assert m["gasket_fit_residual_rms_mm"] == pytest.approx(0.0, abs=1e-9)


def test_intrusion_worst_azimuth_between_sample_points():
    """回归：最坏方位落在相邻测点之间时，按离散测点会漏判，按拟合偏心量须阻断。

    窄同心内余量 (Gi-Db)/2 = 3.0mm。8 方位测点，平移轴 22.5°（落在 0°/45°
    两测点正中），偏心 3.1mm：测点采到的最大平移分量为 3.1·cos22.5°=2.864027，
    离散算法给出 3.0−2.864027 = +0.135973mm（误放行），
    全圆周最坏余量 3.0−3.1 = −0.1mm（垫片侵入流道，须阻断）。
    """
    phi = math.radians(22.5)
    gx, gy = 3.1 * math.sin(phi), 3.1 * math.cos(phi)

    def edge(a):
        return 20.0 + gx * math.sin(math.radians(a)) + gy * math.cos(math.radians(a))

    pts = [pt(a, edge=edge(a)) for a in ANGLES8]
    discrete_component = 3.1 * math.cos(math.radians(22.5))
    assert discrete_component == pytest.approx(2.864027, abs=1e-6)
    assert 3.0 - discrete_component == pytest.approx(0.135973, abs=1e-6)

    narrow = {**FROZEN, "flange_face_diameter_mm": 300.0,
              "gasket_inner_diameter_mm": 206.0, "gasket_outer_diameter_mm": 260.0}
    r = analysis(pts, narrow)
    assert r["evaluable"] and r["evidence_gaps"] == []
    m = r["metrics"]
    assert m["gasket_eccentricity_mm"] == pytest.approx(3.1, abs=1e-6)
    assert m["gasket_azimuth_deg"] == pytest.approx(22.5, abs=1e-3)
    assert m["gasket_inner_margin_mm"] == pytest.approx(-0.1, abs=1e-6)
    assert m["gasket_outer_margin_mm"] == pytest.approx(20.0 - 3.1, abs=1e-6)
    b = next((x for x in r["blockers"] if x["reason"] == BL_GASKET_INTRUSION), None)
    assert b is not None
    assert b["detail"]["gasket_inner_margin_mm"] == pytest.approx(-0.1, abs=1e-6)
    assert r["passed"] is False
    # 各测点边缘读数自身合法（量距最大 22.864 < R+ri=253），不产生几何矛盾缺口
    assert not [g for g in r["evidence_gaps"]
                if g["reason"] == GAP_GASKET_OUTSIDE_FACE]


def test_extreme_translation_makes_measured_edge_negative():
    """平移大到对侧测点边缘量距为负：属于几何矛盾缺口（只列证据，不产出指标）。"""
    pts = [pt(a, edge=EDGE0 + 11.0 * math.sin(math.radians(a))) for a in ANGLES8]
    r = analysis(pts)
    assert r["metrics"] is None and r["evaluable"] is False
    assert any(g["reason"] == GAP_GASKET_OUTSIDE_FACE
               and g["angle_deg"] == 270 for g in r["evidence_gaps"])


def test_radial_offset_impossible_gap():
    pts = points8()
    pts[2]["radial_offset"] = 200.0  # > 法兰面半径 142.5
    r = analysis(pts)
    assert r["evaluable"] is False
    assert any(g["reason"] == GAP_RADIAL_OFFSET_IMPOSSIBLE
               and g["angle_deg"] == 90 for g in r["evidence_gaps"])


# ------------------------------------------------------------ 阻断项：阻止批准开工

def test_parallelism_exceeded_blocks():
    pts = [pt(a, gap=2.0 + 0.8 * math.sin(math.radians(a))) for a in ANGLES8]
    r = analysis(pts)
    assert r["evaluable"] and not r["passed"]
    b = next(x for x in r["blockers"] if x["reason"] == BL_PARALLELISM)
    assert b["detail"]["parallelism_mm"] == pytest.approx(1.6)
    assert r["metrics"]["gap_min_mm"] > 0  # 未接触，不另报强行拉拢


def test_radial_mismatch_exceeded_blocks():
    pts = [pt(a, offset=2.5 * math.sin(math.radians(a))) for a in ANGLES8]
    r = analysis(pts)
    b = next(x for x in r["blockers"] if x["reason"] == BL_RADIAL)
    assert b["detail"]["radial_mismatch_mm"] == pytest.approx(2.5)
    assert b["detail"]["radial_tir_mm"] == pytest.approx(5.0)


def test_gasket_intrusion_blocks():
    # D=300/Go=260/Gi=206/Db=200：流道侧同心余量 (Gi-Db)/2=3mm；
    # 法兰面侧同心余量 (D-Go)/2=20mm。偏心 8mm 时侵入流道，外缘仍在面内。
    frozen = {**FROZEN, "flange_face_diameter_mm": 300.0,
              "gasket_inner_diameter_mm": 206.0, "gasket_outer_diameter_mm": 260.0}
    pts = [pt(a, edge=20.0 + 8.0 * math.sin(math.radians(a)))
           for a in ANGLES8]
    r = analysis(pts, frozen)
    assert r["evaluable"] and r["evidence_gaps"] == []
    b = next(x for x in r["blockers"] if x["reason"] == BL_GASKET_INTRUSION)
    assert b["detail"]["gasket_eccentricity_mm"] == pytest.approx(8.0)
    assert b["detail"]["gasket_inner_margin_mm"] == pytest.approx(-5.0)
    assert r["metrics"]["gasket_outer_margin_mm"] == pytest.approx(12.0)


def test_bolt_not_free_insertion_blocks():
    pts = points8(free=False)
    r = analysis(pts)
    b = next(x for x in r["blockers"] if x["reason"] == BL_FORCED_PULL)
    assert len(b["detail"]["bolts_not_free_at_angles_deg"]) == 8
    assert b["detail"]["local_contact"] is False


def test_single_bolt_not_free_blocks():
    pts = points8()
    pts[5]["bolt_free_insertion"] = False
    r = analysis(pts)
    b = next(x for x in r["blockers"] if x["reason"] == BL_FORCED_PULL)
    assert b["detail"]["bolts_not_free_at_angles_deg"] == [225.0]


def test_local_contact_inferred_between_points_blocks():
    # 四测点（45/135/225/315）实测均为正（最小 .034mm），
    # 但拟合全周最小间隙 -0.2mm（两测点间已接触）：仍须判强行拉拢
    frozen = {**FROZEN, "max_parallelism_mm": 5.0}
    pts = [pt(a, gap=0.6 + 0.8 * math.sin(math.radians(a)))
           for a in (45, 135, 225, 315)]
    assert min(p["axial_gap"] for p in pts) > 0
    r = analysis(pts, frozen)
    b = next(x for x in r["blockers"] if x["reason"] == BL_FORCED_PULL)
    assert b["detail"]["local_contact"] is True
    assert b["detail"]["gap_min_mm"] == pytest.approx(-0.2, abs=1e-6)


def test_gaps_do_not_produce_limit_blockers():
    """证据缺口场景不得据矛盾数据出具限值结论：只有缺口（与直接的穿入证据）。"""
    pts = points8()
    pts[0]["axial_gap"] = -9.0  # 负间隙同时会造成拟合平行度很大，但不得出平行度阻断
    r = analysis(pts)
    assert r["evaluable"] is False
    assert [b for b in r["blockers"] if b["reason"] == BL_PARALLELISM] == []


# ------------------------------------------------------------ 版本差异

def test_diff_matches_by_azimuth():
    before = analysis(points8())
    after_pts = [pt(a, gap=2.0 + 0.1 * math.sin(math.radians(a)),
                    offset=0.2 * math.sin(math.radians(a)))
                 for a in ANGLES8]
    after = analysis(after_pts)
    d = diff_analyses(before, after)
    assert d["frozen_changes"] == []
    assert len(d["points_matched"]) == 8
    m90 = next(x for x in d["points_matched"] if x["angle_prev_deg"] == 90)
    assert m90["axial_gap_delta_mm"] == pytest.approx(0.1)
    assert m90["radial_offset_delta_mm"] == pytest.approx(0.2)
    assert d["points_added_angles_deg"] == [] and d["points_removed_angles_deg"] == []
    assert d["metric_changes"]["parallelism_mm"][1] == pytest.approx(0.2, abs=1e-6)
    assert d["passed_transition"] == [True, True]


def test_diff_handles_repositioned_and_added_points():
    before = analysis([pt(0), pt(90), pt(180), pt(270)])
    # 0° 移到圆周对侧 180° 附近（容差内匹配旧 180），并在 315° 增点：
    # 旧 0° 无匹配 -> 移除；新增 178° 匹配旧 180°，315° 无匹配 -> 新增
    after = analysis([pt(90), pt(178), pt(270), pt(315)])
    d = diff_analyses(before, after, tol_deg=5.0)
    assert len(d["points_matched"]) == 3
    assert d["points_added_angles_deg"] == [315.0]
    assert d["points_removed_angles_deg"] == [0.0]


# ------------------------------------------------------------ API：冻结、留痕、门禁

def test_create_freezes_geometry_and_points(client):
    pid = make_draft(client)
    r = submit(client, pid, points8())
    check = r.json()["alignment_check"]
    assert check["version"] == 1 and check["adjustment_reason"] is None
    assert check["frozen"] == {k: FROZEN[k] for k in (
        "flange_face_diameter_mm", "gasket_inner_diameter_mm",
        "gasket_outer_diameter_mm", "bore_diameter_mm",
        "max_parallelism_mm", "max_radial_mismatch_mm")}
    assert check["analysis"]["passed"] is True
    detail = client.get(f"/alignment-checks/{check['check_id']}").json()
    assert len(detail["points"]) == 8
    assert detail["points"][0]["angle_deg"] == 0.0
    assert detail["points"][0]["length_unit"] == "mm"
    assert client.get(f"/procedures/{pid}/alignment-checks").json()[
        "alignment_checks"][0]["version"] == 1


def test_approve_requires_alignment_check(client):
    pid = make_draft(client)
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "alignment_check_missing"
    submit(client, pid, points8())
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200


def test_failed_check_blocks_approve_with_evidence(client):
    pid = make_draft(client)
    pts = [pt(a, gap=2.0 + 0.8 * math.sin(math.radians(a))) for a in ANGLES8]
    submit(client, pid, pts)
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "alignment_check_not_passed"
    assert detail["version"] == 1
    assert [b["reason"] for b in detail["blockers"]] == [BL_PARALLELISM]
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "draft"


def test_evidence_gap_version_blocks_approve(client):
    pid = make_draft(client)
    submit(client, pid, [pt(0), pt(30), pt(60), pt(90)])  # 覆盖不足
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    reasons = [g["reason"] for g in r.json()["detail"]["evidence_gaps"]]
    assert GAP_INSUFFICIENT_ARC in reasons


def test_adjustment_reason_on_first_version_rejected(client):
    pid = make_draft(client)
    submit(client, pid, points8(), reason="首版不应带调整原因", expected=422)


def test_retest_new_version_requires_reason_and_keeps_old(client):
    pid = make_draft(client)
    # 首版失败（90° 方位螺栓穿不进）
    pts1 = points8()
    pts1[2]["bolt_free_insertion"] = False
    submit(client, pid, pts1)
    # v2 无调整原因 -> 422
    submit(client, pid, points8(), expected=422)
    # 复测注明原因，另存 v2
    r = submit(client, pid, points8(), reason="松开吊具、重新找正后复测")
    assert r.json()["alignment_check"]["version"] == 2
    checks = client.get(f"/procedures/{pid}/alignment-checks").json()["alignment_checks"]
    assert [c["version"] for c in checks] == [1, 2]
    old = client.get(f"/alignment-checks/{checks[0]['check_id']}").json()
    assert old["adjustment_reason"] is None
    old90 = next(p for p in old["points"] if p["angle_deg"] == 90)
    assert old90["bolt_free_insertion"] is False  # 旧记录未被覆盖
    assert checks[1]["adjustment_reason"] == "松开吊具、重新找正后复测"
    # 差异接口
    d = client.get(f"/alignment-checks/{checks[1]['check_id']}/diff").json()
    assert d["from_version"] == 1 and d["to_version"] == 2
    m90 = next(x for x in d["diff"]["points_matched"] if x["angle_prev_deg"] == 90)
    assert m90["bolt_free_insertion_changed"] is True
    # 差异接口对首版拒绝
    assert client.get(f"/alignment-checks/{checks[0]['check_id']}/diff").status_code == 409
    # v2 通过后才放行
    assert client.post(f"/procedures/{pid}/approve").status_code == 200


def test_new_failed_version_after_approval_blocks_start(client):
    pid = make_draft(client)
    submit(client, pid, points8())
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    # 批准后、开工前发现对中变化：approved 阶段允许复测
    pts = [pt(a, gap=2.0 + 0.8 * math.sin(math.radians(a))) for a in ANGLES8]
    submit(client, pid, pts, reason="设备移位后复测")
    r = client.post(f"/procedures/{pid}/start")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "alignment_check_not_passed"


def test_alignment_window_closes_after_start(client):
    pid = make_draft(client)
    submit(client, pid, points8())
    client.post(f"/procedures/{pid}/approve")
    client.post(f"/procedures/{pid}/start")
    r = submit(client, pid, points8(), expected=409)
    assert r.json()["detail"]["reason"] == "alignment_window_closed"


def test_duplicate_azimuth_accepted_as_version_with_gap(client):
    pid = make_draft(client)
    r = submit(client, pid, [pt(0), pt(0), pt(90), pt(180), pt(270)])
    a = r.json()["alignment_check"]["analysis"]
    assert any(g["reason"] == GAP_DUPLICATE_AZIMUTH for g in a["evidence_gaps"])
    assert a["metrics"] is None and a["passed"] is False


def test_angle_normalized_on_storage(client):
    pid = make_draft(client)
    r = submit(client, pid, [pt(450), pt(45), pt(135), pt(225), pt(315 - 360)])
    detail = client.get(
        f"/alignment-checks/{r.json()['alignment_check']['check_id']}").json()
    stored = sorted(p["angle_deg"] for p in detail["points"])
    assert stored == [45.0, 90.0, 135.0, 225.0, 315.0]


def test_between_points_intrusion_blocks_approve_and_start_api(client):
    """API 回归（用户报告场景）：8 方位测点、平移轴 22.5°，最坏方位落在
    相邻测点之间。离散算法余量 +0.135973 会误放行；按拟合偏心量得到
    全圆周最坏余量 -0.1mm，批准与开工门禁都必须拦住。"""
    pid = make_draft(client)
    narrow = {**FROZEN, "flange_face_diameter_mm": 300.0,
              "gasket_inner_diameter_mm": 206.0, "gasket_outer_diameter_mm": 260.0}
    phi = math.radians(22.5)
    gx, gy = 3.1 * math.sin(phi), 3.1 * math.cos(phi)

    def edge_points(ecc):
        ex, ey = ecc * math.sin(phi), ecc * math.cos(phi)
        return [{"angle_deg": a, "axial_gap": 2.0, "radial_offset": 0.0,
                 "gasket_edge_position": 20.0
                     + ex * math.sin(math.radians(a)) + ey * math.cos(math.radians(a)),
                 "bolt_free_insertion": True} for a in ANGLES8]

    # v1：偏心 3.1，旧离散算法会给 +0.135973；批准门禁拦截
    submit(client, pid, edge_points(3.1), frozen=narrow)
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "alignment_check_not_passed"
    assert BL_GASKET_INTRUSION in [b["reason"] for b in detail["blockers"]]
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "draft"

    # v2 找正到偏心 2.0（全圆周内余量 +1.0）后可批准
    submit(client, pid, edge_points(2.0), frozen=narrow, reason="重新对中垫片后复测")
    assert client.post(f"/procedures/{pid}/approve").status_code == 200

    # v3 批准后、开工前垫片再次偏移到 3.1mm：开工门禁拦截
    submit(client, pid, edge_points(3.1), frozen=narrow, reason="设备移位后复测")
    r = client.post(f"/procedures/{pid}/start")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "alignment_check_not_passed"
    assert BL_GASKET_INTRUSION in [b["reason"] for b in detail["blockers"]]

    # 作业包采用最新（失败）版本：离散余量 +0.135973 但拟合最坏余量 -0.1
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["alignment"]["version"] == 3
    assert pkg["alignment"]["analysis"]["passed"] is False
    assert pkg["alignment"]["analysis"]["metrics"][
        "gasket_inner_margin_mm"] == pytest.approx(-0.1, abs=1e-6)

    # v4 再次找正后方可开工
    submit(client, pid, edge_points(2.0), frozen=narrow, reason="重新找正后复测")
    assert client.post(f"/procedures/{pid}/start").status_code == 200


def test_bad_frozen_geometry_rejected(client):
    pid = make_draft(client)
    bad = [
        {**FROZEN, "gasket_inner_diameter_mm": 280.0},   # Gi >= Go
        {**FROZEN, "gasket_outer_diameter_mm": 300.0},   # Go > D
        {**FROZEN, "bore_diameter_mm": 300.0},           # Db > D
    ]
    for frozen in bad:
        r = client.post(f"/procedures/{pid}/alignment-checks",
                        json=check_body(points8(), frozen=frozen))
        assert r.status_code == 422, frozen


def test_too_few_points_rejected(client):
    pid = make_draft(client)
    r = client.post(f"/procedures/{pid}/alignment-checks",
                    json=check_body([pt(0), pt(90), pt(180)]))
    assert r.status_code == 422


# ------------------------------------------------------------ 作业包与 SVG 共用版本

def test_package_and_svg_share_adopted_version(client):
    pid = make_draft(client)
    submit(client, pid, points8())
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["alignment"]["version"] == 1
    assert pkg["alignment"]["analysis"]["metrics"]["parallelism_mm"] == 0.0
    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "<svg" in svg and "对中预检：v1" in svg
    # 复测后采用最新版本（旧版保留），包与图仍共用同一版本
    submit(client, pid, points8(), reason="调整垫片后复测")
    pkg2 = client.get(f"/procedures/{pid}/package").json()
    assert pkg2["alignment"]["version"] == 2
    svg2 = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "对中预检：v2" in svg2 and "对中预检：v1" not in svg2


def test_svg_renders_without_alignment(client):
    pid = make_draft(client)
    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "<svg" in svg and "尚无对中预检" in svg
