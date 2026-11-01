"""只读 Web 报表：标准库 http.server 起一个本地成本看板。

不引入前端框架、不联网、不写数据库 —— 它只是把已经算好的 ``CostRun``
渲染成 HTML，供浏览器下钻查看：

    GET /                 总览：KPI、预警、工单成本、成本构成
    GET /order/<工单号>    单张工单的材料/人工/费用明细与差异分解
    GET /journal           记账凭证与试算平衡表
    GET /restore           成本还原与差异穿透
    GET /standard          标准成本卡
    GET /api/summary       KPI 的 JSON 版本

只监听 127.0.0.1，且只有 GET。
"""

from __future__ import annotations

import html
import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from .accounting import build_journal
from .attribution import LABELS, VarianceTracer
from .models import MaterialType, Severity
from .money import ZERO
from .restoration import Restorer
from .run import CostRun

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

SEVERITY = {
    Severity.CRITICAL: ("严重", "bad"),
    Severity.WARNING: ("警告", "warn"),
    Severity.INFO: ("提示", "info"),
}

CSS = """
:root{
  --bg:#f6f7f9;--card:#fff;--ink:#1f2933;--muted:#6b7280;--line:#e5e7eb;
  --accent:#2563eb;--bad:#dc2626;--warn:#d97706;--ok:#059669;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:#111827;color:#fff;padding:14px 24px;display:flex;
  align-items:center;gap:28px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.5px}
header .meta{color:#9ca3af;font-size:12px}
nav a{color:#cbd5e1;text-decoration:none;margin-right:16px;font-size:13px}
nav a:hover{color:#fff}
nav a.on{color:#fff;border-bottom:2px solid #60a5fa;padding-bottom:2px}
main{max-width:1180px;margin:24px auto 60px;padding:0 20px}
h2{font-size:15px;margin:28px 0 10px;color:#111827}
h2 .sub{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card .s{color:var(--muted);font-size:12px;margin-top:2px}
.card.bad .v{color:var(--bad)}
.card.good .v{color:var(--ok)}
.tbl{width:100%;border-collapse:collapse;background:var(--card);
  border:1px solid var(--line);border-radius:8px;overflow:hidden}
.tbl th,.tbl td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left}
.tbl th{background:#f9fafb;color:var(--muted);font-weight:500;font-size:12px}
.tbl tr:last-child td{border-bottom:0}
.tbl td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl tr.total td{font-weight:600;background:#f9fafb}
.tbl tr:hover td{background:#f9fbff}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;line-height:20px}
.badge.bad{background:#fee2e2;color:#b91c1c}
.badge.warn{background:#fef3c7;color:#b45309}
.badge.info{background:#dbeafe;color:#1d4ed8}
.alert{background:var(--card);border:1px solid var(--line);border-left-width:4px;
  border-radius:6px;padding:10px 14px;margin-bottom:8px}
.alert.bad{border-left-color:var(--bad)}
.alert.warn{border-left-color:var(--warn)}
.alert.info{border-left-color:var(--accent)}
.alert .rule{color:var(--muted);font-size:12px;margin-left:8px}
.mono{font-family:Consolas,"SFMono-Regular",Menlo,monospace}
.muted{color:var(--muted)}
.small{font-size:12px}
.bar{display:flex;height:22px;border-radius:6px;overflow:hidden;background:#e5e7eb;margin:8px 0}
.bar span{display:flex;align-items:center;justify-content:center;color:#fff;
  font-size:11px;white-space:nowrap;overflow:hidden}
.bar .m{background:#2563eb}.bar .l{background:#7c3aed}.bar .o{background:#0891b2}
.vch{background:var(--card);border:1px solid var(--line);border-radius:8px;
  margin-bottom:14px;overflow:hidden}
.vch .hd{padding:10px 14px;background:#f9fafb;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.vch .hd .no{font-family:Consolas,monospace;color:var(--muted)}
form{display:inline}
.footer{color:var(--muted);font-size:12px;margin-top:32px}
"""


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _qty(value: Decimal) -> str:
    text = f"{value:,.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct(value: Decimal | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"n" if i in numeric else ""}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    head_html = f"<thead><tr>{head}</tr></thead>"
    body_html = f"<tbody>{''.join(body)}</tbody>"
    return f'<table class="tbl">{head_html}{body_html}</table>'


def _badge(severity: Severity) -> str:
    label, cls = SEVERITY[severity]
    return f'<span class="badge {cls}">{label}</span>'


def _nav(active: str) -> str:
    items = [
        ("overview", "/", "总览"),
        ("restore", "/restore", "成本还原"),
        ("journal", "/journal", "凭证与试算"),
        ("standard", "/standard", "标准成本卡"),
    ]
    links = "".join(
        f'<a class="{"on" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in items
    )
    return f"<nav>{links}</nav>"


def _layout(run: CostRun, title: str, body: str, active: str) -> str:
    sheet = run.sheet
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 成本控制台</title><style>{CSS}</style></head>
<body>
<header>
  <h1>成本控制台</h1>
  <div class="meta">{esc(sheet.company)} · {esc(sheet.period)}</div>
  {_nav(active)}
</header>
<main>{body}
<div class="footer">只读看板 · 数据来自本次成本计算结果 · 标准库 http.server 渲染</div>
</main></body></html>"""


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------


def page_dashboard(run: CostRun) -> str:
    sheet = run.sheet
    rate = (
        None
        if sheet.std_total == 0
        else (sheet.variance_total / sheet.std_total * 100).quantize(Decimal("0.01"))
    )
    variance_cls = "bad" if sheet.variance_total > 0 else "good"
    cards = f"""
<div class="cards">
  <div class="card"><div class="k">实际总成本</div><div class="v">{_money(sheet.total_cost)}</div>
    <div class="s">期初 {_money(sheet.opening_cost)} · 本期 {_money(sheet.current_cost)}</div></div>
  <div class="card"><div class="k">标准总成本</div><div class="v">{_money(sheet.std_total)}</div>
    <div class="s">按约当产量的标准允许额</div></div>
  <div class="card {variance_cls}"><div class="k">总差异</div>
    <div class="v">{_money(sheet.variance_total)}</div>
    <div class="s">差异率 {rate}%</div></div>
  <div class="card {"bad" if run.unallocated_overhead > 0 else ""}">
    <div class="k">未分配制造费用</div><div class="v">{_money(run.unallocated_overhead)}</div>
    <div class="s">有费用、无动因</div></div>
  <div class="card"><div class="k">预警</div><div class="v">{len(sheet.alerts)}</div>
    <div class="s">严重 {sum(1 for a in sheet.alerts if a.severity is Severity.CRITICAL)}
    · 警告 {sum(1 for a in sheet.alerts if a.severity is Severity.WARNING)}</div></div>
</div>"""

    total = sheet.current_cost or Decimal(1)
    m = float(sheet.material_cost / total * 100)
    lb = float(sheet.labor_cost / total * 100)
    oh = 100 - m - lb
    bar = f"""
<div class="bar">
  <span class="m" style="width:{m:.1f}%">材料 {m:.1f}%</span>
  <span class="l" style="width:{lb:.1f}%">人工 {lb:.1f}%</span>
  <span class="o" style="width:{oh:.1f}%">费用 {oh:.1f}%</span>
</div>
<div class="small muted">本期发生额构成（材料含半成品转入；还原后的结构见「成本还原」）</div>"""

    alerts = "".join(
        f'<div class="alert {SEVERITY[a.severity][1]}">{_badge(a.severity)}'
        f'<span>{esc(a.message)}</span><span class="rule">{esc(a.rule)}</span></div>'
        for a in sheet.alerts
    ) or '<div class="muted">本期无预警。</div>'

    order_rows = [
        [
            f'<a href="/order/{quote(o.order_no)}">{esc(o.order_no)}</a>',
            esc(o.product),
            f"{_qty(o.qty_completed)}/{_qty(o.qty_planned)}",
            _money(o.material_cost),
            _money(o.labor_cost),
            _money(o.overhead_cost),
            _money(o.total_cost),
            f"{o.unit_cost:.4f}",
            _money(o.std_total),
            _money(o.variance_total),
            _pct(o.variance_rate),
        ]
        for o in sheet.orders
    ]
    orders = _table(
        ["工单", "产品", "完工/计划", "材料", "人工", "费用", "实际合计", "单位成本", "标准", "差异", "差异率"],
        order_rows,
        numeric={2, 3, 4, 5, 6, 7, 8, 9, 10},
    )

    product_rows = [
        [
            esc(p.product),
            esc(p.name),
            _qty(p.qty_completed),
            _money(p.finished_cost),
            f"{p.unit_cost:.4f}",
            f"{p.std_unit_cost:.4f}",
            _money(p.variance),
            _pct(p.variance_rate),
        ]
        for p in sheet.products
    ]
    products = _table(
        ["产品", "名称", "完工数量", "入库成本", "实际单位", "标准单位", "差异", "差异率"],
        product_rows,
        numeric={2, 3, 4, 5, 6, 7},
    )

    body = f"""
<h2>本期概况</h2>
{cards}
<h2>成本构成</h2>{bar}
<h2>控制预警<span class="sub">{len(sheet.alerts)} 条</span></h2>
{alerts}
<h2>工单成本<span class="sub">点击工单号下钻</span></h2>
{orders}
<h2>产品单位成本</h2>
{products}"""
    return _layout(run, "总览", body, "overview")


def page_order(run: CostRun, order_no: str) -> str:
    cost = run.sheet.order(order_no)
    ds = run.dataset
    name = ds.material(cost.product).name

    rows: list[list[str]] = []
    if cost.opening_cost:
        rows.append(
            [
                "期初在产转入",
                _qty(cost.qty_opening),
                "",
                _money(cost.opening_cost),
                _money(cost.opening_std),
                _money(cost.variance_opening),
            ]
        )
    for line in cost.material_lines:
        material = ds.material(line.material)
        label = "半成品转入" if material.mtype is MaterialType.SEMI else "直接材料"
        rows.append(
            [
                f"{label} · {esc(material.name)}",
                _qty(line.qty),
                f"{line.unit_price:.4f}",
                _money(line.amount),
                _money(line.std_amount),
                _money(line.variance),
            ]
        )
    for line in cost.labor_lines:
        center = ds.center(line.cost_center)
        rows.append(
            [
                f"直接人工 · {esc(center.name)}",
                f"{_qty(line.hours)} h",
                f"{line.rate:.4f}",
                _money(line.amount),
                _money(line.std_amount),
                _money(line.variance),
            ]
        )
    for line in cost.overhead_lines:
        center = ds.center(line.cost_center)
        rows.append(
            [
                f"制造费用 · {esc(center.name)}",
                f"{_qty(line.hours)} h",
                f"{line.rate:.4f}",
                _money(line.amount),
                _money(line.std_amount),
                _money(line.variance),
            ]
        )
    rows.append(
        [
            "合计",
            "",
            "",
            _money(cost.total_cost),
            _money(cost.std_total),
            _money(cost.variance_total),
        ]
    )
    table = _table(["成本项目", "用量/工时", "单价/费率", "实际金额", "标准金额", "差异"], rows, numeric={1, 2, 3, 4, 5})

    variances = [
        ("材料价差", cost.material_price_variance),
        ("材料量差", cost.material_qty_variance),
        ("人工工资率", cost.labor_rate_variance),
        ("人工效率", cost.labor_efficiency_variance),
        ("费用耗费", cost.overhead_spending_variance),
        ("费用效率", cost.overhead_efficiency_variance),
    ]
    cards = "".join(
        f'<div class="card{" bad" if v > 0 else ""}"><div class="k">{esc(k)}</div>'
        f'<div class="v">{_money(v)}</div></div>'
        for k, v in variances
    )
    variance_note = ""
    if cost.opening_cost:
        variance_note = (
            f'<p class="small muted">差异 = 期初沉淀 {_money(cost.variance_opening)}'
            f" + 本期 {_money(cost.variance_current)} = {_money(cost.variance_total)}</p>"
        )

    notes = "".join(f"<li>{esc(n)}</li>" for n in cost.notes)
    body = f"""
<h2>{esc(cost.order_no)} · {esc(cost.product)} {esc(name)}
  <span class="sub">计划 {_qty(cost.qty_planned)} / 完工 {_qty(cost.qty_completed)}
  / 报废 {_qty(cost.qty_scrapped)} / 在产 {_qty(cost.wip_qty)}</span></h2>
<div class="cards">
  <div class="card"><div class="k">实际总成本</div><div class="v">{_money(cost.total_cost)}</div></div>
  <div class="card"><div class="k">单位成本</div><div class="v">{cost.unit_cost:.4f}</div>
    <div class="s">标准 {run.standard.unit_cost(cost.product):.4f}</div></div>
  <div class="card {"bad" if cost.variance_total > 0 else "good"}">
    <div class="k">差异</div><div class="v">{_money(cost.variance_total)}</div>
    <div class="s">{_pct(cost.variance_rate)}</div></div>
  <div class="card"><div class="k">期末在产</div><div class="v">{_money(cost.wip_cost)}</div></div>
</div>
<h2>成本明细</h2>
{table}
<h2>差异分解</h2>
<div class="cards">{cards}</div>
{variance_note}
<h2>成本去向</h2>
<table class="tbl"><tbody>
<tr><td>完工入库</td><td class="n">{_money(cost.finished_cost)}</td></tr>
<tr><td>其中：废品损失（由良品负担）</td><td class="n">{_money(cost.scrap_cost)}</td></tr>
<tr><td>期末在产品</td><td class="n">{_money(cost.wip_cost)}</td></tr>
</tbody></table>
{f"<h2>说明</h2><ul>{notes}</ul>" if notes else ""}
<p><a href="/">← 返回总览</a></p>"""
    return _layout(run, f"{cost.order_no} 成本明细", body, "")


def page_journal(run: CostRun) -> str:
    book = build_journal(run)
    entries = []
    for entry in book.entries:
        lines = "".join(
            f'<tr><td>{line.side}</td><td>{esc(line.account)} '
            f'{esc(_account_name(line.account))}</td>'
            f'<td class="n">{_money(line.amount)}</td><td>{esc(line.summary)}</td></tr>'
            for line in entry.lines
        )
        entries.append(
            f'<div class="vch"><div class="hd"><div><strong>{esc(entry.summary)}</strong>'
            f'<span class="no"> · {esc(entry.entry_no)} · {esc(entry.entry_date)}</span></div>'
            f'<div class="small">借 {_money(entry.debit_total)} / 贷 {_money(entry.credit_total)}'
            f' {"✓" if entry.is_balanced else "✗"}</div></div>'
            f'<table class="tbl"><thead><tr><th>方向</th><th>科目</th><th>金额</th><th>摘要</th></tr>'
            f"</thead><tbody>{lines}</tbody></table></div>"
        )

    tb = book.trial_balance(run.dataset.opening_balances)
    rows = [
        [
            r.code,
            esc(r.name),
            _money(r.opening),
            _money(r.debit),
            _money(r.credit),
            _money(r.closing),
        ]
        for r in tb
    ]
    rows.append(
        [
            "合计",
            "",
            "",
            _money(sum((r.debit for r in tb), ZERO)),
            _money(sum((r.credit for r in tb), ZERO)),
            _money(sum((r.closing for r in tb), ZERO)),
        ]
    )
    table = _table(["科目", "名称", "期初(借+)", "本期借方", "本期贷方", "期末(借+)"], rows, numeric={2, 3, 4, 5})

    body = f"""
<h2>记账凭证<span class="sub">共 {len(book.entries)} 张，借贷合计 {_money(book.debit_total)}</span></h2>
{"".join(entries)}
<h2>试算平衡表</h2>
{table}
<div class="small muted">期初余额由数据集带入；期末余额 = 期初 + 借方 − 贷方，合计应为 0。</div>"""
    return _layout(run, "凭证与试算", body, "journal")


def _account_name(code: str) -> str:
    from .accounts import ACCOUNTS

    return ACCOUNTS[code].name if code in ACCOUNTS else code


def page_restore(run: CostRun) -> str:
    items = Restorer(run).all()
    rows = [
        [
            f'<a href="/order/{quote(item.order_no)}">{esc(item.order_no)}</a>',
            esc(item.product),
            _money(item.finished_cost),
            _money(item.book_material),
            _money(item.material),
            _money(item.book_labor),
            _money(item.labor),
            _money(item.book_overhead),
            _money(item.overhead),
            _money(item.transfer_in),
        ]
        for item in items
    ]
    table = _table(
        ["工单", "产品", "完工成本", "账面材料", "还原材料", "账面人工", "还原人工",
         "账面费用", "还原费用", "半成品转入"],
        rows,
        numeric={2, 3, 4, 5, 6, 7, 8, 9},
    )

    structure = []
    finished = [i for i in items if run.dataset.material(i.product).mtype is MaterialType.FINISHED]
    for item in finished:
        total = item.finished_cost or Decimal(1)
        structure.append(
            f'<div class="card"><div class="k">{esc(item.product)} {esc(item.name)}'
            f' · 完工成本 {_money(item.finished_cost)}</div>'
            f'<div class="s">材料 {_pct(item.book_material / total)} → {_pct(item.material / total)}'
            f' · 人工 {_pct(item.book_labor / total)} → {_pct(item.labor / total)}'
            f' · 费用 {_pct(item.book_overhead / total)} → {_pct(item.overhead / total)}</div></div>'
        )

    traces = [t for t in VarianceTracer(run).all() if t.pass_through != 0]
    trace_html = []
    for trace in traces:
        own = " · ".join(
            f"{LABELS[key]} {_money(trace.own_components.get(key, ZERO))}"
            for key in trace.own_components
        )
        ups = "".join(
            f"<tr><td>{esc(u.order_no)} {esc(u.product)}</td><td class='n'>{_qty(u.qty)}</td>"
            f"<td class='n'>{_money(u.amount)}</td><td class='small'>"
            + " · ".join(f"{LABELS[k]} {_money(v)}" for k, v in u.components.items())
            + "</td></tr>"
            for u in trace.upstream
        )
        trace_html.append(
            f'<div class="vch"><div class="hd"><div><strong>{esc(trace.order_no)} '
            f'{esc(trace.product)}</strong> 差异 {_money(trace.total)} = 自身 '
            f'{_money(trace.own_total)} + 上游 {_money(trace.pass_through)}</div></div>'
            f'<div style="padding:10px 14px" class="small">{esc(own)}</div>'
            f'<table class="tbl"><thead><tr><th>上游工单</th><th>结转数量</th>'
            f'<th>转入差异</th><th>构成（按比例映射）</th></tr></thead><tbody>{ups}</tbody></table></div>'
        )

    body = f"""
<h2>成本还原<span class="sub">把半成品里的加工费拆回原始成本项目</span></h2>
{table}
<h2>产成品成本结构变化</h2>
<div class="cards">{"".join(structure) or '<div class="muted">本期无产成品完工。</div>'}</div>
<h2>差异穿透<span class="sub">成品超支是谁造成的</span></h2>
{"".join(trace_html) or '<div class="muted">本期没有半成品结转。</div>'}
<div class="small muted">转入金额按实际结转账与标准成本之差计算，是精确值；上游差异构成按比例映射。</div>"""
    return _layout(run, "成本还原", body, "restore")


def page_standard(run: CostRun) -> str:
    ds = run.dataset
    blocks = []
    codes = [
        code
        for code, material in ds.materials.items()
        if material.mtype is not MaterialType.RAW
        and (ds.bom.get(code) or ds.routing.get(code))
    ]
    for code in sorted(codes, key=lambda c: (run.standard.depth(c), c)):
        material = ds.material(code)
        if material.mtype is MaterialType.RAW:
            continue
        card = run.standard.card(code)
        rows: list[list[str]] = []
        for line in card.material_lines:
            component = ds.material(line.material)
            label = "半成品" if component.mtype is MaterialType.SEMI else "材料"
            rows.append(
                [
                    f"{label} · {esc(component.name)}",
                    _qty(line.qty),
                    f"{line.price:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        for line in card.labor_lines:
            center_name = ds.center(line.cost_center).name
            rows.append(
                [
                    f"人工 · {esc(center_name)}",
                    f"{_qty(line.hours)} h",
                    f"{line.rate:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        for line in card.overhead_lines:
            center_name = ds.center(line.cost_center).name
            rows.append(
                [
                    f"制造费用 · {esc(center_name)}",
                    f"{_qty(line.hours)} h",
                    f"{line.rate:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        rows.append(["合计", "", "", f"{card.total:.4f}"])
        blocks.append(
            f'<h2>{esc(code)} {esc(material.name)}'
            f'<span class="sub">层级 {run.standard.depth(code)} · 标准单位成本 {card.total:.4f}</span></h2>'
            + _table(["项目", "单位用量/工时", "单价/费率", "单位成本"], rows, numeric={1, 2, 3})
        )
    return _layout(run, "标准成本卡", "".join(blocks), "standard")


def page_not_found(run: CostRun) -> str:
    body = '<h2>页面不存在</h2><p><a href="/">返回总览</a></p>'
    return _layout(run, "404", body, "")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def render(run: CostRun, path: str) -> tuple[int, str, str]:
    parsed = urlparse(path)
    route = parsed.path
    if route == "/":
        return 200, "text/html; charset=utf-8", page_dashboard(run)
    if route.startswith("/order/"):
        order_no = unquote(route[len("/order/"):])
        try:
            return 200, "text/html; charset=utf-8", page_order(run, order_no)
        except KeyError:
            return 404, "text/html; charset=utf-8", page_not_found(run)
    if route == "/journal":
        return 200, "text/html; charset=utf-8", page_journal(run)
    if route == "/restore":
        return 200, "text/html; charset=utf-8", page_restore(run)
    if route == "/standard":
        return 200, "text/html; charset=utf-8", page_standard(run)
    if route == "/api/summary":
        payload = {
            "company": run.sheet.company,
            "period": run.sheet.period,
            "total_cost": str(run.sheet.total_cost),
            "std_total": str(run.sheet.std_total),
            "variance": str(run.sheet.variance_total),
            "unallocated_overhead": str(run.unallocated_overhead),
            "alerts": len(run.sheet.alerts),
            "orders": [
                {
                    "order_no": o.order_no,
                    "product": o.product,
                    "total_cost": str(o.total_cost),
                    "unit_cost": str(o.unit_cost),
                    "variance": str(o.variance_total),
                }
                for o in run.sheet.orders
            ],
        }
        return (
            200,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    return 404, "text/html; charset=utf-8", page_not_found(run)


def make_server(
    run: CostRun, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "costcontrol/0.3"

        def do_GET(self) -> None:  # noqa: N802
            status, content_type, body = render(run, self.path)
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)
