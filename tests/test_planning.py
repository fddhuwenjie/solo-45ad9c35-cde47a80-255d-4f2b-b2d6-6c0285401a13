"""受限栓位施工规划：规划器回溯搜索、时间窗/换工具动作、无解诊断与 API 集成。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.planning import (BoltSite, PlanInfeasible, circular_angle_distance,
                          default_min_separation, expand_angles, schedule_plan)

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

ALIGN_GEOM = {
    "flange_face_diameter_mm": 285.0,
    "gasket_inner_diameter_mm": 220.0,
    "gasket_outer_diameter_mm": 270.0,
    "bore_diameter_mm": 200.0,
    "max_parallelism_mm": 1.0,
    "max_radial_mismatch_mm": 2.0,
}
EDGE0 = (ALIGN_GEOM["flange_face_diameter_mm"]
         - ALIGN_GEOM["gasket_outer_diameter_mm"]) / 2.0

# 反例：4 栓实际方位 50°/200°/310°/320°，最小角间隔 60°
COUNTEREXAMPLE_ANGLES = {1: 50.0, 2: 200.0, 3: 310.0, 4: 320.0}


def counterexample_sites() -> dict[int, BoltSite]:
    return {k: BoltSite(k, a, [], ["TW-1"]) for k, a in COUNTEREXAMPLE_ANGLES.items()}


def counterexample_constraints() -> dict:
    return {
        "shift_start": "2026-09-13T08:00:00",
        "min_separation_deg": 60.0,
        "step_minutes": 5.0,
        "tool_change_minutes": 2.0,
        "tool_windows": [],
        "bolts": [{"bolt_no": k, "angle_deg": a}
                  for k, a in COUNTEREXAMPLE_ANGLES.items()],
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def submit_alignment(client, pid):
    pts = [{"angle_deg": a, "axial_gap": 2.0, "radial_offset": 0.0,
            "gasket_edge_position": EDGE0, "bolt_free_insertion": True,
            "length_unit": "mm"} for a in (0, 45, 90, 135, 180, 225, 270, 315)]
    r = client.post(f"/procedures/{pid}/alignment-checks",
                    json={**ALIGN_GEOM, "points": pts, "operator": "预检员",
                          "measured_at": "2026-09-12T08:00:00"})
    assert r.status_code == 201, r.text


def make_draft(client, **overrides):
    r = client.post("/procedures", json={**BASE, **overrides})
    assert r.status_code == 201, r.text
    return r.json()["procedure"]["id"]


def report(client, pid, bolt_no, torque, *, tool="TW-1001", ts="2026-09-13T08:30:00"):
    return client.post(f"/procedures/{pid}/reports", json={
        "bolt_no": bolt_no, "tool_id": tool, "operator": "张三",
        "reported_at": ts, "measured_torque": torque})


# ------------------------------------------------------------ 规划器（纯函数）

def test_counterexample_greedy_trap_is_feasible():
    """反例：贪心按对径优先会走进 1→2→4 后卡死（剩 3 与 4 角距 10°），
    回溯搜索须找到 1→3→2→4（连续角距 100°/110°/120°），不得误判无解。"""
    steps, actions = schedule_plan(
        bolt_count=4, stage_ratios=[1.0], target_torque=100.0,
        sites=counterexample_sites(), min_separation_deg=60.0,
        step_minutes=5.0, tool_change_minutes=2.0,
        shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
        default_tool="TW-1")
    seq = [s["bolt_no"] for s in steps]
    assert seq == [1, 3, 2, 4]
    dists = [circular_angle_distance(COUNTEREXAMPLE_ANGLES[a], COUNTEREXAMPLE_ANGLES[b])
             for a, b in zip(seq, seq[1:])]
    assert dists == [100.0, 110.0, 120.0]
    assert all(d >= 60.0 for d in dists)
    assert actions == []


def test_counterexample_no_false_relaxation():
    """同一反例在 API 预览/批准层：可行，不得返回 409 或建议降低角间隔。"""
    # 纯函数层再验证一次：不应抛 PlanInfeasible（也就不存在 relaxations）
    try:
        schedule_plan(
            bolt_count=4, stage_ratios=[0.5, 1.0], target_torque=100.0,
            sites=counterexample_sites(), min_separation_deg=60.0,
            step_minutes=5.0, tool_change_minutes=2.0,
            shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
            default_tool="TW-1")
    except PlanInfeasible as exc:  # pragma: no cover - 修复前会走到这里
        pytest.fail(f"可行方案被误判为无解：{exc.as_detail()}")


def test_truly_infeasible_reports_minimal_relaxation():
    """真无解：4 栓方位 0°/10°/20°/180°、min_sep=60°——三栓挤在 20° 内，
    任何顺序都必有两栓相邻，搜索全部顺序后给出最少解除限制。"""
    sites = {1: BoltSite(1, 0.0, [], ["TW-1"]),
             2: BoltSite(2, 10.0, [], ["TW-1"]),
             3: BoltSite(3, 20.0, [], ["TW-1"]),
             4: BoltSite(4, 180.0, [], ["TW-1"])}
    with pytest.raises(PlanInfeasible) as ei:
        schedule_plan(
            bolt_count=4, stage_ratios=[1.0], target_torque=100.0, sites=sites,
            min_separation_deg=60.0, step_minutes=5.0, tool_change_minutes=2.0,
            shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
            default_tool="TW-1")
    detail = ei.value.as_detail()
    assert detail["reason"] == "plan_infeasible"
    assert detail["round_no"] == 1
    assert detail["blocked_bolts"]
    assert any(r.get("constraint") == "min_separation_deg"
               for r in detail["relaxations"])


def test_default_min_separation_blocks_adjacent():
    """缺省最小角间隔复现同轮非相邻规则；8 栓对径优先贪心即得交叉序列。"""
    angles = expand_angles(8, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["TW-1"]) for k in range(1, 9)}
    steps, _ = schedule_plan(
        bolt_count=8, stage_ratios=[1.0], target_torque=100.0, sites=sites,
        min_separation_deg=None, step_minutes=5.0, tool_change_minutes=2.0,
        shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
        default_tool="TW-1")
    assert [s["bolt_no"] for s in steps] == [1, 5, 2, 6, 3, 7, 4, 8]
    assert default_min_separation(8) > 45.0


def test_wait_action_when_window_opens_later():
    """栓 5 的时间窗 09:00 才开：计划生成等待动作，该栓排在 09:00。"""
    angles = expand_angles(8, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["TW-1"]) for k in range(1, 9)}
    sites[5].windows = [(datetime(2026, 9, 13, 9, 0), datetime(2026, 9, 13, 12, 0))]
    steps, actions = schedule_plan(
        bolt_count=8, stage_ratios=[1.0], target_torque=100.0, sites=sites,
        min_separation_deg=None, step_minutes=5.0, tool_change_minutes=2.0,
        shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
        default_tool="TW-1")
    assert len(steps) == 8
    waits = [a for a in actions if a["kind"] == "wait"]
    assert waits and waits[0]["until"] == "2026-09-13T09:00:00"
    bolt5 = next(s for s in steps if s["bolt_no"] == 5)
    assert bolt5["scheduled_at"] == "2026-09-13T09:00:00"


def test_tool_change_action_when_backup_tool_earlier():
    """批准工具 10:00 才可用而备用工具全天可用：生成换工具动作。"""
    angles = expand_angles(8, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["TW-1", "TW-2"]) for k in range(1, 9)}
    tool_windows = {"TW-1": [(datetime(2026, 9, 13, 10, 0),
                              datetime(2026, 9, 13, 12, 0))]}
    steps, actions = schedule_plan(
        bolt_count=8, stage_ratios=[1.0], target_torque=100.0, sites=sites,
        min_separation_deg=None, step_minutes=5.0, tool_change_minutes=2.0,
        shift_start=datetime(2026, 9, 13, 8, 0), tool_windows=tool_windows,
        default_tool="TW-1")
    changes = [a for a in actions if a["kind"] == "tool_change"]
    assert changes and changes[0]["from_tool"] == "TW-1"
    assert changes[0]["to_tool"] == "TW-2"
    assert steps[0]["tool_id"] == "TW-2"


def test_time_window_infeasible_diagnosis():
    """栓 5 的窗在排程起点之前结束：无解，建议解除该栓时间窗。"""
    angles = expand_angles(8, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["TW-1"]) for k in range(1, 9)}
    sites[5].windows = [(datetime(2026, 9, 13, 6, 0), datetime(2026, 9, 13, 7, 0))]
    with pytest.raises(PlanInfeasible) as ei:
        schedule_plan(
            bolt_count=8, stage_ratios=[1.0], target_torque=100.0, sites=sites,
            min_separation_deg=None, step_minutes=5.0, tool_change_minutes=2.0,
            shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
            default_tool="TW-1")
    detail = ei.value.as_detail()
    assert detail["round_no"] == 1
    assert any(b["bolt_no"] == 5 for b in detail["blocked_bolts"])
    assert any(r.get("constraint") == "time_window" and r.get("bolt_no") == 5
               for r in detail["relaxations"])


def test_locked_steps_stay_in_place():
    """修订：已完成步骤原位锁定，只重排未完成步骤。"""
    angles = expand_angles(8, 0.0, True)
    sites = {k: BoltSite(k, angles[k], [], ["TW-1"]) for k in range(1, 9)}
    locked = [
        {"round_no": 1, "order_in_round": 1, "bolt_no": 1, "ratio": 1.0,
         "target_torque": 100.0, "tool_id": "TW-1",
         "scheduled_at": "2026-09-13T08:00:00", "angle_deg": 0.0},
        {"round_no": 1, "order_in_round": 2, "bolt_no": 5, "ratio": 1.0,
         "target_torque": 100.0, "tool_id": "TW-1",
         "scheduled_at": "2026-09-13T08:05:00", "angle_deg": 180.0},
    ]
    steps, _ = schedule_plan(
        bolt_count=8, stage_ratios=[1.0], target_torque=100.0, sites=sites,
        min_separation_deg=None, step_minutes=5.0, tool_change_minutes=2.0,
        shift_start=datetime(2026, 9, 13, 8, 0), tool_windows={},
        default_tool="TW-1", locked_steps=locked)
    assert steps[:2] == locked
    assert [s["order_in_round"] for s in steps] == list(range(1, 9))
    assert sorted(s["bolt_no"] for s in steps[2:]) == [2, 3, 4, 6, 7, 8]
    # 重排首步与已完成末栓（5 号，180°）满足角间隔
    assert circular_angle_distance(180.0, steps[2]["angle_deg"]) > 45.0


# ------------------------------------------------------------ API 集成

def test_api_counterexample_preview_and_approve(client):
    """反例经 API：预览可行，批准 200，冻结计划为 1→3→2→4。"""
    pid = make_draft(client, bolt_count=4)
    r = client.put(f"/procedures/{pid}/constraints", json=counterexample_constraints())
    assert r.status_code == 200, r.text
    preview = r.json()["preview"]
    assert preview["feasible"] is True
    assert "diagnosis" not in preview

    submit_alignment(client, pid)
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 200, r.text

    body = client.get(f"/procedures/{pid}").json()
    assert body["plan_status"]["frozen"] is True
    assert [s["bolt_no"] for s in body["plan"] if s["round_no"] == 1] == [1, 3, 2, 4]
    dists = [circular_angle_distance(s1["angle_deg"], s2["angle_deg"])
             for s1, s2 in zip(body["plan"], body["plan"][1:])
             if s1["round_no"] == s2["round_no"] == 1]
    assert dists == [100.0, 110.0, 120.0]


def test_api_infeasible_approve_409(client):
    """真无解：批准返回 409 plan_infeasible，含冲突轮次、受阻栓位与最少解除限制。"""
    pid = make_draft(client, bolt_count=4)
    cons = counterexample_constraints()
    cons["bolts"] = [{"bolt_no": 1, "angle_deg": 0.0},
                     {"bolt_no": 2, "angle_deg": 10.0},
                     {"bolt_no": 3, "angle_deg": 20.0},
                     {"bolt_no": 4, "angle_deg": 180.0}]
    assert client.put(f"/procedures/{pid}/constraints", json=cons).status_code == 200
    preview = client.get(f"/procedures/{pid}/constraints").json()["preview"]
    assert preview["feasible"] is False
    assert preview["diagnosis"]["reason"] == "plan_infeasible"

    submit_alignment(client, pid)
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "plan_infeasible"
    assert detail["round_no"] == 1
    assert detail["blocked_bolts"]
    assert detail["relaxations"]
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "draft"


def test_api_plan_revision_locks_completed(client):
    """开工后现场障碍：派生计划修订，已完成步骤原位锁定，恢复序列同源。"""
    pid = make_draft(client)
    assert client.put(f"/procedures/{pid}/constraints", json={
        "shift_start": "2026-09-13T08:00:00",
        "step_minutes": 5.0, "tool_change_minutes": 2.0,
        "tool_windows": [], "bolts": [],
    }).status_code == 200
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200

    plan = client.get(f"/procedures/{pid}").json()["plan"]
    first_two = [(s["round_no"], s["bolt_no"]) for s in plan[:2]]
    for _round, bolt in first_two:
        r = report(client, pid, bolt, 96.0)
        assert r.status_code == 201, r.text

    # 栓 6 被脚手架挡住：新时间窗 12:00 才开
    r = client.post(f"/procedures/{pid}/plan-revisions", json={
        "change_note": "栓 6 脚手架遮挡，12:00 后方可作业",
        "constraints": {
            "shift_start": "2026-09-13T08:00:00",
            "step_minutes": 5.0, "tool_change_minutes": 2.0,
            "tool_windows": [],
            "bolts": [{"bolt_no": 6,
                       "windows": [{"start": "2026-09-13T12:00:00",
                                    "end": "2026-09-13T18:00:00"}]}],
        },
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["locked_steps"] == 2
    assert body["steps"][:2] == [s for s in plan[:2]]
    assert body["steps"][2]["scheduled_at"] >= "2026-09-13T08:10:00"
    bolt6 = next(s for s in body["steps"] if s["bolt_no"] == 6 and s["round_no"] == 1)
    assert bolt6["scheduled_at"] >= "2026-09-13T12:00:00"

    # 恢复序列、作业包、SVG 同源（同一冻结计划 v2）
    resume = client.get(f"/procedures/{pid}/resume").json()["resume_sequence"]
    assert [s["bolt_no"] for s in resume] == [s["bolt_no"] for s in body["steps"][2:]]
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert pkg["plan"] == body["steps"]
    assert pkg["planning"]["status"]["revision"] == 2
    svg = client.get(f"/procedures/{pid}/diagram.svg")
    assert svg.status_code == 200 and "<svg" in svg.text

    # 回传按新计划顺序推进
    nxt = body["steps"][2]
    r = report(client, pid, nxt["bolt_no"], 96.0)
    assert r.status_code == 201, r.text


def test_api_plan_revision_infeasible_keeps_current_plan(client):
    """修订无解：409 诊断，当前冻结计划不变。"""
    pid = make_draft(client, bolt_count=4)
    assert client.put(f"/procedures/{pid}/constraints",
                      json=counterexample_constraints()).status_code == 200
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    before = client.get(f"/procedures/{pid}").json()["plan"]

    cons = counterexample_constraints()
    for b in cons["bolts"]:
        if b["bolt_no"] == 1:  # 栓 1 的可操作窗在排程起点前已结束
            b["windows"] = [{"start": "2026-09-13T01:00:00",
                             "end": "2026-09-13T02:00:00"}]
    r = client.post(f"/procedures/{pid}/plan-revisions",
                    json={"change_note": "栓 1 时间窗已过期", "constraints": cons})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "plan_infeasible"
    assert client.get(f"/procedures/{pid}").json()["plan"] == before


def test_api_constraints_locked_after_approve(client):
    """批准后现场约束随计划冻结：PUT 拒绝，须派生计划修订。"""
    pid = make_draft(client)
    assert client.put(f"/procedures/{pid}/constraints", json={
        "shift_start": "2026-09-13T08:00:00",
        "step_minutes": 5.0, "tool_change_minutes": 2.0,
        "tool_windows": [], "bolts": [],
    }).status_code == 200
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    r = client.put(f"/procedures/{pid}/constraints", json={
        "shift_start": "2026-09-13T08:00:00",
        "step_minutes": 5.0, "tool_change_minutes": 2.0,
        "tool_windows": [], "bolts": [],
    })
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "constraints_locked"


def test_api_report_uses_planned_tool(client):
    """计划步骤锁定工具：换工具步骤须用计划工具回传，批准工具反而被拒。"""
    pid = make_draft(client)
    assert client.put(f"/procedures/{pid}/constraints", json={
        "shift_start": "2026-09-13T08:00:00",
        "step_minutes": 5.0, "tool_change_minutes": 2.0,
        "tool_windows": [{"tool_id": "TW-1001", "start": "2026-09-13T10:00:00",
                          "end": "2026-09-13T12:00:00"}],
        "bolts": [{"bolt_no": k, "allowed_tools": ["TW-1001", "TW-2002"]}
                  for k in range(1, 9)],
    }).status_code == 200
    submit_alignment(client, pid)
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200

    plan = client.get(f"/procedures/{pid}").json()["plan"]
    assert plan[0]["tool_id"] == "TW-2002"  # 批准工具 10:00 才可用，计划换用备用
    r = report(client, pid, plan[0]["bolt_no"], 96.0, tool="TW-1001")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "tool_mismatch"
    r = report(client, pid, plan[0]["bolt_no"], 96.0, tool="TW-2002")
    assert r.status_code == 201, r.text
