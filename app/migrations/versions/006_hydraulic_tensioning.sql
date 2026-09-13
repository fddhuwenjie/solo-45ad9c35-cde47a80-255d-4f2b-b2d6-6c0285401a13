-- 版本 6：液压张拉执行（拉伸器分组同步加压）。
-- 新增张拉方案、分组回传与逐通道原始读数 3 张表。
-- 液压张拉方案：创建时冻结螺栓截面/目标预紧力/拉伸器能力与行程/压力表校准/
-- 栓组与载荷转移系数；人工改组、采纳超声或中断重排均派生新修订，旧修订不覆盖
CREATE TABLE tensioning_plans (
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
CREATE TABLE tensioning_reports (
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
CREATE TABLE tensioning_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES tensioning_reports(id),
    bolt_no INTEGER NOT NULL,
    pressure_mpa REAL NOT NULL,
    stroke_mm REAL NOT NULL,
    applied_load_kn REAL NOT NULL,
    residual_load_kn REAL NOT NULL
);
