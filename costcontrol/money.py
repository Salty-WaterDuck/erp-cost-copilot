"""金额与数量的安全计算。

成本核算里最忌讳浮点误差：0.1 + 0.2 != 0.3 会让成本计算单永远无法平账。
本模块统一用 Decimal，并把三个精度层级固定下来：

* 金额（元）：2 位小数
* 数量：4 位小数（半成品、公斤、工时都可能带小数）
* 单价 / 费率：4 位小数

分配尾差用「最大余额法」处理：先按权重取整到分，再把剩余的分钱按小数
部分从大到小补齐，保证「分出去的合计 == 待分配总额」，一分不多一分不少。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Sequence

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.0001")
UNIT_STEP = Decimal("0.0001")
ZERO = Decimal("0")

CENT_INT = 100


def D(value: object) -> Decimal:
    """把 int/float/str/Decimal 统一转成 Decimal（浮点先转字符串）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def q_money(value: Decimal | int | str) -> Decimal:
    """金额取整到分，四舍五入。"""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def q_qty(value: Decimal | int | str) -> Decimal:
    """数量取整到 4 位小数。"""
    return D(value).quantize(QTY_STEP, rounding=ROUND_HALF_UP)


def q_unit(value: Decimal | int | str) -> Decimal:
    """单价 / 费率取整到 4 位小数。"""
    return D(value).quantize(UNIT_STEP, rounding=ROUND_HALF_UP)


def safe_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """分母为 0 时返回 None，而不是抛异常或算出 Infinity。"""
    if denominator == 0:
        return None
    return numerator / denominator


def allocate(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """按权重把 total 精确拆分，保证 sum(结果) == total。

    用法：把 45_000 元制造费用按 4 张工单的工时 38/27/40/14 分摊。
    权重必须非负且合计大于 0；total 会被取整到分。
    """
    if not weights:
        raise ValueError("权重列表不能为空")
    if any(w < 0 for w in weights):
        raise ValueError("权重不能为负数")
    if total < 0:
        raise ValueError("待分配总额不能为负数")
    total = q_money(total)
    base = sum((D(w) for w in weights), ZERO)
    if base == 0:
        raise ZeroDivisionError("权重合计为 0，无法分配")

    raw = [total * D(w) / base for w in weights]
    floors = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]
    residual = total - sum(floors, ZERO)
    leftover = int((residual / CENT).to_integral_value(rounding=ROUND_HALF_UP))

    # 小数部分大的先补一分；并列时按下标先后，保证结果可复现。
    order = sorted(
        range(len(raw)),
        key=lambda i: (-(raw[i] - floors[i]), i),
    )
    result = list(floors)
    for i in order[:leftover]:
        result[i] += CENT
    return result


def allocate_to_dict(
    total: Decimal, weights: dict[str, Decimal]
) -> dict[str, Decimal]:
    """按键名字典分配，便于按工单 / 成本中心回填。"""
    keys = list(weights)
    parts = allocate(total, [weights[k] for k in keys])
    return dict(zip(keys, parts))


def pct(part: Decimal, whole: Decimal) -> Decimal | None:
    """百分比，保留 2 位；分母为 0 返回 None。"""
    if whole == 0:
        return None
    return (part / whole * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
