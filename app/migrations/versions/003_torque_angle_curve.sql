-- 版本 3：扭矩-转角轨迹复核。
-- 3.1 为 procedures 补入轨迹复核参数（批准时锁定）。早期库由临时补列逻辑加上，
--     默认值保持与旧逻辑一致（宽松值），新工艺由 API 写入真实值。
ALTER TABLE procedures ADD COLUMN curve_direction TEXT NOT NULL DEFAULT 'cw';
ALTER TABLE procedures ADD COLUMN snug_torque REAL NOT NULL DEFAULT 0;
ALTER TABLE procedures ADD COLUMN post_snug_angle_min_deg REAL NOT NULL DEFAULT 0;
ALTER TABLE procedures ADD COLUMN post_snug_angle_max_deg REAL NOT NULL DEFAULT 100000;
ALTER TABLE procedures ADD COLUMN max_sample_interval_ms REAL NOT NULL DEFAULT 1e15;
ALTER TABLE procedures ADD COLUMN slope_drop_limit REAL NOT NULL DEFAULT 1e15;
ALTER TABLE procedures ADD COLUMN max_outlier_rate_pct REAL NOT NULL DEFAULT 100;

-- 扭矩-转角轨迹：每栓一条，关联终轮已接受记录；revision 指向当前采用修订
CREATE TABLE torque_curves (
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
CREATE TABLE curve_revisions (
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
