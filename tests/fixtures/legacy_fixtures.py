"""冻结旧库夹具：在临时路径重建 user_version=0 的历史结构并填入业务数据。

冻结的二进制快照在 legacy_fixtures/ 下（*_v0.db），由 ``build_fixtures.py``
用本模块重新生成。夹具覆盖服务演进中的代表性历史版本：

- ``legacy_v1``：仅 procedures/records/anomalies（最初发布结构，无轨迹复核列）；
- ``legacy_ultrasonic_v2``：v1 + 超声批次/基线/读数/缺口/补拧草稿；
- ``legacy_curve_v3``：v2 + procedures 补列 + 扭矩-转角曲线表；
- ``legacy_full_v7``：旧 ``init_db()`` 一次 CREATE TABLE IF NOT EXISTS + 临时
  补列建成的全表库（user_version 仍为 0，升级前的现网形态）。

每个夹具都写入工艺、测量、张拉与热态（按该版本是否存在）业务行，升级后
必须原样可读，用于验证“升级不改写业务数据”。
"""
from __future__ import annotations

import sqlite3

# 最初发布（v1）的三表 DDL（无轨迹复核列），冻结自项目首个 db.py。
V1_SCHEMA = """
CREATE TABLE procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    parent_id INTEGER REFERENCES procedures(id),
    change_note TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    flange_class TEXT NOT NULL,
    bolt_count INTEGER NOT NULL,
    gasket TEXT NOT NULL,
    target_torque REAL NOT NULL,
    stage_ratios TEXT NOT NULL,
    tolerance_pct REAL NOT NULL,
    tool_id TEXT NOT NULL,
    tool_range_min REAL NOT NULL,
    tool_range_max REAL NOT NULL,
    calibration_valid_until TEXT NOT NULL,
    start_angle_deg REAL NOT NULL DEFAULT 0,
    clockwise INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    reviewed_at TEXT,
    archived_at TEXT,
    reviewer TEXT,
    review_note TEXT
);
CREATE TABLE records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    round_no INTEGER NOT NULL,
    bolt_no INTEGER NOT NULL,
    tool_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    measured_torque REAL NOT NULL,
    rework_of INTEGER REFERENCES records(id),
    created_at TEXT NOT NULL
);
CREATE TABLE anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
"""

# 各阶段增量 DDL，与 app/migrations/versions/*.sql 同源，冻结在此以防迁移文件
# 变更后无法重建“旧库”。这些片段用于搭建升级前快照，不参与产品运行时迁移。
V2_SCHEMA = """
CREATE TABLE measurement_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    scope_bolts TEXT NOT NULL,
    locked_bolts TEXT NOT NULL DEFAULT '[]',
    locked_results TEXT,
    length_mm REAL NOT NULL, area_mm2 REAL NOT NULL, elastic_modulus_mpa REAL NOT NULL,
    sound_velocity REAL NOT NULL, temp_coefficient REAL NOT NULL, reference_temp_c REAL NOT NULL,
    temp_comp_min_c REAL NOT NULL, temp_comp_max_c REAL NOT NULL,
    target_load_min_kn REAL NOT NULL, target_load_max_kn REAL NOT NULL,
    material_load_limit_kn REAL NOT NULL, max_imbalance_pct REAL NOT NULL,
    instrument_id TEXT NOT NULL, instrument_calibration_until TEXT NOT NULL,
    derived_from_batch_id INTEGER REFERENCES measurement_batches(id),
    confirmed_revision INTEGER, confirmed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE measurement_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    bolt_no INTEGER NOT NULL, tof_s REAL NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (batch_id, bolt_no)
);
CREATE TABLE measurement_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    bolt_no INTEGER NOT NULL, tof_s REAL NOT NULL, temperature_c REAL NOT NULL,
    operator TEXT NOT NULL, measured_at TEXT NOT NULL,
    supersedes INTEGER REFERENCES measurement_readings(id),
    excluded INTEGER NOT NULL DEFAULT 0, amendment_note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE measurement_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    revision INTEGER NOT NULL, bolt_no INTEGER NOT NULL, reason TEXT NOT NULL,
    message TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL
);
CREATE TABLE rework_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    source_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    rework_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    locked_bolts TEXT NOT NULL, target_bolts TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

V3_ALTER = """
ALTER TABLE procedures ADD COLUMN curve_direction TEXT NOT NULL DEFAULT 'cw';
ALTER TABLE procedures ADD COLUMN snug_torque REAL NOT NULL DEFAULT 0;
ALTER TABLE procedures ADD COLUMN post_snug_angle_min_deg REAL NOT NULL DEFAULT 0;
ALTER TABLE procedures ADD COLUMN post_snug_angle_max_deg REAL NOT NULL DEFAULT 100000;
ALTER TABLE procedures ADD COLUMN max_sample_interval_ms REAL NOT NULL DEFAULT 1e15;
ALTER TABLE procedures ADD COLUMN slope_drop_limit REAL NOT NULL DEFAULT 1e15;
ALTER TABLE procedures ADD COLUMN max_outlier_rate_pct REAL NOT NULL DEFAULT 100;
"""

V3_SCHEMA = """
CREATE TABLE torque_curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    record_id INTEGER NOT NULL REFERENCES records(id), created_at TEXT NOT NULL,
    UNIQUE (procedure_id, bolt_no)
);
CREATE TABLE curve_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, curve_id INTEGER NOT NULL REFERENCES torque_curves(id),
    revision INTEGER NOT NULL, record_id INTEGER NOT NULL REFERENCES records(id),
    time_unit TEXT NOT NULL, torque_unit TEXT NOT NULL, angle_unit TEXT NOT NULL,
    points TEXT NOT NULL, snug_override INTEGER, amendment_note TEXT,
    analysis TEXT NOT NULL, usable INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (curve_id, revision)
);
"""

V4_SCHEMA = """
CREATE TABLE alignment_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    version INTEGER NOT NULL, flange_face_diameter_mm REAL NOT NULL,
    gasket_inner_diameter_mm REAL NOT NULL, gasket_outer_diameter_mm REAL NOT NULL,
    bore_diameter_mm REAL NOT NULL, max_parallelism_mm REAL NOT NULL,
    max_radial_mismatch_mm REAL NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
    adjustment_reason TEXT, analysis TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (procedure_id, version)
);
CREATE TABLE alignment_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT, check_id INTEGER NOT NULL REFERENCES alignment_checks(id),
    angle_deg REAL NOT NULL, axial_gap REAL NOT NULL, radial_offset REAL NOT NULL,
    gasket_edge_position REAL NOT NULL, bolt_free_insertion INTEGER NOT NULL, length_unit TEXT NOT NULL
);
"""

V5_SCHEMA = """
CREATE TABLE site_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);
CREATE TABLE plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL, constraint_revision INTEGER, plan TEXT NOT NULL,
    change_note TEXT, created_at TEXT NOT NULL, UNIQUE (procedure_id, revision)
);
"""

V6_SCHEMA = """
CREATE TABLE tensioning_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL, parent_id INTEGER REFERENCES tensioning_plans(id),
    status TEXT NOT NULL DEFAULT 'open', area_mm2 REAL NOT NULL, length_mm REAL NOT NULL,
    elastic_modulus_mpa REAL NOT NULL, target_load_kn REAL NOT NULL,
    load_tolerance_pct REAL NOT NULL, tensioner_id TEXT NOT NULL, tensioner_count INTEGER NOT NULL,
    hydraulic_area_mm2 REAL NOT NULL, max_pressure_mpa REAL NOT NULL, max_stroke_mm REAL NOT NULL,
    min_tool_spacing INTEGER NOT NULL, load_transfer_coefficient REAL NOT NULL,
    min_hold_seconds REAL NOT NULL, pressure_sync_tolerance_pct REAL NOT NULL,
    gauge_id TEXT NOT NULL, gauge_calibration_until TEXT NOT NULL,
    stage_ratios TEXT NOT NULL, scheme TEXT NOT NULL,
    ultrasonic_batch_id INTEGER REFERENCES measurement_batches(id),
    ultrasonic_snapshot TEXT, change_note TEXT, approved_at TEXT, confirmed_at TEXT,
    created_at TEXT NOT NULL, UNIQUE (procedure_id, revision)
);
CREATE TABLE tensioning_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES tensioning_plans(id),
    plan_revision INTEGER NOT NULL, round_no INTEGER NOT NULL, group_no INTEGER NOT NULL,
    operator TEXT NOT NULL, reported_at TEXT NOT NULL, gauge_id TEXT NOT NULL,
    hold_seconds REAL NOT NULL, release_order TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (plan_id, round_no, group_no)
);
CREATE TABLE tensioning_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES tensioning_reports(id),
    bolt_no INTEGER NOT NULL, pressure_mpa REAL NOT NULL, stroke_mm REAL NOT NULL,
    applied_load_kn REAL NOT NULL, residual_load_kn REAL NOT NULL
);
"""

V7_SCHEMA = """
CREATE TABLE thermal_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL, parent_id INTEGER REFERENCES thermal_cases(id),
    status TEXT NOT NULL DEFAULT 'open', source_type TEXT NOT NULL, source_id INTEGER,
    payload TEXT NOT NULL, frozen TEXT NOT NULL, initial_loads TEXT NOT NULL, result TEXT NOT NULL,
    change_note TEXT, decided_by TEXT, decision_note TEXT, confirmed_at TEXT,
    created_at TEXT NOT NULL, UNIQUE (procedure_id, revision)
);
CREATE TABLE thermal_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES thermal_cases(id),
    revision INTEGER NOT NULL, bolt_no INTEGER, reason TEXT NOT NULL, message TEXT NOT NULL,
    interval TEXT, created_at TEXT NOT NULL
);
"""

# 工艺行的插入列随阶段变化（v3 前无轨迹复核列）。
_PROC_BASE_COLS = (
    "version, parent_id, change_note, status, flange_class, bolt_count, gasket,"
    " target_torque, stage_ratios, tolerance_pct, tool_id, tool_range_min,"
    " tool_range_max, calibration_valid_until, start_angle_deg, clockwise, created_at,"
    " approved_at, started_at, completed_at, reviewed_at, archived_at, reviewer, review_note"
)
_PROC_BASE_VALS = (
    1, None, None, "in_progress", "PN40 DN200", 8, "缠绕垫片", 320.0, "[0.3,0.6,1.0]",
    5.0, "TW-1001", 50.0, 500.0, "2026-12-31", 0.0, 1, "2026-01-01T00:00:00",
    "2026-01-01T01:00:00", "2026-01-01T02:00:00", None, None, None, None, None,
)


def _qmarks(n: int) -> str:
    return ",".join("?" * n)


def _insert_v1_business_data(con: sqlite3.Connection) -> None:
    """写入工艺 + 逐栓扭矩记录 + 异常（全部历史版本共有）。"""
    con.execute(f"INSERT INTO procedures ({_PROC_BASE_COLS}) VALUES ({_qmarks(24)})",
                _PROC_BASE_VALS)
    con.execute(
        "INSERT INTO records (procedure_id, round_no, bolt_no, tool_id, operator,"
        " reported_at, measured_torque, rework_of, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (1, 1, 1, "TW-1001", "张三", "2026-01-02T08:00:00", 98.5, None,
         "2026-01-02T08:00:00"))
    con.execute(
        "INSERT INTO records (procedure_id, round_no, bolt_no, tool_id, operator,"
        " reported_at, measured_torque, rework_of, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (1, 3, 1, "TW-1001", "张三", "2026-01-02T10:00:00", 318.0, None,
         "2026-01-02T10:00:00"))
    con.execute(
        "INSERT INTO anomalies (procedure_id, bolt_no, reason, message, payload, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (1, 5, "out_of_sequence", "夹具旧库：记录一次被拒跳步",
         '{"bolt_no": 5}', "2026-01-02T09:00:00"))


def _insert_measurement_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO measurement_batches (procedure_id, revision, status, scope_bolts,"
        " locked_bolts, locked_results, length_mm, area_mm2, elastic_modulus_mpa,"
        " sound_velocity, temp_coefficient, reference_temp_c, temp_comp_min_c,"
        " temp_comp_max_c, target_load_min_kn, target_load_max_kn, material_load_limit_kn,"
        " max_imbalance_pct, instrument_id, instrument_calibration_until,"
        " derived_from_batch_id, confirmed_revision, confirmed_at, created_at)"
        f" VALUES ({_qmarks(24)})",
        (1, 2, "confirmed", "[1,2,3,4,5,6,7,8]", "[]", None,
         120.0, 353.0, 206000.0, 5900.0, 0.00012, 20.0, -10.0, 80.0,
         60.0, 80.0, 120.0, 10.0, "UT-7", "2026-12-31", None, 2,
         "2026-01-03T00:00:00", "2026-01-02T11:00:00"))
    for bolt, tof in ((1, 2.0340e-5), (2, 2.0335e-5)):
        con.execute(
            "INSERT INTO measurement_baselines (batch_id, bolt_no, tof_s, created_at)"
            " VALUES (?,?,?,?)", (1, bolt, tof, "2026-01-02T11:05:00"))
    con.execute(
        "INSERT INTO measurement_readings (batch_id, bolt_no, tof_s, temperature_c,"
        " operator, measured_at, supersedes, excluded, amendment_note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 2.0360e-5, 22.0, "李四", "2026-01-02T12:00:00", None, 0, None,
         "2026-01-02T12:00:00"))
    con.execute(
        "INSERT INTO measurement_readings (batch_id, bolt_no, tof_s, temperature_c,"
        " operator, measured_at, supersedes, excluded, amendment_note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 2.0359e-5, 22.0, "李四", "2026-01-02T12:30:00", 1, 0, "重测：复核离群",
         "2026-01-02T12:30:00"))
    con.execute(
        "INSERT INTO measurement_gaps (batch_id, revision, bolt_no, reason, message,"
        " payload, created_at) VALUES (?,?,?,?,?,?,?)",
        (1, 1, 5, "baseline_missing", "夹具旧库：缺基线", None,
         "2026-01-02T12:00:00"))


def _insert_curve_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO torque_curves (procedure_id, bolt_no, revision, record_id, created_at)"
        " VALUES (?,?,?,?,?)", (1, 1, 1, 2, "2026-01-02T13:00:00"))
    con.execute(
        "INSERT INTO curve_revisions (curve_id, revision, record_id, time_unit,"
        " torque_unit, angle_unit, points, snug_override, amendment_note, analysis,"
        " usable, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 2, "ms", "N·m", "deg",
         '[{"t":0,"torque":0,"angle":0},{"t":10,"torque":40,"angle":5}]',
         None, None, '{"defects":[]}', 1, "2026-01-02T13:00:00"))


def _insert_alignment_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO alignment_checks (procedure_id, version, flange_face_diameter_mm,"
        " gasket_inner_diameter_mm, gasket_outer_diameter_mm, bore_diameter_mm,"
        " max_parallelism_mm, max_radial_mismatch_mm, operator, measured_at,"
        " adjustment_reason, analysis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 285.0, 220.0, 270.0, 200.0, 1.0, 2.0, "王五",
         "2026-01-01T03:00:00", None, '{"passed": true, "blockers": []}',
         "2026-01-01T03:00:00"))
    con.execute(
        "INSERT INTO alignment_points (check_id, angle_deg, axial_gap, radial_offset,"
        " gasket_edge_position, bolt_free_insertion, length_unit)"
        " VALUES (?,?,?,?,?,?,?)",
        (1, 0.0, 2.0, 0.0, 7.5, 1, "mm"))


def _insert_site_plan_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO site_constraints (procedure_id, revision, payload, created_at)"
        " VALUES (?,?,?,?)",
        (1, 1, '{"bolts": [], "shift_start": "2026-01-02T08:00:00"}',
         "2026-01-01T04:00:00"))
    con.execute(
        "INSERT INTO plan_revisions (procedure_id, revision, constraint_revision,"
        " plan, change_note, created_at) VALUES (?,?,?,?,?,?)",
        (1, 1, 1, '{"steps": [], "actions": [], "meta": {}}', "批准冻结",
         "2026-01-01T04:30:00"))


def _insert_tensioning_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO tensioning_plans (procedure_id, revision, parent_id, status,"
        " area_mm2, length_mm, elastic_modulus_mpa, target_load_kn, load_tolerance_pct,"
        " tensioner_id, tensioner_count, hydraulic_area_mm2, max_pressure_mpa,"
        " max_stroke_mm, min_tool_spacing, load_transfer_coefficient, min_hold_seconds,"
        " pressure_sync_tolerance_pct, gauge_id, gauge_calibration_until, stage_ratios,"
        " scheme, ultrasonic_batch_id, ultrasonic_snapshot, change_note, approved_at,"
        " confirmed_at, created_at) VALUES (" + _qmarks(28) + ")",
        (1, 1, None, "confirmed", 353.0, 120.0, 206000.0, 150.0, 10.0,
         "TS-2", 4, 1500.0, 60.0, 25.0, 1, 0.15, 30.0, 5.0,
         "PG-9", "2026-12-31", "[1.0]", '[[1,5,3,7]]', None, None, None,
         "2026-01-04T00:00:00", "2026-01-04T01:00:00", "2026-01-03T08:00:00"))
    con.execute(
        "INSERT INTO tensioning_reports (plan_id, plan_revision, round_no, group_no,"
        " operator, reported_at, gauge_id, hold_seconds, release_order, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 1, 1, "赵六", "2026-01-04T02:00:00", "PG-9", 45.0, "[1,5,3,7]",
         "2026-01-04T02:00:00"))
    for bolt, pressure, stroke, applied, residual in (
            (1, 42.1, 5.1, 152.0, 144.0), (5, 42.0, 5.0, 151.0, 140.0)):
        con.execute(
            "INSERT INTO tensioning_channels (report_id, bolt_no, pressure_mpa,"
            " stroke_mm, applied_load_kn, residual_load_kn) VALUES (?,?,?,?,?,?)",
            (1, bolt, pressure, stroke, applied, residual))


def _insert_thermal_data(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO thermal_cases (procedure_id, revision, parent_id, status,"
        " source_type, source_id, payload, frozen, initial_loads, result, change_note,"
        " decided_by, decision_note, confirmed_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1, None, "confirmed", "tensioning", 1, '{"bolt": {}}', '{"bolt": {}}',
         '{"1": 144.0, "5": 140.0}', '{"nodes": []}', None, "钱七", "同意确认",
         "2026-01-05T00:00:00", "2026-01-04T08:00:00"))
    con.execute(
        "INSERT INTO thermal_gaps (case_id, revision, bolt_no, reason, message,"
        " interval, created_at) VALUES (?,?,?,?,?,?,?)",
        (1, 1, 5, "initial_load_missing", "夹具旧库：缺逐栓初载", None,
         "2026-01-04T08:05:00"))


# 夹具名 -> 建到哪个阶段（1..7），以及该阶段追加的业务数据。
_STAGES: tuple[tuple[str, int, str, object], ...] = (
    ("legacy_v1", 1, V1_SCHEMA, _insert_v1_business_data),
    ("legacy_ultrasonic_v2", 2, V2_SCHEMA, _insert_measurement_data),
    ("legacy_curve_v3", 3, V3_SCHEMA, _insert_curve_data),
    ("legacy_alignment_v4", 4, V4_SCHEMA, _insert_alignment_data),
    ("legacy_plan_v5", 5, V5_SCHEMA, _insert_site_plan_data),
    ("legacy_tensioning_v6", 6, V6_SCHEMA, _insert_tensioning_data),
    ("legacy_full_v7", 7, V7_SCHEMA, _insert_thermal_data),
)


def build_fixture(path: str, name: str) -> None:
    """按夹具名在 path 重建一个 user_version=0 的历史快照库。"""
    stages_by_name = {n: (ver, schema, data_fn) for n, ver, schema, data_fn in _STAGES}
    target_version, _, _ = stages_by_name[name]
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(V1_SCHEMA)
        _insert_v1_business_data(con)
        for fixture_name, ver, schema, data_fn in _STAGES[1:]:
            if ver > target_version:
                break
            if ver == 3:
                con.executescript(V3_ALTER)
            con.executescript(schema)
            data_fn(con)
        # 旧库从无 user_version 概念：保持 0，由迁移器按对象推断。
        con.execute("PRAGMA user_version = 0")
        con.commit()
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        con.close()


FIXTURE_NAMES = tuple(n for n, *_ in _STAGES)
