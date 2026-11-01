"""制造费用分配。

制造费用不能直接计入某个产品，必须先归集到成本中心（费用池），再按动因
分配到消耗了该成本中心资源的工单。默认动因是「实际人工工时」，也可以换成
机器工时或产量 —— 换动因只需要换 ``drivers`` 的构造方式，分配算法不变。

两个容易被忽略但很关键的点：

1. **尾差**：费用池 3,900 元按工时 38/27/40/14 分摊，四舍五入后合计可能
   差一分钱。用最大余额法保证分完正好等于费用池。
2. **未分配余额**：某成本中心发生了费用，但当期没有工单消耗它的动因
   （例如设备维护组），这笔钱不能悄悄丢掉，必须作为「未分配制造费用」
   暴露出来 —— 它是成本控制里最典型的漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Dataset
from .money import ZERO, allocate, q_money, q_unit, safe_div


@dataclass
class OverheadAllocation:
    period: str
    pools: dict[str, Decimal] = field(default_factory=dict)          # 成本中心 -> 费用池
    drivers: dict[tuple[str, str], Decimal] = field(default_factory=dict)  # (中心, 工单) -> 动因量
    driver_totals: dict[str, Decimal] = field(default_factory=dict)
    allocated: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    unallocated: dict[str, Decimal] = field(default_factory=dict)
    rates: dict[str, Decimal] = field(default_factory=dict)

    def for_order(self, order_no: str) -> dict[str, Decimal]:
        return {
            center: amount
            for (center, order), amount in self.allocated.items()
            if order == order_no and amount != 0
        }

    @property
    def unallocated_total(self) -> Decimal:
        return q_money(sum(self.unallocated.values(), ZERO))


def pool_by_center(ds: Dataset) -> dict[str, Decimal]:
    pools: dict[str, Decimal] = {}
    for item in ds.overhead:
        pools[item.cost_center] = pools.get(item.cost_center, ZERO) + item.amount
    return {k: q_money(v) for k, v in pools.items()}


def driver_by_order(ds: Dataset) -> dict[tuple[str, str], Decimal]:
    """动因量：默认用报工工时。"""
    drivers: dict[tuple[str, str], Decimal] = {}
    for report in ds.labor:
        key = (report.cost_center, report.order_no)
        drivers[key] = drivers.get(key, ZERO) + report.hours
    return drivers


def allocate_overhead(ds: Dataset) -> OverheadAllocation:
    pools = pool_by_center(ds)
    drivers = driver_by_order(ds)

    result = OverheadAllocation(period=ds.period, pools=pools, drivers=drivers)

    # 每个成本中心的动因合计
    for (center, _order), hours in drivers.items():
        result.driver_totals[center] = result.driver_totals.get(center, ZERO) + hours

    for center, pool in pools.items():
        if pool == 0:
            continue
        hours_total = result.driver_totals.get(center, ZERO)
        rate = safe_div(pool, hours_total)
        if rate is None:
            # 有费用、无动因：整笔挂账，交由控制规则告警。
            result.unallocated[center] = pool
            continue
        result.rates[center] = q_unit(rate)
        weights = {
            order: hours
            for (c, order), hours in drivers.items()
            if c == center and hours != 0
        }
        if not weights:
            result.unallocated[center] = pool
            continue
        parts = allocate(pool, list(weights.values()))
        for order, amount in zip(weights, parts):
            result.allocated[(center, order)] = amount

    return result
