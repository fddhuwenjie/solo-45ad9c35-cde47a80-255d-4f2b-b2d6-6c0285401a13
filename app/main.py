"""法兰螺栓紧固工艺管理服务：创建 -> 批准 -> 开工 -> 逐栓回传 -> 复核 -> 封存。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import ValidationError

from .alignment import analyze_alignment, diff_analyses
from .curve import analyze_curve, evaluate_curve_review
from .db import get_conn, init_db, utcnow
from .planning import (PlanInfeasible, default_min_separation, expand_angles,
                       schedule_plan)
from .rules import find_infeasible_rounds, validate_report
from .schemas import (AlignmentCheckCreate, BaselineRequest, CurveAmend, CurveSubmit,
                      DeriveRequest, ExcludeRequest, MeasurementBatchCreate,
                      PlanRevisionCreate, RemeasurementRequest, RetestRequest,
                      ProcedureCreate, ReviewRequest, SiteConstraintsInput,
                      TensioningAdoptUltrasonic, TensioningPlanCreate,
                      TensioningRevisionCreate, TensioningRoundReport, ThermalCaseCreate,
                      ThermalRevisionCreate, TorqueReport)
from .sequencing import build_plan, sequence_violations
from .svg import render_svg
from .tensioning import (build_scheme, channel_results, diff_schemes, evaluate_plan,
                         find_infeasible_rounds as find_infeasible_tension_rounds,
                         flatten_groups, scheme_setpoints, validate_round_report)
from .thermal import diff_cases, evaluate_case, normalize_case
from .ultrasonic import GAP_MESSAGES, evaluate_batch, evaluate_reading
STATUS_LABEL = {
    "draft": "已创建",
    "approved": "已批准",
    "in_progress": "进行中",
    "completed": "已完成待复核",
    "reviewed": "已复核",
    "archived": "已封存",
}

# 允许派生新版本的状态（草稿直接 PUT 修改即可）
DERIVABLE = ("approved", "in_progress", "completed", "reviewed", "archived")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="法兰紧固工艺管理", version="1.0.0", lifespan=lifespan)


def get_db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 工具函数

def _row_to_proc(row: sqlite3.Row) -> dict:
    proc = dict(row)
    proc["stage_ratios"] = json.loads(proc["stage_ratios"])
    proc["calibration_valid_until"] = date.fromisoformat(proc["calibration_valid_until"])
    proc["clockwise"] = bool(proc["clockwise"])
    return proc


def _fetch_proc(conn: sqlite3.Connection, pid: int) -> dict:
    row = conn.execute("SELECT * FROM procedures WHERE id =?", (pid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"工艺 {pid} 不存在")
    return _row_to_proc(row)


def _fetch_proc_or_none(conn: sqlite3.Connection, pid: int | None) -> dict | None:
    if pid is None:
        return None
    row = conn.execute("SELECT * FROM procedures WHERE id=?", (pid,)).fetchone()
    return _row_to_proc(row) if row else None


def _done_records(conn: sqlite3.Connection, pid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM records WHERE procedure_id=? AND rework_of IS NULL ORDER BY id", (pid,)
    ).fetchall()
    return [dict(r) for r in rows]


def _all_records(conn: sqlite3.Connection, pid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM records WHERE procedure_id=? ORDER BY id", (pid,)
    ).fetchall()
    return [dict(r) for r in rows]


def _locked_bolts(conn: sqlite3.Connection, pid: int) -> set[int]:
    """补拧派生工艺锁定（不补拧、沿用合格结果）的螺栓；普通工艺为空集。"""
    row = conn.execute(
        "SELECT locked_bolts FROM rework_jobs WHERE rework_procedure_id=?", (pid,)
    ).fetchone()
    return set(json.loads(row["locked_bolts"])) if row else set()


# ---------------------------------------------------------------- 受限栓位施工计划

def _latest_constraints(conn: sqlite3.Connection, pid: int) -> dict | None:
    """最新现场约束（草稿可改、批准冻结、plan-revisions 派生新版）。"""
    row = conn.execute(
        "SELECT * FROM site_constraints WHERE procedure_id=? ORDER BY revision DESC",
        (pid,)).fetchone()
    if row is None:
        return None
    return {"revision": row["revision"], "input": json.loads(row["payload"]),
            "created_at": row["created_at"]}


def _current_plan(conn: sqlite3.Connection, pid: int) -> dict | None:
    """当前冻结计划（最新计划修订）；未批准时为 None。"""
    row = conn.execute(
        "SELECT * FROM plan_revisions WHERE procedure_id=? ORDER BY revision DESC",
        (pid,)).fetchone()
    if row is None:
        return None
    payload = json.loads(row["plan"])
    return {"revision": row["revision"], "constraint_revision": row["constraint_revision"],
            "change_note": row["change_note"], "created_at": row["created_at"],
            "steps": payload["steps"], "actions": payload["actions"],
            "meta": payload.get("meta") or {}}


def _constraint_bolt_errors(proc: dict, ci: dict) -> list[int]:
    """现场约束中超出 1..N 的栓号（PUT 改小 bolt_count 后旧约束可能失效）。"""
    return [b["bolt_no"] for b in ci["bolts"]
            if not (1 <= b["bolt_no"] <= proc["bolt_count"])]


def _parse_windows(wins: list[dict]) -> list[tuple[datetime, datetime]]:
    return [(datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
            for w in wins or []]


def _run_planner(proc: dict, ci: dict, *, locked_steps: list[dict] | tuple = (),
                 locked_bolts: set[int] | frozenset[int] = frozenset()):
    """用现场约束跑规划器（纯函数）；约束栓号越界时抛 409。"""
    bad = _constraint_bolt_errors(proc, ci)
    if bad:
        raise HTTPException(409, detail={
            "reason": "constraints_bolt_out_of_range",
            "message": f"现场约束登记的栓号 {bad} 超出当前工艺螺栓数 "
                       f"{proc['bolt_count']}；请修正后重新登记",
            "bolt_nos": bad,
        })
    overrides = {b["bolt_no"]: b.get("angle_deg") for b in ci["bolts"]}
    angles = expand_angles(proc["bolt_count"], proc["start_angle_deg"],
                           bool(proc["clockwise"]), overrides)
    tool_windows: dict[str, list] = {}
    for tw in ci["tool_windows"]:
        tool_windows.setdefault(tw["tool_id"], []).append(
            (datetime.fromisoformat(tw["start"]), datetime.fromisoformat(tw["end"])))
    by_bolt = {b["bolt_no"]: b for b in ci["bolts"]}
    from .planning import BoltSite
    sites = {}
    for k in range(1, proc["bolt_count"] + 1):
        c = by_bolt.get(k) or {}
        sites[k] = BoltSite(
            bolt_no=k, angle_deg=angles[k],
            windows=_parse_windows(c.get("windows") or []),
            allowed_tools=c.get("allowed_tools") or [proc["tool_id"]],
            clearance_deg=c.get("clearance_deg") or 0.0,
        )
    return schedule_plan(
        bolt_count=proc["bolt_count"], stage_ratios=proc["stage_ratios"],
        target_torque=proc["target_torque"], sites=sites,
        min_separation_deg=ci.get("min_separation_deg"),
        step_minutes=ci.get("step_minutes", 5.0),
        tool_change_minutes=ci.get("tool_change_minutes", 2.0),
        shift_start=datetime.fromisoformat(ci["shift_start"]),
        tool_windows=tool_windows, default_tool=proc["tool_id"],
        locked_steps=locked_steps, locked_bolts=locked_bolts)


def _default_steps(proc: dict, locked: set[int]) -> list[dict]:
    """规则圆周计划（无现场约束）：交叉序列 + 计划字段默认值。"""
    angles = expand_angles(proc["bolt_count"], proc["start_angle_deg"],
                           bool(proc["clockwise"]))
    steps = build_plan(proc["bolt_count"], proc["target_torque"],
                       proc["stage_ratios"], locked)
    for s in steps:
        s["tool_id"] = proc["tool_id"]
        s["scheduled_at"] = None
        s["angle_deg"] = angles[s["bolt_no"]]
    return steps


def _proc_plan(conn: sqlite3.Connection, proc: dict) -> list[dict]:
    """当前生效计划：已冻结读冻结版本；草稿有约束给规划器预览，否则规则圆周。"""
    frozen = _current_plan(conn, proc["id"])
    if frozen is not None:
        return frozen["steps"]
    cons = _latest_constraints(conn, proc["id"])
    if cons is not None:
        try:
            steps, _ = _run_planner(proc, cons["input"],
                                    locked_bolts=_locked_bolts(conn, proc["id"]))
            return steps
        except PlanInfeasible:
            return []  # 预览不可行：空计划，诊断见 plan_status
    return _default_steps(proc, _locked_bolts(conn, proc["id"]))


def _plan_status(conn: sqlite3.Connection, proc: dict) -> dict:
    """计划可行性视图：冻结版本号或草稿预览诊断。"""
    frozen = _current_plan(conn, proc["id"])
    if frozen is not None:
        return {"feasible": True, "frozen": True, "revision": frozen["revision"],
                "constraint_revision": frozen["constraint_revision"]}
    cons = _latest_constraints(conn, proc["id"])
    if cons is not None:
        try:
            _run_planner(proc, cons["input"],
                         locked_bolts=_locked_bolts(conn, proc["id"]))
            return {"feasible": True, "frozen": False, "revision": None,
                    "constraint_revision": cons["revision"]}
        except PlanInfeasible as exc:
            return {"feasible": False, "frozen": False, "revision": None,
                    "constraint_revision": cons["revision"],
                    "diagnosis": exc.as_detail()}
    return {"feasible": True, "frozen": False, "revision": None,
            "constraint_revision": None}


def _freeze_plan(conn: sqlite3.Connection, proc: dict) -> None:
    """批准时冻结计划 v1：有约束跑规划器（无解即 409），否则规则圆周。"""
    if _current_plan(conn, proc["id"]) is not None:
        return
    locked = _locked_bolts(conn, proc["id"])
    cons = _latest_constraints(conn, proc["id"])
    if cons is not None:
        try:
            steps, actions = _run_planner(proc, cons["input"], locked_bolts=locked)
        except PlanInfeasible as exc:
            raise HTTPException(409, detail=exc.as_detail())
        payload = {"steps": steps, "actions": actions,
                   "meta": {"constraint_revision": cons["revision"],
                            "generated_at": utcnow()}}
        cons_rev = cons["revision"]
    else:
        payload = {"steps": _default_steps(proc, locked), "actions": [],
                   "meta": {"generated_at": utcnow()}}
        cons_rev = None
    conn.execute(
        "INSERT INTO plan_revisions"
        " (procedure_id, revision, constraint_revision, plan, change_note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (proc["id"], 1, cons_rev, json.dumps(payload), "批准冻结", utcnow()))
    conn.commit()


def _insert_proc(conn: sqlite3.Connection, data: ProcedureCreate, *,
                 version: int = 1, parent_id: int | None = None,
                 change_note: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO procedures
           (version, parent_id, change_note, status, flange_class, bolt_count, gasket,
            target_torque, stage_ratios, tolerance_pct, tool_id, tool_range_min,
            tool_range_max, calibration_valid_until, start_angle_deg, clockwise,
            curve_direction, snug_torque, post_snug_angle_min_deg,
            post_snug_angle_max_deg, max_sample_interval_ms, slope_drop_limit,
            max_outlier_rate_pct, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version, parent_id, change_note, "draft", data.flange_class, data.bolt_count,
            data.gasket, data.target_torque, json.dumps(data.stage_ratios),
            data.tolerance_pct, data.tool_id, data.tool_range_min, data.tool_range_max,
            data.calibration_valid_until.isoformat(), data.start_angle_deg,
            int(data.clockwise), data.curve_direction, data.snug_torque,
            data.post_snug_angle_min_deg, data.post_snug_angle_max_deg,
            data.max_sample_interval_ms, data.slope_drop_limit,
            data.max_outlier_rate_pct, utcnow(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _proc_as_create(proc: dict, *, stage_ratios: list[float] | None = None) -> ProcedureCreate:
    """把（已批准）工艺参数重新装配为 ProcedureCreate，供派生/补拧草稿使用。"""
    return ProcedureCreate(
        flange_class=proc["flange_class"], bolt_count=proc["bolt_count"],
        gasket=proc["gasket"], target_torque=proc["target_torque"],
        stage_ratios=stage_ratios or proc["stage_ratios"],
        tolerance_pct=proc["tolerance_pct"], tool_id=proc["tool_id"],
        tool_range_min=proc["tool_range_min"], tool_range_max=proc["tool_range_max"],
        calibration_valid_until=proc["calibration_valid_until"],
        start_angle_deg=proc["start_angle_deg"], clockwise=proc["clockwise"],
        curve_direction=proc["curve_direction"], snug_torque=proc["snug_torque"],
        post_snug_angle_min_deg=proc["post_snug_angle_min_deg"],
        post_snug_angle_max_deg=proc["post_snug_angle_max_deg"],
        max_sample_interval_ms=proc["max_sample_interval_ms"],
        slope_drop_limit=proc["slope_drop_limit"],
        max_outlier_rate_pct=proc["max_outlier_rate_pct"],
    )


def _transition(conn: sqlite3.Connection, pid: int, expect: str, new: str,
                time_field: str, extra: str = "", params: tuple = ()) -> dict:
    proc = _fetch_proc(conn, pid)
    if proc["status"] != expect:
        raise HTTPException(
            409, f"工艺 {pid} 当前状态 {proc['status']}（{STATUS_LABEL.get(proc['status'])}），"
                 f"须为 {expect} 才能执行此操作"
        )
    conn.execute(
        f"UPDATE procedures SET status=?, {time_field}=?{extra} WHERE id=?",
        (new, utcnow(), *params, pid),
    )
    conn.commit()
    return _fetch_proc(conn, pid)


def _progress_view(proc: dict, done: list[dict], plan: list[dict]) -> dict:
    next_step = plan[len(done)] if len(done) < len(plan) else None
    return {
        "procedure_id": proc["id"],
        "version": proc["version"],
        "status": proc["status"],
        "status_label": STATUS_LABEL.get(proc["status"]),
        "completed_steps": len(done),
        "total_steps": len(plan),
        "next_step": next_step,
    }


def _preflight(conn: sqlite3.Connection, proc: dict) -> None:
    """批准/开工前校验：交叉序列可实现，且每轮允许区间与工具量程有交集。

    登记现场约束后顺序由规划器按实际方位生成（批准时冻结），规则圆周的
    交叉序列可实现性检查不再适用；量程预检与对中门禁不变。
    """
    locked = _locked_bolts(conn, proc["id"])
    if _latest_constraints(conn, proc["id"]) is None:
        violations = sequence_violations(proc["bolt_count"], locked)
        if violations:
            pairs = "、".join(f"{a}→{b}" for a, b in violations)
            raise HTTPException(409, detail={
                "reason": "sequence_not_realizable",
                "message": f"{proc['bolt_count']} 栓法兰无法生成满足同轮非相邻规则的交叉序列"
                           f"（相邻步骤：{pairs}），拒绝推进；请调整螺栓数量或工艺规则",
                "adjacent_pairs": [list(p) for p in violations],
            })
    conflicts = find_infeasible_rounds(
        proc["target_torque"], proc["stage_ratios"], proc["tolerance_pct"],
        proc["tool_range_min"], proc["tool_range_max"])
    if conflicts:
        desc = "；".join(
            f"第 {c['round_no']} 轮允许区间 {c['allowed_interval']} N·m "
            f"与工具量程 {c['tool_range']} N·m 无交集" for c in conflicts)
        raise HTTPException(409, detail={
            "reason": "round_interval_infeasible",
            "message": f"以下轮次任何回传都无法合格：{desc}",
            "conflicts": conflicts,
        })
    alignment_gate = _alignment_gate(conn, proc)
    if alignment_gate is not None:
        raise HTTPException(409, detail=alignment_gate)


# ---------------------------------------------------------------- 装配对中预检

# 预检建版窗口：工艺参数已冻结但尚未拉拢紧固
ALIGNMENT_STATUSES = ("draft", "approved")
ALIGNMENT_FROZEN_FIELDS = (
    "flange_face_diameter_mm", "gasket_inner_diameter_mm",
    "gasket_outer_diameter_mm", "bore_diameter_mm",
    "max_parallelism_mm", "max_radial_mismatch_mm",
)


def _check_points(conn: sqlite3.Connection, check_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alignment_points WHERE check_id=? ORDER BY id", (check_id,)
    ).fetchall()
    return [{
        "angle_deg": r["angle_deg"], "axial_gap": r["axial_gap"],
        "radial_offset": r["radial_offset"],
        "gasket_edge_position": r["gasket_edge_position"],
        "bolt_free_insertion": bool(r["bolt_free_insertion"]),
        "length_unit": r["length_unit"],
    } for r in rows]


def _check_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "check_id": row["id"], "procedure_id": row["procedure_id"],
        "version": row["version"],
        "frozen": {f: row[f] for f in ALIGNMENT_FROZEN_FIELDS},
        "operator": row["operator"], "measured_at": row["measured_at"],
        "adjustment_reason": row["adjustment_reason"],
        "created_at": row["created_at"],
        "analysis": json.loads(row["analysis"]),
    }


def _list_alignment_checks(conn: sqlite3.Connection, pid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alignment_checks WHERE procedure_id=? ORDER BY version", (pid,)
    ).fetchall()
    return [_check_view(conn, r) for r in rows]


def _adopted_alignment(conn: sqlite3.Connection, pid: int) -> dict | None:
    """作业包与圆周 SVG 共用的预检版本：最新版本（旧版本永不覆盖）。"""
    checks = _list_alignment_checks(conn, pid)
    return checks[-1] if checks else None


def _alignment_gate(conn: sqlite3.Connection, proc: dict) -> dict | None:
    """批准/开工门禁：采用最新预检版本；缺失或未通过即阻断。"""
    checks = _list_alignment_checks(conn, proc["id"])
    if not checks:
        return {
            "reason": "alignment_check_missing",
            "message": f"工艺 {proc['id']} 尚无装配对中预检；须在批准前提交 >=4 个"
                       "按方位分布的测点（轴向间隙、径向偏移、垫片边缘位置、螺栓自由穿入）",
        }
    latest = checks[-1]
    analysis = latest["analysis"]
    if analysis["passed"]:
        return None
    gaps = [f'{g["reason"]}@{g["angle_deg"]}°' if g["angle_deg"] is not None
            else g["reason"] for g in analysis["evidence_gaps"]]
    blockers = [b["reason"] for b in analysis["blockers"]]
    parts: list[str] = []
    if gaps:
        parts.append("证据缺口：" + "、".join(gaps))
    if blockers:
        parts.append("阻断项：" + "、".join(blockers))
    return {
        "reason": "alignment_check_not_passed",
        "message": f"装配对中预检 v{latest['version']} 未通过；" + "；".join(parts)
                   + "。调整后须复测并注明调整原因（另存新版本，旧记录不覆盖）",
        "alignment_check_id": latest["check_id"],
        "version": latest["version"],
        "evidence_gaps": analysis["evidence_gaps"],
        "blockers": analysis["blockers"],
    }


# ---------------------------------------------------------------- 工艺生命周期

@app.post("/procedures", status_code=201)
def create_procedure(data: ProcedureCreate, conn: sqlite3.Connection = Depends(get_db)):
    """创建工艺（草稿），同时生成稳定的分轮交叉紧固计划。"""
    pid = _insert_proc(conn, data)
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    return {"procedure": proc, "plan": plan}


@app.get("/procedures")
def list_procedures(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT id, version, parent_id, status, flange_class, bolt_count, tool_id, created_at "
        "FROM procedures ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/procedures/{pid}")
def get_procedure(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    done = _done_records(conn, pid)
    return {"procedure": proc, "plan": plan,
            "plan_status": _plan_status(conn, proc),
            "progress": _progress_view(proc, done, plan)}


@app.put("/procedures/{pid}")
def update_draft(pid: int, data: ProcedureCreate, conn: sqlite3.Connection = Depends(get_db)):
    """修改草稿参数；已批准版本参数锁定，须派生新版本。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "draft":
        raise HTTPException(409, f"工艺 {pid} 已批准，参数锁定；目标或工具变化须派生新版本")
    conn.execute(
        """UPDATE procedures SET flange_class=?, bolt_count=?, gasket=?, target_torque=?,
           stage_ratios=?, tolerance_pct=?, tool_id=?, tool_range_min=?, tool_range_max=?,
           calibration_valid_until=?, start_angle_deg=?, clockwise=?,
           curve_direction=?, snug_torque=?, post_snug_angle_min_deg=?,
           post_snug_angle_max_deg=?, max_sample_interval_ms=?, slope_drop_limit=?,
           max_outlier_rate_pct=? WHERE id=?""",
        (data.flange_class, data.bolt_count, data.gasket, data.target_torque,
         json.dumps(data.stage_ratios), data.tolerance_pct, data.tool_id,
         data.tool_range_min, data.tool_range_max, data.calibration_valid_until.isoformat(),
         data.start_angle_deg, int(data.clockwise), data.curve_direction,
         data.snug_torque, data.post_snug_angle_min_deg, data.post_snug_angle_max_deg,
         data.max_sample_interval_ms, data.slope_drop_limit, data.max_outlier_rate_pct,
         pid),
    )
    conn.commit()
    return {"procedure": _fetch_proc(conn, pid)}


@app.post("/procedures/{pid}/approve")
def approve(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """批准：锁定全部参数并冻结施工计划；批准前校验序列可实现性与每轮可行区间。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "draft":
        raise HTTPException(409, f"工艺 {pid} 当前状态 {proc['status']}，须为 draft 才能批准")
    _preflight(conn, proc)
    _freeze_plan(conn, proc)  # 登记现场约束时规划器无解即 409 plan_infeasible
    return {"procedure": _transition(conn, pid, "draft", "approved", "approved_at")}


@app.post("/procedures/{pid}/start")
def start(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """开工；开工前复核序列可实现性与每轮可行区间。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "approved":
        raise HTTPException(409, f"工艺 {pid} 当前状态 {proc['status']}，须为 approved 才能开工")
    _preflight(conn, proc)
    return {"procedure": _transition(conn, pid, "approved", "in_progress", "started_at")}


@app.post("/procedures/{pid}/review")
def review(pid: int, body: ReviewRequest, conn: sqlite3.Connection = Depends(get_db)):
    """复核：全部回传完成，且终轮每栓都有可用轨迹、整圈离群率不超限才可通过。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "completed":
        raise HTTPException(
            409, f"工艺 {pid} 当前状态 {proc['status']}（{STATUS_LABEL.get(proc['status'])}），"
                 "须为 completed 才能执行此操作"
        )
    gate = _curve_review(conn, proc)
    if not gate["passed"]:
        parts: list[str] = []
        if gate["missing_bolts"]:
            parts.append(f"缺轨迹栓 {gate['missing_bolts']}")
        if gate["unusable_bolts"]:
            parts.append(f"轨迹不可用栓 {gate['unusable_bolts']}")
        if "outlier_rate_exceeded" in gate["blockers"]:
            parts.append(f"整圈离群率 {gate['outlier_rate_pct']}% 超上限 "
                         f"{gate['max_outlier_rate_pct']}%（离群栓 {gate['outlier_bolts']}）")
        raise HTTPException(409, detail={
            "reason": "curve_review_failed",
            "message": "终轮轨迹复核未通过：" + "；".join(parts),
            "blockers": gate["blockers"],
            "curve_review": gate,
        })
    proc = _transition(conn, pid, "completed", "reviewed", "reviewed_at",
                       extra=", reviewer=?, review_note=?",
                       params=(body.reviewer, body.note))
    return {"procedure": proc}


@app.post("/procedures/{pid}/archive")
def archive(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """封存：复核通过后归档，禁止任何回传。"""
    return {"procedure": _transition(conn, pid, "reviewed", "archived", "archived_at")}


@app.post("/procedures/{pid}/derive", status_code=201)
def derive(pid: int, body: DeriveRequest, conn: sqlite3.Connection = Depends(get_db)):
    """从已批准版本派生新版本（目标扭矩或工具等参数变化时）。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in DERIVABLE:
        raise HTTPException(409, f"工艺 {pid} 仍为草稿，可直接 PUT 修改，无需派生")
    base = {
        "flange_class": proc["flange_class"], "bolt_count": proc["bolt_count"],
        "gasket": proc["gasket"], "target_torque": proc["target_torque"],
        "stage_ratios": proc["stage_ratios"], "tolerance_pct": proc["tolerance_pct"],
        "tool_id": proc["tool_id"], "tool_range_min": proc["tool_range_min"],
        "tool_range_max": proc["tool_range_max"],
        "calibration_valid_until": proc["calibration_valid_until"],
        "start_angle_deg": proc["start_angle_deg"], "clockwise": proc["clockwise"],
        "curve_direction": proc["curve_direction"], "snug_torque": proc["snug_torque"],
        "post_snug_angle_min_deg": proc["post_snug_angle_min_deg"],
        "post_snug_angle_max_deg": proc["post_snug_angle_max_deg"],
        "max_sample_interval_ms": proc["max_sample_interval_ms"],
        "slope_drop_limit": proc["slope_drop_limit"],
        "max_outlier_rate_pct": proc["max_outlier_rate_pct"],
    }
    overrides = body.model_dump(exclude_none=True, exclude={"change_note"})
    base.update(overrides)
    try:
        data = ProcedureCreate(**base)
    except ValidationError as exc:
        raise HTTPException(422, detail=json.loads(exc.json()))
    new_id = _insert_proc(conn, data, version=proc["version"] + 1, parent_id=pid,
                          change_note=body.change_note)
    return {"procedure": _fetch_proc(conn, new_id), "derived_from": pid}


# ---------------------------------------------------------------- 逐栓回传

@app.post("/procedures/{pid}/reports", status_code=201)
def submit_report(pid: int, report: TorqueReport, conn: sqlite3.Connection = Depends(get_db)):
    """逐栓回传。任一规则不满足即拒绝推进、记录异常并指出涉事螺栓。"""
    proc = _fetch_proc(conn, pid)
    frozen_plan = _current_plan(conn, pid)
    proc_for_rules = {
        **proc, "locked_bolts": _locked_bolts(conn, pid),
        # 现场计划已按实际方位排定角间隔：跳过规则圆周的栓号相邻检查
        "site_plan": bool(frozen_plan and frozen_plan["constraint_revision"] is not None),
    }
    plan = _proc_plan(conn, proc)
    done = _done_records(conn, pid)

    rework_origin = None
    if report.rework_of is not None:
        row = conn.execute(
            "SELECT * FROM records WHERE id=? AND procedure_id=?",
            (report.rework_of, pid),
        ).fetchone()
        rework_origin = dict(row) if row else None

    rejection = validate_report(proc_for_rules, plan, done, report, rework_origin)
    if rejection is not None:
        conn.execute(
            "INSERT INTO anomalies (procedure_id, bolt_no, reason, message, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, rejection.bolt_no, rejection.reason, rejection.message,
             report.model_dump_json(), utcnow()),
        )
        conn.commit()
        raise HTTPException(409, detail=rejection.as_detail())

    if report.rework_of is not None:
        round_no = rework_origin["round_no"]
    else:
        round_no = plan[len(done)]["round_no"]

    cur = conn.execute(
        """INSERT INTO records
           (procedure_id, round_no, bolt_no, tool_id, operator, reported_at,
            measured_torque, rework_of, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, round_no, report.bolt_no, report.tool_id, report.operator,
         report.reported_at.isoformat(), report.measured_torque, report.rework_of, utcnow()),
    )

    new_status = proc["status"]
    if report.rework_of is None and len(done) + 1 == len(plan):
        new_status = "completed"
        conn.execute("UPDATE procedures SET status='completed', completed_at=? WHERE id=?",
                     (utcnow(), pid))
    conn.commit()

    next_step = None
    if report.rework_of is None and len(done) + 1 < len(plan):
        next_step = plan[len(done) + 1]
    return {
        "record_id": cur.lastrowid,
        "procedure_id": pid,
        "status": new_status,
        "round_no": round_no,
        "bolt_no": report.bolt_no,
        "rework_of": report.rework_of,
        "next_step": next_step,
    }


@app.get("/procedures/{pid}/resume")
def resume(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """作业中断后依据已完成位置给出恢复序列（与作业包同一冻结计划）。"""
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    done = _done_records(conn, pid)
    return {
        **_progress_view(proc, done, plan),
        "resume_sequence": plan[len(done):],
    }


# ---------------------------------------------------------------- 受限栓位施工规划

def _constraints_view(proc: dict, cons: dict) -> dict:
    """约束展开视图：每栓实际方位（登记/展开）、时间窗、允许工具与角区。"""
    ci = cons["input"]
    overrides = {b["bolt_no"]: b.get("angle_deg") for b in ci["bolts"]}
    angles = expand_angles(proc["bolt_count"], proc["start_angle_deg"],
                           bool(proc["clockwise"]), overrides)
    by_bolt = {b["bolt_no"]: b for b in ci["bolts"]}
    bolts = []
    for k in range(1, proc["bolt_count"] + 1):
        c = by_bolt.get(k) or {}
        bolts.append({
            "bolt_no": k,
            "angle_deg": angles[k],
            "angle_source": "registered" if c.get("angle_deg") is not None else "computed",
            "windows": c.get("windows") or [],
            "allowed_tools": c.get("allowed_tools") or [proc["tool_id"]],
            "clearance_deg": c.get("clearance_deg") or 0.0,
        })
    return {
        "shift_start": ci["shift_start"],
        "min_separation_deg": (ci.get("min_separation_deg")
                               or default_min_separation(proc["bolt_count"])),
        "step_minutes": ci.get("step_minutes", 5.0),
        "tool_change_minutes": ci.get("tool_change_minutes", 2.0),
        "tool_windows": ci["tool_windows"],
        "bolts": bolts,
    }


@app.put("/procedures/{pid}/constraints")
def put_constraints(pid: int, body: SiteConstraintsInput,
                    conn: sqlite3.Connection = Depends(get_db)):
    """草稿登记/替换现场约束（整组另存新版本，旧版本保留）。

    批准后约束随计划冻结；现场障碍或工具变化须 POST plan-revisions 派生修订。
    """
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "draft":
        raise HTTPException(409, detail={
            "reason": "constraints_locked",
            "message": f"工艺 {pid} 当前状态 {proc['status']}：现场约束已随批准计划冻结；"
                       "现场障碍或工具变化须从批准版派生计划修订"
                       "（POST /procedures/{id}/plan-revisions），只重排未完成步骤",
        })
    ci = json.loads(body.model_dump_json())
    bad = _constraint_bolt_errors(proc, ci)
    if bad:
        raise HTTPException(422, detail={
            "reason": "unknown_bolt",
            "message": f"栓号 {bad} 超出范围（共 {proc['bolt_count']} 栓）",
            "bolt_nos": bad,
        })
    row = conn.execute(
        "SELECT MAX(revision) r FROM site_constraints WHERE procedure_id=?",
        (pid,)).fetchone()
    rev = (row["r"] or 0) + 1
    conn.execute(
        "INSERT INTO site_constraints (procedure_id, revision, payload, created_at)"
        " VALUES (?,?,?,?)", (pid, rev, json.dumps(ci), utcnow()))
    conn.commit()
    proc = _fetch_proc(conn, pid)
    return {"procedure_id": pid, "revision": rev, "constraints": ci,
            "expanded": _constraints_view(proc, {"input": ci}),
            "preview": _plan_status(conn, proc)}


@app.get("/procedures/{pid}/constraints")
def get_constraints(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """当前现场约束与展开视图（含规划器预览可行性诊断）。"""
    proc = _fetch_proc(conn, pid)
    cons = _latest_constraints(conn, pid)
    if cons is None:
        return {"procedure_id": pid, "revision": None, "constraints": None,
                "expanded": None, "preview": _plan_status(conn, proc)}
    return {"procedure_id": pid, "revision": cons["revision"],
            "constraints": cons["input"],
            "expanded": _constraints_view(proc, cons),
            "preview": _plan_status(conn, proc)}


@app.post("/procedures/{pid}/plan-revisions", status_code=201)
def create_plan_revision(pid: int, body: PlanRevisionCreate,
                         conn: sqlite3.Connection = Depends(get_db)):
    """现场障碍或工具变化：从批准版派生计划修订，只重排未完成步骤。

    已完成步骤按原时刻/工具/轮内次序锁定；无解即 409 并给出首个冲突轮次、
    受阻栓位与最少需解除的限制，当前冻结计划不受影响。
    """
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in ("approved", "in_progress"):
        raise HTTPException(409, detail={
            "reason": "plan_revision_window",
            "message": f"工艺 {pid} 当前状态 {proc['status']}：计划修订须从批准版"
                       "（approved/in_progress）派生；草稿请直接 PUT constraints",
        })
    ci = json.loads(body.constraints.model_dump_json())
    bad = _constraint_bolt_errors(proc, ci)
    if bad:
        raise HTTPException(422, detail={
            "reason": "unknown_bolt",
            "message": f"栓号 {bad} 超出范围（共 {proc['bolt_count']} 栓）",
            "bolt_nos": bad,
        })
    frozen = _current_plan(conn, pid)
    if frozen is None:  # 兼容旧库：批准时未冻结的计划现场补冻结
        _freeze_plan(conn, proc)
        frozen = _current_plan(conn, pid)
    done = _done_records(conn, pid)
    locked_steps = frozen["steps"][:len(done)]  # 执行严格按序：已完成即计划前缀
    try:
        steps, actions = _run_planner(
            proc, ci, locked_steps=locked_steps,
            locked_bolts=_locked_bolts(conn, pid))
    except PlanInfeasible as exc:
        raise HTTPException(409, detail=exc.as_detail())

    row = conn.execute(
        "SELECT MAX(revision) r FROM site_constraints WHERE procedure_id=?",
        (pid,)).fetchone()
    cons_rev = (row["r"] or 0) + 1
    conn.execute(
        "INSERT INTO site_constraints (procedure_id, revision, payload, created_at)"
        " VALUES (?,?,?,?)", (pid, cons_rev, json.dumps(ci), utcnow()))
    plan_rev = frozen["revision"] + 1
    payload = {"steps": steps, "actions": actions,
               "meta": {"constraint_revision": cons_rev,
                        "locked_steps": len(locked_steps),
                        "generated_at": utcnow()}}
    conn.execute(
        "INSERT INTO plan_revisions"
        " (procedure_id, revision, constraint_revision, plan, change_note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (pid, plan_rev, cons_rev, json.dumps(payload), body.change_note, utcnow()))
    conn.commit()
    return {
        "procedure_id": pid,
        "revision": plan_rev,
        "constraint_revision": cons_rev,
        "change_note": body.change_note,
        "locked_steps": len(locked_steps),
        "rescheduled_steps": len(steps) - len(locked_steps),
        "steps": steps,
        "actions": actions,
        "message": f"计划修订 v{plan_rev} 已冻结：已完成 {len(locked_steps)} 步原位锁定，"
                   f"其余 {len(steps) - len(locked_steps)} 步按新现场约束重排；"
                   "恢复序列、作业包与圆周图即时切换为同一冻结计划",
    }


@app.get("/procedures/{pid}/plan-revisions")
def list_plan_revisions(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """计划修订链（批准冻结 v1 起，逐版保留）。"""
    _fetch_proc(conn, pid)
    rows = conn.execute(
        "SELECT * FROM plan_revisions WHERE procedure_id=? ORDER BY revision",
        (pid,)).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["plan"])
        out.append({
            "revision": r["revision"],
            "constraint_revision": r["constraint_revision"],
            "change_note": r["change_note"],
            "step_count": len(payload["steps"]),
            "actions": payload["actions"],
            "created_at": r["created_at"],
        })
    return {"procedure_id": pid, "plan_revisions": out}


# ---------------------------------------------------------------- 扭矩-转角轨迹

# 允许提交/修订轨迹的工艺状态（复核通过后轨迹冻结）
CURVE_STATUSES = ("in_progress", "completed")


def _fetch_curve(conn: sqlite3.Connection, cid: int) -> dict:
    row = conn.execute("SELECT * FROM torque_curves WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"轨迹 {cid} 不存在")
    return dict(row)


def _curve_views(conn: sqlite3.Connection, pid: int) -> list[dict]:
    """每栓当前采用修订的视图（曲线 id、修订号、关联记录、可用性与分析结果）。"""
    rows = conn.execute(
        """SELECT c.id AS curve_id, c.bolt_no, c.revision, c.created_at,
                  r.record_id, r.usable, r.analysis
           FROM torque_curves c
           JOIN curve_revisions r ON r.curve_id=c.id AND r.revision=c.revision
           WHERE c.procedure_id=? ORDER BY c.bolt_no""", (pid,)).fetchall()
    return [{
        "curve_id": r["curve_id"], "bolt_no": r["bolt_no"], "revision": r["revision"],
        "record_id": r["record_id"], "usable": bool(r["usable"]),
        "analysis": json.loads(r["analysis"]), "created_at": r["created_at"],
    } for r in rows]


def _curve_review(conn: sqlite3.Connection, proc: dict) -> dict:
    return evaluate_curve_review(proc, _proc_plan(conn, proc),
                                 _curve_views(conn, proc["id"]))


def _check_final_round_record(conn: sqlite3.Connection, proc: dict,
                              record_id: int) -> sqlite3.Row:
    rec = conn.execute(
        "SELECT * FROM records WHERE id=? AND procedure_id=?",
        (record_id, proc["id"])).fetchone()
    if rec is None:
        raise HTTPException(409, detail={
            "reason": "record_not_found",
            "message": f"记录 {record_id} 不存在或不属于工艺 {proc['id']}",
        })
    final_round = len(proc["stage_ratios"])
    if rec["round_no"] != final_round:
        raise HTTPException(409, detail={
            "reason": "not_final_round_record",
            "message": f"记录 {record_id} 属于第 {rec['round_no']} 轮；"
                       f"轨迹只能关联终轮（第 {final_round} 轮）的已接受记录",
        })
    return rec


def _require_curve_window(proc: dict) -> None:
    if proc["status"] not in CURVE_STATUSES:
        raise HTTPException(409, detail={
            "reason": "curve_window_closed",
            "message": f"工艺状态 {proc['status']}：轨迹仅在 in_progress/completed 阶段"
                       "可提交或修订；复核通过后轨迹冻结",
        })


def _store_revision(conn: sqlite3.Connection, curve_id: int, revision: int,
                    record_id: int, *, time_unit: str, torque_unit: str,
                    angle_unit: str, points: list[dict], snug_override: int | None,
                    amendment_note: str | None, analysis: dict) -> None:
    conn.execute(
        """INSERT INTO curve_revisions
           (curve_id, revision, record_id, time_unit, torque_unit, angle_unit,
            points, snug_override, amendment_note, analysis, usable, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (curve_id, revision, record_id, time_unit, torque_unit, angle_unit,
         json.dumps(points), snug_override, amendment_note, json.dumps(analysis),
         int(not analysis["defects"]), utcnow()),
    )


@app.post("/procedures/{pid}/curves", status_code=201)
def submit_curve(pid: int, body: CurveSubmit, conn: sqlite3.Connection = Depends(get_db)):
    """终轮已接受记录关联一条扭矩-转角轨迹；每栓一条，换曲线须走修订。"""
    proc = _fetch_proc(conn, pid)
    _require_curve_window(proc)
    rec = _check_final_round_record(conn, proc, body.record_id)
    bolt_no = rec["bolt_no"]
    exists = conn.execute(
        "SELECT id FROM torque_curves WHERE procedure_id=? AND bolt_no=?",
        (pid, bolt_no)).fetchone()
    if exists:
        raise HTTPException(409, detail={
            "reason": "curve_exists_use_amend",
            "message": f"螺栓 {bolt_no} 已有轨迹 {exists['id']}；移动贴合点或换用曲线"
                       "须走修订接口并注明原因（旧轨迹保留可查）",
            "curve_id": exists["id"],
        })
    points = [p.model_dump() for p in body.points]
    analysis = analyze_curve(proc, points, time_unit=body.time_unit,
                             torque_unit=body.torque_unit, angle_unit=body.angle_unit)
    cur = conn.execute(
        "INSERT INTO torque_curves (procedure_id, bolt_no, revision, record_id, created_at)"
        " VALUES (?,?,1,?,?)", (pid, bolt_no, body.record_id, utcnow()))
    _store_revision(conn, cur.lastrowid, 1, body.record_id,
                    time_unit=body.time_unit, torque_unit=body.torque_unit,
                    angle_unit=body.angle_unit, points=points, snug_override=None,
                    amendment_note=None, analysis=analysis)
    conn.commit()
    return {
        "curve_id": cur.lastrowid, "procedure_id": pid, "bolt_no": bolt_no,
        "record_id": body.record_id, "revision": 1,
        "usable": not analysis["defects"], "analysis": analysis,
    }


@app.get("/procedures/{pid}/curves")
def list_curves(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """逐栓当前采用修订与整圈轨迹复核结论。"""
    proc = _fetch_proc(conn, pid)
    return {"procedure_id": pid, "curves": _curve_views(conn, pid),
            "review": _curve_review(conn, proc)}


@app.get("/procedures/{pid}/curve-review")
def curve_review(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """整圈轨迹复核结论（review 门禁同款）：缺失/不可用/离群与离群率。"""
    return _curve_review(conn, _fetch_proc(conn, pid))


@app.get("/curves/{cid}")
def get_curve(cid: int, conn: sqlite3.Connection = Depends(get_db)):
    """轨迹详情：全部修订（含旧轨迹原始点列），保持可查。"""
    curve = _fetch_curve(conn, cid)
    revisions = [dict(r) for r in conn.execute(
        "SELECT * FROM curve_revisions WHERE curve_id=? ORDER BY revision",
        (cid,)).fetchall()]
    for r in revisions:
        r["points"] = json.loads(r["points"])
        r["analysis"] = json.loads(r["analysis"])
        r["usable"] = bool(r["usable"])
    return {"curve": curve, "revisions": revisions}


@app.post("/curves/{cid}/amend", status_code=201)
def amend_curve(cid: int, body: CurveAmend, conn: sqlite3.Connection = Depends(get_db)):
    """修订轨迹：人工移动贴合点或换用曲线，记录原因并另存修订，旧轨迹保留。"""
    curve = _fetch_curve(conn, cid)
    proc = _fetch_proc(conn, curve["procedure_id"])
    _require_curve_window(proc)
    prev = dict(conn.execute(
        "SELECT * FROM curve_revisions WHERE curve_id=? AND revision=?",
        (cid, curve["revision"])).fetchone())

    record_id = curve["record_id"]
    if body.record_id is not None:
        rec = _check_final_round_record(conn, proc, body.record_id)
        if rec["bolt_no"] != curve["bolt_no"]:
            raise HTTPException(409, detail={
                "reason": "record_bolt_mismatch",
                "message": f"记录 {body.record_id} 属于螺栓 {rec['bolt_no']}，"
                           f"与轨迹所在螺栓 {curve['bolt_no']} 不一致",
            })
        record_id = body.record_id

    if body.points is not None:
        points = [p.model_dump() for p in body.points]
        time_unit = body.time_unit or prev["time_unit"]
        torque_unit = body.torque_unit or prev["torque_unit"]
        angle_unit = body.angle_unit or prev["angle_unit"]
        snug_override = body.snug_index  # 换曲线后旧人工贴合点不再适用
    else:
        points = json.loads(prev["points"])
        time_unit = prev["time_unit"]
        torque_unit = prev["torque_unit"]
        angle_unit = prev["angle_unit"]
        snug_override = (body.snug_index if body.snug_index is not None
                         else prev["snug_override"])
    if snug_override is not None and not (0 <= snug_override < len(points)):
        raise HTTPException(422, detail={
            "reason": "snug_index_out_of_range",
            "message": f"人工贴合点索引 {snug_override} 超出轨迹点数 {len(points)}",
        })

    analysis = analyze_curve(proc, points, time_unit=time_unit,
                             torque_unit=torque_unit, angle_unit=angle_unit,
                             snug_override=snug_override)
    new_rev = curve["revision"] + 1
    conn.execute("UPDATE torque_curves SET revision=?, record_id=? WHERE id=?",
                 (new_rev, record_id, cid))
    _store_revision(conn, cid, new_rev, record_id, time_unit=time_unit,
                    torque_unit=torque_unit, angle_unit=angle_unit, points=points,
                    snug_override=snug_override,
                    amendment_note=f"修订：{body.reason}", analysis=analysis)
    conn.commit()
    return {
        "curve_id": cid, "procedure_id": proc["id"], "bolt_no": curve["bolt_no"],
        "record_id": record_id, "revision": new_rev,
        "usable": not analysis["defects"], "analysis": analysis,
    }


# ---------------------------------------------------------------- 装配对中预检路由

@app.post("/procedures/{pid}/alignment-checks", status_code=201)
def create_alignment_check(pid: int, body: AlignmentCheckCreate,
                           conn: sqlite3.Connection = Depends(get_db)):
    """提交装配对中预检：冻结几何与限值，拟合相对倾斜/错边/垫片对中。

    每个版本不可变；复测必须注明调整原因并另存新版本（version+1），旧记录保留。
    结论含证据缺口或阻断项时版本照常落库，但阻止工艺批准与开工。
    """
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in ALIGNMENT_STATUSES:
        raise HTTPException(409, detail={
            "reason": "alignment_window_closed",
            "message": f"工艺状态 {proc['status']}：对中预检仅在 draft/approved 阶段"
                       "（螺栓尚未受力拉拢之前）提交；开工后发现对中问题须派生新工艺",
        })
    existing = conn.execute(
        "SELECT COUNT(*) c FROM alignment_checks WHERE procedure_id=?", (pid,)
    ).fetchone()["c"]
    next_version = existing + 1
    if next_version == 1 and body.adjustment_reason is not None:
        raise HTTPException(422, detail={
            "reason": "adjustment_reason_on_first_version",
            "message": "首个预检版本无旧版本可调整，adjustment_reason 必须为空",
        })
    if next_version >= 2 and not (body.adjustment_reason or "").strip():
        raise HTTPException(422, detail={
            "reason": "adjustment_reason_required",
            "message": f"复测另存为 v{next_version}，必须注明调整原因（旧版本保留不可覆盖）",
        })

    frozen = {f: getattr(body, f) for f in ALIGNMENT_FROZEN_FIELDS}
    points = [p.model_dump() for p in body.points]
    analysis = analyze_alignment(frozen, points)

    cur = conn.execute(
        """INSERT INTO alignment_checks
           (procedure_id, version, flange_face_diameter_mm, gasket_inner_diameter_mm,
            gasket_outer_diameter_mm, bore_diameter_mm, max_parallelism_mm,
            max_radial_mismatch_mm, operator, measured_at, adjustment_reason,
            analysis, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, next_version, body.flange_face_diameter_mm, body.gasket_inner_diameter_mm,
         body.gasket_outer_diameter_mm, body.bore_diameter_mm, body.max_parallelism_mm,
         body.max_radial_mismatch_mm, body.operator, body.measured_at.isoformat(),
         body.adjustment_reason, json.dumps(analysis), utcnow()),
    )
    check_id = cur.lastrowid
    for p in body.points:
        conn.execute(
            """INSERT INTO alignment_points
               (check_id, angle_deg, axial_gap, radial_offset, gasket_edge_position,
                bolt_free_insertion, length_unit)
               VALUES (?,?,?,?,?,?,?)""",
            (check_id, p.angle_deg % 360.0, p.axial_gap, p.radial_offset,
             p.gasket_edge_position, int(p.bolt_free_insertion), p.length_unit),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM alignment_checks WHERE id=?", (check_id,)).fetchone()
    return {"alignment_check": _check_view(conn, row)}


@app.get("/procedures/{pid}/alignment-checks")
def list_alignment_checks(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """列出全部预检版本（旧版本不可覆盖，逐版保留）。"""
    _fetch_proc(conn, pid)
    return {"procedure_id": pid, "alignment_checks": _list_alignment_checks(conn, pid)}


@app.get("/alignment-checks/{cid}")
def get_alignment_check(cid: int, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM alignment_checks WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"对中预检 {cid} 不存在")
    view = _check_view(conn, row)
    view["points"] = _check_points(conn, cid)
    return view


@app.get("/alignment-checks/{cid}/diff")
def alignment_check_diff(cid: int, conn: sqlite3.Connection = Depends(get_db)):
    """与上一版本的差异：冻结几何、按方位匹配的测点与拟合指标变化。"""
    row = conn.execute("SELECT * FROM alignment_checks WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"对中预检 {cid} 不存在")
    if row["version"] <= 1:
        raise HTTPException(409, detail={
            "reason": "no_previous_alignment_version",
            "message": f"预检 v{row['version']} 为首版，无历史版本可对比",
        })
    prev = conn.execute(
        "SELECT * FROM alignment_checks WHERE procedure_id=? AND version=?",
        (row["procedure_id"], row["version"] - 1)).fetchone()
    return {
        "procedure_id": row["procedure_id"],
        "from_check_id": prev["id"], "to_check_id": cid,
        "from_version": prev["version"], "to_version": row["version"],
        "adjustment_reason": row["adjustment_reason"],
        "diff": diff_analyses(json.loads(prev["analysis"]), json.loads(row["analysis"])),
    }


# ---------------------------------------------------------------- 作业包与图示

def _revision_chain(conn: sqlite3.Connection, pid: int) -> dict:
    fields = ("id", "version", "parent_id", "status", "change_note",
              "target_torque", "tool_id", "created_at")
    def brief(proc: dict) -> dict:
        return {k: proc[k] for k in fields}

    ancestors: list[dict] = []
    node = _fetch_proc_or_none(conn, pid)
    while node is not None:
        ancestors.append(brief(node))
        node = _fetch_proc_or_none(conn, node["parent_id"])
    ancestors.reverse()
    children = [
        brief(_row_to_proc(r))
        for r in conn.execute("SELECT * FROM procedures WHERE parent_id=? ORDER BY id",
                              (pid,)).fetchall()
    ]
    return {"ancestors": ancestors, "children_of_current": children}


@app.get("/procedures/{pid}/package")
def job_package(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """JSON 作业包：计划、实测、异常与修订链。"""
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    records = _all_records(conn, pid)
    anomalies = [dict(r) for r in conn.execute(
        "SELECT * FROM anomalies WHERE procedure_id=? ORDER BY id", (pid,)).fetchall()]
    done = [r for r in records if r["rework_of"] is None]
    alignment = _adopted_alignment(conn, pid)
    frozen = _current_plan(conn, pid)
    return {
        "procedure": proc,
        "progress": _progress_view(proc, done, plan),
        "plan": plan,
        "planning": {
            "status": _plan_status(conn, proc),
            "actions": frozen["actions"] if frozen else [],
            "meta": frozen["meta"] if frozen else None,
        },
        "records": records,
        "anomalies": anomalies,
        "revisions": _revision_chain(conn, pid),
        "alignment": alignment,
        "measurement": _measurement_reference(conn, pid),
        "tensioning": _tensioning_reference(conn, pid),
        "thermal": _thermal_reference(conn, pid),
        "curves": _curve_review(conn, proc),
        "generated_at": utcnow(),
    }


@app.get("/procedures/{pid}/diagram.svg")
def diagram(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """圆周示意 SVG：实际方位、完成轮次、下一栓、异常、补拧、计划动作与轨迹标记。"""
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    records = _all_records(conn, pid)
    anomalies = [dict(r) for r in conn.execute(
        "SELECT bolt_no FROM anomalies WHERE procedure_id=?", (pid,)).fetchall()]
    done = [r for r in records if r["rework_of"] is None]
    next_step = plan[len(done)] if len(done) < len(plan) else None
    alignment = _adopted_alignment(conn, pid)
    measurement = _measurement_reference(conn, pid)
    curves = _curve_review(conn, proc)
    # 与 JSON 作业包同一冻结计划：栓位取计划实际方位，动作为计划等待/换工具
    angles = expand_angles(proc["bolt_count"], proc["start_angle_deg"],
                           bool(proc["clockwise"]))
    for s in plan:
        if s.get("angle_deg") is not None:
            angles[s["bolt_no"]] = s["angle_deg"]
    frozen = _current_plan(conn, pid)
    actions = frozen["actions"] if frozen else []
    clearances: dict[int, float] = {}
    cons = _latest_constraints(conn, pid)
    if cons is not None:
        for b in cons["input"]["bolts"]:
            if b.get("clearance_deg"):
                clearances[b["bolt_no"]] = b["clearance_deg"]
    svg = render_svg(proc, plan, records, anomalies, next_step, measurement, curves,
                     alignment, plan_angles=angles, plan_actions=actions,
                     clearances=clearances)
    return Response(content=svg, media_type="image/svg+xml")


# ---------------------------------------------------------------- 超声伸长复核

# 允许建立测量批次 / 提交基线的工艺状态
BATCH_CREATABLE = ("approved", "in_progress", "completed", "reviewed", "archived")
BASELINE_STATUSES = ("approved", "in_progress")
REMEASURE_STATUSES = ("completed", "reviewed")


def _row_to_batch(row: sqlite3.Row) -> dict:
    b = dict(row)
    for key in ("scope_bolts", "locked_bolts", "locked_results"):
        b[key] = json.loads(b[key]) if b[key] else ([] if key != "locked_results" else {})
    return b


def _fetch_batch(conn: sqlite3.Connection, bid: int) -> dict:
    row = conn.execute("SELECT * FROM measurement_batches WHERE id=?", (bid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"测量批次 {bid} 不存在")
    return _row_to_batch(row)


def _batch_evals(conn: sqlite3.Connection, batch: dict) -> tuple[list[dict], list[dict]]:
    baselines = [dict(r) for r in conn.execute(
        "SELECT * FROM measurement_baselines WHERE batch_id=? ORDER BY bolt_no",
        (batch["id"],)).fetchall()]
    readings = [dict(r) for r in conn.execute(
        "SELECT * FROM measurement_readings WHERE batch_id=? ORDER BY id",
        (batch["id"],)).fetchall()]
    return baselines, readings


def _verdict_payload(batch: dict, verdict) -> dict:
    return {
        "confirmed": verdict.confirmed,
        "blockers": verdict.blockers,
        "target_load_band_kn": list(verdict.target_band),
        "dispersion_cv_pct": verdict.dispersion_cv_pct,
        "max_deviation_pct": verdict.max_deviation_pct,
        "max_imbalance_pct": verdict.max_imbalance_pct,
        "imbalance_limit_pct": verdict.imbalance_limit_pct,
        "diametral_imbalance": verdict.diametral,
        "bolts": verdict.bolt_results,
        "evidence_gaps": [
            {"bolt_no": g["bolt_no"],
             "reasons": g["reasons"],
             "messages": [GAP_MESSAGES.get(r, r) for r in g["reasons"]]}
            for g in verdict.gaps
        ],
    }


def _batch_detail(conn: sqlite3.Connection, batch: dict) -> dict:
    proc = _fetch_proc(conn, batch["procedure_id"])
    frozen = {**batch, "bolt_count": proc["bolt_count"]}
    baselines, readings = _batch_evals(conn, batch)
    verdict = evaluate_batch(frozen, baselines, readings)
    out_band = [
        r["bolt_no"] for r in verdict.bolt_results
        if not r["gaps"] and not r["in_target_band"]
    ]
    return {
        "batch": batch,
        "procedure_id": batch["procedure_id"],
        "procedure_status": proc["status"],
        "baselines": [{k: b[k] for k in ("bolt_no", "tof_s", "created_at")} for b in baselines],
        "readings": [
            {k: (bool(r[k]) if k == "excluded" else r[k])
             for k in ("id", "bolt_no", "tof_s", "temperature_c", "operator",
                       "measured_at", "supersedes", "excluded", "amendment_note",
                       "created_at")}
            for r in readings
        ],
        "baseline_complete": len(baselines) == len(batch["scope_bolts"]),
        "verdict": _verdict_payload(batch, verdict),
        "out_of_band_bolts": out_band,
    }


def _record_gap(conn: sqlite3.Connection, batch: dict, bolt_no: int, reasons: list[str],
                payload: dict) -> None:
    for reason in reasons:
        conn.execute(
            "INSERT INTO measurement_gaps"
            " (batch_id, revision, bolt_no, reason, message, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (batch["id"], batch["revision"], bolt_no, reason,
             GAP_MESSAGES.get(reason, reason), json.dumps(payload), utcnow()),
        )


def _require_batch_open(batch: dict) -> None:
    if batch["status"] == "confirmed":
        raise HTTPException(409, detail={
            "reason": "batch_confirmed",
            "message": f"测量批次 {batch['id']} 已确认（修订 {batch['confirmed_revision']}），"
                       "只读；新测量须另建批次",
        })
    if batch["status"] == "superseded":
        raise HTTPException(409, detail={
            "reason": "batch_superseded",
            "message": f"测量批次 {batch['id']} 已派生补拧批次而废止，只读",
        })


def _measurement_reference(conn: sqlite3.Connection, pid: int) -> dict | None:
    rows = conn.execute(
        "SELECT * FROM measurement_batches WHERE procedure_id=? ORDER BY id", (pid,)
    ).fetchall()
    if not rows:
        return None
    batches = [_row_to_batch(r) for r in rows]
    confirmed = [b for b in batches if b["status"] == "confirmed"]
    if confirmed:
        b = confirmed[-1]
    else:
        open_batches = [b for b in batches if b["status"] == "open"]
        if not open_batches:
            return None
        b = open_batches[-1]
    baselines, readings = _batch_evals(conn, b)
    proc = _fetch_proc(conn, pid)
    frozen = {**b, "bolt_count": proc["bolt_count"]}
    verdict = evaluate_batch(frozen, baselines, readings)
    return {
        "batch_id": b["id"],
        "revision": b["revision"],
        "status": b["status"],
        "confirmed_revision": b["confirmed_revision"],
        "instrument_id": b["instrument_id"],
        "target_load_band_kn": [b["target_load_min_kn"], b["target_load_max_kn"]],
        "verdict": _verdict_payload(b, verdict),
    }


@app.post("/procedures/{pid}/measurement-batches", status_code=201)
def create_measurement_batch(pid: int, data: MeasurementBatchCreate,
                             conn: sqlite3.Connection = Depends(get_db)):
    """从 approved（及以后）工艺建立超声测量批次，冻结螺栓/材料/仪器参数。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in BATCH_CREATABLE:
        raise HTTPException(409, detail={
            "reason": "not_approvable_for_measurement",
            "message": f"工艺 {pid} 当前状态 {proc['status']}，须经批准（approved）后才能"
                       "建立测量批次",
        })

    # 补拧工艺：沿用上一批冻结参数与锁定螺栓；仍须显式提交全部冻结字段以核对
    rw = conn.execute(
        "SELECT * FROM rework_jobs WHERE rework_procedure_id=?", (pid,)).fetchone()
    derived_from = None
    locked_bolts: list[int] = []
    locked_results: dict = {}
    scope: list[int] = list(range(1, proc["bolt_count"] + 1))
    if rw is not None:
        source = _fetch_batch(conn, rw["source_batch_id"])
        derived_from = source["id"]
        locked_bolts = json.loads(rw["locked_bolts"])
        scope = json.loads(rw["target_bolts"])
        # 锁定栓合格结果快照：源批次整圈结论中的锁定栓（含源批次自己继承的锁定栓）
        source_bolts = _batch_detail(conn, source)["verdict"]["bolts"]
        locked_results = {
            str(b["bolt_no"]): b for b in source_bolts if b["bolt_no"] in locked_bolts
        }

    open_row = conn.execute(
        "SELECT id FROM measurement_batches WHERE procedure_id=? AND status='open'",
        (pid,)).fetchone()
    if open_row is not None:
        raise HTTPException(409, detail={
            "reason": "open_batch_exists",
            "message": f"工艺 {pid} 已有开放测量批次 {open_row['id']}；确认或废止后才能新建",
            "batch_id": open_row["id"],
        })

    cur = conn.execute(
        """INSERT INTO measurement_batches
           (procedure_id, revision, status, scope_bolts, locked_bolts, locked_results,
            length_mm, area_mm2, elastic_modulus_mpa, sound_velocity, temp_coefficient,
            reference_temp_c, temp_comp_min_c, temp_comp_max_c, target_load_min_kn,
            target_load_max_kn, material_load_limit_kn, max_imbalance_pct, instrument_id,
            instrument_calibration_until, derived_from_batch_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, 1, "open", json.dumps(scope), json.dumps(locked_bolts),
         json.dumps(locked_results) if locked_results else None,
         data.length_mm, data.area_mm2, data.elastic_modulus_mpa, data.sound_velocity,
         data.temp_coefficient, data.reference_temp_c, data.temp_comp_min_c,
         data.temp_comp_max_c, data.target_load_min_kn, data.target_load_max_kn,
         data.material_load_limit_kn, data.max_imbalance_pct, data.instrument_id,
         data.instrument_calibration_until.isoformat(), derived_from, utcnow()),
    )
    conn.commit()
    batch = _fetch_batch(conn, cur.lastrowid)
    return {"batch": batch, "frozen": _batch_detail(conn, batch)}


@app.get("/procedures/{pid}/measurement-batches")
def list_measurement_batches(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    _fetch_proc(conn, pid)
    rows = conn.execute(
        "SELECT * FROM measurement_batches WHERE procedure_id=? ORDER BY id",
        (pid,)).fetchall()
    return [_row_to_batch(r) for r in rows]


@app.get("/measurement-batches/{bid}")
def get_measurement_batch(bid: int, conn: sqlite3.Connection = Depends(get_db)):
    return _batch_detail(conn, _fetch_batch(conn, bid))


@app.post("/measurement-batches/{bid}/baselines", status_code=201)
def submit_baseline(bid: int, body: BaselineRequest,
                    conn: sqlite3.Connection = Depends(get_db)):
    """开工前逐栓提交基线飞行时间；每栓每批至多一条，重复即拒绝（不覆盖）。"""
    batch = _fetch_batch(conn, bid)
    proc = _fetch_proc(conn, batch["procedure_id"])
    _require_batch_open(batch)
    if proc["status"] not in BASELINE_STATUSES:
        raise HTTPException(409, detail={
            "reason": "baseline_window_closed",
            "message": f"工艺状态 {proc['status']}：基线仅在 approved/in_progress 阶段提交；"
                       "缺失基线的螺栓只能在复核结论中记为证据缺口",
        })
    if body.bolt_no not in batch["scope_bolts"]:
        raise HTTPException(409, detail={
            "reason": "bolt_out_of_scope",
            "message": f"螺栓 {body.bolt_no} 不在批次测量范围 {sorted(batch['scope_bolts'])}"
                       + ("（补拧锁定螺栓沿用原合格结果）" if batch["locked_bolts"] else ""),
        })
    exists = conn.execute(
        "SELECT id FROM measurement_baselines WHERE batch_id=? AND bolt_no=?",
        (bid, body.bolt_no)).fetchone()
    if exists:
        raise HTTPException(409, detail={
            "reason": "baseline_exists",
            "message": f"螺栓 {body.bolt_no} 基线已冻结（记录 {exists['id']}），"
                       "禁止覆盖；参数变更须新建批次",
        })
    cur = conn.execute(
        "INSERT INTO measurement_baselines (batch_id, bolt_no, tof_s, created_at)"
        " VALUES (?,?,?,?)",
        (bid, body.bolt_no, body.tof_s, utcnow()))
    conn.commit()
    return {
        "baseline_id": cur.lastrowid, "batch_id": bid, "bolt_no": body.bolt_no,
        "tof_s": body.tof_s,
        "baselines_received": conn.execute(
            "SELECT COUNT(*) c FROM measurement_baselines WHERE batch_id=?",
            (bid,)).fetchone()["c"],
        "baselines_required": len(batch["scope_bolts"]),
    }


def _submit_reading(conn: sqlite3.Connection, batch: dict, *, bolt_no: int, tof_s: float,
                    temperature_c: float, operator: str, measured_at: datetime,
                    supersedes: int | None, amendment_note: str | None,
                    payload: dict) -> dict:
    _require_batch_open(batch)
    if bolt_no not in batch["scope_bolts"]:
        raise HTTPException(409, detail={
            "reason": "bolt_out_of_scope",
            "message": f"螺栓 {bolt_no} 不在批次测量范围 {sorted(batch['scope_bolts'])}"
                       + ("（补拧锁定螺栓沿用原合格结果）" if batch["locked_bolts"] else ""),
        })
    cur = conn.execute(
        """INSERT INTO measurement_readings
           (batch_id, bolt_no, tof_s, temperature_c, operator, measured_at,
            supersedes, excluded, amendment_note, created_at)
           VALUES (?,?,?,?,?,?,?,0,?,?)""",
        (batch["id"], bolt_no, tof_s, temperature_c, operator,
         measured_at.isoformat(), supersedes, amendment_note, utcnow()),
    )
    reading = dict(conn.execute(
        "SELECT * FROM measurement_readings WHERE id=?", (cur.lastrowid,)).fetchone())
    baselines, readings = _batch_evals(conn, batch)
    frozen = {**batch, "bolt_count": _fetch_proc(conn, batch["procedure_id"])["bolt_count"]}
    result = evaluate_reading(frozen,
                              next((b for b in baselines if b["bolt_no"] == bolt_no), None),
                              reading)
    if result["gaps"]:
        _record_gap(conn, batch, bolt_no, result["gaps"], payload)
    conn.commit()
    return {"reading_id": cur.lastrowid, "batch_id": batch["id"], "revision": batch["revision"],
            "bolt_result": result,
            "valid": not result["gaps"],
            "evidence_gap": [GAP_MESSAGES[g] for g in result["gaps"]] or None}


@app.post("/measurement-batches/{bid}/readings", status_code=201)
def submit_reading(bid: int, body: RemeasurementRequest,
                   conn: sqlite3.Connection = Depends(get_db)):
    """completed/reviewed 后逐栓提交复测读数；证据缺口照记（201），绝不判合格。"""
    batch = _fetch_batch(conn, bid)
    proc = _fetch_proc(conn, batch["procedure_id"])
    if proc["status"] not in REMEASURE_STATUSES:
        raise HTTPException(409, detail={
            "reason": "not_ready_for_remeasurement",
            "message": f"工艺状态 {proc['status']}：复测须在 completed/reviewed 后提交",
        })
    existing = conn.execute(
        "SELECT id FROM measurement_readings WHERE batch_id=? AND bolt_no=? ORDER BY id DESC",
        (bid, body.bolt_no)).fetchone()
    if existing is not None:
        raise HTTPException(409, detail={
            "reason": "reading_exists_use_retest",
            "message": f"螺栓 {body.bolt_no} 已有读数 {existing['id']}；重新测量须走重测接口"
                       "并注明理由（原值保留，批次修订号 +1）",
            "reading_id": existing["id"],
        })
    return _submit_reading(
        conn, batch, bolt_no=body.bolt_no, tof_s=body.tof_s,
        temperature_c=body.temperature_c, operator=body.operator,
        measured_at=body.measured_at, supersedes=None, amendment_note=None,
        payload=body.model_dump(mode="json"))


@app.post("/measurement-batches/{bid}/retests", status_code=201)
def retest_reading(bid: int, body: RetestRequest,
                   conn: sqlite3.Connection = Depends(get_db)):
    """重测：原读数保留（supersedes 指向），须注明理由，批次修订号 +1。"""
    batch = _fetch_batch(conn, bid)
    _require_batch_open(batch)
    if body.bolt_no not in batch["scope_bolts"]:
        raise HTTPException(409, detail={
            "reason": "bolt_out_of_scope",
            "message": f"螺栓 {body.bolt_no} 不在批次测量范围 {sorted(batch['scope_bolts'])}",
        })
    prev = conn.execute(
        "SELECT * FROM measurement_readings WHERE batch_id=? AND bolt_no=? ORDER BY id DESC",
        (bid, body.bolt_no)).fetchone()
    if prev is None:
        raise HTTPException(409, detail={
            "reason": "no_reading_to_retest",
            "message": f"螺栓 {body.bolt_no} 尚无复测读数，不能重测（请先提交复测）",
        })
    conn.execute("UPDATE measurement_batches SET revision=revision+1 WHERE id=?", (bid,))
    conn.commit()
    batch = _fetch_batch(conn, bid)
    return _submit_reading(
        conn, batch, bolt_no=body.bolt_no, tof_s=body.tof_s,
        temperature_c=body.temperature_c, operator=body.operator,
        measured_at=body.measured_at, supersedes=prev["id"],
        amendment_note=f"重测：{body.reason}", payload=body.model_dump(mode="json"))


@app.post("/measurement-batches/{bid}/exclusions", status_code=201)
def exclude_reading(bid: int, body: ExcludeRequest,
                    conn: sqlite3.Connection = Depends(get_db)):
    """排除读数：注明理由，原值保留不删除，修订号 +1，须重测补证后才能确认。"""
    batch = _fetch_batch(conn, bid)
    _require_batch_open(batch)
    if body.bolt_no not in batch["scope_bolts"]:
        raise HTTPException(409, detail={
            "reason": "bolt_out_of_scope",
            "message": f"螺栓 {body.bolt_no} 不在批次测量范围 {sorted(batch['scope_bolts'])}",
        })
    latest = conn.execute(
        "SELECT * FROM measurement_readings WHERE batch_id=? AND bolt_no=? ORDER BY id DESC",
        (bid, body.bolt_no)).fetchone()
    if latest is None:
        raise HTTPException(409, detail={
            "reason": "no_reading_to_exclude",
            "message": f"螺栓 {body.bolt_no} 无读数可排除",
        })
    if latest["excluded"]:
        raise HTTPException(409, detail={
            "reason": "reading_already_excluded",
            "message": f"螺栓 {body.bolt_no} 最新读数 {latest['id']} 已排除；请直接重测",
        })
    conn.execute(
        "UPDATE measurement_readings SET excluded=1, amendment_note=? WHERE id=?",
        (f"排除：{body.reason}", latest["id"]))
    conn.execute("UPDATE measurement_batches SET revision=revision+1 WHERE id=?", (bid,))
    _record_gap(conn, batch, body.bolt_no, ["reading_excluded"],
                {"reading_id": latest["id"], "reason": body.reason})
    conn.commit()
    return {"reading_id": latest["id"], "batch_id": bid,
            "revision": _fetch_batch(conn, bid)["revision"], "bolt_no": body.bolt_no,
            "excluded": True, "reason": body.reason,
            "message": "读数已排除并保留原值；该栓须重测取得有效结果"}


@app.post("/measurement-batches/{bid}/confirm")
def confirm_batch(bid: int, conn: sqlite3.Connection = Depends(get_db)):
    """确认批次：全部螺栓有效、落入预紧力目标带、对径不平衡达标。

    不满足则只记录证据缺口，批次保持 open，返回 blockers 与逐栓明细。
    """
    batch = _fetch_batch(conn, bid)
    _require_batch_open(batch)
    detail = _batch_detail(conn, batch)
    verdict = detail["verdict"]

    # 证据缺口留痕（确认动作本身产生一版完整缺口快照，去重同一修订+理由）
    for gap in verdict["evidence_gaps"]:
        for reason in gap["reasons"]:
            exists = conn.execute(
                "SELECT 1 FROM measurement_gaps WHERE batch_id=? AND revision=? AND bolt_no=?"
                " AND reason=?", (bid, batch["revision"], gap["bolt_no"], reason)).fetchone()
            if not exists:
                _record_gap(conn, batch, gap["bolt_no"], [reason],
                            {"stage": "confirm_attempt"})

    if not verdict["confirmed"]:
        conn.commit()
        raise HTTPException(409, detail={
            "reason": "batch_not_confirmed",
            "message": "存在证据缺口或限值超标，不能确认批次；可对失败批次派生补拧草稿",
            "blockers": verdict["blockers"],
            "verdict": verdict,
        })

    conn.execute(
        "UPDATE measurement_batches SET status='confirmed', confirmed_revision=revision,"
        " confirmed_at=? WHERE id=?", (utcnow(), bid))
    conn.commit()
    return {"batch_id": bid, "status": "confirmed",
            "confirmed_revision": batch["revision"], "verdict": verdict}


@app.post("/measurement-batches/{bid}/derive-rework", status_code=201)
def derive_rework(bid: int, conn: sqlite3.Connection = Depends(get_db)):
    """失败批次派生补拧草稿：锁定合格螺栓，其余栓按现有交叉规则安排末轮补拧。"""
    batch = _fetch_batch(conn, bid)
    if batch["status"] != "open":
        raise HTTPException(409, detail={
            "reason": f"batch_{batch['status']}",
            "message": "仅开放（未确认）批次可派生补拧",
        })
    detail = _batch_detail(conn, batch)
    verdict = detail["verdict"]
    bolts = verdict["bolts"]
    scope_set = set(batch["scope_bolts"])
    inherited_locked = set(batch["locked_bolts"])

    # 1) 证据缺口 / 超出目标预紧力带的范围螺栓必须补拧；
    #    继承的锁定合格螺栓始终保留。
    target_reasons: dict[int, list[str]] = {}
    for b in bolts:
        bolt = b["bolt_no"]
        if bolt not in scope_set:
            continue
        if b["gaps"]:
            target_reasons[bolt] = ["evidence_gap"]
        elif not b["in_target_band"]:
            target_reasons[bolt] = ["load_out_of_target_band"]
    good = (scope_set - set(target_reasons)) | inherited_locked

    # 2) 对径不平衡超限：补拧只能增大预紧力，故应补拧该对中载荷较小的
    #    一栓向对侧靠拢；该栓须可作业（在补拧范围、非继承锁定）。
    unaddressable: list[dict] = []
    for d in verdict["diametral_imbalance"]:
        imb = d["imbalance_pct"]
        if imb is None or imb <= batch["max_imbalance_pct"]:
            continue
        fa, fb = d["load_a_kn"], d["load_b_kn"]
        if fa is None or fb is None:
            continue
        pick = d["bolt_a"] if fa <= fb else d["bolt_b"]
        if pick not in scope_set or pick in inherited_locked:
            # 较小载荷栓已锁定/不在范围：补拧另一栓只会加剧不平衡，
            # 扭矩补拧不可修正，须松退重紧或解除锁定，显式拒绝。
            unaddressable.append({"bolt_a": d["bolt_a"], "bolt_b": d["bolt_b"],
                                  "load_a_kn": fa, "load_b_kn": fb,
                                  "imbalance_pct": imb})
            continue
        target_reasons.setdefault(pick, []).append(
            f"diametral_imbalance:{d['bolt_a']}/{d['bolt_b']}={imb}%"
            f">{batch['max_imbalance_pct']}%")
        good.discard(pick)

    targets = [x for x in batch["scope_bolts"] if x in target_reasons]
    good = sorted(good)
    if not targets and unaddressable:
        raise HTTPException(409, detail={
            "reason": "rework_uncorrectable_imbalance",
            "message": "存在对径不平衡，但较低载荷栓已锁定合格（补拧只能增大载荷，"
                       "拧紧对侧会加剧不平衡）；请松退重紧相关螺栓或调整锁定集合后再派生",
            "pairs": unaddressable,
        })
    if not targets:
        raise HTTPException(409, detail={
            "reason": "nothing_to_rework",
            "message": "所有螺栓均合格，无需补拧；可直接确认批次",
        })

    proc = _fetch_proc(conn, batch["procedure_id"])
    locked_set = set(good)
    violations = sequence_violations(proc["bolt_count"], locked_set)
    if violations:
        pairs = "、".join(f"{a}→{b}" for a, b in violations)
        raise HTTPException(409, detail={
            "reason": "rework_sequence_not_realizable",
            "message": "锁定合格螺栓后剩余螺栓按交叉规则会出现相邻连续步骤"
                       f"（{pairs}），无法生成补拧序列",
            "adjacent_pairs": [list(p) for p in violations],
        })

    data = _proc_as_create(proc, stage_ratios=[1.0])
    new_pid = _insert_proc(
        conn, data, version=proc["version"] + 1, parent_id=proc["id"],
        change_note=f"超声批次 {bid} 复核失败派生补拧；锁定 {len(good)} 栓，"
                    f"补拧 {len(targets)} 栓（末轮）")
    locked_snapshot = {
        str(b["bolt_no"]): b for b in bolts if b["bolt_no"] in good
    }
    conn.execute(
        "INSERT INTO rework_jobs"
        " (source_batch_id, source_procedure_id, rework_procedure_id,"
        "  locked_bolts, target_bolts, created_at) VALUES (?,?,?,?,?,?)",
        (bid, proc["id"], new_pid, json.dumps(good), json.dumps(targets), utcnow()))
    conn.execute("UPDATE measurement_batches SET status='superseded' WHERE id=?", (bid,))
    conn.commit()

    new_proc = _fetch_proc(conn, new_pid)
    return {
        "rework_procedure": new_proc,
        "derived_from_procedure": proc["id"],
        "source_batch_id": bid,
        "locked_bolts": good,
        "target_bolts": targets,
        "rework_reasons": {str(k): v for k, v in target_reasons.items()},
        "locked_results": locked_snapshot,
        "plan": _proc_plan(conn, new_proc),
        "message": "补拧草稿已生成（末轮 100%）；批准、开工、回传后按同一冻结参数"
                   "建立新测量批次（仅补拧范围需基线/复测，锁定栓结果自动继承）",
    }


# ---------------------------------------------------------------- 液压张拉执行

# 允许建立张拉方案的工艺状态（与超声批次一致：工艺参数已冻结）
TENSION_CREATABLE = ("approved", "in_progress", "completed", "reviewed", "archived")
# 允许回传/修订的方案状态
TENSION_ACTIVE = ("open", "approved")

TENSION_FROZEN_FIELDS = (
    "area_mm2", "length_mm", "elastic_modulus_mpa", "target_load_kn",
    "load_tolerance_pct", "tensioner_id", "tensioner_count", "hydraulic_area_mm2",
    "max_pressure_mpa", "max_stroke_mm", "min_tool_spacing",
    "load_transfer_coefficient", "min_hold_seconds", "pressure_sync_tolerance_pct",
    "gauge_id", "gauge_calibration_until",
)


def _row_to_tension_plan(row: sqlite3.Row) -> dict:
    plan = dict(row)
    plan["stage_ratios"] = json.loads(plan["stage_ratios"])
    plan["scheme_rounds"] = json.loads(plan.pop("scheme"))
    snap = plan.get("ultrasonic_snapshot")
    plan["ultrasonic_snapshot"] = json.loads(snap) if snap else None
    return plan


def _fetch_tension_plan(conn: sqlite3.Connection, tid: int) -> dict:
    row = conn.execute("SELECT * FROM tensioning_plans WHERE id=?", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"张拉方案 {tid} 不存在")
    return _row_to_tension_plan(row)


def _current_tension_plan(conn: sqlite3.Connection, pid: int) -> dict | None:
    """当前活动修订（最新未废止修订）；批准快照/版本差异/作业包共用同一行。"""
    row = conn.execute(
        "SELECT * FROM tensioning_plans WHERE procedure_id=? AND status!='superseded'"
        " ORDER BY revision DESC", (pid,)).fetchone()
    return _row_to_tension_plan(row) if row else None


def _tension_lineage_ids(conn: sqlite3.Connection, plan: dict) -> list[int]:
    """当前修订所在谱系（沿 parent_id 上溯）的全部方案 id。"""
    ids = [plan["id"]]
    parent = plan["parent_id"]
    while parent is not None:
        ids.append(parent)
        row = conn.execute("SELECT parent_id FROM tensioning_plans WHERE id=?",
                           (parent,)).fetchone()
        parent = row["parent_id"] if row else None
    return ids


def _tension_reports(conn: sqlite3.Connection, plan: dict) -> list[dict]:
    """当前谱系全部已接受回传（跨修订，按 id 升序）；新谱系从空开始。"""
    ids = _tension_lineage_ids(conn, plan)
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM tensioning_reports WHERE plan_id IN ({marks}) ORDER BY id",
        ids).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["release_order"] = json.loads(d["release_order"])
        out.append(d)
    return out


def _tension_channels(conn: sqlite3.Connection, report_ids: list[int]) -> dict[int, list[dict]]:
    if not report_ids:
        return {}
    marks = ",".join("?" for _ in report_ids)
    rows = conn.execute(
        f"SELECT * FROM tensioning_channels WHERE report_id IN ({marks})"
        " ORDER BY id", report_ids).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["report_id"], []).append(
            {k: d[k] for k in ("bolt_no", "pressure_mpa", "stroke_mm",
                               "applied_load_kn", "residual_load_kn")})
    return out


def _done_tension_groups(reports: list[dict]) -> list[dict]:
    return [{"round_no": r["round_no"], "group_no": r["group_no"]} for r in reports]


def _tension_detail(conn: sqlite3.Connection, plan: dict) -> dict:
    """方案详情：冻结参数、分轮换位方案（含逐组状态）、回传与评估结论。

    方案详情路由、版本差异与 JSON 作业包统一读取本视图（同一张拉方案与结果）。
    """
    proc = _fetch_proc(conn, plan["procedure_id"])
    reports = _tension_reports(conn, plan)
    channels = _tension_channels(conn, [r["id"] for r in reports])
    evaluation = evaluate_plan(plan, plan["scheme_rounds"], reports, channels)
    status_by_key = {(g["round_no"], g["group_no"]): g for g in evaluation["groups"]}
    scheme = []
    for rd in scheme_setpoints(plan, plan["scheme_rounds"]):
        groups = []
        for g in rd["groups"]:
            key = (rd["round_no"], g["group_no"])
            view = status_by_key.get(key, {})
            groups.append({**g, "status": view.get("status", "pending"),
                           "report_id": view.get("report_id")})
        scheme.append({**rd, "groups": groups})
    chain = conn.execute(
        "SELECT id, revision, parent_id, status, change_note, approved_at, created_at"
        " FROM tensioning_plans WHERE procedure_id=? ORDER BY revision",
        (plan["procedure_id"],)).fetchall()
    return {
        "plan": {**{k: plan[k] for k in TENSION_FROZEN_FIELDS},
                 "stage_ratios": plan["stage_ratios"],
                 "id": plan["id"], "procedure_id": plan["procedure_id"],
                 "revision": plan["revision"], "parent_id": plan["parent_id"],
                 "status": plan["status"],
                 "ultrasonic_batch_id": plan["ultrasonic_batch_id"],
                 "change_note": plan["change_note"],
                 "approved_at": plan["approved_at"],
                 "confirmed_at": plan["confirmed_at"],
                 "created_at": plan["created_at"]},
        "procedure_status": proc["status"],
        "scheme": scheme,
        "reports": [{**r, "channels": channels.get(r["id"], [])} for r in reports],
        "evaluation": evaluation,
        "revision_chain": [dict(r) for r in chain],
    }


def _tensioning_reference(conn: sqlite3.Connection, pid: int) -> dict | None:
    plan = _current_tension_plan(conn, pid)
    return _tension_detail(conn, plan) if plan else None


def _tension_feasibility_gate(plan: dict) -> None:
    """创建/批准/修订前预检：逐轮设定泵压不超能力、预测行程不超限。"""
    conflicts = find_infeasible_tension_rounds(plan)
    if conflicts:
        desc = "；".join(
            f"比例 {c['round_ratio']} 轮需 {c['required_pressure_mpa']}MPa/"
            f"{c['required_stroke_mm']}mm，超上限 {c['max_pressure_mpa']}MPa/"
            f"{c['max_stroke_mm']}mm" for c in conflicts)
        raise HTTPException(409, detail={
            "reason": "tensioning_infeasible",
            "message": f"以下轮次任何回传都无法合格：{desc}",
            "conflicts": conflicts,
        })


def _insert_tension_plan(conn: sqlite3.Connection, pid: int, params: dict, *,
                         revision: int, parent_id: int | None, status: str,
                         scheme_rounds: list[dict], change_note: str | None,
                         ultrasonic_batch_id: int | None = None,
                         ultrasonic_snapshot: dict | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO tensioning_plans
           (procedure_id, revision, parent_id, status, area_mm2, length_mm,
            elastic_modulus_mpa, target_load_kn, load_tolerance_pct, tensioner_id,
            tensioner_count, hydraulic_area_mm2, max_pressure_mpa, max_stroke_mm,
            min_tool_spacing, load_transfer_coefficient, min_hold_seconds,
            pressure_sync_tolerance_pct, gauge_id, gauge_calibration_until,
            stage_ratios, scheme, ultrasonic_batch_id, ultrasonic_snapshot,
            change_note, approved_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, revision, parent_id, status, params["area_mm2"], params["length_mm"],
         params["elastic_modulus_mpa"], params["target_load_kn"],
         params["load_tolerance_pct"], params["tensioner_id"],
         params["tensioner_count"], params["hydraulic_area_mm2"],
         params["max_pressure_mpa"], params["max_stroke_mm"],
         params["min_tool_spacing"], params["load_transfer_coefficient"],
         params["min_hold_seconds"], params["pressure_sync_tolerance_pct"],
         params["gauge_id"], params["gauge_calibration_until"],
         json.dumps(params["stage_ratios"]), json.dumps(scheme_rounds),
         ultrasonic_batch_id,
         json.dumps(ultrasonic_snapshot) if ultrasonic_snapshot else None,
         change_note, utcnow() if status == "approved" else None, utcnow()),
    )
    return cur.lastrowid


def _derive_tension_revision(conn: sqlite3.Connection, plan: dict, *,
                             reason: str, overrides: dict,
                             ultrasonic_batch_id: int | None = None,
                             ultrasonic_snapshot: dict | None = None) -> dict:
    """派生新修订：已完成组原位锁定，仅未完成组按新参数重排；旧修订废止。"""
    params = {**{k: plan[k] for k in TENSION_FROZEN_FIELDS},
              "stage_ratios": plan["stage_ratios"]}
    params.update(overrides)
    try:
        data = TensioningPlanCreate(**params)
    except ValidationError as exc:
        raise HTTPException(422, detail=json.loads(exc.json()))
    new_params = data.model_dump()
    new_params["gauge_calibration_until"] = data.gauge_calibration_until.isoformat()
    _tension_feasibility_gate(new_params)

    proc = _fetch_proc(conn, plan["procedure_id"])
    reports = _tension_reports(conn, plan)
    done_keys = {(r["round_no"], r["group_no"]) for r in reports}
    locked_groups = [
        {"round_no": rd["round_no"], "group_no": g["group_no"], "bolts": g["bolts"]}
        for rd in plan["scheme_rounds"] for g in rd["groups"]
        if (rd["round_no"], g["group_no"]) in done_keys
    ]
    rounds = build_scheme(proc["bolt_count"], new_params["stage_ratios"],
                          new_params["tensioner_count"],
                          new_params["min_tool_spacing"], locked_groups=locked_groups)
    new_status = plan["status"]  # open→open / approved→approved（批准快照随修订更新）
    new_id = _insert_tension_plan(
        conn, plan["procedure_id"], new_params, revision=plan["revision"] + 1,
        parent_id=plan["id"], status=new_status, scheme_rounds=rounds,
        change_note=reason, ultrasonic_batch_id=ultrasonic_batch_id,
        ultrasonic_snapshot=ultrasonic_snapshot)
    conn.execute("UPDATE tensioning_plans SET status='superseded' WHERE id=?",
                 (plan["id"],))
    conn.commit()
    return _fetch_tension_plan(conn, new_id)


@app.post("/procedures/{pid}/tensioning-plans", status_code=201)
def create_tensioning_plan(pid: int, data: TensioningPlanCreate,
                           conn: sqlite3.Connection = Depends(get_db)):
    """建立液压张拉方案：冻结截面/目标预紧力/拉伸器能力与行程/压力表校准/
    栓组与载荷转移系数，并生成分轮换位方案（每轮全覆盖、组内同步、逐轮换位）。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in TENSION_CREATABLE:
        raise HTTPException(409, detail={
            "reason": "not_approvable_for_tensioning",
            "message": f"工艺 {pid} 当前状态 {proc['status']}，须经批准（approved）后才能"
                       "建立张拉方案",
        })
    existing = _current_tension_plan(conn, pid)
    if existing is not None and existing["status"] in TENSION_ACTIVE:
        raise HTTPException(409, detail={
            "reason": "active_plan_exists",
            "message": f"工艺 {pid} 已有活动张拉方案 {existing['id']}"
                       f"（修订 {existing['revision']}，状态 {existing['status']}）；"
                       "改组或参数变化须派生修订",
            "plan_id": existing["id"],
        })
    params = data.model_dump()
    params["gauge_calibration_until"] = data.gauge_calibration_until.isoformat()
    _tension_feasibility_gate(params)
    rounds = build_scheme(proc["bolt_count"], data.stage_ratios, data.tensioner_count,
                          data.min_tool_spacing)
    row = conn.execute(
        "SELECT MAX(revision) r FROM tensioning_plans WHERE procedure_id=?",
        (pid,)).fetchone()
    new_id = _insert_tension_plan(conn, pid, params, revision=(row["r"] or 0) + 1,
                                  parent_id=None, status="open",
                                  scheme_rounds=rounds, change_note=None)
    conn.commit()
    return _tension_detail(conn, _fetch_tension_plan(conn, new_id))


@app.get("/procedures/{pid}/tensioning-plans")
def list_tensioning_plans(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """张拉方案修订链（逐版保留，旧修订不覆盖）。"""
    _fetch_proc(conn, pid)
    rows = conn.execute(
        "SELECT id, revision, parent_id, status, change_note, approved_at,"
        " confirmed_at, created_at FROM tensioning_plans WHERE procedure_id=?"
        " ORDER BY revision", (pid,)).fetchall()
    return {"procedure_id": pid, "tensioning_plans": [dict(r) for r in rows]}


@app.get("/tensioning-plans/{tid}")
def get_tensioning_plan(tid: int, conn: sqlite3.Connection = Depends(get_db)):
    """方案详情：冻结参数、分轮换位方案、逐组回传结果与确认评估。"""
    return _tension_detail(conn, _fetch_tension_plan(conn, tid))


@app.post("/tensioning-plans/{tid}/approve")
def approve_tensioning_plan(tid: int, conn: sqlite3.Connection = Depends(get_db)):
    """批准张拉方案：冻结批准快照（修订内容不可变），批准前复核逐轮可行性。"""
    plan = _fetch_tension_plan(conn, tid)
    if plan["status"] != "open":
        raise HTTPException(409, detail={
            "reason": "plan_not_open",
            "message": f"张拉方案 {tid} 当前状态 {plan['status']}，须为 open 才能批准",
        })
    _tension_feasibility_gate(plan)
    conn.execute("UPDATE tensioning_plans SET status='approved', approved_at=?"
                 " WHERE id=?", (utcnow(), tid))
    conn.commit()
    return _tension_detail(conn, _fetch_tension_plan(conn, tid))


@app.post("/tensioning-plans/{tid}/round-reports", status_code=201)
def submit_tension_round_report(tid: int, report: TensioningRoundReport,
                                conn: sqlite3.Connection = Depends(get_db)):
    """分组回传：各通道压力/行程、保压时段与卸压次序。

    机具超行程、压力不同步、覆盖冲突、校准失效、保压不足或残余预紧力超差时，
    定位栓号与原始区间、记录异常并拒绝推进（该组须整改后重新回传）。
    """
    plan = _fetch_tension_plan(conn, tid)
    proc = _fetch_proc(conn, plan["procedure_id"])
    reports = _tension_reports(conn, plan)
    done = _done_tension_groups(reports)
    rejection = validate_round_report(plan, plan["scheme_rounds"], done, report,
                                      proc["status"])
    if rejection is not None:
        conn.execute(
            "INSERT INTO anomalies (procedure_id, bolt_no, reason, message, payload,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (proc["id"], rejection.bolt_no, rejection.reason, rejection.message,
             report.model_dump_json(), utcnow()))
        conn.commit()
        raise HTTPException(409, detail=rejection.as_detail())

    cur = conn.execute(
        """INSERT INTO tensioning_reports
           (plan_id, plan_revision, round_no, group_no, operator, reported_at,
            gauge_id, hold_seconds, release_order, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (tid, plan["revision"], report.round_no, report.group_no, report.operator,
         report.reported_at.isoformat(), report.gauge_id, report.hold_seconds,
         json.dumps(report.release_order), utcnow()))
    report_id = cur.lastrowid
    results = channel_results(plan, report)
    for ch in results:
        conn.execute(
            """INSERT INTO tensioning_channels
               (report_id, bolt_no, pressure_mpa, stroke_mm, applied_load_kn,
                residual_load_kn) VALUES (?,?,?,?,?,?)""",
            (report_id, ch["bolt_no"], ch["pressure_mpa"], ch["stroke_mm"],
             ch["applied_load_kn"], ch["residual_load_kn"]))
    conn.commit()

    sequence = flatten_groups(plan["scheme_rounds"])
    next_group = (sequence[len(done) + 1] if len(done) + 1 < len(sequence) else None)
    return {
        "report_id": report_id,
        "plan_id": tid,
        "plan_revision": plan["revision"],
        "round_no": report.round_no,
        "group_no": report.group_no,
        "channels": results,
        "completed_groups": len(done) + 1,
        "total_groups": len(sequence),
        "next_group": next_group,
    }


@app.post("/tensioning-plans/{tid}/confirm")
def confirm_tensioning_plan(tid: int, conn: sqlite3.Connection = Depends(get_db)):
    """确认张拉方案：全部组回传完成且末轮逐栓残余预紧力落入目标带。

    存在覆盖缺口或残余预紧力超差时返回 409 与逐栓明细，方案保持 approved。
    """
    plan = _fetch_tension_plan(conn, tid)
    if plan["status"] != "approved":
        raise HTTPException(409, detail={
            "reason": "plan_not_approved",
            "message": f"张拉方案 {tid} 当前状态 {plan['status']}，须为 approved 才能确认",
        })
    detail = _tension_detail(conn, plan)
    evaluation = detail["evaluation"]
    if not evaluation["confirmable"]:
        parts: list[str] = []
        if evaluation["missing_groups"]:
            miss = "、".join(f"R{g['round_no']}G{g['group_no']}"
                             for g in evaluation["missing_groups"])
            parts.append(f"未回传组：{miss}")
        if evaluation["out_of_band_bolts"]:
            bolts = [b["bolt_no"] for b in evaluation["out_of_band_bolts"]]
            parts.append(f"残余预紧力超差栓 {bolts}（目标带 "
                         f"{evaluation['final_residual_band_kn']}kN，证据来源 "
                         f"{evaluation['evidence_source']}）")
        raise HTTPException(409, detail={
            "reason": "tensioning_not_confirmed",
            "message": "；".join(parts) + "；不能确认",
            "blockers": evaluation["blockers"],
            "evaluation": evaluation,
        })
    conn.execute("UPDATE tensioning_plans SET status='confirmed', confirmed_at=?"
                 " WHERE id=?", (utcnow(), tid))
    conn.commit()
    return _tension_detail(conn, _fetch_tension_plan(conn, tid))


@app.post("/tensioning-plans/{tid}/revisions", status_code=201)
def create_tensioning_revision(tid: int, body: TensioningRevisionCreate,
                               conn: sqlite3.Connection = Depends(get_db)):
    """人工改组/参数修订：必须说明理由；已完成组原位锁定，只重排未完成组。"""
    plan = _fetch_tension_plan(conn, tid)
    if plan["status"] not in TENSION_ACTIVE:
        raise HTTPException(409, detail={
            "reason": f"plan_{plan['status']}",
            "message": f"张拉方案 {tid} 当前状态 {plan['status']}，不能派生修订",
        })
    overrides = body.model_dump(exclude_none=True, exclude={"reason"})
    if not overrides:
        raise HTTPException(409, detail={
            "reason": "empty_revision",
            "message": "空修订：未变更任何冻结参数；人工改组请调整栓组/间隔/轮次等"
                       "字段，采用超声实测值请用 adopt-ultrasonic",
        })
    new_plan = _derive_tension_revision(conn, plan, reason=body.reason,
                                        overrides=overrides)
    return {
        "tensioning_plan": _tension_detail(conn, new_plan),
        "derived_from": tid,
        "diff": diff_schemes(plan, plan["scheme_rounds"],
                             new_plan, new_plan["scheme_rounds"]),
    }


@app.post("/tensioning-plans/{tid}/adopt-ultrasonic", status_code=201)
def adopt_ultrasonic(tid: int, body: TensioningAdoptUltrasonic,
                     conn: sqlite3.Connection = Depends(get_db)):
    """采用既有超声实测值作为残余预紧力证据：必须说明理由并派生修订。

    快照取自本工艺已确认测量批次的逐栓换算载荷；确认时以实测值替代预测值。
    """
    plan = _fetch_tension_plan(conn, tid)
    if plan["status"] not in TENSION_ACTIVE:
        raise HTTPException(409, detail={
            "reason": f"plan_{plan['status']}",
            "message": f"张拉方案 {tid} 当前状态 {plan['status']}，不能派生修订",
        })
    pid = plan["procedure_id"]
    if body.batch_id is not None:
        batch = _fetch_batch(conn, body.batch_id)
        if batch["procedure_id"] != pid:
            raise HTTPException(409, detail={
                "reason": "batch_procedure_mismatch",
                "message": f"测量批次 {body.batch_id} 属于工艺 {batch['procedure_id']}，"
                           f"与本方案工艺 {pid} 不符",
            })
        if batch["status"] != "confirmed":
            raise HTTPException(409, detail={
                "reason": "batch_not_confirmed",
                "message": f"测量批次 {body.batch_id} 状态 {batch['status']}，"
                           "仅已确认批次的实测值可被采用",
            })
    else:
        row = conn.execute(
            "SELECT id FROM measurement_batches WHERE procedure_id=?"
            " AND status='confirmed' ORDER BY id DESC", (pid,)).fetchone()
        if row is None:
            raise HTTPException(409, detail={
                "reason": "no_confirmed_batch",
                "message": f"工艺 {pid} 无已确认超声测量批次，无法采用实测值",
            })
        batch = _fetch_batch(conn, row["id"])
    verdict = _batch_detail(conn, batch)["verdict"]
    snapshot = {
        str(b["bolt_no"]): {"load_kn": b["load_kn"], "reading_id": b["reading_id"]}
        for b in verdict["bolts"] if b["load_kn"] is not None and not b["gaps"]
    }
    new_plan = _derive_tension_revision(
        conn, plan, reason=f"采用超声实测：{body.reason}", overrides={},
        ultrasonic_batch_id=batch["id"], ultrasonic_snapshot=snapshot)
    return {
        "tensioning_plan": _tension_detail(conn, new_plan),
        "derived_from": tid,
        "adopted_batch_id": batch["id"],
        "adopted_loads_kn": {k: v["load_kn"] for k, v in snapshot.items()},
        "diff": diff_schemes(plan, plan["scheme_rounds"],
                             new_plan, new_plan["scheme_rounds"]),
    }


@app.get("/tensioning-plans/{tid}/diff")
def tensioning_plan_diff(tid: int, conn: sqlite3.Connection = Depends(get_db)):
    """与上一修订的差异：冻结参数、逐轮分组（锁定组原位保留）与超声采纳变化。"""
    plan = _fetch_tension_plan(conn, tid)
    if plan["parent_id"] is None:
        raise HTTPException(409, detail={
            "reason": "no_previous_revision",
            "message": f"张拉方案修订 {plan['revision']} 为首版，无历史修订可对比",
        })
    prev = _fetch_tension_plan(conn, plan["parent_id"])
    return {
        "procedure_id": plan["procedure_id"],
        "from_plan_id": prev["id"], "to_plan_id": tid,
        "from_revision": prev["revision"], "to_revision": plan["revision"],
        "diff": diff_schemes(prev, prev["scheme_rounds"],
                             plan, plan["scheme_rounds"]),
    }


# ---------------------------------------------------------------- 热态预紧力校核

# 工艺参数已冻结后才能建热态工况（与超声批次/张拉方案一致）
THERMAL_CREATABLE = ("approved", "in_progress", "completed", "reviewed", "archived")
THERMAL_ACTIVE = ("open",)


def _thermal_payload(data) -> dict:
    payload = json.loads(data.model_dump_json())
    # normalize 需要字符串时刻排序（ISO 8601 可字典序），保留原始声明单位
    return payload


def _initial_loads_from_ultrasonic(conn: sqlite3.Connection, batch: dict) -> dict:
    """已确认超声批次的逐栓有效换算载荷（证据缺口栓不进快照）。"""
    baselines, readings = _batch_evals(conn, batch)
    proc = _fetch_proc(conn, batch["procedure_id"])
    verdict = evaluate_batch({**batch, "bolt_count": proc["bolt_count"]},
                             baselines, readings)
    return {str(b["bolt_no"]): b["load_kn"]
            for b in verdict.bolt_results
            if not b["gaps"] and b["load_kn"] is not None}


def _initial_loads_from_tensioning(conn: sqlite3.Connection, plan: dict) -> dict:
    """已确认张拉方案评估视图中的逐栓末轮残余预紧力（含采纳超声的实测值）。"""
    detail = _tension_detail(conn, plan)
    return {str(b["bolt_no"]): b["residual_load_kn"]
            for b in detail["evaluation"]["bolt_results"]
            if b["residual_load_kn"] is not None}


def _resolve_thermal_source(conn: sqlite3.Connection, pid: int, source_type: str,
                            source_id: int | None) -> tuple[int, dict, dict, str | None]:
    """解析初始载荷来源并冻结逐栓初载快照。

    返回 (source_id, source_row, initial_loads, gate_reason)：
    来源不存在/跨工艺/未确认时 gate_reason 给出 409 原因；已确认但逐栓初载
    缺失（证据缺口）不是建案门禁，缺载栓在评估中记 initial_load_missing。
    """
    if source_type == "ultrasonic":
        if source_id is None:
            row = conn.execute(
                "SELECT * FROM measurement_batches WHERE procedure_id=?"
                " AND status='confirmed' ORDER BY id DESC", (pid,)).fetchone()
            if row is None:
                return None, None, {}, "no_confirmed_batch"
            batch = _row_to_batch(row)
            return batch["id"], batch, _initial_loads_from_ultrasonic(conn, batch), None
        row = conn.execute("SELECT * FROM measurement_batches WHERE id=?",
                           (source_id,)).fetchone()
        if row is None:
            return None, None, {}, "source_not_found"
        batch = _row_to_batch(row)
        if batch["procedure_id"] != pid:
            return None, None, {}, "source_procedure_mismatch"
        if batch["status"] != "confirmed":
            return None, None, {}, "source_not_confirmed"
        return batch["id"], batch, _initial_loads_from_ultrasonic(conn, batch), None

    if source_id is None:
        row = conn.execute(
            "SELECT * FROM tensioning_plans WHERE procedure_id=?"
            " AND status='confirmed' ORDER BY id DESC", (pid,)).fetchone()
        if row is None:
            return None, None, {}, "no_confirmed_plan"
        plan = _row_to_tension_plan(row)
        return plan["id"], plan, _initial_loads_from_tensioning(conn, plan), None
    row = conn.execute("SELECT * FROM tensioning_plans WHERE id=?",
                       (source_id,)).fetchone()
    if row is None:
        return None, None, {}, "source_not_found"
    plan = _row_to_tension_plan(row)
    if plan["procedure_id"] != pid:
        return None, None, {}, "source_procedure_mismatch"
    if plan["status"] != "confirmed":
        return None, None, {}, "source_not_confirmed"
    return plan["id"], plan, _initial_loads_from_tensioning(conn, plan), None


def _row_to_thermal_case(row: sqlite3.Row) -> dict:
    case = dict(row)
    case["payload"] = json.loads(case["payload"])
    case["frozen"] = json.loads(case["frozen"])
    case["initial_loads"] = json.loads(case["initial_loads"])
    case["result"] = json.loads(case["result"])
    return case


def _fetch_thermal_case(conn: sqlite3.Connection, cid: int) -> dict:
    row = conn.execute("SELECT * FROM thermal_cases WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"热态工况 {cid} 不存在")
    return _row_to_thermal_case(row)


def _current_thermal_case(conn: sqlite3.Connection, pid: int) -> dict | None:
    """当前活动修订（最新未废止）；作业包与详情共用同一行。"""
    row = conn.execute(
        "SELECT * FROM thermal_cases WHERE procedure_id=? AND status!='superseded'"
        " ORDER BY revision DESC", (pid,)).fetchone()
    return _row_to_thermal_case(row) if row else None


def _record_thermal_gaps(conn: sqlite3.Connection, case: dict, bolt_count: int) -> None:
    """评估缺口逐条落 thermal_gaps（栓号 + 时间区间），与结果视图同源。"""
    for b in case["result"]["bolts"]:
        for g in b["gaps"]:
            conn.execute(
                "INSERT INTO thermal_gaps"
                " (case_id, revision, bolt_no, reason, message, interval, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (case["id"], case["revision"], b["bolt_no"], g["reason"],
                 g["message"], json.dumps(g["interval"]) if g.get("interval") else None,
                 utcnow()))


def _insert_thermal_case(conn: sqlite3.Connection, pid: int, revision: int,
                         parent_id: int | None, payload: dict, *,
                         change_note: str | None) -> dict:
    proc = _fetch_proc(conn, pid)
    if len(payload["bolt_zones"]) != proc["bolt_count"]:
        raise HTTPException(422, detail={
            "reason": "bolt_zone_count_mismatch",
            "message": f"bolt_zones 长度 {len(payload['bolt_zones'])} 与螺栓数 "
                       f"{proc['bolt_count']} 不一致（须按栓号 1..N 逐栓分配分区）",
        })
    source_id, source, initial_loads, gate = _resolve_thermal_source(
        conn, pid, payload["source_type"], payload["source_id"])
    if gate is not None:
        labels = {
            "no_confirmed_batch": "工艺无已确认超声批次，逐栓初始载荷未经确认",
            "no_confirmed_plan": "工艺无已确认张拉方案，逐栓初始载荷未经确认",
            "source_not_found": "指定的初始载荷来源不存在",
            "source_procedure_mismatch": "初始载荷来源不属于本工艺",
            "source_not_confirmed": "初始载荷来源尚未确认，不能作为热态校核初载",
        }
        raise HTTPException(409, detail={
            "reason": "initial_load_unconfirmed",
            "message": labels[gate],
            "source_type": payload["source_type"],
            "source_id": payload["source_id"],
        })
    frozen = normalize_case({**payload, "source_id": source_id})
    result = evaluate_case(frozen, initial_loads, proc["bolt_count"])
    cur = conn.execute(
        """INSERT INTO thermal_cases
           (procedure_id, revision, parent_id, status, source_type, source_id,
            payload, frozen, initial_loads, result, change_note, created_at)
           VALUES (?,?,?,'open',?,?,?,?,?,?,?,?)""",
        (pid, revision, parent_id, payload["source_type"], source_id,
         json.dumps(payload), json.dumps(frozen), json.dumps(initial_loads),
         json.dumps(result), change_note, utcnow()))
    case = _fetch_thermal_case(conn, cur.lastrowid)
    _record_thermal_gaps(conn, case, proc["bolt_count"])
    conn.commit()
    return _fetch_thermal_case(conn, cur.lastrowid)


def _thermal_frozen_view(frozen: dict) -> dict:
    """冻结参数回显（内部单位 mm/mm²/MPa，附声明单位）。"""
    def part(p):
        return {k: p[k] for k in ("name", "length_mm", "area_mm2", "modulus_mpa",
                                  "cte", "prop_min_c", "prop_max_c", "length_unit",
                                  "area_unit", "modulus_unit")}

    g = frozen["gasket"]
    return {
        "bolt": part(frozen["bolt"]),
        "members": [part(m) for m in frozen["members"]],
        "gasket": {
            "name": g["name"],
            "effective_area_mm2": g["effective_area_mm2"],
            "thickness_mm": g["thickness_mm"],
            "cte": g["cte"],
            "prop_min_c": g["prop_min_c"],
            "prop_max_c": g["prop_max_c"],
            "area_unit": g["area_unit"], "length_unit": g["length_unit"],
            "compression_rebound_curve": [
                {"compression_mm": x, "loading_mpa": pc, "rebound_mpa": pr}
                for x, pc, pr in zip(g["curve"]["compression_mm"],
                                     g["curve"]["loading_mpa"],
                                     g["curve"]["rebound_mpa"])],
        },
        "bolt_zones": frozen["bolt_zones"],
        "reference_temperatures_c": frozen["reference"],
        "limits": frozen["limits"],
        "max_interval_seconds": frozen["max_interval_seconds"],
        "temperature_nodes": [{"at": n["at"], "temperatures": n["zones"]}
                              for n in frozen["nodes"]],
    }


def _thermal_detail(conn: sqlite3.Connection, case: dict) -> dict:
    """工况详情：冻结参数、逐栓初始载荷、逐时结果、人工决定与缺口（修订查询共用）。"""
    gaps = [dict(r) for r in conn.execute(
        "SELECT bolt_no, reason, message, interval FROM thermal_gaps"
        " WHERE case_id=? ORDER BY id", (case["id"],)).fetchall()]
    for g in gaps:
        g["interval"] = json.loads(g["interval"]) if g["interval"] else None
    return {
        "case": {
            "id": case["id"], "procedure_id": case["procedure_id"],
            "revision": case["revision"], "parent_id": case["parent_id"],
            "status": case["status"], "source_type": case["source_type"],
            "source_id": case["source_id"], "change_note": case["change_note"],
            "decided_by": case["decided_by"], "decision_note": case["decision_note"],
            "confirmed_at": case["confirmed_at"], "created_at": case["created_at"],
        },
        "submitted": case["payload"],
        "frozen": _thermal_frozen_view(case["frozen"]),
        "initial_loads_kn": {int(k): v for k, v in case["initial_loads"].items()},
        "result": case["result"],
        "gaps": gaps,
    }


def _thermal_reference(conn: sqlite3.Connection, pid: int) -> dict | None:
    case = _current_thermal_case(conn, pid)
    return _thermal_detail(conn, case) if case else None


@app.post("/procedures/{pid}/thermal-cases", status_code=201)
def create_thermal_case(pid: int, data: ThermalCaseCreate,
                        conn: sqlite3.Connection = Depends(get_db)):
    """建立热态预紧力校核工况：冻结部件/垫片曲线/限值/分区温度时序，逐栓初载
    只从已确认超声批次或已确认张拉方案读取。

    单位冲突、温度断档、材料/垫片曲线覆盖不足或初载缺失时版本照常落库（201），
    结果列出栓号与对应区间，但 evaluable/confirmable=false，禁止确认。
    """
    proc = _fetch_proc(conn, pid)
    if proc["status"] not in THERMAL_CREATABLE:
        raise HTTPException(409, detail={
            "reason": "not_approvable_for_thermal",
            "message": f"工艺 {pid} 当前状态 {proc['status']}，须经批准（approved）后才能"
                       "建立热态工况",
        })
    active = _current_thermal_case(conn, pid)
    if active is not None and active["status"] in THERMAL_ACTIVE:
        raise HTTPException(409, detail={
            "reason": "active_thermal_case_exists",
            "message": f"工艺 {pid} 已有开放热态工况 {active['id']}（修订 "
                       f"{active['revision']}）；替代边界/曲线须派生修订",
            "case_id": active["id"],
        })
    payload = _thermal_payload(data)
    row = conn.execute("SELECT MAX(revision) r FROM thermal_cases WHERE procedure_id=?",
                       (pid,)).fetchone()
    case = _insert_thermal_case(conn, pid, (row["r"] or 0) + 1, None, payload,
                                change_note=None)
    return _thermal_detail(conn, case)


@app.get("/procedures/{pid}/thermal-cases")
def list_thermal_cases(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """热态工况修订链（逐版保留，旧修订不覆盖）。"""
    _fetch_proc(conn, pid)
    rows = conn.execute(
        "SELECT id, revision, parent_id, status, source_type, source_id, change_note,"
        " decided_by, decision_note, confirmed_at, created_at FROM thermal_cases"
        " WHERE procedure_id=? ORDER BY revision", (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        result = json.loads(conn.execute(
            "SELECT result FROM thermal_cases WHERE id=?", (d["id"],)).fetchone()["result"])
        d["confirmable"] = result["confirmable"]
        d["blockers"] = result["blockers"]
        out.append(d)
    return {"procedure_id": pid, "thermal_cases": out}


@app.get("/thermal-cases/{cid}")
def get_thermal_case(cid: int, conn: sqlite3.Connection = Depends(get_db)):
    """工况详情：所选版本的冻结参数、逐时结果与人工决定。"""
    return _thermal_detail(conn, _fetch_thermal_case(conn, cid))


@app.post("/thermal-cases/{cid}/revisions", status_code=201)
def create_thermal_revision(cid: int, body: ThermalRevisionCreate,
                            conn: sqlite3.Connection = Depends(get_db)):
    """人工采用替代边界或材料曲线：必须写明理由，派生修订（旧修订废止不覆盖）。

    仅开放工况可修订；空修订（无任何冻结内容变更）拒绝。
    """
    case = _fetch_thermal_case(conn, cid)
    if case["status"] != "open":
        raise HTTPException(409, detail={
            "reason": f"thermal_case_{case['status']}",
            "message": f"热态工况 {cid} 当前状态 {case['status']}，不能派生修订",
        })
    overrides = json.loads(
        body.model_dump_json(exclude_none=True, exclude={"reason"}))
    if not overrides:
        raise HTTPException(409, detail={
            "reason": "empty_revision",
            "message": "空修订：未变更任何冻结参数；替代边界/曲线须给出具体变更"
                       "（部件、垫片曲线、限值、分区温度时序或初载来源）",
        })
    payload = {
        "source_type": overrides.get("source_type", case["payload"]["source_type"]),
        "source_id": overrides.get("source_id", case["payload"].get("source_id")),
        "bolt": overrides.get("bolt", case["payload"]["bolt"]),
        "members": overrides.get("members", case["payload"]["members"]),
        "gasket": overrides.get("gasket", case["payload"]["gasket"]),
        "bolt_zones": overrides.get("bolt_zones", case["payload"]["bolt_zones"]),
        "reference": overrides.get("reference", case["payload"]["reference"]),
        "limits": overrides.get("limits", case["payload"]["limits"]),
        "nodes": overrides.get("nodes", case["payload"]["nodes"]),
        "notes": overrides.get("notes", case["payload"].get("notes")),
    }
    # 新初载来源 / 变更结构需通过与建案相同的 Pydantic 校验
    try:
        ThermalCaseCreate(**payload)
    except ValidationError as exc:
        raise HTTPException(422, detail=json.loads(exc.json()))
    new_case = _insert_thermal_case(
        conn, case["procedure_id"], case["revision"] + 1, cid, payload,
        change_note=body.reason)
    conn.execute("UPDATE thermal_cases SET status='superseded' WHERE id=?", (cid,))
    conn.commit()
    new_case = _fetch_thermal_case(conn, new_case["id"])
    return {
        "thermal_case": _thermal_detail(conn, new_case),
        "derived_from": cid,
        "diff": diff_cases(
            {"frozen": case["frozen"], "source_type": case["source_type"],
             "source_id": case["source_id"], "initial_loads": case["initial_loads"]},
            {"frozen": new_case["frozen"], "source_type": new_case["source_type"],
             "source_id": new_case["source_id"],
             "initial_loads": new_case["initial_loads"],
             "change_note": body.reason}),
    }


@app.post("/thermal-cases/{cid}/confirm")
def confirm_thermal_case(cid: int, body: ReviewRequest | None = None,
                         conn: sqlite3.Connection = Depends(get_db)):
    """批准热态校核结果：无证据缺口且无接触分离/压溃/螺栓超载/密封裕量不足。

    不满足则 409 返回 blockers 与逐栓/区间明细，工况保持开放。
    """
    case = _fetch_thermal_case(conn, cid)
    if case["status"] != "open":
        raise HTTPException(409, detail={
            "reason": f"thermal_case_{case['status']}",
            "message": f"热态工况 {cid} 当前状态 {case['status']}，仅开放工况可确认",
        })
    result = case["result"]
    if not result["confirmable"]:
        raise HTTPException(409, detail={
            "reason": "thermal_case_not_confirmed",
            "message": "存在证据缺口或接触分离/压溃/螺栓超载/密封裕量不足，"
                       "不能确认；采用替代边界或曲线须写明理由派生修订",
            "blockers": result["blockers"],
            "gap_reasons": result["gap_reasons"],
            "violation_reasons": result["violation_reasons"],
            "bolts": [{"bolt_no": b["bolt_no"],
                       "first_violation_at": b["first_violation_at"],
                       "first_violation_reason": b["first_violation_reason"],
                       "gaps": [{"reason": g["reason"], "interval": g["interval"]}
                                for g in b["gaps"]]}
                      for b in result["bolts"]
                      if b["gaps"] or b["first_violation_reason"]],
        })
    conn.execute(
        "UPDATE thermal_cases SET status='confirmed', confirmed_at=?, decided_by=?,"
        " decision_note=? WHERE id=?",
        (utcnow(), body.reviewer if body else None,
         body.note if body else None, cid))
    conn.commit()
    return _thermal_detail(conn, _fetch_thermal_case(conn, cid))


@app.get("/thermal-cases/{cid}/diff")
def thermal_case_diff(cid: int, conn: sqlite3.Connection = Depends(get_db)):
    """与上一修订的差异：冻结参数、温度时序、初载来源与逐栓初载变化。"""
    case = _fetch_thermal_case(conn, cid)
    if case["parent_id"] is None:
        raise HTTPException(409, detail={
            "reason": "no_previous_revision",
            "message": f"热态工况修订 {case['revision']} 为首版，无历史修订可对比",
        })
    prev = _fetch_thermal_case(conn, case["parent_id"])
    return {
        "procedure_id": case["procedure_id"],
        "from_case_id": prev["id"], "to_case_id": cid,
        "from_revision": prev["revision"], "to_revision": case["revision"],
        "diff": diff_cases(
            {"frozen": prev["frozen"], "source_type": prev["source_type"],
             "source_id": prev["source_id"], "initial_loads": prev["initial_loads"]},
            {"frozen": case["frozen"], "source_type": case["source_type"],
             "source_id": case["source_id"], "initial_loads": case["initial_loads"],
             "change_note": case["change_note"]}),
    }
