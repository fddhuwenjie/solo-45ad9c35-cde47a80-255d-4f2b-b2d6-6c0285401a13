"""圆周示意 SVG：螺栓方位、完成轮次、下一栓、异常、补拧、对中预检、超声与轨迹标记。"""
from __future__ import annotations

import math
from xml.sax.saxutils import escape

from .curve import format_defect

STATUS_LABEL = {
    "draft": "已创建(草稿)",
    "approved": "已批准",
    "in_progress": "进行中",
    "completed": "已完成待复核",
    "reviewed": "已复核",
    "archived": "已封存",
}

_FILL_PENDING = "#ffffff"
_FILL_PARTIAL = "#e9c46a"
_FILL_DONE = "#2a9d8f"
_STROKE_NEXT = "#0077b6"
_STROKE_ANOMALY = "#d62828"
_STROKE_US_OK = "#1b9e3e"       # 超声预紧力在目标带
_STROKE_US_BAD = "#e85d04"      # 超声证据缺口/超差/被排除
_STROKE_ALIGN_OK = "#264653"    # 对中预检通过（深青黑，与其他环区分）
_STROKE_ALIGN_BAD = "#d00000"   # 对中预检证据缺口/阻断
_CURVE_COLORS = {
    "ok": "#0a9396",            # 轨迹可用且贴合后转角在批准范围
    "outlier": "#9b5de5",       # 轨迹可用但离群（计入整圈离群率）
    "unusable": "#d62828",      # 轨迹存在缺陷，不得进入复核结论
    "missing": "#6c757d",       # 尚无轨迹
}


def _bolt_xy(index: int, n: int, cx: float, cy: float, r: float,
             start_angle_deg: float, clockwise: bool) -> tuple[float, float]:
    theta = math.radians(start_angle_deg + index * 360.0 / n)
    sgn = 1.0 if clockwise else -1.0
    return cx + sgn * r * math.sin(theta), cy - r * math.cos(theta)


def _angle_xy(angle_deg: float, cx: float, cy: float, radius: float) -> tuple[float, float]:
    """测点方位（0=正上方，顺时针为正）对应的圆周坐标。"""
    theta = math.radians(angle_deg)
    return cx + radius * math.sin(theta), cy - radius * math.cos(theta)


def render_svg(proc: dict, plan: list[dict], records: list[dict],
               anomalies: list[dict], next_step: dict | None,
               measurement: dict | None = None,
               curve_review: dict | None = None,
               alignment: dict | None = None) -> str:
    n = proc["bolt_count"]
    rounds = len(proc["stage_ratios"])
    cx, cy, r = 340.0, 410.0, 210.0

    rounds_done: dict[int, int] = {}
    rework_bolts: set[int] = set()
    for rec in records:
        if rec["rework_of"] is None:
            rounds_done[rec["bolt_no"]] = rounds_done.get(rec["bolt_no"], 0) + 1
        else:
            rework_bolts.add(rec["bolt_no"])
    anomaly_bolts = {a["bolt_no"] for a in anomalies if a["bolt_no"] is not None}
    order_of = {s["bolt_no"]: s["order_in_round"] for s in plan if s["round_no"] == 1}
    next_bolt = next_step["bolt_no"] if next_step else None

    us_results: dict[int, dict] = {}
    us_bad: set[int] = set()
    us_locked: set[int] = set()
    if measurement:
        for b in measurement["verdict"]["bolts"]:
            us_results[b["bolt_no"]] = b
            if b["gaps"] or not b["in_target_band"]:
                us_bad.add(b["bolt_no"])
            if b.get("locked"):
                us_locked.add(b["bolt_no"])

    # 对中预检采用版本（与作业包同一版本与测点）
    align_ok = False
    align_metrics = None
    align_gap_angles: set[float] = set()
    align_block = None
    if alignment:
        a = alignment["analysis"]
        align_ok = a["passed"]
        align_metrics = a["metrics"]
        align_gap_angles = {g["angle_deg"] for g in a["evidence_gaps"]
                            if g["angle_deg"] is not None}
        align_block = next((b for b in a["blockers"]
                            if b["reason"] == "forced_pull_required"), None)

    curve_of: dict[int, dict] = {}
    defect_lines: list[str] = []
    if curve_review:
        curve_of = {b["bolt_no"]: b for b in curve_review["bolts"]}
        for b in curve_review["bolts"]:
            if b["state"] != "unusable":
                continue
            for d in b["defects"][:2]:
                defect_lines.append(f'栓{b["bolt_no"]}（采用 r{b["revision"]}）：'
                                    f'{format_defect(d)}')
        if len(defect_lines) > 6:
            defect_lines = defect_lines[:6] + ["……其余缺陷区间见 JSON 作业包"]

    # 底部清单行数决定画布高度（图例/说明三行 + 缺陷清单）
    width = 680.0
    height = 866.0 + 18.0 * len(defect_lines)

    header2 = (
        f'工具：{escape(proc["tool_id"])}（量程 {proc["tool_range_min"]}~'
        f'{proc["tool_range_max"]} N·m，校准至 {proc["calibration_valid_until"]}）　'
        f'垫片：{escape(proc["gasket"])}'
    )
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="sans-serif">',
        f'<rect width="{width:.0f}" height="{height:.0f}" fill="#f8f9fa"/>',
        f'<text x="{cx:.0f}" y="36" text-anchor="middle" font-size="20" font-weight="bold">'
        f'法兰紧固工艺 #{proc["id"]} v{proc["version"]} — {escape(proc["flange_class"])}</text>',
        f'<text x="{cx:.0f}" y="60" text-anchor="middle" font-size="14">'
        f'状态：{STATUS_LABEL.get(proc["status"], proc["status"])}　'
        f'目标扭矩：{proc["target_torque"]} N·m ±{proc["tolerance_pct"]}%　'
        f'分级：{escape(str(proc["stage_ratios"]))}</text>',
        f'<text x="{cx:.0f}" y="82" text-anchor="middle" font-size="14">{header2}</text>',
    ]
    y = 106
    if alignment:
        a = alignment["analysis"]
        m = align_metrics
        verdict_txt = "通过" if align_ok else "未通过"
        color = "#1b6b37" if align_ok else _STROKE_ALIGN_BAD
        parts.append(
            f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="14" font-weight="bold" '
            f'fill="{color}">装配对中预检：v{alignment["version"]}（{verdict_txt}，'
            f'{a["point_count"]} 测点）　'
            + (escape(alignment["adjustment_reason"] or "首版")) + "</text>")
        y += 20
        if m:
            parts.append(
                f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="12" fill="#343a40">'
                f'间隙 {m["gap_min_mm"]}~{m["gap_max_mm"]} mm　'
                f'平行度 {m["parallelism_mm"]}（限值 {m["parallelism_limit_mm"]}）mm　'
                f'倾角 {m["tilt_deg"]}°@{m["tilt_azimuth_deg"]}　'
                f'径向错边 {m["radial_mismatch_mm"]}（TIR {m["radial_tir_mm"]}，'
                f'限值 {m["radial_limit_mm"]}）mm</text>')
            y += 18
            parts.append(
                f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="12" fill="#343a40">'
                f'垫片偏心 {m["gasket_eccentricity_mm"]} mm'
                + (f'@{m["gasket_azimuth_deg"]}°' if m["gasket_azimuth_deg"] is not None else "")
                + f'　流道侧居中余量 {m["gasket_inner_margin_mm"]} mm'
                  f'（≤0 即侵入流道）　法兰面侧余量 {m["gasket_outer_margin_mm"]} mm</text>')
            y += 18
        if not align_ok:
            problems = "、".join(
                [f'缺口 {g["reason"]}' + (f'@{g["angle_deg"]}°' if g["angle_deg"] is not None else "")
                 for g in a["evidence_gaps"][:4]]
                + [f'阻断 {b["reason"]}' for b in a["blockers"][:2]])
            parts.append(
                f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="12" '
                f'fill="{_STROKE_ALIGN_BAD}">{escape(problems)}</text>')
            y += 18
    if measurement:
        v = measurement["verdict"]
        band = measurement["target_load_band_kn"]
        parts.append(
            f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="13" font-weight="bold">'
            f'超声伸长复核：批次 #{measurement["batch_id"]} 修订 r{measurement["revision"]}'
            f'（{escape(measurement["status"])}'
            + (f'，已确认 r{measurement["confirmed_revision"]}'
               if measurement["confirmed_revision"] else "")
            + f'）　目标预紧力 {band[0]}~{band[1]} kN　仪器 {escape(measurement["instrument_id"])}</text>')
        y += 20
        cv = v["dispersion_cv_pct"]
        imb = v["max_imbalance_pct"]
        parts.append(
            f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="12" fill="#495057">'
            f'整圈离散度 CV：{"—" if cv is None else str(cv) + "%"}　'
            f'最大对径不平衡：{"—" if imb is None else str(imb) + "%"}'
            f'（限值 {v["imbalance_limit_pct"]}%）　'
            f'结论：{"已确认" if v["confirmed"] else "未确认（" + "、".join(v["blockers"]) + "）"}'
            f'</text>')
        y += 20
    if curve_review:
        total = len(curve_review["required_bolts"])
        verdict = ("通过" if curve_review["passed"]
                   else "未通过（" + "、".join(curve_review["blockers"]) + "）")
        parts.append(
            f'<text x="{cx:.0f}" y="{y}" text-anchor="middle" font-size="13" '
            f'font-weight="bold" fill="#0a9396">'
            f'扭矩-转角轨迹：可用 {curve_review["usable_count"]}/{total}　'
            f'离群率 {curve_review["outlier_rate_pct"]}%'
            f'（上限 {curve_review["max_outlier_rate_pct"]}%）　'
            f'复核结论：{escape(verdict)}</text>')
    parts.append(
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="none" '
        f'stroke="#adb5bd" stroke-width="1.5" stroke-dasharray="6 5"/>')

    # 对中测点：菱形按方位画在法兰圆外侧；相对倾斜/垫片偏心用半径线指示
    if alignment and align_metrics:
        m = align_metrics
        if m["tilt_azimuth_deg"] is not None:
            x2, y2 = _angle_xy(m["tilt_azimuth_deg"], cx, cy, r - 14)
            parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x2:.1f}" y2="{y2:.1f}" '
                         f'stroke="#adb5bd" stroke-width="1" stroke-dasharray="3 3"/>')
    if alignment:
        a = alignment["analysis"]
        blocked_angles = set()
        if align_block:
            blocked_angles = {ang for ang in align_block["detail"]
                              .get("bolts_not_free_at_angles_deg", [])}
        for p in a["points"]:
            x, yp = _angle_xy(p["angle_deg"], cx, cy, r + 16)
            bad = (p["angle_deg"] in align_gap_angles or p["angle_deg"] in blocked_angles
                   or not p["bolt_free_insertion"])
            color = _STROKE_ALIGN_BAD if bad else (_STROKE_ALIGN_OK if align_ok else "#e85d04")
            d = 6
            parts.append(
                f'<polygon points="{x:.1f},{yp - d:.1f} {x + d:.1f},{yp:.1f} '
                f'{x:.1f},{yp + d:.1f} {x - d:.1f},{yp:.1f}" fill="{color}" '
                f'stroke="#343a40" stroke-width="0.8"/>')

    for bolt in range(1, n + 1):
        x, yb = _bolt_xy(bolt - 1, n, cx, cy, r, proc["start_angle_deg"], bool(proc["clockwise"]))
        done = rounds_done.get(bolt, 0)
        fill = _FILL_DONE if done >= rounds else (_FILL_PARTIAL if done > 0 else _FILL_PENDING)
        if bolt == next_bolt:
            parts.append(f'<circle cx="{x:.1f}" cy="{yb:.1f}" r="24" fill="none" '
                         f'stroke="{_STROKE_NEXT}" stroke-width="3"/>')
        if bolt in anomaly_bolts:
            parts.append(f'<circle cx="{x:.1f}" cy="{yb:.1f}" r="29" fill="none" '
                         f'stroke="{_STROKE_ANOMALY}" stroke-width="2" stroke-dasharray="4 3"/>')
        us = us_results.get(bolt)
        if us is not None:
            color = _STROKE_US_BAD if bolt in us_bad else _STROKE_US_OK
            parts.append(f'<circle cx="{x:.1f}" cy="{yb:.1f}" r="33" fill="none" '
                         f'stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{yb:.1f}" r="18" fill="{fill}" '
                     f'stroke="#343a40" stroke-width="1.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{yb + 4:.1f}" text-anchor="middle" font-size="12" '
                     f'fill="#6c757d">{order_of.get(bolt, "—")}</text>')
        lx, ly = _bolt_xy(bolt - 1, n, cx, cy, r + 44, proc["start_angle_deg"],
                          bool(proc["clockwise"]))
        parts.append(f'<text x="{lx:.1f}" y="{ly + 5:.1f}" text-anchor="middle" font-size="15" '
                     f'font-weight="bold" fill="#212529">{bolt}</text>')
        if done or bolt in rework_bolts:
            label = f'{done}/{rounds}' + ('R' if bolt in rework_bolts else '')
            parts.append(f'<text x="{x:.1f}" y="{yb + 30:.1f}" text-anchor="middle" '
                         f'font-size="10" fill="#495057">{label}</text>')
        if us is not None:
            if us["load_kn"] is None:
                us_label = ("锁" if bolt in us_locked else "缺口")
                us_fill = "#6c757d" if bolt in us_locked else _STROKE_US_BAD
            else:
                us_label = f'{us["load_kn"]:.1f}kN'
                us_fill = _STROKE_US_BAD if bolt in us_bad else _STROKE_US_OK
            parts.append(f'<text x="{x:.1f}" y="{yb - 40:.1f}" text-anchor="middle" '
                         f'font-size="10" font-weight="bold" fill="{us_fill}">{us_label}</text>')
        cv_bolt = curve_of.get(bolt)
        if cv_bolt is not None:
            color = _CURVE_COLORS[cv_bolt["state"]]
            parts.append(f'<circle cx="{x - 30:.1f}" cy="{yb + 40:.1f}" r="4" fill="{color}"/>')
            if cv_bolt["state"] == "missing":
                cv_label = "无轨迹"
            else:
                angle = cv_bolt["post_snug_angle_deg"]
                angle_txt = "—" if angle is None else f"{angle:.1f}°"
                cv_label = (f'r{cv_bolt["revision"]}·{angle_txt}'
                            f'·#{cv_bolt["record_id"]}')
            parts.append(f'<text x="{x + 4:.1f}" y="{yb + 44:.1f}" text-anchor="middle" '
                         f'font-size="9" fill="{color}">{escape(cv_label)}</text>')

    legend_y = height - 142
    legend = [
        (_FILL_PENDING, "待紧固"), (_FILL_PARTIAL, "部分轮次"), (_FILL_DONE, "全部轮次"),
    ]
    x0 = 70.0
    for fill, label in legend:
        parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y}" r="10" fill="{fill}" stroke="#343a40"/>')
        parts.append(f'<text x="{x0 + 16:.0f}" y="{legend_y + 5}" font-size="13">{label}</text>')
        x0 += 100.0
    parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y}" r="12" fill="none" '
                 f'stroke="{_STROKE_NEXT}" stroke-width="3"/>')
    parts.append(f'<text x="{x0 + 18:.0f}" y="{legend_y + 5}" font-size="13">下一栓</text>')
    x0 += 92.0
    parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y}" r="13" fill="none" '
                 f'stroke="{_STROKE_ANOMALY}" stroke-width="2" stroke-dasharray="4 3"/>')
    parts.append(f'<text x="{x0 + 19:.0f}" y="{legend_y + 5}" font-size="13">扭矩异常</text>')
    x0 += 92.0
    if alignment:
        parts.append(f'<polygon points="{x0:.0f},{legend_y - 7:.1f} {x0 + 7:.0f},{legend_y:.0f} '
                     f'{x0:.0f},{legend_y + 7:.1f} {x0 - 7:.0f},{legend_y:.0f}" '
                     f'fill="{_STROKE_ALIGN_OK}" stroke="#343a40" stroke-width="0.8"/>')
        parts.append(f'<text x="{x0 + 13:.0f}" y="{legend_y + 5}" font-size="13">'
                     f'对中测点合格</text>')
        x0 += 110.0
        parts.append(f'<polygon points="{x0:.0f},{legend_y - 7:.1f} {x0 + 7:.0f},{legend_y:.0f} '
                     f'{x0:.0f},{legend_y + 7:.1f} {x0 - 7:.0f},{legend_y:.0f}" '
                     f'fill="{_STROKE_ALIGN_BAD}" stroke="#343a40" stroke-width="0.8"/>')
        parts.append(f'<text x="{x0 + 13:.0f}" y="{legend_y + 5}" font-size="13">'
                     f'测点缺口/螺栓未穿入</text>')
    if measurement:
        x0 = 70.0
        legend_y2 = legend_y + 24
        parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y2}" r="13" fill="none" '
                     f'stroke="{_STROKE_US_OK}" stroke-width="2.5"/>')
        parts.append(f'<text x="{x0 + 19:.0f}" y="{legend_y2 + 5}" font-size="13">超声合格</text>')
        x0 += 92.0
        parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y2}" r="13" fill="none" '
                     f'stroke="{_STROKE_US_BAD}" stroke-width="2.5"/>')
        parts.append(f'<text x="{x0 + 19:.0f}" y="{legend_y2 + 5}" font-size="13">超声缺口/超差</text>')
    if curve_review:
        legend_y3 = legend_y + (48 if measurement else 24)
        x0 = 70.0
        for state, label in (("ok", "轨迹合格"), ("outlier", "轨迹离群"),
                             ("unusable", "轨迹不可用"), ("missing", "无轨迹")):
            parts.append(f'<circle cx="{x0:.0f}" cy="{legend_y3}" r="5" '
                         f'fill="{_CURVE_COLORS[state]}"/>')
            parts.append(f'<text x="{x0 + 10:.0f}" y="{legend_y3 + 5}" font-size="13">{label}</text>')
            x0 += 118.0
        parts.append(f'<text x="{x0 + 6:.0f}" y="{legend_y3 + 5}" font-size="11" fill="#868e96">'
                     f'栓下标注：采用修订 r·贴合后转角·关联记录#id</text>')
    note_y = legend_y + (72 if (measurement or curve_review) else 24)
    parts.append(f'<text x="{cx:.0f}" y="{note_y}" text-anchor="middle" font-size="11" '
                 f'fill="#868e96">圆点内数字为轮内紧固次序；外侧粗体为螺栓编号；'
                 f'法兰圆外侧菱形为对中预检测点（红=缺口/螺栓不能自由穿入）；'
                 f'螺栓旁为超声换算预紧力</text>')
    parts.append(f'<text x="{cx:.0f}" y="{note_y + 18}" text-anchor="middle" font-size="11" '
                 f'fill="#868e96">本图与 JSON 作业包引用同一预检版本与测点'
                 + (f'：对中预检 v{alignment["version"]}' if alignment else "（尚无对中预检）")
                 + (f'、超声批次 #{measurement["batch_id"]} r{measurement["revision"]}'
                    if measurement else "")
                 + '；轨迹标注为当前采用修订</text>')
    if defect_lines:
        dy = note_y + 44
        parts.append(f'<text x="70" y="{dy}" font-size="12" font-weight="bold" '
                     f'fill="{_CURVE_COLORS["unusable"]}">轨迹缺陷区间（不得进入复核结论）：</text>')
        for i, line in enumerate(defect_lines):
            parts.append(f'<text x="86" y="{dy + 18 + 18 * i}" font-size="11" '
                         f'fill="#495057">{escape(line)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
