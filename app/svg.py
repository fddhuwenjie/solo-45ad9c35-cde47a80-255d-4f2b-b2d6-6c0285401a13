"""圆周示意 SVG：螺栓方位、完成轮次、下一栓、异常与补拧标记。"""
from __future__ import annotations

import math
from xml.sax.saxutils import escape

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


def _bolt_xy(index: int, n: int, cx: float, cy: float, r: float,
             start_angle_deg: float, clockwise: bool) -> tuple[float, float]:
    theta = math.radians(start_angle_deg + index * 360.0 / n)
    sgn = 1.0 if clockwise else -1.0
    return cx + sgn * r * math.sin(theta), cy - r * math.cos(theta)


def render_svg(proc: dict, plan: list[dict], records: list[dict],
               anomalies: list[dict], next_step: dict | None) -> str:
    n = proc["bolt_count"]
    rounds = len(proc["stage_ratios"])
    cx, cy, r = 340.0, 400.0, 210.0
    width, height = 680.0, 780.0

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
        f'<text x="{cx:.0f}" y="88" text-anchor="middle" font-size="14">'
        f'工具：{escape(proc["tool_id"])}（量程 {proc["tool_range_min"]}~{proc["tool_range_max"]} N·m，'
        f'校准至 {proc["calibration_valid_until"]}）　垫片：{escape(proc["gasket"])}</text>',
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="none" '
        f'stroke="#adb5bd" stroke-width="1.5" stroke-dasharray="6 5"/>',
    ]

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
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="18" fill="{fill}" '
                     f'stroke="#343a40" stroke-width="1.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-size="12" '
                     f'fill="#6c757d">{order_of.get(bolt, "")}</text>')
        lx, ly = _bolt_xy(bolt - 1, n, cx, cy, r + 44, proc["start_angle_deg"],
                          bool(proc["clockwise"]))
        parts.append(f'<text x="{lx:.1f}" y="{ly + 5:.1f}" text-anchor="middle" font-size="15" '
                     f'font-weight="bold" fill="#212529">{bolt}</text>')
        if done or bolt in rework_bolts:
            label = f'{done}/{rounds}' + ('R' if bolt in rework_bolts else '')
            parts.append(f'<text x="{x:.1f}" y="{y + 30:.1f}" text-anchor="middle" '
                         f'font-size="10" fill="#495057">{label}</text>')

    legend = [
        (_FILL_PENDING, "待紧固"), (_FILL_PARTIAL, "部分轮次"), (_FILL_DONE, "全部轮次"),
    ]
    x0 = 90.0
    for fill, label in legend:
        parts.append(f'<circle cx="{x0:.0f}" cy="730" r="10" fill="{fill}" stroke="#343a40"/>')
        parts.append(f'<text x="{x0 + 16:.0f}" y="735" font-size="13">{label}</text>')
        x0 += 110.0
    parts.append(f'<circle cx="{x0:.0f}" cy="730" r="12" fill="none" '
                 f'stroke="{_STROKE_NEXT}" stroke-width="3"/>')
    parts.append(f'<text x="{x0 + 18:.0f}" y="735" font-size="13">下一栓</text>')
    x0 += 100.0
    parts.append(f'<circle cx="{x0:.0f}" cy="730" r="13" fill="none" '
                 f'stroke="{_STROKE_ANOMALY}" stroke-width="2" stroke-dasharray="4 3"/>')
    parts.append(f'<text x="{x0 + 19:.0f}" y="735" font-size="13">异常栓　(R=有补拧)</text>')
    parts.append(f'<text x="{cx:.0f}" y="765" text-anchor="middle" font-size="11" '
                 f'fill="#868e96">圆点内数字为轮内紧固次序；外侧粗体为螺栓编号</text>')
    parts.append("</svg>")
    return "\n".join(parts)
