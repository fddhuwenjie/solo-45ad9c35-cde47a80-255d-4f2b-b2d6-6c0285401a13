"""圆周示意 SVG：螺栓方位、完成轮次、下一栓、异常、补拧、超声复核与轨迹复核标记。"""
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


def render_svg(proc: dict, plan: list[dict], records: list[dict],
               anomalies: list[dict], next_step: dict | None,
               measurement: dict | None = None,
               curve_review: dict | None = None) -> str:
    n = proc["bolt_count"]
    rounds = len(proc["stage_ratios"])
    cx, cy, r = 340.0, 400.0, 210.0

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

    # 底部缺陷区间清单行数决定画布高度
    width = 680.0
    height = 830.0 + 18.0 * len(defect_lines)

    header2 = (
        f'工具：{escape(proc["tool_id"])}（量程 {proc["tool_range_min"]}~'
        f'{proc["tool_range_max"]} N·m，校准至 {proc["calibration_valid_until"]}）　'
        f'垫片：{escape(proc["gasket"])}'
    )
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="sans-serif">',
        f'<rect width="{width:.0f}" height="{height:.0f}" fill="#f8f9fa"/>',
        f'<text x="{cx:.0f}" y="40" text-anchor="middle" font-size="20" font-weight="bold">'
        f'法兰紧固工艺 #{proc["id"]} v{proc["version"]} — {escape(proc["flange_class"])}</text>',
        f'<text x="{cx:.0f}" y="66" text-anchor="middle" font-size="14">'
        f'状态：{STATUS_LABEL.get(proc["status"], proc["status"])}　'
        f'目标扭矩：{proc["target_torque"]} N·m ±{proc["tolerance_pct"]}%　'
        f'分级：{escape(str(proc["stage_ratios"]))}</text>',
        f'<text x="{cx:.0f}" y="88" text-anchor="middle" font-size="14">{header2}</text>',
    ]
    if measurement:
        v = measurement["verdict"]
        band = measurement["target_load_band_kn"]
        parts.append(
            f'<text x="{cx:.0f}" y="110" text-anchor="middle" font-size="14" font-weight="bold">'
            f'超声伸长复核：批次 #{measurement["batch_id"]} 修订 r{measurement["revision"]}'
            f'（{escape(measurement["status"])}'
            + (f'，已确认 r{measurement["confirmed_revision"]}'
               if measurement["confirmed_revision"] else "")
            + f'）　目标预紧力 {band[0]}~{band[1]} kN　仪器 {escape(measurement["instrument_id"])}</text>')
        cv = v["dispersion_cv_pct"]
        imb = v["max_imbalance_pct"]
        parts.append(
            f'<text x="{cx:.0f}" y="132" text-anchor="middle" font-size="13" fill="#495057">'
            f'整圈离散度 CV：{"—" if cv is None else str(cv) + "%"}　'
            f'最大对径不平衡：{"—" if imb is None else str(imb) + "%"}'
            f'（限值 {v["imbalance_limit_pct"]}%）　'
            f'结论：{"已确认" if v["confirmed"] else "未确认（" + "、".join(v["blockers"]) + "）"}'
            f'</text>')
    if curve_review:
        total = len(curve_review["required_bolts"])
        verdict = ("通过" if curve_review["passed"]
                   else "未通过（" + "、".join(curve_review["blockers"]) + "）")
        parts.append(
            f'<text x="{cx:.0f}" y="154" text-anchor="middle" font-size="13" '
            f'font-weight="bold" fill="#0a9396">'
            f'扭矩-转角轨迹：可用 {curve_review["usable_count"]}/{total}　'
            f'离群率 {curve_review["outlier_rate_pct"]}%'
            f'（上限 {curve_review["max_outlier_rate_pct"]}%）　'
            f'复核结论：{escape(verdict)}</text>')
    parts.append(
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="none" '
        f'stroke="#adb5bd" stroke-width="1.5" stroke-dasharray="6 5"/>')

    for bolt in range(1, n + 1):
        x, y = _bolt_xy(bolt - 1, n, cx, cy, r, proc["start_angle_deg"], bool(proc["clockwise"]))
        done = rounds_done.get(bolt, 0)
        fill = _FILL_DONE if done >= rounds else (_FILL_PARTIAL if done > 0 else _FILL_PENDING)
        if bolt == next_bolt:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="24" fill="none" '
                         f'stroke="{_STROKE_NEXT}" stroke-width="3"/>')
        if bolt in anomaly_bolts:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="29" fill="none" '
                         f'stroke="{_STROKE_ANOMALY}" stroke-width="2" stroke-dasharray="4 3"/>')
        us = us_results.get(bolt)
        if us is not None:
            color = _STROKE_US_BAD if bolt in us_bad else _STROKE_US_OK
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="33" fill="none" '
                         f'stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="18" fill="{fill}" '
                     f'stroke="#343a40" stroke-width="1.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-size="12" '
                     f'fill="#6c757d">{order_of.get(bolt, "—")}</text>')
        lx, ly = _bolt_xy(bolt - 1, n, cx, cy, r + 44, proc["start_angle_deg"],
                          bool(proc["clockwise"]))
        parts.append(f'<text x="{lx:.1f}" y="{ly + 5:.1f}" text-anchor="middle" font-size="15" '
                     f'font-weight="bold" fill="#212529">{bolt}</text>')
        if done or bolt in rework_bolts:
            label = f'{done}/{rounds}' + ('R' if bolt in rework_bolts else '')
            parts.append(f'<text x="{x:.1f}" y="{y + 30:.1f}" text-anchor="middle" '
                         f'font-size="10" fill="#495057">{label}</text>')
        if us is not None:
            if us["load_kn"] is None:
                us_label = ("锁" if bolt in us_locked else "缺口")
                us_fill = "#6c757d" if bolt in us_locked else _STROKE_US_BAD
            else:
                us_label = f'{us["load_kn"]:.1f}kN'
                us_fill = _STROKE_US_BAD if bolt in us_bad else _STROKE_US_OK
            parts.append(f'<text x="{x:.1f}" y="{y - 40:.1f}" text-anchor="middle" '
                         f'font-size="10" font-weight="bold" fill="{us_fill}">{us_label}</text>')
        cv_bolt = curve_of.get(bolt)
        if cv_bolt is not None:
            color = _CURVE_COLORS[cv_bolt["state"]]
            parts.append(f'<circle cx="{x - 30:.1f}" cy="{y + 40:.1f}" r="4" fill="{color}"/>')
            if cv_bolt["state"] == "missing":
                cv_label = "无轨迹"
            else:
                angle = cv_bolt["post_snug_angle_deg"]
                angle_txt = "—" if angle is None else f"{angle:.1f}°"
                cv_label = (f'r{cv_bolt["revision"]}·{angle_txt}'
                            f'·#{cv_bolt["record_id"]}')
            parts.append(f'<text x="{x + 4:.1f}" y="{y + 44:.1f}" text-anchor="middle" '
                         f'font-size="9" fill="{color}">{escape(cv_label)}</text>')

    legend = [
        (_FILL_PENDING, "待紧固"), (_FILL_PARTIAL, "部分轮次"), (_FILL_DONE, "全部轮次"),
    ]
    x0 = 70.0
    for fill, label in legend:
        parts.append(f'<circle cx="{x0:.0f}" cy="724" r="10" fill="{fill}" stroke="#343a40"/>')
        parts.append(f'<text x="{x0 + 16:.0f}" y="729" font-size="13">{label}</text>')
        x0 += 100.0
    parts.append(f'<circle cx="{x0:.0f}" cy="724" r="12" fill="none" '
                 f'stroke="{_STROKE_NEXT}" stroke-width="3"/>')
    parts.append(f'<text x="{x0 + 18:.0f}" y="729" font-size="13">下一栓</text>')
    x0 += 92.0
    parts.append(f'<circle cx="{x0:.0f}" cy="724" r="13" fill="none" '
                 f'stroke="{_STROKE_ANOMALY}" stroke-width="2" stroke-dasharray="4 3"/>')
    parts.append(f'<text x="{x0 + 19:.0f}" y="729" font-size="13">扭矩异常</text>')
    if measurement:
        x0 += 104.0
        parts.append(f'<circle cx="{x0:.0f}" cy="724" r="13" fill="none" '
                     f'stroke="{_STROKE_US_OK}" stroke-width="2.5"/>')
        parts.append(f'<text x="{x0 + 19:.0f}" y="729" font-size="13">超声合格</text>')
        x0 += 92.0
        parts.append(f'<circle cx="{x0:.0f}" cy="724" r="13" fill="none" '
                     f'stroke="{_STROKE_US_BAD}" stroke-width="2.5"/>')
        parts.append(f'<text x="{x0 + 19:.0f}" y="729" font-size="13">超声缺口/超差</text>')
    if curve_review:
        x0 = 70.0
        for state, label in (("ok", "轨迹合格"), ("outlier", "轨迹离群"),
                             ("unusable", "轨迹不可用"), ("missing", "无轨迹")):
            parts.append(f'<circle cx="{x0:.0f}" cy="748" r="5" '
                         f'fill="{_CURVE_COLORS[state]}"/>')
            parts.append(f'<text x="{x0 + 10:.0f}" y="753" font-size="13">{label}</text>')
            x0 += 118.0
        parts.append(f'<text x="{x0 + 6:.0f}" y="753" font-size="11" fill="#868e96">'
                     f'栓下标注：采用修订 r·贴合后转角·关联记录#id</text>')
    parts.append(f'<text x="{cx:.0f}" y="776" text-anchor="middle" font-size="11" '
                 f'fill="#868e96">圆点内数字为轮内紧固次序；外侧粗体为螺栓编号；'
                 f'螺栓旁为超声换算预紧力（缺口仅留痕，不判合格）</text>')
    parts.append(f'<text x="{cx:.0f}" y="794" text-anchor="middle" font-size="11" '
                 f'fill="#868e96">本图与 JSON 作业包引用同一测量版本'
                 + (f'：批次 #{measurement["batch_id"]} r{measurement["revision"]}'
                    if measurement else "（尚无测量批次）")
                 + '；轨迹标注为当前采用修订</text>')
    if defect_lines:
        parts.append(f'<text x="70" y="820" font-size="12" font-weight="bold" '
                     f'fill="{_CURVE_COLORS["unusable"]}">轨迹缺陷区间（不得进入复核结论）：</text>')
        for i, line in enumerate(defect_lines):
            parts.append(f'<text x="86" y="{838 + 18 * i}" font-size="11" '
                         f'fill="#495057">{escape(line)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
