# 法兰螺栓紧固工艺管理服务

Python + FastAPI 接收请求，Pydantic 核验字段，工艺与执行版本写入 SQLite。
按**对角对称（十字交叉）**与**分轮递增**规则生成稳定的紧固顺序，覆盖
`创建 → 批准 → 开工 → 逐栓回传 → 复核 → 封存` 全流程。

## 运行

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload          # 默认库文件 ./flange.db
FLANGE_DB=/path/to.db .venv/bin/uvicorn app.main:app
.venv/bin/python -m pytest tests/ -q             # 139 个测试
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

## 受限栓位施工规划（现场障碍下的分轮排序）

现场脚手架、管托或扳手反力臂会挡住部分栓位，等分圆周的固定交叉序列可能排出
当班无法执行的步骤；临时跳栓又会破坏分轮受力。草稿阶段可为每颗螺栓登记
**实际方位、可操作时间窗、允许工具及套筒/反力臂所需角区**，规划器在分轮递增
框架内重排每轮顺序；未登记任何约束时仍按规则圆周展开
（`bolt_count`、`start_angle_deg`、`clockwise` 输入行为不变）。

`PUT /procedures/{id}/constraints`（仅 draft，整组替换、逐版留痕）：

| 字段 | 说明 |
|---|---|
| `shift_start` | 排程起点（当班开始时刻） |
| `min_separation_deg` | 同轮连续两步最小角间隔；缺省复现同轮非相邻规则（360/N） |
| `step_minutes` / `tool_change_minutes` | 单栓作业时长 / 换工具耗时（分钟） |
| `tool_windows` | 工具可用时段（工具被其他作业占用时）；未登记的工具全时段可用 |
| `bolts[].angle_deg` | 实际方位角；缺省按规则圆周展开 |
| `bolts[].windows` | 该栓可操作时间窗；空 = 全时段 |
| `bolts[].allowed_tools` | 允许工具列表；缺省 = 仅批准工具 |
| `bolts[].clearance_deg` | 套筒/反力臂所需角区半宽（连续两步角距须 ≥ 两栓角区之和） |

规划器约束（`app/planning.py`，纯函数）：每轮所有栓恰好出现一次
（**不得用跳过螺栓伪造可行方案**）；同轮连续两步角距 ≥ max（最小角间隔，
两栓角区之和）；轮内顺序用**带回溯的深度优先搜索**确定——对径优先只是
选序偏好，贪心走不通时回溯尝试其他候选，全部候选顺序都失败才判定无解；
时间/工具不可行时生成**等待**或**换工具**动作。无解返回 `plan_infeasible`
（首个冲突轮次、受阻栓位、最少需解除的限制；与顺序无关的时间窗/工具失效
优先于搜索死胡同诊断）。

- 批准时冻结计划 v1（`plan_revisions` 表）；草稿预览与批准门禁共用同一规划器，
  预览/批准不可行即 409；
- 批准后约束随计划冻结（PUT 返回 `constraints_locked`）；现场障碍或工具变化须
  `POST /procedures/{id}/plan-revisions` 从批准版派生修订——**已完成步骤原位锁定
  （时刻/工具/轮内次序不变），只重排未完成步骤**；无解即 409，当前冻结计划不变；
- 回传按计划顺序与**计划步骤锁定工具**校验（换工具步骤须用计划工具）；
  恢复序列、JSON 作业包（`planning` 字段）与圆周 SVG（实际方位、角区虚线弧、
  等待/换工具动作清单）读取同一冻结计划。

## 液压张拉执行（拉伸器分组同步加压）

大口径法兰用液压拉伸器张拉时，螺栓须分组同步加压：拉伸器数量不足、相邻机具
相撞或卸压载荷转移，都可能让泵压记录看似正常而最终预紧力失衡。在扭矩流程
之外增加**液压张拉执行模块**：方案与修订全部写入 SQLite，换算与校验为纯函数。

### 建案（从 approved 工艺）

`POST /procedures/{id}/tensioning-plans` 冻结以下参数（冻结后只能派生修订）：

| 字段 | 说明 |
|---|---|
| `area_mm2` / `length_mm` / `elastic_modulus_mpa` | 螺栓有效截面 / 有效长度 / 弹性模量 |
| `target_load_kn` / `load_tolerance_pct` | 目标预紧力与残余允许偏差 ±% |
| `tensioner_id` / `tensioner_count` | 拉伸器编号 / 可同时安装数量（栓组上限） |
| `hydraulic_area_mm2` / `max_pressure_mpa` / `max_stroke_mm` | 液压有效面积 / 能力上限 / 最大行程 |
| `min_tool_spacing` | 相邻机具最小栓位间隔（防相撞） |
| `load_transfer_coefficient` | 载荷转移系数 λ（残余 = 施加 × (1−λ)） |
| `min_hold_seconds` / `pressure_sync_tolerance_pct` | 最短保压 / 组内压力同步允差 % |
| `gauge_id` / `gauge_calibration_until` | 压力表编号与校准有效期（含当日） |
| `stage_ratios` | 分轮比例，严格递增且末级 1.0 |

**分轮换位方案**：每轮覆盖全部螺栓且每栓恰好一次；同组栓用同一泵源同步加压，
组内任意两栓圆周间隔 ≥ 最小机具间隔，组大小 ≤ 拉伸器数量。第 r 轮候选顺序取
交叉序列旋转 r 位后贪心分组——各轮组归属与执行次序不同（换位），卸压载荷转移
的影响在全周均布。每轮换算设定泵压 `p_set = ρ·F_target/(1−λ)/A_h` 与预测行程
`ΔL = F·L/(E·A)`；创建/批准/修订时逐轮预检，设定泵压超能力或预测行程超限即
409 `tensioning_infeasible`（任何回传都不可能合格）。

### 分组回传与确认

`POST /tensioning-plans/{id}/approve` 冻结批准快照（修订内容不可变）后，
`POST /tensioning-plans/{id}/round-reports` 按方案组序回传：各通道压力/活塞行程、
保压时段与卸压次序（须恰好覆盖本组）。服务换算逐栓施加载荷
`F = p·A_h` 与预测残余预紧力 `F_res = F·(1−λ)`。以下情形**拒绝推进、记录异常
（anomalies）并定位栓号与原始区间**：

| reason | 含义 |
|---|---|
| `out_of_sequence` | 跳组，返回期望组与栓号 |
| `coverage_conflict` | 通道与计划组不符（缺栓/多栓/重复，覆盖他组栓位） |
| `gauge_mismatch` / `calibration_expired` | 压力表不符 / 回传时刻晚于校准有效期 |
| `hold_insufficient` | 保压时段不足冻结下限 |
| `release_order_invalid` | 卸压次序未恰好覆盖本组 |
| `stroke_exceeded` | 活塞行程超最大行程（机具超行程） |
| `pressure_over_capacity` | 通道压力超拉伸器能力 |
| `pressure_out_of_sync` | 组内压力极差/均值超同步允差（泵压正常≠各栓受力一致） |
| `residual_out_of_tolerance` | 预测残余预紧力超当轮目标带 |

被拒回传不推进进度，该组整改后重新回传。`POST /tensioning-plans/{id}/confirm`
要求全部组回传完成且末轮逐栓残余预紧力落入目标带，否则 409 返回 `blockers`
（`incomplete_coverage` / `residual_out_of_tolerance`）与逐栓明细。

### 修订：人工改组、中断重排与采纳超声

- `POST /tensioning-plans/{id}/revisions` — 人工改组/参数变化：**必须说明理由**，
  派生新修订（revision+1，旧修订废止不覆盖）；**已完成组原位锁定，只重排未完成组**
  （贪心分组对已完成前缀稳定，同参数重排可确定复现剩余组）；空修订拒绝。
- `POST /tensioning-plans/{id}/adopt-ultrasonic` — 采用既有**已确认**超声批次的
  逐栓实测载荷作为残余预紧力证据：**必须说明理由**并派生修订，快照随修订冻结；
  确认时以实测值替代预测值（`evidence_source=ultrasonic`）。
- `GET /tensioning-plans/{id}/diff` — 与上一修订的差异：冻结参数、逐轮分组
  （锁定组原位保留）与超声采纳变化。

批准快照、版本差异与 `GET /procedures/{id}/package` 的 `tensioning` 字段
**共用同一张拉方案与结果**（同一详情视图：冻结参数、分轮换位方案、逐组回传
与确认评估）。

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
  schemas.py     Pydantic 输入核验（工艺 + 超声 + 对中预检 + 轨迹 + 现场约束）
  sequencing.py  交叉顺序与分轮计划（支持补拧锁定螺栓过滤）
  planning.py    受限栓位规划器：回溯搜索排序、等待/换工具动作、无解诊断（纯函数）
  alignment.py   间隙面/径向/垫片偏心最小二乘拟合、证据缺口、阻断项、版本差异（纯函数）
  rules.py       回传校验规则（纯函数）
  ultrasonic.py  时差->伸长->预紧力换算、证据缺口、离散度与对径不平衡（纯函数）
  db.py          SQLite 模式与连接（预检版本/测点、批次/基线/读数/缺口/补拧作业/现场约束/计划修订）
  svg.py         圆周示意 SVG（含对中预检层、超声复核层与施工计划动作）
  main.py        FastAPI 路由与状态机
tests/test_flow.py       扭矩全流程与各类拒绝场景
tests/test_alignment.py  对中拟合、证据缺口、阻断项、复测留痕、门禁、版本差异
tests/test_ultrasonic.py 超声复核、证据缺口、重测排除、补拧派生
tests/test_planning.py   受限栓位规划器回溯搜索、等待/换工具、无解诊断与计划修订
samples/                 请求样例
```
