# 法兰螺栓紧固工艺管理服务

Python + FastAPI 接收请求，Pydantic 核验字段，工艺与执行版本写入 SQLite。
按**对角对称（十字交叉）**与**分轮递增**规则生成稳定的紧固顺序，覆盖
`创建 → 批准 → 开工 → 逐栓回传 → 复核 → 封存` 全流程。

## 运行

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload          # 默认库文件 ./flange.db
FLANGE_DB=/path/to.db .venv/bin/uvicorn app.main:app
.venv/bin/python -m pytest tests/ -q             # 52 个测试
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

## 超声伸长复核（预紧力直接测量）

终轮扭矩合格并不保证预紧力一致——摩擦系数差异、垫片压缩不均会让同一圈螺栓
承受不同预紧力，单点扭矩记录无法体现这种偏载。因此在扭矩流程之外增加**超声伸长
复核**：测量批次与修订全部写入 SQLite，HTTP/契约仍为 FastAPI + Pydantic。

### 建批（从 approved 工艺）

`POST /procedures/{id}/measurement-batches` 冻结以下参数（冻结后只能另建新批次）：

| 字段 | 说明 |
|---|---|
| `length_mm` / `area_mm2` / `elastic_modulus_mpa` | 螺栓有效长度、应力截面积、弹性模量 E |
| `sound_velocity` / `temp_coefficient` / `reference_temp_c` | 参考声速 v0、声速温度系数 α、参考温度 |
| `temp_comp_min_c` / `temp_comp_max_c` | 温度补偿范围（超出即证据缺口） |
| `target_load_min_kn` / `target_load_max_kn` | 目标预紧力区间 |
| `material_load_limit_kn` | 材料允许载荷上限 |
| `max_imbalance_pct` | 对径不平衡限值 `|F_a−F_b|/均值` |
| `instrument_id` / `instrument_calibration_until` | 超声仪编号与校准有效期（含当日） |

### 测量时序

1. **开工前逐栓基线**：`POST /measurement-batches/{bid}/baselines`（栓号 + 基线飞行
   时间 tof）；窗口仅在 `approved`/`in_progress` 开放，每栓每批至多一条、不可覆盖。
2. 工艺 `completed` 或 `reviewed` 后提交**复测**：
   `POST /measurement-batches/{bid}/readings`（tof、温度、操作者、时刻）。
3. 换算：脉冲回波 `L = v·tof/2`；温度补偿 `v(t) = v0·(1+α·Δt)`；
   `ΔL = (v(t)·tof − v0·tof0)/2`（mm）；`F = E·A·ΔL/L_eff`。
4. 输出**逐栓偏差**（相对目标带中值）、**整圈离散度**（变异系数 CV）、
   **对径不平衡**（`k` 与 `k+n/2` 配对）。

### 证据缺口（只记录，绝不判合格）

以下情形读数照常落库（201），但标记为无效结果并写入 `measurement_gaps`，
批次**不能确认**：

| reason | 含义 |
|---|---|
| `baseline_missing` | 该栓缺基线飞行时间 |
| `calibration_expired` | 复测时刻晚于仪器校准有效期 |
| `temperature_out_of_range` | 温度超出冻结补偿范围 |
| `non_positive_elongation` | 伸长量非正（未承载/读数异常） |
| `load_over_material_limit` | 换算载荷超过材料上限 |
| `reading_excluded` / `reading_missing` | 读数被排除 / 从未复测 |

**确认** `POST /measurement-batches/{bid}/confirm`：全部螺栓均有有效结果、
全部落入目标预紧力区间且对径不平衡不超限；否则 409 返回 `blockers`
（`evidence_gaps` / `target_band_exceeded` / `diametral_imbalance`），批次保持开放。

**重测** `POST /measurement-batches/{bid}/retests`：须注明理由，原读数保留
（新记录 `supersedes` 指向原值），批次修订号 +1，取每栓最新读数。
**排除** `POST /measurement-batches/{bid}/exclusions`：注明理由后读数置排除位、
原值不删除，修订号 +1，须重测补证。

### 失败批次 → 补拧草稿

`POST /measurement-batches/{bid}/derive-rework`：锁定全部合格螺栓，派生末轮
（`[1.0]`）补拧新工艺版本，其余螺栓按**现有交叉规则**过滤后排序（批准前同样做
序列可实现性与量程预检；锁定栓回传以 `bolt_locked` 拒绝）。补拧工艺批准后按同一
冻结参数建新批次：范围仅补拧螺栓（仅这些栓需基线/复测），锁定栓合格结果自动继承
并参与整圈离散度与对径不平衡评估。

`GET /procedures/{id}/package` 的 `measurement` 字段与
`GET /procedures/{id}/diagram.svg` 引用**同一测量版本**（已确认优先，否则当前
开放批次）；SVG 用绿/橙双环标出每栓超声结论与换算载荷（kN）。

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
  schemas.py     Pydantic 输入核验（工艺 + 超声批次/基线/复测/重测/排除）
  sequencing.py  交叉顺序与分轮计划（支持补拧锁定螺栓过滤）
  rules.py       回传校验规则（纯函数）
  ultrasonic.py  时差->伸长->预紧力换算、证据缺口、离散度与对径不平衡（纯函数）
  db.py          SQLite 模式与连接（批次/基线/读数/缺口/补拧作业）
  svg.py         圆周示意 SVG（含超声复核层）
  main.py        FastAPI 路由与状态机
tests/test_flow.py       扭矩全流程与各类拒绝场景
tests/test_ultrasonic.py 超声复核、证据缺口、重测排除、补拧派生
samples/                 请求样例
```
