-- 版本 5：受限栓位现场约束与冻结施工计划。
-- 新增现场约束（整组 JSON 逐版保存）与冻结计划修订表。
-- 现场约束（受限栓位）：整组 JSON 逐版保存；草稿可改（新增版本），
-- 批准随计划冻结；批准后现场障碍/工具变化经 plan-revisions 派生新版本
CREATE TABLE site_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    payload TEXT NOT NULL,                   -- SiteConstraintsInput JSON
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);

-- 冻结施工计划：批准时生成 v1；现场变化派生修订（只重排未完成步骤）。
-- 恢复序列、JSON 作业包与圆周 SVG 统一读取本表最新版本
CREATE TABLE plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    revision INTEGER NOT NULL,               -- 同一工艺自 1 递增
    constraint_revision INTEGER,             -- 对应 site_constraints.revision；NULL = 规则圆周
    plan TEXT NOT NULL,                      -- {"steps": [...], "actions": [...], "meta": {...}}
    change_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (procedure_id, revision)
);
