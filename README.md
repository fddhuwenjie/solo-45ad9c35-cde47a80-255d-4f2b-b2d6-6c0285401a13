# 法兰螺栓紧固工艺管理服务

Python + FastAPI 接收请求，Pydantic 核验字段，工艺与执行版本写入 SQLite。
按**对角对称（十字交叉）**与**分轮递增**规则生成稳定的紧固顺序，覆盖
`创建 → 批准 → 开工 → 逐栓回传 → 复核 → 封存` 全流程。

## 运行

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload          # 默认库文件 ./flange.db
FLANGE_DB=/path/to.db .venv/bin/uvicorn app.main:app
.venv/bin/python -m pytest tests/ -q             # 21 个测试
```

## 输入项（POST /procedures）

| 字段 | 说明 |
|---|---|
| `flange_class` | 法兰等级，如 `PN40 DN200` |
| `bolt_count` | 螺栓数量（偶数 4~96） |
| `start_angle_deg` / `clockwise` | 1 号栓方位角 / 编号方向（编号方位） |
| `gasket` | 垫片 |
| `target_torque` | 目标扭矩 N·m，须在工具量程内 |
| `stage_ratios` | 分级比例，严格递增且末级 1.0，如 `[0.3, 0.6, 1.0]` |
| `tolerance_pct` | 允许偏差 ±% |
| `tool_id` / `tool_range_min` / `tool_range_max` | 工具编号与量程 |
| `calibration_valid_until` | 校准有效期（含当日） |

紧固顺序：n 栓按 `1, 1+n/2, 2, 2+n/2, …` 交叉展开（8 栓为 1-5-2-6-3-7-4-8），
每轮所有栓按该顺序拧到当轮比例。n≥6 时同轮相邻两步在圆周上必不相邻；
n=4 数学上无法生成全程非相邻序列（1-3-2-4 中 3→2 相邻），该配置在批准/开工前拒绝。

**批准/开工前校验**（不满足即 409 并说明原因）：

- `sequence_not_realizable` — 交叉序列存在圆周相邻的连续步骤（仅 n=4 会触发），
  返回相邻步骤对；
- `round_interval_infeasible` — 某轮允许区间 `[目标×(1±偏差)]` 与工具量程无交集，
  任何回传都无法合格，返回冲突轮次、允许区间与量程。

## 回传校验（POST /procedures/{id}/reports）

回传写明工具、操作者、时刻与实测扭矩。以下情形**拒绝推进、记录异常并指出涉事螺栓**：

| reason | 含义 |
|---|---|
| `calibration_expired` | 回传时刻晚于校准有效期 |
| `tool_out_of_range` | 实测扭矩超出工具量程 |
| `tool_mismatch` | 回传工具与批准版本锁定工具不符 |
| `out_of_sequence` | 跳步，返回期望栓号 |
| `adjacent_in_round` | 同轮连续紧固相邻螺栓（无豁免） |
| `torque_out_of_tolerance` | 扭矩超差 |
| `duplicate_in_round` | 同一螺栓本轮已有记录 |
| `not_started` / `not_in_progress` / `already_complete` / `archived` | 状态不允许 |

**中断恢复**：`GET /procedures/{id}/resume` 依据已完成位置给出剩余恢复序列。
**补拧**：回传带 `rework_of`（原记录 id），不推进顺序、不覆盖原记录，原记录永久保留。

## 版本与修订链

批准后参数锁定（PUT 仅草稿可用）；目标或工具变化须
`POST /procedures/{id}/derive` 派生新版本（version+1、parent 链接、变更说明入链）。

## 作业包与图示

- `GET /procedures/{id}/package` — JSON 作业包：计划、实测、异常、修订链
- `GET /procedures/{id}/diagram.svg` — 圆周示意：方位、完成轮次、下一栓、异常红圈、补拧 R 标记
- `samples/` — 正常、跳步、过期工具三个请求样例（见 `samples/README.md`）

## 代码结构

```
app/
  schemas.py     Pydantic 输入核验
  sequencing.py  交叉顺序与分轮计划
  rules.py       回传校验规则（纯函数）
  db.py          SQLite 模式与连接
  svg.py         圆周示意 SVG
  main.py        FastAPI 路由与状态机
tests/test_flow.py  全流程与各类拒绝场景
samples/            请求样例
```
