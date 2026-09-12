"""对角对称（十字交叉）紧固顺序与分轮计划生成。

规则：n 个螺栓均布圆周，编号 1..n。顺序按"先对角、再顺移一位"展开：
    1, 1+n/2, 2, 2+n/2, 3, 3+n/2, ...
保证 n >= 6 时同轮任意相邻两步在圆周上不相邻；n = 4 时数学上不可满足
（C4 补图无哈密顿路径），采用经典 1-3-2-4 并豁免相邻校验。
"""
from __future__ import annotations


def cross_sequence(n: int) -> list[int]:
    """返回 n 个螺栓的对角交叉紧固顺序（螺栓号 1..n）。"""
    if n < 4 or n % 2 != 0:
        raise ValueError("螺栓数量须为 >= 4 的偶数")
    seq: list[int] = []
    for k in range(n):
        pos = k // 2 if k % 2 == 0 else n // 2 + k // 2
        seq.append(pos + 1)
    return seq


def circular_distance(a: int, b: int, n: int) -> int:
    """螺栓 a、b 在圆周上的最短间隔（1 表示相邻）。"""
    d = abs(a - b) % n
    return min(d, n - d)


def build_plan(bolt_count: int, target_torque: float, stage_ratios: list[float]) -> list[dict]:
    """按分轮递增规则生成稳定计划：每轮所有螺栓按交叉顺序紧固到该轮比例。"""
    seq = cross_sequence(bolt_count)
    plan: list[dict] = []
    for round_no, ratio in enumerate(stage_ratios, start=1):
        for order_in_round, bolt_no in enumerate(seq, start=1):
            plan.append(
                {
                    "round_no": round_no,
                    "order_in_round": order_in_round,
                    "bolt_no": bolt_no,
                    "ratio": ratio,
                    "target_torque": round(target_torque * ratio, 2),
                }
            )
    return plan
