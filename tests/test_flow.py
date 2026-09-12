"""端到端流程与规则测试：正常流转、跳步、过期工具、越量程、超差、重复、补拧、派生。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.rules import find_infeasible_rounds, validate_report
from app.schemas import TorqueReport
from app.sequencing import build_plan, circular_distance, cross_sequence, sequence_violations

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
}

SEQ8 = [1, 5, 2, 6, 3, 7, 4, 8]  # 8 栓交叉顺序


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANGE_DB", str(tmp_path / "test.db"))
    init_db()
    with TestClient(app) as c:
        yield c


def make_started(client: TestClient, **overrides) -> int:
    payload = {**BASE, **overrides}
    r = client.post("/procedures", json=payload)
    assert r.status_code == 201, r.text
    pid = r.json()["procedure"]["id"]
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200
    return pid


def report(client, pid, bolt_no, torque, *, ts="2026-09-12T09:00:00",
           tool="TW-1001", rework_of=None):
    body = {
        "bolt_no": bolt_no,
        "tool_id": tool,
        "operator": "张三",
        "reported_at": ts,
        "measured_torque": torque,
    }
    if rework_of is not None:
        body["rework_of"] = rework_of
    return client.post(f"/procedures/{pid}/reports", json=body)


def run_all_steps(client, pid):
    """按 3 轮 x 8 栓顺序全部回传合格值。"""
    for ratio in (0.3, 0.6, 1.0):
        target = round(320.0 * ratio, 2)
        for bolt in SEQ8:
            r = report(client, pid, bolt, target)
            assert r.status_code == 201, r.text
    return r


# ------------------------------------------------------------ 顺序生成

def test_cross_sequence_properties():
    assert cross_sequence(8) == SEQ8
    assert cross_sequence(4) == [1, 3, 2, 4]
    for n in (6, 8, 10, 12, 16, 20, 24):
        seq = cross_sequence(n)
        assert sorted(seq) == list(range(1, n + 1))
        for a, b in zip(seq, seq[1:]):
            assert circular_distance(a, b, n) > 1, f"n={n}: {a}-{b} 相邻"


def test_plan_stages_increase():
    plan = build_plan(8, 320.0, [0.3, 0.6, 1.0])
    assert len(plan) == 24
    assert [s["target_torque"] for s in plan if s["bolt_no"] == 1] == [96.0, 192.0, 320.0]
    assert [s["round_no"] for s in plan] == [1] * 8 + [2] * 8 + [3] * 8


# ------------------------------------------------------------ 正常全流程

def test_full_lifecycle(client):
    pid = make_started(client)
    r = run_all_steps(client, pid)
    assert r.json()["status"] == "completed"

    r = client.post(f"/procedures/{pid}/review", json={"reviewer": "李四", "note": "合格"})
    assert r.status_code == 200
    assert r.json()["procedure"]["status"] == "reviewed"

    r = client.post(f"/procedures/{pid}/archive")
    assert r.json()["procedure"]["status"] == "archived"

    pkg = client.get(f"/procedures/{pid}/package").json()
    assert len(pkg["records"]) == 24
    assert len(pkg["plan"]) == 24
    assert pkg["anomalies"] == []
    assert pkg["revisions"]["ancestors"][0]["id"] == pid

    svg = client.get(f"/procedures/{pid}/diagram.svg")
    assert svg.status_code == 200 and "<svg" in svg.text


def test_report_before_start_rejected(client):
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    r = report(client, pid, 1, 96.0)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "not_started"


def test_review_requires_completion(client):
    pid = make_started(client)
    r = client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    assert r.status_code == 409


# ------------------------------------------------------------ 各类拒绝

def test_out_of_sequence_rejected(client):
    pid = make_started(client)
    assert report(client, pid, 1, 96.0).status_code == 201
    r = report(client, pid, 3, 96.0)  # 期望 5
    detail = r.json()["detail"]
    assert r.status_code == 409
    assert detail["reason"] == "out_of_sequence"
    assert detail["bolt_no"] == 3 and detail["expected_bolt_no"] == 5
    # 异常已入包，进度未推进
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert len(pkg["anomalies"]) == 1
    assert pkg["progress"]["completed_steps"] == 1


def test_duplicate_in_round_rejected(client):
    pid = make_started(client)
    assert report(client, pid, 1, 96.0).status_code == 201
    assert report(client, pid, 5, 96.0).status_code == 201
    r = report(client, pid, 1, 96.0)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "duplicate_in_round"


def test_calibration_expired_rejected(client):
    pid = make_started(client)
    r = report(client, pid, 1, 96.0, ts="2027-01-05T09:00:00")
    detail = r.json()["detail"]
    assert r.status_code == 409
    assert detail["reason"] == "calibration_expired"
    assert detail["bolt_no"] == 1


def test_tool_out_of_range_rejected(client):
    pid = make_started(client)
    r = report(client, pid, 1, 600.0)  # 量程上限 500
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "tool_out_of_range"


def test_torque_out_of_tolerance_rejected(client):
    pid = make_started(client)
    r = report(client, pid, 1, 120.0)  # 目标 96，偏差 25% > 5%
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "torque_out_of_tolerance"


def test_tool_mismatch_rejected(client):
    pid = make_started(client)
    r = report(client, pid, 1, 96.0, tool="TW-9999")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "tool_mismatch"


def test_report_after_archive_rejected(client):
    pid = make_started(client)
    run_all_steps(client, pid)
    client.post(f"/procedures/{pid}/review", json={"reviewer": "李四"})
    client.post(f"/procedures/{pid}/archive")
    r = report(client, pid, 1, 96.0)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "archived"


# ------------------------------------------------------------ 恢复与补拧

def test_resume_sequence(client):
    pid = make_started(client)
    for bolt in SEQ8[:3]:
        assert report(client, pid, bolt, 96.0).status_code == 201
    r = client.get(f"/procedures/{pid}/resume").json()
    assert r["completed_steps"] == 3 and r["total_steps"] == 24
    assert r["resume_sequence"][0]["bolt_no"] == SEQ8[3]
    assert len(r["resume_sequence"]) == 21


def test_rework_does_not_overwrite(client):
    pid = make_started(client)
    r = report(client, pid, 1, 96.0)
    rec_id = r.json()["record_id"]
    # 补拧：指向原记录，不改变进度
    r = report(client, pid, 1, 97.0, rework_of=rec_id)
    assert r.status_code == 201
    assert r.json()["rework_of"] == rec_id
    pkg = client.get(f"/procedures/{pid}/package").json()
    assert len(pkg["records"]) == 2
    assert pkg["progress"]["completed_steps"] == 1  # 补拧不推进
    original = [r for r in pkg["records"] if r["id"] == rec_id][0]
    assert original["measured_torque"] == 96.0  # 原记录未被覆盖
    # 继续正常顺序
    assert report(client, pid, 5, 96.0).status_code == 201


def test_rework_requires_existing_record(client):
    pid = make_started(client)
    r = report(client, pid, 1, 96.0, rework_of=999)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "rework_target_missing"


# ------------------------------------------------------------ 版本与锁定

def test_approved_params_locked(client):
    pid = make_started(client)
    r = client.put(f"/procedures/{pid}", json={**BASE, "target_torque": 300.0})
    assert r.status_code == 409


def test_derive_new_version(client):
    pid = make_started(client)
    r = client.post(f"/procedures/{pid}/derive", json={
        "change_note": "目标扭矩调整并更换扳手",
        "target_torque": 280.0,
        "tool_id": "TW-2002",
        "tool_range_min": 40.0,
        "tool_range_max": 400.0,
        "calibration_valid_until": "2027-06-30",
    })
    assert r.status_code == 201, r.text
    new = r.json()["procedure"]
    assert new["version"] == 2 and new["parent_id"] == pid
    assert new["status"] == "draft"
    assert new["target_torque"] == 280.0 and new["tool_id"] == "TW-2002"
    # 修订链
    pkg = client.get(f"/procedures/{new['id']}/package").json()
    chain = pkg["revisions"]["ancestors"]
    assert [n["id"] for n in chain] == [pid, new["id"]]
    assert chain[-1]["change_note"] == "目标扭矩调整并更换扳手"


def test_derive_from_draft_rejected(client):
    r = client.post("/procedures", json=BASE)
    pid = r.json()["procedure"]["id"]
    r = client.post(f"/procedures/{pid}/derive", json={"change_note": "x"})
    assert r.status_code == 409


# ------------------------------------------------------------ 输入校验

def test_invalid_inputs_rejected(client):
    bad_cases = [
        {**BASE, "bolt_count": 7},                       # 非偶数
        {**BASE, "stage_ratios": [0.5, 0.3, 1.0]},       # 非递增
        {**BASE, "stage_ratios": [0.5, 0.8]},            # 末级非 1.0
        {**BASE, "target_torque": 600.0},                # 目标超量程
        {**BASE, "tool_range_min": 500.0, "tool_range_max": 50.0},
    ]
    for payload in bad_cases:
        r = client.post("/procedures", json=payload)
        assert r.status_code == 422, payload


def test_four_bolt_approve_rejected(client):
    """4 栓无法生成全程非相邻序列（1-3-2-4 中 3→2 相邻），批准即拒绝并说明原因。"""
    r = client.post("/procedures", json={**BASE, "bolt_count": 4})
    assert r.status_code == 201  # 草稿可创建
    pid = r.json()["procedure"]["id"]
    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "sequence_not_realizable"
    assert [3, 2] in detail["adjacent_pairs"]
    assert "4 栓" in detail["message"]
    # 状态保持草稿，无法开工
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "draft"
    assert client.post(f"/procedures/{pid}/start").status_code == 409


def test_sequence_violations():
    assert sequence_violations(4) == [(3, 2)]
    for n in (6, 8, 10, 12, 16, 20, 24):
        assert sequence_violations(n) == []


def test_adjacent_guard_applies_to_four_bolts():
    """豁免已删除：4 栓序列中相邻的 3→2 在规则层同样被拒绝。"""
    proc = {
        "status": "in_progress", "bolt_count": 4, "tool_id": "T",
        "calibration_valid_until": __import__("datetime").date(2026, 12, 31),
        "tool_range_min": 0.0, "tool_range_max": 1000.0,
        "target_torque": 100.0, "stage_ratios": [1.0], "tolerance_pct": 5.0,
    }
    plan = build_plan(4, 100.0, [1.0])  # 1-3-2-4
    done = [{"round_no": 1, "bolt_no": 1}, {"round_no": 1, "bolt_no": 3}]
    report = TorqueReport(bolt_no=2, tool_id="T", operator="x",
                          reported_at="2026-09-12T09:00:00", measured_torque=100.0)
    rej = validate_report(proc, plan, done, report)
    assert rej is not None and rej.reason == "adjacent_in_round"


def test_approve_rejects_infeasible_round(client):
    """首轮允许区间 28.5~31.5 与量程 50~150 无交集：拒绝批准并指出冲突轮次。"""
    payload = {**BASE, "target_torque": 100.0, "stage_ratios": [0.3, 1.0],
               "tool_range_min": 50.0, "tool_range_max": 150.0}
    r = client.post("/procedures", json=payload)
    assert r.status_code == 201  # 目标扭矩在量程内，草稿可创建
    pid = r.json()["procedure"]["id"]

    r = client.post(f"/procedures/{pid}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "round_interval_infeasible"
    assert detail["conflicts"] == [{
        "round_no": 1, "ratio": 0.3, "target_torque": 30.0,
        "allowed_interval": [28.5, 31.5], "tool_range": [50.0, 150.0],
    }]
    assert "第 1 轮" in detail["message"]
    # 只有第 1 轮冲突（第 2 轮 [95, 105] 与量程有交集）
    assert len(detail["conflicts"]) == 1
    # 保持草稿；修正量程后可批准、可开工
    assert client.get(f"/procedures/{pid}").json()["procedure"]["status"] == "draft"
    assert client.put(f"/procedures/{pid}",
                      json={**payload, "tool_range_min": 20.0}).status_code == 200
    assert client.post(f"/procedures/{pid}/approve").status_code == 200
    assert client.post(f"/procedures/{pid}/start").status_code == 200


def test_find_infeasible_rounds_unit():
    # 区间上界恰好等于量程下限：有交集（单点），可行
    assert find_infeasible_rounds(100.0, [0.5, 1.0], 5.0, 52.5, 150.0) == []
    # 两轮都越界
    conflicts = find_infeasible_rounds(100.0, [0.3, 1.0], 5.0, 110.0, 150.0)
    assert [c["round_no"] for c in conflicts] == [1, 2]


# ------------------------------------------------------------ 相邻守卫（规则层单测）

def test_adjacent_guard_unit():
    proc = {
        "status": "in_progress", "bolt_count": 8, "tool_id": "T",
        "calibration_valid_until": __import__("datetime").date(2026, 12, 31),
        "tool_range_min": 0.0, "tool_range_max": 1000.0,
        "target_torque": 100.0, "stage_ratios": [1.0], "tolerance_pct": 5.0,
    }
    plan = [
        {"round_no": 1, "order_in_round": 1, "bolt_no": 3, "ratio": 1.0, "target_torque": 100.0},
        {"round_no": 1, "order_in_round": 2, "bolt_no": 4, "ratio": 1.0, "target_torque": 100.0},
    ]
    done = [{"round_no": 1, "bolt_no": 3}]
    report = TorqueReport(bolt_no=4, tool_id="T", operator="x",
                          reported_at="2026-09-12T09:00:00", measured_torque=100.0)
    rej = validate_report(proc, plan, done, report)
    assert rej is not None and rej.reason == "adjacent_in_round"
