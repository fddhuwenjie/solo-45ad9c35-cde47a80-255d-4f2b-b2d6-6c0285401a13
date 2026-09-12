# 请求样例

以下样例基于 `create_procedure.json` 创建的 8 栓工艺（目标 320 N·m，分级 30%/60%/100%，
交叉顺序为 1-5-2-6-3-7-4-8，校准有效期至 2026-12-31）。

```bash
# 1. 创建 -> 批准 -> 开工
curl -X POST localhost:8000/procedures -H 'Content-Type: application/json' \
     -d @samples/create_procedure.json          # 返回 {"procedure": {"id": 1, ...}, "plan": [...]}
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
