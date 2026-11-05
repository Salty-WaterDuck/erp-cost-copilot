"""costcontrol —— 面向组装型制造业的 ERP 成本控制引擎（纯标准库）。"""

from .accounting import build_journal
from .adapters import load_mapping, read_export, write_export
from .attribution import VarianceTracer
from .close import close_period
from .dataset import load_dataset, validate
from .diagnosis import diagnose_facts, render_diagnosis
from .facts import build_variance_facts
from .llm_explainer import LLMExplainer, load_sense, provider_from_env
from .models import (
    BomLine,
    CostCenter,
    CostSheet,
    Dataset,
    LaborReport,
    Material,
    MaterialIssue,
    MaterialType,
    OrderCost,
    OverheadActual,
    ProductionOrder,
    RoutingOp,
)
from .restoration import Restorer
from .responsibility import attribute, render_report
from .run import CostRun, run_cost
from .store import PeriodStatus, Store

__version__ = "0.17.0"

__all__ = [
    "load_dataset",
    "validate",
    "run_cost",
    "build_journal",
    "close_period",
    "Restorer",
    "VarianceTracer",
    "Store",
    "PeriodStatus",
    "LLMExplainer",
    "load_sense",
    "provider_from_env",
    "build_variance_facts",
    "diagnose_facts",
    "render_diagnosis",
    "attribute",
    "render_report",
    "load_mapping",
    "read_export",
    "write_export",
    "CostRun",
    "Dataset",
    "Material",
    "MaterialType",
    "BomLine",
    "CostCenter",
    "RoutingOp",
    "ProductionOrder",
    "MaterialIssue",
    "LaborReport",
    "OverheadActual",
    "OrderCost",
    "CostSheet",
    "__version__",
]
