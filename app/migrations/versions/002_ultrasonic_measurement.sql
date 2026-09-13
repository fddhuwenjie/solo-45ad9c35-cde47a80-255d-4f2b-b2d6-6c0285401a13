-- 版本 2：超声伸长复核（预紧力直接测量）。
-- 新增测量批次、基线、复测读数、证据缺口与失败批次补拧草稿 5 张表。
-- 冻结自超声模块引入时的实际 DDL。
CREATE TABLE measurement_batches (
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
CREATE TABLE measurement_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    bolt_no INTEGER NOT NULL,
    tof_s REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, bolt_no)
);

-- 逐栓复测读数；重测新增行（supersedes 指向被重测读数），排除仅置位不覆盖
CREATE TABLE measurement_readings (
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
CREATE TABLE measurement_gaps (
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
CREATE TABLE rework_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_batch_id INTEGER NOT NULL REFERENCES measurement_batches(id),
    source_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    rework_procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    locked_bolts TEXT NOT NULL,             -- JSON：锁定（不补拧）螺栓
    target_bolts TEXT NOT NULL,             -- JSON：补拧螺栓
    created_at TEXT NOT NULL
);
