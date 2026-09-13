-- 版本 4：装配对中预检（紧固前自由状态）。
-- 新增预检版本表与逐测点表；冻结法兰/垫片几何与限值，旧版本不可覆盖。
CREATE TABLE alignment_checks (
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
CREATE TABLE alignment_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL REFERENCES alignment_checks(id),
    angle_deg REAL NOT NULL,
    axial_gap REAL NOT NULL,
    radial_offset REAL NOT NULL,
    gasket_edge_position REAL NOT NULL,
    bolt_free_insertion INTEGER NOT NULL,
    length_unit TEXT NOT NULL
);
