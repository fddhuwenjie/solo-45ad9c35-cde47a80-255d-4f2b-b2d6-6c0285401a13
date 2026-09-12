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
from .rules import find_infeasible_rounds, validate_report
from .schemas import (AlignmentCheckCreate, BaselineRequest, CurveAmend, CurveSubmit,
                      DeriveRequest, ExcludeRequest, MeasurementBatchCreate,
                      RemeasurementRequest, RetestRequest, ProcedureCreate, ReviewRequest,
                      TorqueReport)
from .sequencing import build_plan, sequence_violations
from .svg import render_svg
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


def _proc_plan(conn: sqlite3.Connection, proc: dict) -> list[dict]:
    return build_plan(proc["bolt_count"], proc["target_torque"],
                      proc["stage_ratios"], _locked_bolts(conn, proc["id"]))


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
    """批准/开工前校验：交叉序列可实现，且每轮允许区间与工具量程有交集。"""
    locked = _locked_bolts(conn, proc["id"])
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
    return {"procedure": proc, "plan": plan, "progress": _progress_view(proc, done, plan)}


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
    """批准：锁定全部参数；批准前校验序列可实现性与每轮可行区间。"""
    proc = _fetch_proc(conn, pid)
    if proc["status"] != "draft":
        raise HTTPException(409, f"工艺 {pid} 当前状态 {proc['status']}，须为 draft 才能批准")
    _preflight(conn, proc)
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
    proc_for_rules = {**proc, "locked_bolts": _locked_bolts(conn, pid)}
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
    """作业中断后依据已完成位置给出恢复序列。"""
    proc = _fetch_proc(conn, pid)
    plan = _proc_plan(conn, proc)
    done = _done_records(conn, pid)
    return {
        **_progress_view(proc, done, plan),
        "resume_sequence": plan[len(done):],
    }


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
    return {
        "procedure": proc,
        "progress": _progress_view(proc, done, plan),
        "plan": plan,
        "records": records,
        "anomalies": anomalies,
        "revisions": _revision_chain(conn, pid),
        "alignment": alignment,
        "measurement": _measurement_reference(conn, pid),
        "curves": _curve_review(conn, proc),
        "generated_at": utcnow(),
    }


@app.get("/procedures/{pid}/diagram.svg")
def diagram(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """圆周示意 SVG：方位、完成轮次、下一栓、异常、补拧与轨迹复核标记。"""
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
    svg = render_svg(proc, plan, records, anomalies, next_step, measurement, curves,
                     alignment)
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
