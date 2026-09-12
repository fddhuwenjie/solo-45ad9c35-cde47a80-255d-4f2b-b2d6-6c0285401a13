"""法兰螺栓紧固工艺管理服务：创建 -> 批准 -> 开工 -> 逐栓回传 -> 复核 -> 封存。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import ValidationError

from .db import get_conn, init_db, utcnow
from .rules import validate_report
from .schemas import DeriveRequest, ProcedureCreate, ReviewRequest, TorqueReport
from .sequencing import build_plan
from .svg import render_svg

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


def _insert_proc(conn: sqlite3.Connection, data: ProcedureCreate, *,
                 version: int = 1, parent_id: int | None = None,
                 change_note: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO procedures
           (version, parent_id, change_note, status, flange_class, bolt_count, gasket,
            target_torque, stage_ratios, tolerance_pct, tool_id, tool_range_min,
            tool_range_max, calibration_valid_until, start_angle_deg, clockwise, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version, parent_id, change_note, "draft", data.flange_class, data.bolt_count,
            data.gasket, data.target_torque, json.dumps(data.stage_ratios),
            data.tolerance_pct, data.tool_id, data.tool_range_min, data.tool_range_max,
            data.calibration_valid_until.isoformat(), data.start_angle_deg,
            int(data.clockwise), utcnow(),
        ),
    )
    conn.commit()
    return cur.lastrowid


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


# ---------------------------------------------------------------- 工艺生命周期

@app.post("/procedures", status_code=201)
def create_procedure(data: ProcedureCreate, conn: sqlite3.Connection = Depends(get_db)):
    """创建工艺（草稿），同时生成稳定的分轮交叉紧固计划。"""
    pid = _insert_proc(conn, data)
    proc = _fetch_proc(conn, pid)
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
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
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
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
           calibration_valid_until=?, start_angle_deg=?, clockwise=? WHERE id=?""",
        (data.flange_class, data.bolt_count, data.gasket, data.target_torque,
         json.dumps(data.stage_ratios), data.tolerance_pct, data.tool_id,
         data.tool_range_min, data.tool_range_max, data.calibration_valid_until.isoformat(),
         data.start_angle_deg, int(data.clockwise), pid),
    )
    conn.commit()
    return {"procedure": _fetch_proc(conn, pid)}


@app.post("/procedures/{pid}/approve")
def approve(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """批准：锁定全部参数。"""
    return {"procedure": _transition(conn, pid, "draft", "approved", "approved_at")}


@app.post("/procedures/{pid}/start")
def start(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """开工。"""
    return {"procedure": _transition(conn, pid, "approved", "in_progress", "started_at")}


@app.post("/procedures/{pid}/review")
def review(pid: int, body: ReviewRequest, conn: sqlite3.Connection = Depends(get_db)):
    """复核：全部回传完成后进行。"""
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
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
    done = _done_records(conn, pid)

    rework_origin = None
    if report.rework_of is not None:
        row = conn.execute(
            "SELECT * FROM records WHERE id=? AND procedure_id=?",
            (report.rework_of, pid),
        ).fetchone()
        rework_origin = dict(row) if row else None

    rejection = validate_report(proc, plan, done, report, rework_origin)
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
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
    done = _done_records(conn, pid)
    return {
        **_progress_view(proc, done, plan),
        "resume_sequence": plan[len(done):],
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
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
    records = _all_records(conn, pid)
    anomalies = [dict(r) for r in conn.execute(
        "SELECT * FROM anomalies WHERE procedure_id=? ORDER BY id", (pid,)).fetchall()]
    done = [r for r in records if r["rework_of"] is None]
    return {
        "procedure": proc,
        "progress": _progress_view(proc, done, plan),
        "plan": plan,
        "records": records,
        "anomalies": anomalies,
        "revisions": _revision_chain(conn, pid),
        "generated_at": utcnow(),
    }


@app.get("/procedures/{pid}/diagram.svg")
def diagram(pid: int, conn: sqlite3.Connection = Depends(get_db)):
    """圆周示意 SVG：方位、完成轮次、下一栓、异常与补拧标记。"""
    proc = _fetch_proc(conn, pid)
    plan = build_plan(proc["bolt_count"], proc["target_torque"], proc["stage_ratios"])
    records = _all_records(conn, pid)
    anomalies = [dict(r) for r in conn.execute(
        "SELECT bolt_no FROM anomalies WHERE procedure_id=?", (pid,)).fetchall()]
    done = [r for r in records if r["rework_of"] is None]
    next_step = plan[len(done)] if len(done) < len(plan) else None
    svg = render_svg(proc, plan, records, anomalies, next_step)
    return Response(content=svg, media_type="image/svg+xml")
