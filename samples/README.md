# 请求样例

以下样例基于 `create_procedure.json` 创建的 8 栓工艺（目标 320 N·m，分级 30%/60%/100%，
交叉顺序为 1-5-2-6-3-7-4-8，校准有效期至 2026-12-31）。

```bash
# 1. 创建 -> 对中预检 -> 批准 -> 开工
curl -X POST localhost:8000/procedures -H 'Content-Type: application/json' \
     -d @samples/create_procedure.json          # 返回 {"procedure": {"id": 1, ...}, "plan": [...]}
curl -X POST localhost:8000/procedures/1/alignment-checks \
     -H 'Content-Type: application/json' -d @samples/alignment_check.json
curl -X POST localhost:8000/procedures/1/approve
curl -X POST localhost:8000/procedures/1/start

# 2. 正常回传（第 1 轮第 1 栓，目标 320*0.3=96 N·m）-> 201
curl -X POST localhost:8000/procedures/1/reports -H 'Content-Type: application/json' \
     -d @samples/report_normal.json

# 3. 跳步：第 1 轮下一栓应为 5，实际回传 3 -> 409
#    {"detail": {"reason": "out_of_sequence", "bolt_no": 3, "expected_bolt_no": 5, ...}}
curl -X POST localhost:8000/procedures/1/reports -H 'Content-Type: application/json' \
     -d @samples/report_skip.json

# 4. 过期工具：回传时刻 2027-01-05 晚于校准有效期 2026-12-31 -> 409
#    {"detail": {"reason": "calibration_expired", "bolt_no": 5, ...}}
curl -X POST localhost:8000/procedures/1/reports -H 'Content-Type: application/json' \
     -d @samples/report_expired_tool.json
```

被拒绝的回传会写入 `anomalies` 并体现在 `GET /procedures/1/package` 与
`GET /procedures/1/diagram.svg`（红圈标记涉事螺栓）中。

## 超声伸长复核样例（终轮扭矩合格后直接核对预紧力）

基于同一工艺（须已 approved）。先建批冻结螺栓/材料/仪器参数，开工前逐栓交基线，
全部回传完成（completed）后逐栓交复测：

```bash
# 建批（approved 及以后）；冻结有效长度、截面积、E、声速温度系数、目标预紧力区间、
# 材料上限、对径不平衡限值与仪器校准 -> 201，返回 batch.id
curl -X POST localhost:8000/procedures/1/measurement-batches \
     -H 'Content-Type: application/json' -d @samples/measurement_batch.json

# 开工前逐栓基线（tof = 2·L/v0，本例 2·150mm/5900m/s ≈ 5.0847e-5 s）
curl -X POST localhost:8000/measurement-batches/1/baselines \
     -H 'Content-Type: application/json' -d @samples/measurement_baseline.json

# completed/reviewed 后复测（温度、操作者、时刻必填）
curl -X POST localhost:8000/measurement-batches/1/readings \
     -H 'Content-Type: application/json' -d @samples/measurement_reading.json

# 逐栓偏差/整圈离散度/对径不平衡；全部有效且达标 -> confirmed，否则 409 + blockers
curl -X POST localhost:8000/measurement-batches/1/confirm
curl localhost:8000/measurement-batches/1            # 完整复核明细

# 读数异常可重测（原值保留，修订号+1）或排除（须注明理由）
curl -X POST localhost:8000/measurement-batches/1/retests -H 'Content-Type: application/json' \
     -d '{"bolt_no":1,"tof_s":0.0000509457,"temperature_c":22,"operator":"赵六",
           "measured_at":"2026-09-12T12:00:00","reason":"耦合剂不均，重新耦合复测"}'

# 确认失败时派生补拧草稿：锁定合格螺栓，其余按交叉规则只做末轮
curl -X POST localhost:8000/measurement-batches/1/derive-rework
```

作业包 `GET /procedures/1/package` 的 `measurement` 字段与 SVG
（绿/橙环 + 每栓 kN 载荷）引用同一测量批次与修订号。

## 装配对中预检样例（批准/开工前）

两片法兰在螺栓尚未受力时若已被强行拉拢，终拧扭矩与超声预紧力都可能合格，
管口应力与垫片偏心却无记录。预检在批准前完成，首版不带调整原因：

```bash
# 首版（8 方位测点，冻结法兰面直径/垫片内外径/内孔直径/平行度与错边限值）
curl -X POST localhost:8000/procedures/1/alignment-checks \
     -H 'Content-Type: application/json' -d @samples/alignment_check.json

# 调整后复测：必须另存版本并注明调整原因，旧记录不覆盖
curl -X POST localhost:8000/procedures/1/alignment-checks \
     -H 'Content-Type: application/json' -d @samples/alignment_recheck.json

curl localhost:8000/procedures/1/alignment-checks      # 全部版本
curl localhost:8000/alignment-checks/2                 # v2 详情（冻结+测点+结论）
curl localhost:8000/alignment-checks/2/diff            # v1->v2 差异（按方位匹配测点）
```

作业包 `alignment` 字段与圆周 SVG（法兰圆外菱形测点、相对倾斜虚线、
平行度/错边/垫片偏心指标）引用同一采用版本（最新版）。

