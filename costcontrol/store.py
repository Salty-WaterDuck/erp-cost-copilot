"""SQLite 持久化：让一套成本数据活过多个月。

设计要点
--------
1. **金额与数量一律存 TEXT**。SQLite 的 REAL 是二进制浮点，0.1 存进去就不
   是 0.1 了；成本数据必须在数据库里也保持十进制精度。
2. **期间隔离**。业务单据按 ``period`` 分区，主数据（物料/BOM/工艺路线/
   成本中心）全局共享。加载某期时只取该期的单据。
3. **期间状态机**。期间默认 OPEN，结账后置为 CLOSED；CLOSED 期间不允许
   重新导入数据，避免「结完账又改单据」。
4. **一个事务写完一期**。导入中途失败不会留下半套数据。

暂不支持：主数据版本化（改 BOM 会影响历史期间）、跨期在产结转（下一版）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path

from .close import CarryForward
from .dataset import validate
from .models import (
    BomLine,
    CostCenter,
    Dataset,
    LaborReport,
    Material,
    MaterialIssue,
    MaterialType,
    OpeningWip,
    OrderStatus,
    OverheadActual,
    ProductionOrder,
    RoutingOp,
)
from .money import ZERO

SCHEMA_VERSION = "2"

PERIOD_TABLES = (
    "orders",
    "issues",
    "labor",
    "overhead",
    "opening_balances",
    "period_params",
    "wip_opening",
)

_CLOSE_PERIOD_SQL = (
    "UPDATE periods SET status = ?, closed_at = datetime('now','localtime') "
    "WHERE period = ?"
)
_REOPEN_PERIOD_SQL = "UPDATE periods SET status = ?, closed_at = NULL WHERE period = ?"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    period     TEXT PRIMARY KEY,
    company    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL,
    closed_at  TEXT
);

CREATE TABLE IF NOT EXISTS cost_centers (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    labor_rate      TEXT NOT NULL,
    overhead_budget TEXT NOT NULL,
    budget_hours    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    code           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    mtype          TEXT NOT NULL,
    uom            TEXT NOT NULL,
    standard_price TEXT NOT NULL,
    spec           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bom (
    parent     TEXT NOT NULL,
    component  TEXT NOT NULL,
    qty_per    TEXT NOT NULL,
    scrap_rate TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (parent, component)
);

CREATE TABLE IF NOT EXISTS routing (
    product     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    cost_center TEXT NOT NULL,
    std_hours   TEXT NOT NULL,
    operation   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (product, seq)
);

CREATE TABLE IF NOT EXISTS orders (
    period               TEXT NOT NULL,
    order_no             TEXT NOT NULL,
    product              TEXT NOT NULL,
    qty_planned          TEXT NOT NULL,
    qty_completed        TEXT NOT NULL,
    qty_scrapped         TEXT NOT NULL,
    wip_progress         TEXT NOT NULL,
    material_completion  TEXT NOT NULL,
    status               TEXT NOT NULL,
    start_date           TEXT,
    due_date             TEXT,
    carried_from         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (period, order_no)
);

CREATE TABLE IF NOT EXISTS wip_opening (
    period              TEXT NOT NULL,
    order_no            TEXT NOT NULL,
    product             TEXT NOT NULL,
    qty_planned         TEXT NOT NULL,
    qty_completed       TEXT NOT NULL,
    qty_in_wip          TEXT NOT NULL,
    material_completion TEXT NOT NULL,
    wip_progress        TEXT NOT NULL,
    cost_material       TEXT NOT NULL,
    cost_labor          TEXT NOT NULL,
    cost_oh             TEXT NOT NULL,
    carried_from        TEXT NOT NULL,
    PRIMARY KEY (period, order_no)
);

CREATE TABLE IF NOT EXISTS issues (
    period     TEXT NOT NULL,
    doc_no     TEXT NOT NULL,
    order_no   TEXT NOT NULL,
    material   TEXT NOT NULL,
    qty        TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    issue_date TEXT,
    is_return  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (period, doc_no)
);

CREATE TABLE IF NOT EXISTS labor (
    period      TEXT NOT NULL,
    doc_no      TEXT NOT NULL,
    order_no    TEXT NOT NULL,
    cost_center TEXT NOT NULL,
    hours       TEXT NOT NULL,
    hourly_rate TEXT,
    work_date   TEXT,
    operation   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (period, doc_no)
);

CREATE TABLE IF NOT EXISTS overhead (
    period       TEXT NOT NULL,
    doc_no       TEXT NOT NULL,
    cost_center  TEXT NOT NULL,
    expense_type TEXT NOT NULL,
    amount       TEXT NOT NULL,
    PRIMARY KEY (period, doc_no)
);

CREATE TABLE IF NOT EXISTS opening_balances (
    period  TEXT NOT NULL,
    account TEXT NOT NULL,
    amount  TEXT NOT NULL,
    PRIMARY KEY (period, account)
);

CREATE TABLE IF NOT EXISTS period_params (
    period TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (period, key)
);

CREATE INDEX IF NOT EXISTS idx_orders_period ON orders(period);
CREATE INDEX IF NOT EXISTS idx_issues_period ON issues(period);
CREATE INDEX IF NOT EXISTS idx_labor_period ON labor(period);
CREATE INDEX IF NOT EXISTS idx_overhead_period ON overhead(period);
"""


class PeriodStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class StoreError(RuntimeError):
    pass


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@dataclass(frozen=True)
class PeriodInfo:
    period: str
    company: str
    status: PeriodStatus
    created_at: str
    closed_at: str | None
    orders: int

    @property
    def is_open(self) -> bool:
        return self.status is PeriodStatus.OPEN


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn:
            self.conn.executescript(SCHEMA)
            self._migrate_columns()
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    def _migrate_columns(self) -> None:
        """把 v1 库平滑升到 v2：只加列，不动数据。"""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(orders)")
        }
        if "carried_from" not in columns:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN carried_from TEXT NOT NULL DEFAULT ''"
            )

    def _require_schema(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='periods'"
        ).fetchone()
        if row is None:
            raise StoreError(f"数据库未初始化：{self.path}（请先执行 --init）")
        version_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        version = int(version_row["value"]) if version_row else 0
        if version < int(SCHEMA_VERSION):
            raise StoreError(
                f"数据库结构版本 {version} 低于 {SCHEMA_VERSION}，请先执行 --init 升级"
            )

    # ------------------------------------------------------------------
    def save_period(self, ds: Dataset) -> PeriodInfo:
        """把一整套期间数据写入数据库；同一期间重复导入会整体覆盖。"""
        self._require_schema()
        existing = self.period_info(ds.period)
        if existing is not None and not existing.is_open:
            raise StoreError(f"期间 {ds.period} 已结账，不能覆盖")

        with self.conn:
            self.conn.execute(
                "INSERT INTO periods(period, company, status, created_at) "
                "VALUES(?, ?, 'OPEN', datetime('now','localtime')) "
                "ON CONFLICT(period) DO UPDATE SET company = excluded.company",
                (ds.period, ds.company),
            )
            self._write_master(ds)
            self._write_period_rows(ds)
        info = self.period_info(ds.period)
        assert info is not None
        return info

    def _write_master(self, ds: Dataset) -> None:
        # 主数据全局共享：整表替换，保证 BOM/工艺路线与导入文件一致。
        self.conn.execute("DELETE FROM bom")
        self.conn.execute("DELETE FROM routing")
        for material in ds.materials.values():
            self.conn.execute(
                "INSERT INTO materials(code, name, mtype, uom, standard_price, spec) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, mtype=excluded.mtype, "
                "uom=excluded.uom, standard_price=excluded.standard_price, spec=excluded.spec",
                (
                    material.code,
                    material.name,
                    material.mtype.value,
                    material.uom,
                    str(material.standard_price),
                    material.spec,
                ),
            )
        for center in ds.cost_centers.values():
            self.conn.execute(
                "INSERT INTO cost_centers(code, name, labor_rate, overhead_budget, budget_hours) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name, labor_rate=excluded.labor_rate, "
                "overhead_budget=excluded.overhead_budget, budget_hours=excluded.budget_hours",
                (
                    center.code,
                    center.name,
                    str(center.labor_rate),
                    str(center.overhead_budget),
                    str(center.budget_hours),
                ),
            )
        for parent, lines in ds.bom.items():
            for line in lines:
                self.conn.execute(
                    "INSERT INTO bom(parent, component, qty_per, scrap_rate, note) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (parent, line.component, str(line.qty_per), str(line.scrap_rate), line.note),
                )
        for product, ops in ds.routing.items():
            for op in ops:
                self.conn.execute(
                    "INSERT INTO routing(product, seq, cost_center, std_hours, operation) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (product, op.seq, op.cost_center, str(op.std_hours), op.operation),
                )

    def _write_period_rows(self, ds: Dataset) -> None:
        period = ds.period
        for table in ("orders", "issues", "labor", "overhead", "period_params"):
            self.conn.execute(f"DELETE FROM {table} WHERE period = ?", (period,))
        if ds.opening_balances:
            self.conn.execute(
                "DELETE FROM opening_balances WHERE period = ?", (period,)
            )
        if ds.wip_opening:
            self.conn.execute("DELETE FROM wip_opening WHERE period = ?", (period,))

        for order in ds.orders:
            self.conn.execute(
                "INSERT INTO orders(period, order_no, product, qty_planned, qty_completed, "
                "qty_scrapped, wip_progress, material_completion, status, start_date, "
                "due_date, carried_from) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    period,
                    order.order_no,
                    order.product,
                    str(order.qty_planned),
                    str(order.qty_completed),
                    str(order.qty_scrapped),
                    str(order.wip_progress),
                    str(order.material_completion),
                    order.status.value,
                    order.start_date.isoformat() if order.start_date else None,
                    order.due_date.isoformat() if order.due_date else None,
                    order.carried_from,
                ),
            )
        for issue in ds.issues:
            self.conn.execute(
                "INSERT INTO issues(period, doc_no, order_no, material, qty, unit_price, "
                "issue_date, is_return) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    period,
                    issue.doc_no,
                    issue.order_no,
                    issue.material,
                    str(issue.qty),
                    str(issue.unit_price),
                    issue.issue_date.isoformat() if issue.issue_date else None,
                    1 if issue.is_return else 0,
                ),
            )
        for report in ds.labor:
            self.conn.execute(
                "INSERT INTO labor(period, doc_no, order_no, cost_center, hours, hourly_rate, "
                "work_date, operation) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    period,
                    report.doc_no,
                    report.order_no,
                    report.cost_center,
                    str(report.hours),
                    str(report.hourly_rate) if report.hourly_rate is not None else None,
                    report.work_date.isoformat() if report.work_date else None,
                    report.operation,
                ),
            )
        for item in ds.overhead:
            self.conn.execute(
                "INSERT INTO overhead(period, doc_no, cost_center, expense_type, amount) "
                "VALUES(?, ?, ?, ?, ?)",
                (period, item.doc_no, item.cost_center, item.expense_type, str(item.amount)),
            )
        for code, amount in ds.opening_balances.items():
            self._insert_balance(period, code, amount)
        for opening in ds.wip_opening.values():
            self._insert_wip_opening(period, opening)
        for key, value in ds.params.items():
            self.conn.execute(
                "INSERT INTO period_params(period, key, value) VALUES(?, ?, ?)",
                (period, key, str(value)),
            )

    def _insert_balance(self, period: str, code: str, amount: Decimal) -> None:
        self.conn.execute(
            "INSERT INTO opening_balances(period, account, amount) VALUES(?, ?, ?)",
            (period, code, str(amount)),
        )

    def _insert_wip_opening(self, period: str, opening: OpeningWip) -> None:
        self.conn.execute(
            "INSERT INTO wip_opening(period, order_no, product, qty_planned, "
            "qty_completed, qty_in_wip, material_completion, wip_progress, "
            "cost_material, cost_labor, cost_oh, carried_from) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                period,
                opening.order_no,
                opening.product,
                str(opening.qty_planned),
                str(opening.qty_completed),
                str(opening.qty_in_wip),
                str(opening.material_completion),
                str(opening.wip_progress),
                str(opening.cost_material),
                str(opening.cost_labor),
                str(opening.cost_oh),
                opening.carried_from,
            ),
        )

    # ------------------------------------------------------------------
    def load_period(self, period: str) -> Dataset:
        self._require_schema()
        row = self.conn.execute(
            "SELECT * FROM periods WHERE period = ?", (period,)
        ).fetchone()
        if row is None:
            raise StoreError(f"期间 {period} 不存在")

        materials = {
            r["code"]: Material(
                code=r["code"],
                name=r["name"],
                mtype=MaterialType(r["mtype"]),
                uom=r["uom"],
                standard_price=_d(r["standard_price"]),
                spec=r["spec"],
            )
            for r in self.conn.execute("SELECT * FROM materials")
        }
        cost_centers = {
            r["code"]: CostCenter(
                code=r["code"],
                name=r["name"],
                labor_rate=_d(r["labor_rate"]),
                overhead_budget=_d(r["overhead_budget"]),
                budget_hours=_d(r["budget_hours"]),
            )
            for r in self.conn.execute("SELECT * FROM cost_centers")
        }
        bom: dict[str, list[BomLine]] = {}
        for r in self.conn.execute("SELECT * FROM bom ORDER BY parent, component"):
            bom.setdefault(r["parent"], []).append(
                BomLine(
                    parent=r["parent"],
                    component=r["component"],
                    qty_per=_d(r["qty_per"]),
                    scrap_rate=_d(r["scrap_rate"]),
                    note=r["note"],
                )
            )
        routing: dict[str, list[RoutingOp]] = {}
        for r in self.conn.execute("SELECT * FROM routing ORDER BY product, seq"):
            routing.setdefault(r["product"], []).append(
                RoutingOp(
                    product=r["product"],
                    seq=int(r["seq"]),
                    cost_center=r["cost_center"],
                    std_hours=_d(r["std_hours"]),
                    operation=r["operation"],
                )
            )

        orders = [
            ProductionOrder(
                order_no=r["order_no"],
                product=r["product"],
                qty_planned=_d(r["qty_planned"]),
                qty_completed=_d(r["qty_completed"]),
                qty_scrapped=_d(r["qty_scrapped"]),
                wip_progress=_d(r["wip_progress"]),
                material_completion=_d(r["material_completion"]),
                status=OrderStatus(r["status"]),
                start_date=_date(r["start_date"]),
                due_date=_date(r["due_date"]),
                carried_from=r["carried_from"],
            )
            for r in self.conn.execute(
                "SELECT * FROM orders WHERE period = ? ORDER BY order_no", (period,)
            )
        ]
        issues = [
            MaterialIssue(
                doc_no=r["doc_no"],
                order_no=r["order_no"],
                material=r["material"],
                qty=_d(r["qty"]),
                unit_price=_d(r["unit_price"]),
                issue_date=_date(r["issue_date"]),
                is_return=bool(r["is_return"]),
            )
            for r in self.conn.execute(
                "SELECT * FROM issues WHERE period = ? ORDER BY doc_no", (period,)
            )
        ]
        labor = [
            LaborReport(
                doc_no=r["doc_no"],
                order_no=r["order_no"],
                cost_center=r["cost_center"],
                hours=_d(r["hours"]),
                hourly_rate=_d(r["hourly_rate"]) if r["hourly_rate"] is not None else None,
                work_date=_date(r["work_date"]),
                operation=r["operation"],
            )
            for r in self.conn.execute(
                "SELECT * FROM labor WHERE period = ? ORDER BY doc_no", (period,)
            )
        ]
        overhead = [
            OverheadActual(
                cost_center=r["cost_center"],
                expense_type=r["expense_type"],
                amount=_d(r["amount"]),
                period=period,
                doc_no=r["doc_no"],
            )
            for r in self.conn.execute(
                "SELECT * FROM overhead WHERE period = ? ORDER BY doc_no", (period,)
            )
        ]
        opening = {
            r["account"]: _d(r["amount"])
            for r in self.conn.execute(
                "SELECT * FROM opening_balances WHERE period = ?", (period,)
            )
        }
        wip_opening = {
            r["order_no"]: OpeningWip(
                order_no=r["order_no"],
                product=r["product"],
                qty_planned=_d(r["qty_planned"]),
                qty_completed=_d(r["qty_completed"]),
                qty_in_wip=_d(r["qty_in_wip"]),
                material_completion=_d(r["material_completion"]),
                wip_progress=_d(r["wip_progress"]),
                cost_material=_d(r["cost_material"]),
                cost_labor=_d(r["cost_labor"]),
                cost_oh=_d(r["cost_oh"]),
                carried_from=r["carried_from"],
            )
            for r in self.conn.execute(
                "SELECT * FROM wip_opening WHERE period = ? ORDER BY order_no", (period,)
            )
        }
        params = {
            r["key"]: _d(r["value"])
            for r in self.conn.execute(
                "SELECT * FROM period_params WHERE period = ?", (period,)
            )
        }

        ds = Dataset(
            company=row["company"],
            period=period,
            cost_centers=cost_centers,
            materials=materials,
            bom=bom,
            routing=routing,
            orders=orders,
            issues=issues,
            labor=labor,
            overhead=overhead,
            params=params,
            opening_balances=opening,
            wip_opening=wip_opening,
        )
        validate(ds)
        return ds

    # ------------------------------------------------------------------
    def period_info(self, period: str) -> PeriodInfo | None:
        self._require_schema()
        row = self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM orders o WHERE o.period = p.period) AS orders "
            "FROM periods p WHERE p.period = ?",
            (period,),
        ).fetchone()
        return self._to_info(row) if row else None

    def list_periods(self) -> list[PeriodInfo]:
        self._require_schema()
        rows = self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM orders o WHERE o.period = p.period) AS orders "
            "FROM periods p ORDER BY p.period"
        )
        return [self._to_info(row) for row in rows]

    @staticmethod
    def _to_info(row: sqlite3.Row) -> PeriodInfo:
        return PeriodInfo(
            period=row["period"],
            company=row["company"],
            status=PeriodStatus(row["status"]),
            created_at=row["created_at"],
            closed_at=row["closed_at"],
            orders=int(row["orders"]),
        )

    def set_status(self, period: str, status: PeriodStatus) -> PeriodInfo:
        self._require_schema()
        info = self.period_info(period)
        if info is None:
            raise StoreError(f"期间 {period} 不存在")
        with self.conn:
            if status is PeriodStatus.CLOSED:
                self.conn.execute(_CLOSE_PERIOD_SQL, (status.value, period))
            else:
                self.conn.execute(_REOPEN_PERIOD_SQL, (status.value, period))
        info = self.period_info(period)
        assert info is not None
        return info

    def apply_carry(self, carry: CarryForward) -> None:
        self._require_schema()
        with self.conn:
            self._apply_carry_rows(carry)

    def _apply_carry_rows(self, carry: CarryForward) -> None:
        row = self.conn.execute(
            "SELECT company FROM periods WHERE period = ?", (carry.source_period,)
        ).fetchone()
        company = row["company"] if row else ""
        self.conn.execute(
            "INSERT INTO periods(period, company, status, created_at) "
            "VALUES(?, ?, 'OPEN', datetime('now','localtime')) "
            "ON CONFLICT(period) DO UPDATE SET company = excluded.company",
            (carry.period, company),
        )
        self.conn.execute(
            "DELETE FROM wip_opening WHERE period = ?", (carry.period,)
        )
        self.conn.execute(
            "DELETE FROM opening_balances WHERE period = ?", (carry.period,)
        )
        for opening in carry.wip:
            # 下期工单脚手架：计划数 = 剩余量，完工为 0，等单据导入后覆盖
            self.conn.execute(
                "INSERT INTO orders(period, order_no, product, qty_planned, "
                "qty_completed, qty_scrapped, wip_progress, material_completion, "
                "status, start_date, due_date, carried_from) "
                "VALUES(?, ?, ?, ?, '0', '0', ?, ?, 'IN_PROGRESS', NULL, NULL, ?) "
                "ON CONFLICT(period, order_no) DO NOTHING",
                (
                    carry.period,
                    opening.order_no,
                    opening.product,
                    str(opening.qty_planned),
                    str(opening.wip_progress),
                    str(opening.material_completion),
                    opening.carried_from,
                ),
            )
            self._insert_wip_opening(carry.period, opening)
        for code, amount in carry.balances.items():
            self._insert_balance(carry.period, code, amount)
        self.conn.execute(
            "INSERT INTO period_params(period, key, value) "
            "SELECT ?, key, value FROM period_params WHERE period = ? "
            "ON CONFLICT(period, key) DO UPDATE SET value = excluded.value",
            (carry.period, carry.source_period),
        )

    def close_with_carry(self, period: str, carry: CarryForward) -> PeriodInfo:
        """关账 + 写下一期期初，放在同一个事务里。"""
        self._require_schema()
        with self.conn:
            self.conn.execute(
                _CLOSE_PERIOD_SQL, (PeriodStatus.CLOSED.value, period)
            )
            self._apply_carry_rows(carry)
        info = self.period_info(period)
        assert info is not None
        return info

    def delete_period(self, period: str) -> None:
        self._require_schema()
        info = self.period_info(period)
        if info is None:
            raise StoreError(f"期间 {period} 不存在")
        if not info.is_open:
            raise StoreError(f"期间 {period} 已结账，不能删除")
        with self.conn:
            for table in PERIOD_TABLES:
                self.conn.execute(f"DELETE FROM {table} WHERE period = ?", (period,))
            self.conn.execute("DELETE FROM periods WHERE period = ?", (period,))


__all__ = ["PeriodInfo", "PeriodStatus", "Store", "StoreError"]
