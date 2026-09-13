-- 版本 1 基线（对应首个发布结构）：工艺版本、逐栓记录、异常（被拒回传）。
-- 冻结自项目首个版本的 CREATE TABLE，是后续所有迁移的前置结构。
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
    stage_ratios TEXT NOT NULL,          -- JSON 数组
    tolerance_pct REAL NOT NULL,
    tool_id TEXT NOT NULL,
    tool_range_min REAL NOT NULL,
    tool_range_max REAL NOT NULL,
    calibration_valid_until TEXT NOT NULL, -- ISO 日期
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
    rework_of INTEGER REFERENCES records(id),  -- 补拧指向原记录，原记录永不覆盖
    created_at TEXT NOT NULL
);

CREATE TABLE anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,                            -- 被拒请求的 JSON
    created_at TEXT NOT NULL
);
