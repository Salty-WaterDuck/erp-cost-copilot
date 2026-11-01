"""会计科目表与费用类型到科目的映射。

科目编码参照《企业会计准则》常用编码，保持和国内 ERP 的对话习惯一致，
不追求完整的总账体系 —— 只覆盖成本模块会碰到的那些科目。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    category: str  # ASSET / LIABILITY / EQUITY / COST / EXPENSE
    direction: str  # 余额方向：DEBIT 借 / CREDIT 贷

    @property
    def label(self) -> str:
        return f"{self.code} {self.name}"


ACCOUNTS: dict[str, Account] = {
    a.code: a
    for a in [
        Account("1002", "银行存款", "ASSET", "DEBIT"),
        Account("1403", "原材料", "ASSET", "DEBIT"),
        Account("1405", "库存商品", "ASSET", "DEBIT"),
        Account("1406", "自制半成品", "ASSET", "DEBIT"),
        Account("1602", "累计折旧", "ASSET", "CREDIT"),
        Account("2211", "应付职工薪酬", "LIABILITY", "CREDIT"),
        Account("4001", "实收资本", "EQUITY", "CREDIT"),
        Account("5001", "生产成本", "COST", "DEBIT"),
        Account("5101", "制造费用", "COST", "DEBIT"),
        Account("6602", "管理费用", "EXPENSE", "DEBIT"),
    ]
}

#: 制造费用发生额的资金来源科目。默认走银行存款（付现费用）。
EXPENSE_ACCOUNT: dict[str, str] = {
    "水电费": "1002",
    "设备折旧": "1602",
    "检测设备折旧": "1602",
    "车间管理薪酬": "2211",
    "辅料耗材": "1002",
    "检测耗材": "1002",
    "维修备件": "1002",
    "外委维修": "1002",
}

DEFAULT_EXPENSE_ACCOUNT = "1002"


def account(code: str) -> Account:
    try:
        return ACCOUNTS[code]
    except KeyError:
        raise KeyError(f"会计科目 {code} 不存在") from None


def expense_account(expense_type: str) -> str:
    return EXPENSE_ACCOUNT.get(expense_type, DEFAULT_EXPENSE_ACCOUNT)
