"""扭矩-转角轨迹：单位/跨零展开、贴合点与指标、缺陷区间、修订留痕、review 门禁。"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.curve import (D_ANGLE_REVERSAL, D_EARLY_PEAK, D_INSUFFICIENT_POINTS,
                       D_OUT_OF_RANGE, D_SLOPE_COLLAPSE, D_SNUG_NOT_REACHED,
                       D_TIME_REGRESSION, D_SAMPLING_INTERVAL, MIN_POINTS,
                       analyze_curve, unwrap_angles)
from app.db import init_db
from app.main import app

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

# 纯函数分析用工艺参数（与 BASE 锁定值一致）
PROC = {k: BASE[k] for k in (
    "curve_direction", "snug_torque", "post_snug_angle_min_deg",
    "post_snug_angle_max_deg", "max_sample_interval_ms", "slope_drop_limit",
    "max_outlier_rate_pct")}
PROC["tool_range_max"] = BASE["tool_range_max"]


def good_curve(final_torque: float = 320.0, total_angle: float = 90.0,
               n: int = 65) -> list[dict]:
    """线性升至目标扭矩的合格轨迹：贴合后转角 78.75° ∈ [30, 120]。"""
    return [{"t": i * 0.02, "torque": final_torque * i / (n - 1),
             "angle": total_angle * i / (n - 1)} for i in range(n)]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def make_completed(client, **overrides) -> int:
    r = client.post("/procedures", json={**BASE, **overrides})
    assert r.status_code == 201, r.text
    pid = r.json()["procedure"]["id"]
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200
    for ratio in (0.3, 0.6, 1.0):
        target = round(320.0 * ratio, 2)
        for bolt in SEQ8:
            r = client.post(f"/procedures/{pid}/reports", json={
                "bolt_no": bolt, "tool_id": "TW-1001", "operator": "张三",
                "reported_at": "2026-09-10T09:00:00", "measured_torque": target})
            assert r.status_code == 201, r.text
    return pid


def final_record_ids(client, pid) -> dict[int, int]:
    pkg = client.get(f"/procedures/{pid}/package").json()
    fr = max(r["round_no"] for r in pkg["records"])
    return {r["bolt_no"]: r["id"] for r in pkg["records"]
            if r["round_no"] == fr and r["rework_of"] is None}


def submit(client, pid, record_id, points, **kw):
    return client.post(f"/procedures/{pid}/curves",
                       json={"record_id": record_id, "points": points, **kw})


def submit_all_good(client, pid, skip: set[int] | None = None,
                    overrides: dict[int, list[dict]] | None = None):
    ids = final_record_ids(client, pid)
    for bolt, rec_id in ids.items():
        if skip and bolt in (skip or set()):
            continue
        points = (overrides or {}).get(bolt, good_curve())
        r = submit(client, pid, rec_id, points)
        assert r.status_code == 201, r.text
    return ids


# ------------------------------------------------------------ 单位统一与跨零展开

def test_unwrap_cross_zero_cw():
    # 设备零点在 0°：cw 旋向 350→355→0→5→10 展开为连续转角
    assert unwrap_angles([350, 355, 0, 5, 10], "cw") == [0, 5, 10, 15, 20]


def test_unwrap_cross_zero_ccw():
    # ccw 旋向 10→5→0→355→350 同样展开为正向转角（两种旋向可直接比较）
    assert unwrap_angles([10, 5, 0, 355, 350], "ccw") == [0, 5, 10, 15, 20]


def test_analyze_normalizes_units():
    """ms / lbfft / rev 提交单位与 s / N·m / deg 分析结果一致。"""
    n = 65
    points = [{"t": i * 20.0,                                    # ms
               "torque": 320.0 * i / (n - 1) / 1.3558179483314004,  # lbfft
               "angle": 0.25 * i / (n - 1)}                       # rev（共 90°）
              for i in range(n)]
    a = analyze_curve(PROC, points, time_unit="ms", torque_unit="lbfft",
                      angle_unit="rev")
    assert a["defects"] == []
    assert a["peak_torque_nm"] == pytest.approx(320.0, abs=1e-6)
    assert a["rotation_total_deg"] == pytest.approx(90.0, abs=1e-6)
    assert a["post_snug_angle_deg"] == pytest.approx(78.75, abs=1e-6)
    assert a["duration_s"] == pytest.approx(1.28, abs=1e-9)


def test_analyze_angle_zero_offset_irrelevant():
    """不同设备角度零点（+350° 偏移跨零）得到相同的转角指标。"""
    base = good_curve()
    shifted = [{"t": p["t"], "torque": p["torque"],
                "angle": (p["angle"] + 350.0) % 360.0} for p in base]
    a0 = analyze_curve(PROC, base, time_unit="s", torque_unit="Nm", angle_unit="deg")
    a1 = analyze_curve(PROC, shifted, time_unit="s", torque_unit="Nm",
                       angle_unit="deg")
    assert a1["defects"] == []
    assert a1["post_snug_angle_deg"] == a0["post_snug_angle_deg"]
    assert a1["rotation_total_deg"] == a0["rotation_total_deg"]


# ------------------------------------------------------------ 指标手算核对

def test_analyze_handcalc_metrics():
    a = analyze_curve(PROC, good_curve(), time_unit="s", torque_unit="Nm",
                      angle_unit="deg")
    assert a["defects"] == []
    assert a["snug_index"] == 8 and a["snug_source"] == "auto"  # 扭矩 40 首达点
    assert a["post_snug_angle_deg"] == pytest.approx(78.75, abs=1e-9)
    assert a["post_snug_in_range"] is True
    assert a["peak_torque_nm"] == pytest.approx(320.0)
    assert a["peak_index"] == 64
    # 线性曲线梯形积分精确：W = 均值扭矩 × 转角（deg→rad）
    assert a["work_total_j"] == pytest.approx(160.0 * 90.0 * math.pi / 180.0)
    assert a["work_post_snug_j"] == pytest.approx(180.0 * 78.75 * math.pi / 180.0)
    slopes = [s["slope_nm_per_deg"] for s in a["segment_slopes"]]
    assert len(slopes) == 4
    assert all(s == pytest.approx(320.0 / 90.0, abs=1e-6) for s in slopes)


# ------------------------------------------------------------ 缺陷判定（纯函数，含具体区间）

def _reasons(analysis):
    return [d["reason"] for d in analysis["defects"]]


def test_defect_insufficient_points():
    a = analyze_curve(PROC, good_curve()[:5], time_unit="s", torque_unit="Nm",
                      angle_unit="deg")
    assert D_INSUFFICIENT_POINTS in _reasons(a)
    iv = a["defects"][0]["interval"]
    assert iv == {"point_count": 5, "min_points": MIN_POINTS}


def test_defect_time_regression():
    pts = good_curve()
    pts[40]["t"] = pts[39]["t"] - 0.01
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert D_TIME_REGRESSION in _reasons(a)
    iv = next(d["interval"] for d in a["defects"]
              if d["reason"] == D_TIME_REGRESSION)
    assert iv["point_index"] == 40
    assert iv["t_s"][1] < iv["t_s"][0]


def test_defect_sampling_interval_exceeded():
    pts = good_curve()
    pts[30]["t"] += 0.1  # 该点间隔 0.12s > 上限 0.05s
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert D_SAMPLING_INTERVAL in _reasons(a)
    iv = next(d["interval"] for d in a["defects"]
              if d["reason"] == D_SAMPLING_INTERVAL)
    assert iv["point_index"] == 30
    assert iv["interval_s"] == pytest.approx(0.12, abs=1e-9)
    assert iv["max_interval_s"] == 0.05


def test_defect_reading_out_of_range():
    pts = good_curve()
    pts[50]["torque"] = 600.0  # 量程上限 500
    pts[51]["torque"] = 610.0
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert D_OUT_OF_RANGE in _reasons(a)
    iv = next(d["interval"] for d in a["defects"]
              if d["reason"] == D_OUT_OF_RANGE)
    assert (iv["start_index"], iv["end_index"]) == (50, 51)
    assert iv["max_torque_nm"] == 610.0
    assert iv["tool_range_max_nm"] == 500.0


def test_defect_angle_reversal():
    pts = good_curve()
    pts[30]["angle"] -= 5.0  # 套筒打滑：转角回退 5°
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert D_ANGLE_REVERSAL in _reasons(a)
    iv = next(d["interval"] for d in a["defects"]
              if d["reason"] == D_ANGLE_REVERSAL)
    assert iv["point_index"] == 30
    assert iv["rotation_deg"][1] < iv["rotation_deg"][0]


def test_defect_early_peak():
    # 螺纹咬伤：扭矩提前冲到 320 后回落至 250
    pts = []
    for i in range(65):
        if i <= 51:
            tq = 320.0 * i / 51
        else:
            tq = 320.0 - (i - 51) * (70.0 / 13)
        pts.append({"t": i * 0.02, "torque": tq, "angle": 90.0 * i / 64})
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert D_EARLY_PEAK in _reasons(a)
    iv = next(d["interval"] for d in a["defects"]
              if d["reason"] == D_EARLY_PEAK)
    assert iv["peak_index"] == 51 and iv["end_index"] == 64
    assert iv["peak_torque_nm"] == pytest.approx(320.0)
    assert iv["final_torque_nm"] == pytest.approx(250.0)


def test_defect_slope_collapse():
    # 垫片突然就位/螺纹咬伤：前段斜率 8，后段跌至 0.9（突降 7.1 > 限值 5）
    pts = []
    for i in range(65):
        ang = 90.0 * i / 64
        tq = 8.0 * ang if ang <= 25.0 else 200.0 + 0.9 * (ang - 25.0)
        pts.append({"t": i * 0.02, "torque": tq, "angle": ang})
    a = analyze_curve(PROC, pts, time_unit="s", torque_unit="Nm", angle_unit="deg")
    assert _reasons(a) == [D_SLOPE_COLLAPSE]
    iv = a["defects"][0]["interval"]
    assert iv["segment"] == 2
    assert iv["slope_prev_nm_per_deg"] - iv["slope_nm_per_deg"] > 5.0
    assert iv["rotation_deg"][0] < iv["rotation_deg"][1]


def test_defect_snug_not_reached():
    a = analyze_curve(PROC, good_curve(final_torque=30.0), time_unit="s",
                      torque_unit="Nm", angle_unit="deg")
    assert D_SNUG_NOT_REACHED in _reasons(a)
    assert a["snug_index"] is None
    assert a["post_snug_angle_deg"] is None


def test_manual_snug_override():
    a = analyze_curve(PROC, good_curve(), time_unit="s", torque_unit="Nm",
                      angle_unit="deg", snug_override=20)
    assert a["defects"] == []
    assert a["snug_index"] == 20 and a["snug_source"] == "manual"
    assert a["post_snug_angle_deg"] == pytest.approx(90.0 - 90.0 * 20 / 64)


# ------------------------------------------------------------ 提交与关联校验

def test_curve_requires_final_round_record(client):
    pid = make_completed(client)
    pkg = client.get(f"/procedures/{pid}/package").json()
    round1 = next(r for r in pkg["records"] if r["round_no"] == 1)
    r = submit(client, pid, round1["id"], good_curve())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_final_round_record"
    r = submit(client, pid, 99999, good_curve())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "record_not_found"


def test_curve_window_closed(client):
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    client.post(f"/procedures/{pid}/approve")
    r = submit(client, pid, 1, good_curve())  # approved 状态不可提交
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "curve_window_closed"


def test_duplicate_curve_use_amend(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    assert submit(client, pid, ids[1], good_curve()).status_code == 201
    r = submit(client, pid, ids[1], good_curve())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "curve_exists_use_amend"
    assert detail["curve_id"] >= 1


def test_curve_param_validation(client):
    r = client.post("/procedures", json={**BASE, "snug_torque": 320.0})
    assert r.status_code == 422  # 贴合扭矩须小于目标扭矩
    r = client.post("/procedures", json={**BASE, "post_snug_angle_max_deg": 30.0})
    assert r.status_code == 422  # 转角上限须大于下限


# ------------------------------------------------------------ review 门禁

def test_review_blocked_without_curves(client):
    pid = make_completed(client)
    r = client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "curve_review_failed"
    assert detail["blockers"] == ["curve_missing"]
    assert detail["curve_review"]["missing_bolts"] == list(range(1, 9))
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "completed"


def test_review_passes_with_all_curves(client):
    pid = make_completed(client)
    ids = submit_all_good(client, pid)
    gate = client.get(f"/procedures/{pid}/curve-review").json()
    assert gate["passed"] is True and gate["blockers"] == []
    assert gate["usable_count"] == 8 and gate["outlier_rate_pct"] == 0.0
    r = client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    assert r.status_code == 200
    # 作业包标出采用的修订与关联记录
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["curves"]["passed"] is True
    for b in pkg["curves"]["bolts"]:
        assert b["state"] == "ok" and b["revision"] == 1
        assert b["record_id"] == ids[b["bolt_no"]]
        assert b["post_snug_angle_deg"] == pytest.approx(78.75)


def test_review_blocked_by_defective_curve(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    bad = good_curve()
    bad[40]["t"] = bad[39]["t"] - 0.01  # 时标倒退
    r = submit(client, pid, ids[3], bad)
    assert r.status_code == 201  # 缺陷轨迹照常落库，标记不可用
    assert r.json()["usable"] is False
    defect = r.json()["analysis"]["defects"][0]
    assert defect["reason"] == D_TIME_REGRESSION
    assert defect["interval"]["point_index"] == 40
    submit_all_good(client, pid, skip={3})

    r = client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "curve_review_failed"
    assert detail["blockers"] == ["curve_unusable"]
    assert detail["curve_review"]["unusable_bolts"] == [3]
    bolt3 = next(b for b in detail["curve_review"]["bolts"] if b["bolt_no"] == 3)
    assert bolt3["state"] == "unusable"
    assert bolt3["defects"][0]["interval"]["point_index"] == 40  # 具体区间随结论返回


def test_outlier_rate_gate(client):
    # 2/8 离群 = 25% ≤ 上限 25%：通过
    pid = make_completed(client)
    submit_all_good(client, pid, overrides={1: good_curve(total_angle=200.0),
                                            2: good_curve(total_angle=200.0)})
    gate = client.get(f"/procedures/{pid}/curve-review").json()
    assert gate["outlier_bolts"] == [1, 2]
    assert gate["outlier_rate_pct"] == 25.0
    assert gate["passed"] is True
    assert client.post(f"/procedures/{pid}/review",
                       json={"reviewer": "李四"}).status_code == 200

    # 3/8 离群 = 37.5% > 25%：阻断
    pid2 = make_completed(client)
    submit_all_good(client, pid2,
                    overrides={b: good_curve(total_angle=200.0) for b in (1, 2, 3)})
    r = client.post(f"/procedures/{pid2}/review", json={"reviewer": "李四"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["blockers"] == ["outlier_rate_exceeded"]
    assert detail["curve_review"]["outlier_bolts"] == [1, 2, 3]
    assert "离群率" in detail["message"]


# ------------------------------------------------------------ 修订（移动贴合点 / 换用曲线）

def test_amend_moves_snug_point_keeps_history(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    r = submit(client, pid, ids[5], good_curve())
    cid = r.json()["curve_id"]
    assert r.json()["analysis"]["post_snug_angle_deg"] == pytest.approx(78.75)

    r = client.post(f"/curves/{cid}/amend", json={
        "reason": "设备自动贴合点偏晚，人工核对后移至第 20 点", "snug_index": 20})
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2
    a = r.json()["analysis"]
    assert a["snug_source"] == "manual" and a["snug_index"] == 20
    assert a["post_snug_angle_deg"] == pytest.approx(61.875)

    # 旧轨迹保持可查：首修订仍为自动贴合点 78.75°
    detail = client.get(f"/curves/{cid}").json()
    assert [rev["revision"] for rev in detail["revisions"]] == [1, 2]
    rev1, rev2 = detail["revisions"]
    assert rev1["analysis"]["snug_source"] == "auto"
    assert rev1["analysis"]["post_snug_angle_deg"] == pytest.approx(78.75)
    assert rev1["amendment_note"] is None
    assert "人工核对" in rev2["amendment_note"]
    assert rev2["snug_override"] == 20
    assert len(rev1["points"]) == 65  # 原始轨迹逐版保存


def test_amend_replaces_defective_curve(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    bad = good_curve()
    bad[30]["angle"] -= 5.0  # 角度反转缺陷
    r = submit(client, pid, ids[2], bad)
    cid = r.json()["curve_id"]
    assert r.json()["usable"] is False

    r = client.post(f"/curves/{cid}/amend", json={
        "reason": "套筒打滑段为传感器误记，换用工具备份曲线",
        "points": good_curve()})
    assert r.status_code == 201
    assert r.json()["revision"] == 2 and r.json()["usable"] is True

    submit_all_good(client, pid, skip={2})
    assert client.post(f"/procedures/{pid}/review",
                       json={"reviewer": "李四"}).status_code == 200
    # 旧修订缺陷区间仍可查
    rev1 = client.get(f"/curves/{cid}").json()["revisions"][0]
    assert rev1["usable"] is False
    assert rev1["analysis"]["defects"][0]["reason"] == D_ANGLE_REVERSAL


def test_amend_requires_reason_and_valid_index(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    cid = submit(client, pid, ids[4], good_curve()).json()["curve_id"]
    r = client.post(f"/curves/{cid}/amend", json={"snug_index": 20})
    assert r.status_code == 422  # 缺修订原因
    r = client.post(f"/curves/{cid}/amend",
                    json={"reason": "x", "snug_index": 999})
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "snug_index_out_of_range"


def test_amend_relink_record_bolt_mismatch(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    cid = submit(client, pid, ids[1], good_curve()).json()["curve_id"]
    r = client.post(f"/curves/{cid}/amend",
                    json={"reason": "重关联", "record_id": ids[2]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "record_bolt_mismatch"


def test_curve_frozen_after_review(client):
    pid = make_completed(client)
    ids = submit_all_good(client, pid)
    client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    r = submit(client, pid, ids[1], good_curve())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "curve_window_closed"
    r = client.post("/curves/1/amend", json={"reason": "x", "snug_index": 3})
    assert r.status_code == 409


# ------------------------------------------------------------ 作业包与 SVG 标记

def test_package_and_svg_mark_revision_intervals_records(client):
    pid = make_completed(client)
    ids = final_record_ids(client, pid)
    bad = good_curve()
    bad[40]["t"] = bad[39]["t"] - 0.01
    cid = submit(client, pid, ids[6], bad).json()["curve_id"]
    submit_all_good(client, pid, skip={6})

    # JSON 过程包：采用的修订、异常区间、关联记录
    pkg = client.get(f"/procedures/{pid}/package").json()
    bolt6 = next(b for b in pkg["curves"]["bolts"] if b["bolt_no"] == 6)
    assert bolt6["state"] == "unusable"
    assert bolt6["curve_id"] == cid and bolt6["revision"] == 1
    assert bolt6["record_id"] == ids[6]
    assert bolt6["defects"][0]["reason"] == D_TIME_REGRESSION
    assert bolt6["defects"][0]["interval"]["point_index"] == 40

    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "扭矩-转角轨迹" in svg
    assert "轨迹缺陷区间" in svg
    assert "时标倒退" in svg and "点40" in svg  # 异常区间
    assert f"#{ids[6]}" in svg                 # 关联记录

    # 修订后：作业包与 SVG 标出采用的修订 2
    client.post(f"/curves/{cid}/amend",
                json={"reason": "时标为设备时钟抖动，换用备份曲线",
                      "points": good_curve()})
    pkg = client.get(f"/procedures/{pid}/package").json()
    bolt6 = next(b for b in pkg["curves"]["bolts"] if b["bolt_no"] == 6)
    assert bolt6["revision"] == 2 and bolt6["state"] == "ok"
    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "r2·" in svg
    assert "轨迹缺陷区间" not in svg  # 当前采用修订无缺陷


def test_svg_marks_missing_curves(client):
    pid = make_completed(client)
    svg = client.get(f"/procedures/{pid}/diagram.svg").text
    assert "无轨迹" in svg
    gate = client.get(f"/procedures/{pid}/curve-review").json()
    assert gate["passed"] is False
    assert gate["missing_bolts"] == list(range(1, 9))
