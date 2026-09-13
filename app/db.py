"""SQLite 持久化：工艺版本、逐栓记录、异常（被拒回传）。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    parent_id INTEGER REFERENCES procedures(id),
    change_note TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    flange_class TEXT NOT NULL,
    bolt_count INTEGER NOT NULL,
    gasket TEXT NOT NULL,
    target_torque REAL NOT NULL,
    stage_ratios TEXT NOT NULL,          -- JSON 数组
    tolerance_pct REAL NOT NULL,
    tool_id TEXT NOT NULL,
    tool_range_min REAL NOT NULL,
    tool_range_max REAL NOT NULL,
    calibration_valid_until TEXT NOT NULL, -- ISO 日期
    start_angle_deg REAL NOT NULL DEFAULT 0,
    clockwise INTEGER NOT NULL DEFAULT 1,
    -- 扭矩-转角轨迹复核参数（批准时锁定）
    curve_direction TEXT NOT NULL DEFAULT 'cw',
    snug_torque REAL NOT NULL,
    post_snug_angle_min_deg REAL NOT NULL,
    post_snug_angle_max_deg REAL NOT NULL,
    max_sample_interval_ms REAL NOT NULL,
    slope_drop_limit REAL NOT NULL,
    max_outlier_rate_pct REAL NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    reviewed_at TEXT,
    archived_at TEXT,
    reviewer TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    round_no INTEGER NOT NULL,
    bolt_no INTEGER NOT NULL,
    tool_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    measured_torque REAL NOT NULL,
    rework_of INTEGER REFERENCES records(id),  -- 补拧指向原记录，原记录永不覆盖
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,                            -- 被拒请求的 JSON
    created_at TEXT NOT NULL
);

-- 超声伸长复核：测量批次（建立时冻结全部参数）
CREATE TABLE IF NOT EXISTS measurement_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL DEFAULT 1,     -- 重测/排除使修订号 +1
    status TEXT NOT NULL DEFAULT 'open',    -- open / confirmed / superseded
    scope_bolts TEXT NOT NULL,              -- JSON 数组，覆盖螺栓（补拧批次为子集）
    locked_bolts TEXT NOT NULL DEFAULT '[]', -- 补拧批次锁定（沿用上批合格）螺栓
    locked_results TEXT,                    -- JSON：锁定螺栓在上一批的结果快照
    length_mm REAL NOT NULL,
    area_mm2 REAL NOT NULL,
    elastic_modulus_mpa REAL NOT NULL,
    sound_velocity REAL NOT NULL,
    temp_coefficient REAL NOT NULL,
    reference_temp_c REAL NOT NULL,
    temp_comp_min_c REAL NOT NULL,
    temp_comp_max_c REAL NOT NULL,
    target_load_min_kn REAL NOT NULL,
    target_load_max_kn REAL NOT NULL,
    material_load_limit_kn REAL NOT NULL,
    max_imbalance_pct REAL NOT NULL,
    instrument_id TEXT NOT NULL,
    instrument_calibration_until TEXT NOT NULL,
    derived_from_batch_id INTEGER REFERENCES measurement_batches(id),
    confirmed_revision INTEGER,
    confirmed_at TEXT,
    created_at TEXT NOT NULL
);

-- 逐栓基线飞行时间（开工前）；每栓每批至多一条
CREATE TABLE IF NOT EXISTS measurement_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    bolt_no INTEGER NOT NULL,
    tof_s REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, bolt_no)
);

-- 逐栓复测读数；重测新增行（supersedes 指向被重测读数），排除仅置位不覆盖
CREATE TABLE IF NOT EXISTS measurement_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    bolt_no INTEGER NOT NULL,
    tof_s REAL NOT NULL,
    temperature_c REAL NOT NULL,
    operator TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    supersedes INTEGER REFERENCES measurement_readings(id),
    excluded INTEGER NOT NULL DEFAULT 0,
    amendment_note TEXT,                   -- 重测理由 / 排除理由
    created_at TEXT NOT NULL
);

-- 证据缺口留痕：只记录缺口，绝不伪造合格结论
CREATE TABLE IF NOT EXISTS measurement_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    revision INTEGER NOT NULL,
    bolt_no INTEGER NOT NULL,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

-- 失败批次派生补拧草稿：锁定合格螺栓及其结果快照
CREATE TABLE IF NOT EXISTS rework_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    source_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    rework_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    locked_bolts TEXT NOT NULL,             -- JSON：锁定（不补拧）螺栓
    target_bolts TEXT NOT NULL,             -- JSON：补拧螺栓
    created_at TEXT NOT NULL
);

-- 扭矩-转角轨迹：每栓一条，关联终轮已接受记录；revision 指向当前采用修订
CREATE TABLE IF NOT EXISTS torque_curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,    -- 当前采用修订号
    record_id INTEGER NOT NULL REFERENCES records(id),  -- 当前关联的终轮记录
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, bolt_no)
);

-- 轨迹修订：原始轨迹逐版保存（提交单位），人工移动贴合点/换曲线均另存新版，
-- 旧版本永不覆盖、保持可查；analysis 为分析结果 JSON（指标 + 缺陷区间）
CREATE TABLE IF NOT EXISTS curve_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curve_id INTEGER NOT NULL REFERENCES torque_curves(id),
    revision INTEGER NOT NULL,
    record_id INTEGER NOT NULL REFERENCES records(id),
    time_unit TEXT NOT NULL,
    torque_unit TEXT NOT NULL,
    angle_unit TEXT NOT NULL,
    points TEXT NOT NULL,                   -- 原始轨迹 JSON（提交单位）
    snug_override INTEGER,                  -- 人工贴合点索引；NULL 为自动定位
    amendment_note TEXT,                    -- 修订原因（首修订为 NULL）
    analysis TEXT NOT NULL,                 -- 分析结果 JSON
    usable INTEGER NOT NULL,                -- 该修订是否可进入复核结论
    created_at TEXT NOT NULL,
    UNIQUE (curve_id, revision)
);

-- 装配对中预检：每个版本创建时冻结法兰/垫片几何与限值，旧版本不可覆盖
CREATE TABLE IF NOT EXISTS alignment_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    version INTEGER NOT NULL,               -- 同一工艺自 1 递增；复测另存新版
    flange_face_diameter_mm REAL NOT NULL,
    gasket_inner_diameter_mm REAL NOT NULL,
    gasket_outer_diameter_mm REAL NOT NULL,
    bore_diameter_mm REAL NOT NULL,
    max_parallelism_mm REAL NOT NULL,
    max_radial_mismatch_mm REAL NOT NULL,
    operator TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    adjustment_reason TEXT,                 -- 复测（v>=2）必填的调整原因；首版 NULL
    analysis TEXT NOT NULL,                 -- analyze_alignment 结论 JSON
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, version)
);

-- 预检测点（原始单位逐版保存，旧版本不可覆盖）
CREATE TABLE IF NOT EXISTS alignment_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL REFERENCES alignment_checks(id),
    angle_deg REAL NOT NULL,
    axial_gap REAL NOT NULL,
    radial_offset REAL NOT NULL,
    gasket_edge_position REAL NOT NULL,
    bolt_free_insertion INTEGER NOT NULL,
    length_unit TEXT NOT NULL
);

-- 现场约束（受限栓位）：整组 JSON 逐版保存；草稿可改（新增版本），
-- 批准随计划冻结；批准后现场障碍/工具变化经 plan-revisions 派生新版本
CREATE TABLE IF NOT EXISTS site_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    payload TEXT NOT NULL,                   -- SiteConstraintsInput JSON
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 冻结施工计划：批准时生成 v1；现场变化派生修订（只重排未完成步骤）。
-- 恢复序列、JSON 作业包与圆周 SVG 统一读取本表最新版本
CREATE TABLE IF NOT EXISTS plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    constraint_revision INTEGER,             -- 对应 site_constraints.revision；NULL = 规则圆周
    plan TEXT NOT NULL,                      -- {"steps": [...], "actions": [...], "meta": {...}}
    change_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 液压张拉方案：创建时冻结螺栓截面/目标预紧力/拉伸器能力与行程/压力表校准/
-- 栓组与载荷转移系数；人工改组、采纳超声或中断重排均派生新修订，旧修订不覆盖
CREATE TABLE IF NOT EXISTS tensioning_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    parent_id INTEGER REFERENCES tensioning_plans(id),  -- 派生自上一修订
    status TEXT NOT NULL DEFAULT 'open',     -- open / approved / confirmed / superseded
    area_mm2 REAL NOT NULL,                  -- 螺栓有效截面
    length_mm REAL NOT NULL,                 -- 螺栓有效长度（行程预测）
    elastic_modulus_mpa REAL NOT NULL,
    target_load_kn REAL NOT NULL,            -- 目标预紧力
    load_tolerance_pct REAL NOT NULL,        -- 残余预紧力允许偏差 ±%
    tensioner_id TEXT NOT NULL,
    tensioner_count INTEGER NOT NULL,        -- 可同时安装的拉伸器数量（栓组上限）
    hydraulic_area_mm2 REAL NOT NULL,        -- 拉伸器液压有效面积
    max_pressure_mpa REAL NOT NULL,          -- 拉伸器/泵能力上限
    max_stroke_mm REAL NOT NULL,             -- 最大活塞行程
    min_tool_spacing INTEGER NOT NULL,       -- 相邻机具最小栓位间隔（防相撞）
    load_transfer_coefficient REAL NOT NULL, -- 载荷转移系数 λ
    min_hold_seconds REAL NOT NULL,          -- 最短保压时间
    pressure_sync_tolerance_pct REAL NOT NULL, -- 组内压力同步允差 %
    gauge_id TEXT NOT NULL,                  -- 压力表编号
    gauge_calibration_until TEXT NOT NULL,   -- 压力表校准有效期（含当日）
    stage_ratios TEXT NOT NULL,              -- 分轮比例 JSON
    scheme TEXT NOT NULL,                    -- 分轮换位方案 JSON（每轮每组栓号）
    ultrasonic_batch_id INTEGER REFERENCES measurement_batches(id), -- 采纳的超声批次
    ultrasonic_snapshot TEXT,                -- 采纳时逐栓实测载荷快照 JSON
    change_note TEXT,                        -- 派生修订理由（首修订 NULL）
    approved_at TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 分组回传：同组同步加压的保压时段与卸压次序；修订号与方案同版留痕
CREATE TABLE IF NOT EXISTS tensioning_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES tensioning_plans(id),
    plan_revision INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    group_no INTEGER NOT NULL,
    operator TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    gauge_id TEXT NOT NULL,
    hold_seconds REAL NOT NULL,
    release_order TEXT NOT NULL,             -- JSON 卸压次序
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, round_no, group_no)
);

-- 逐通道原始读数与换算结果（施加载荷/预测残余预紧力），落库后不可改
CREATE TABLE IF NOT EXISTS tensioning_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES tensioning_reports(id),
    bolt_no INTEGER NOT NULL,
    pressure_mpa REAL NOT NULL,
    stroke_mm REAL NOT NULL,
    applied_load_kn REAL NOT NULL,
    residual_load_kn REAL NOT NULL
);

-- 热态预紧力校核工况：建案冻结结构部件/垫片压缩-回弹曲线/限值/带时标分区
-- 温度与逐栓初始载荷快照；人工采用替代边界或曲线派生新修订，旧修订废止不覆盖
CREATE TABLE IF NOT EXISTS thermal_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    parent_id INTEGER REFERENCES thermal_cases(id),  -- 派生自上一修订
    status TEXT NOT NULL DEFAULT 'open',     -- open / confirmed / superseded
    source_type TEXT NOT NULL,               -- ultrasonic / tensioning
    source_id INTEGER,                       -- 超声批次或张拉方案 id（确认时来源）
    payload TEXT NOT NULL,                   -- 建案/修订请求 JSON（原始声明单位）
    frozen TEXT NOT NULL,                    -- normalize_case 冻结参数 JSON（mm/mm²/MPa）
    initial_loads TEXT NOT NULL,             -- 来源逐栓初始载荷快照 {"bolt_no": kN}
    result TEXT NOT NULL,                    -- evaluate_case 逐时结果 JSON
    change_note TEXT,                        -- 人工采用替代边界/曲线理由（首修订 NULL）
    decided_by TEXT,                         -- 确认人
    decision_note TEXT,                      -- 确认意见
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 热态校核证据缺口留痕：单位冲突/温度断档/曲线覆盖不足/初始载荷缺失/不收敛，
-- 逐条给出栓号与对应时间区间；版本照常落库，但有缺口即不可确认
CREATE TABLE IF NOT EXISTS thermal_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES thermal_cases(id),
    revision INTEGER NOT NULL,
    bolt_no INTEGER,                         -- 整案性缺口（如单位冲突）为 NULL
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    interval TEXT,                           -- JSON [起, 止]；无时间属性为 NULL
    created_at TEXT NOT NULL
);
"""

# 既有库迁移：为 procedures 补充轨迹复核参数列（默认宽松值，新工艺由 API 写入真实值）
_PROCEDURE_CURVE_COLUMNS = (
    ("curve_direction", "TEXT NOT NULL DEFAULT 'cw'"),
    ("snug_torque", "REAL NOT NULL DEFAULT 0"),
    ("post_snug_angle_min_deg", "REAL NOT NULL DEFAULT 0"),
    ("post_snug_angle_max_deg", "REAL NOT NULL DEFAULT 100000"),
    ("max_sample_interval_ms", "REAL NOT NULL DEFAULT 1e15"),
    ("slope_drop_limit", "REAL NOT NULL DEFAULT 1e15"),
    ("max_outlier_rate_pct", "REAL NOT NULL DEFAULT 100"),
)


def db_path() -> str:
    return os.environ.get("FLANGE_DB", "flange.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(procedures)")}
        for name, ddl in _PROCEDURE_CURVE_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE procedures ADD COLUMN {name} {ddl}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
