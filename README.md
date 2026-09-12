# 法兰螺栓紧固工艺管理服务

Python + FastAPI 接收请求，Pydantic 核验字段，工艺与执行版本写入 SQLite。
按**对角对称（十字交叉）**与**分轮递增**规则生成稳定的紧固顺序，覆盖
`创建 → 批准 → 开工 → 逐栓回传 → 复核 → 封存` 全流程。

## 运行

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload          # 默认库文件 ./flange.db
FLANGE_DB=/path/to.db .venv/bin/uvicorn app.main:app
.venv/bin/python -m pytest tests/ -q             # 123 个测试
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

- `alignment_check_missing` / `alignment_check_not_passed` — 缺对中预检，
  或采用的预检版本未通过（证据缺口/阻断项，见下节）；
- `sequence_not_realizable` — 交叉序列存在圆周相邻的连续步骤（仅 n=4 会触发），
  返回相邻步骤对；
- `round_interval_infeasible` — 某轮允许区间 `[目标×(1±偏差)]` 与工具量程无交集，
  任何回传都无法合格，返回冲突轮次、允许区间与量程。

## 装配对中预检（紧固前自由状态）

两片法兰在螺栓尚未受力时若已被强行拉拢，终拧扭矩和超声预紧力都可能合格，
管口附加应力、垫片偏心却不会从既有记录中暴露。因此在批准/开工门禁前增加
**装配对中预检**：几何与限值逐版冻结写入 SQLite，计算与路由沿用 Python + FastAPI。

`POST /procedures/{id}/alignment-checks`（仅 draft/approved，即螺栓受力拉拢之前）
冻结以下参数（冻结后只能另存新版本）：

| 字段 | 说明 |
|---|---|
| `flange_face_diameter_mm` | 法兰面（密封面）外径 D |
| `gasket_inner_diameter_mm` / `gasket_outer_diameter_mm` | 垫片内径 Gi / 外径 Go |
| `bore_diameter_mm` | 法兰内孔（流道）直径 Db |
| `max_parallelism_mm` | 平行度（最大−最小间隙）限值 |
| `max_radial_mismatch_mm` | 径向错边限值 |

每版接收 **≥4 个按方位分布**的测点（`angle_deg` 0=正上方、顺时针为正，
与编号方位一致），每点含 `axial_gap`（轴向间隙）、`radial_offset`
（径向偏移，沿 u 方向带符号）、`gasket_edge_position`（自法兰外缘向内量到
垫片外缘）、`bolt_free_insertion`（螺栓能否在法兰不受力时自由穿入），
线值单位按点声明（`mm`/`cm`/`m`/`in`，默认 mm）。

服务最小二乘拟合两法兰面相对倾斜 `gap=c+a·u`、径向偏移向量与垫片偏心，
输出最大/最小间隙（拟合全周极值）、平行度、倾角及方位、径向错边与跳动 TIR、
垫片偏心/方位与**流道侧、法兰面侧全圆周最坏居中余量**
（`(Gi−Db)/2−|g|`、`(D−Go)/2−|g|`，按**拟合偏心量**计算，不按离散测点极值——
最坏方位可能落在相邻测点之间）。垫片边缘拟合带常数项，"垫片整体偏小"的
同心尺寸偏差（`gasket_sizing_offset_mm`）与平移向量分离，不误判为偏心。

**证据缺口（只列证据，不产出指标，更不判合格）**——版本照常落库（201），
但 `evaluable=false`、`metrics=null`，阻止批准/开工：

| reason | 含义 |
|---|---|
| `duplicate_azimuth` | 测点方位重复（记录重复方位，非请求级拒绝） |
| `insufficient_arc_coverage` | 最大空弧 >180°，测点全落在半圆内 |
| `inconsistent_units` | 测点之间长度单位不一致 |
| `negative_axial_gap` | 轴向间隙为负（自由状态两面不应交叠） |
| `gasket_outside_face` | 垫片边缘越出法兰面 / 内缘越过对侧边 |
| `radial_offset_impossible` | 径向偏移绝对值超过法兰面半径 |

**阻断项**（证据充分但几何超限）：

| reason | 含义 |
|---|---|
| `parallelism_exceeded` | 平行度超冻结限值 |
| `radial_mismatch_exceeded` | 径向错边超冻结限值 |
| `gasket_intrusion` | 垫片内缘侵入流道（流道侧居中余量 <0） |
| `forced_pull_required` | 任一螺栓不能自由穿入，或拟合全周最小间隙 ≤0（已局部接触，须强行拉拢） |

**复测留痕**：第 2 版及以后必须填 `adjustment_reason`（注明调整原因），
旧版本永久保留不可覆盖；首版携带该字段返回 422。版本清单
`GET /procedures/{id}/alignment-checks`，版本详情 `GET /alignment-checks/{cid}`，
版本差异 `GET /alignment-checks/{cid}/diff`（冻结几何、按方位 ±5° 匹配的测点
变化与拟合指标变化）。作业包 `alignment` 字段与圆周 SVG 共用**同一采用版本
（最新版）与同一批测点**：图上法兰圆外菱形为测点（红=缺口/螺栓不能穿入），
虚线指示最大倾斜方位，并列出平行度、错边、垫片偏心与两侧余量。

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

`POST /measurement-batches/{bid}/derive-rework`：按复核结果选出待调整螺栓——
存在证据缺口、超出目标带的范围螺栓必选；**对径不平衡超限**的对中选载荷较小的
一栓（补拧只能增大预紧力，向对侧靠拢；若较小载荷栓已被继承锁定、扭矩补拧不可
修正，则返回 `rework_uncorrectable_imbalance`，须松退重紧）。锁定其余合格螺栓，
派生末轮（`[1.0]`）补拧新工艺版本，待补拧螺栓按**现有交叉规则**过滤后排序
（批准前同样做序列可实现性与量程预检；锁定栓回传以 `bolt_locked` 拒绝）。响应
`rework_reasons` 给出每栓入选原因。补拧工艺批准后按同一冻结参数建新批次：
范围仅补拧螺栓（仅这些栓需在补拧前交基线、补拧后交复测），锁定栓合格结果自动
继承并参与整圈离散度与对径不平衡评估。

`GET /procedures/{id}/package` 的 `measurement` 字段与
`GET /procedures/{id}/diagram.svg` 引用**同一测量版本**（已确认优先，否则当前
开放批次）；SVG 用绿/橙双环标出每栓超声结论与换算载荷（kN）。

## 版本与修订链

批准后参数锁定（PUT 仅草稿可用）；目标或工具变化须
`POST /procedures/{id}/derive` 派生新版本（version+1、parent 链接、变更说明入链）。

## 作业包与图示

- `GET /procedures/{id}/package` — JSON 作业包：计划、实测、异常、修订链、对中预检采用版本
- `GET /procedures/{id}/diagram.svg` — 圆周示意：对中测点与倾斜方位、螺栓、完成轮次、下一栓、异常红圈、补拧 R 标记
- `samples/` — 正常、跳步、过期工具与对中预检/复测请求样例（见 `samples/README.md`）

## 代码结构

```
app/
  schemas.py     Pydantic 输入核验（工艺 + 超声 + 对中预检 + 轨迹）
  sequencing.py  交叉顺序与分轮计划（支持补拧锁定螺栓过滤）
  alignment.py   间隙面/径向/垫片偏心最小二乘拟合、证据缺口、阻断项、版本差异（纯函数）
  rules.py       回传校验规则（纯函数）
  ultrasonic.py  时差->伸长->预紧力换算、证据缺口、离散度与对径不平衡（纯函数）
  db.py          SQLite 模式与连接（预检版本/测点、批次/基线/读数/缺口/补拧作业）
  svg.py         圆周示意 SVG（含对中预检层与超声复核层）
  main.py        FastAPI 路由与状态机
tests/test_flow.py       扭矩全流程与各类拒绝场景
tests/test_alignment.py  对中拟合、证据缺口、阻断项、复测留痕、门禁、版本差异
tests/test_ultrasonic.py 超声复核、证据缺口、重测排除、补拧派生
samples/                 请求样例
```
