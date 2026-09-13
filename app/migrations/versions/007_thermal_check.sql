-- 版本 7：热态预紧力校核（升温后的变形协调）。
-- 新增热态工况与证据缺口表；建案冻结部件/垫片压缩-回弹曲线/限值/温度时序/
-- 逐栓初始载荷快照，人工采用替代边界或曲线派生新修订，旧修订废止不覆盖。
-- 热态预紧力校核工况：建案冻结结构部件/垫片压缩-回弹曲线/限值/带时标分区
-- 温度与逐栓初始载荷快照；人工采用替代边界或曲线派生新修订，旧修订废止不覆盖
CREATE TABLE thermal_cases (
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
CREATE TABLE thermal_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES thermal_cases(id),
    revision INTEGER NOT NULL,
    bolt_no INTEGER,                         -- 整案性缺口（如单位冲突）为 NULL
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    interval TEXT,                           -- JSON [起, 止]；无时间属性为 NULL
    created_at TEXT NOT NULL
);
