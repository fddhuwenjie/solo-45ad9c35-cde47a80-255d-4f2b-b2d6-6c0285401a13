-- 版本 8：紧固件摩擦批次标定（扭矩系数台架标定）。
-- 同一套工艺改用镀层螺栓、另一批螺母或新开封润滑剂后，旧扭矩系数未必仍适用。
-- 本版新增：工艺当前紧固件批次状态登记（草稿可改、逐版留痕）、摩擦标定版本
-- （冻结批次身份/试样几何/装配次数/测量通道与逐点扭矩-转角-轴向载荷原始数据，
-- 人工改贴合点、排除试样派生修订，旧修订废止不覆盖），以及工艺批准时对
-- 已确认标定版的引用（procedures.adopted_calibration_id）。

-- 摩擦批次标定版本：identity_key 冻结批次身份（螺栓/螺母批次、表面处理、润滑剂
-- 批次与润滑状态）；payload 为原始提交 JSON（试样/装配次/逐点原始读数与声明单位）；
-- frozen 为归一化冻结参数（几何、通道量程与校准、目标载荷、工具量程快照）；
-- analysis 为评估结果（逐试样扭矩系数、批内离散度、重复装配漂移、可用载荷区间、
-- 推荐扭矩窗口与草稿阻断项）。状态：draft / pending_review（批次变化待复核）/
-- confirmed / superseded
CREATE TABLE friction_calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    parent_id INTEGER REFERENCES friction_calibrations(id),  -- 派生自上一修订
    status TEXT NOT NULL DEFAULT 'draft',
    identity_key TEXT NOT NULL,              -- 批次身份 JSON（五元组）
    payload TEXT NOT NULL,                   -- 原始提交 JSON（含逐点数据与人工决定）
    frozen TEXT NOT NULL,                    -- 冻结参数 JSON（内部单位 N·m/kN/deg）
    analysis TEXT NOT NULL,                  -- 评估结果 JSON（含阻断项与逐试样指标）
    change_note TEXT,                        -- 修订理由（首版 NULL）
    decided_by TEXT,                         -- 确认人
    decision_note TEXT,                      -- 确认意见
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 工艺当前紧固件批次状态：草稿阶段整组替换、逐版留痕；批次变化仅让相关
-- 草稿标定待复核（pending_review），已确认标定版不受影响
CREATE TABLE fastener_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    payload TEXT NOT NULL,                   -- 批次状态 JSON（五元组）
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 工艺批准时引用的已确认标定版（批次身份须与当前批次状态一致）
ALTER TABLE procedures
    ADD COLUMN adopted_calibration_id INTEGER REFERENCES friction_calibrations(id);
