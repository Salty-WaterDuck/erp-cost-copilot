"""成本计算运行编排：一次「期末成本计算」的完整流程。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .allocation import OverheadAllocation, allocate_overhead
from .control import ControlThresholds, evaluate
from .costing import CostingResult, cost_orders, summarize_products
from .models import CostSheet, Dataset
from .standard import StandardCosts


@dataclass
class CostRun:
    dataset: Dataset
    standard: StandardCosts
    overhead: OverheadAllocation
    costing: CostingResult
    sheet: CostSheet

    @property
    def warnings(self) -> list[str]:
        return self.standard.warnings

    @property
    def unallocated_overhead(self) -> Decimal:
        return self.overhead.unallocated_total


def run_cost(
    ds: Dataset,
    params: dict[str, Decimal] | None = None,
    prefer_master_price: bool = False,
) -> CostRun:
    merged = dict(ds.params)
    if params:
        merged.update(params)

    standard = StandardCosts(ds, prefer_master_price=prefer_master_price)
    overhead = allocate_overhead(ds)
    costing = cost_orders(ds, standard, overhead)
    thresholds = ControlThresholds.from_params(merged)
    alerts = evaluate(ds, costing.orders, overhead, thresholds)
    products = summarize_products(ds, costing.orders, standard)

    sheet = CostSheet(
        company=ds.company,
        period=ds.period,
        orders=costing.orders,
        products=products,
        alerts=alerts,
    )
    return CostRun(
        dataset=ds,
        standard=standard,
        overhead=overhead,
        costing=costing,
        sheet=sheet,
    )
