"""命令行入口。

JSON 模式（单期、无需数据库）：
    python -m costcontrol.cli --data data/demo_ningbo.json --detail

SQLite 模式（多期间）：
    python -m costcontrol.cli --db data/cost.db --init
    python -m costcontrol.cli --db data/cost.db --import-json data/demo_ningbo.json
    python -m costcontrol.cli --db data/cost.db --periods
    python -m costcontrol.cli --db data/cost.db --period 2026-10 --detail
    python -m costcontrol.cli --db data/cost.db --period 2026-10 --close
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .accounting import JournalBook, build_journal
from .close import close_period, render_checks
from .dataset import load_dataset
from .report import (
    render_cost_sheet,
    render_journal,
    render_order_detail,
    render_restoration,
    render_standard_cards,
    render_table,
    render_trial_balance,
    render_variance_trace,
)
from .report import qty as fmt_qty
from .run import run_cost
from .store import PeriodStatus, Store, StoreError
from .web import DEFAULT_HOST, DEFAULT_PORT, make_server

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="costcontrol",
        description="面向组装型制造业的 ERP 成本计算与控制引擎",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help="数据集 JSON 路径（默认 data/demo_ningbo.json）",
    )
    parser.add_argument("--db", type=Path, help="SQLite 数据库路径（多期间模式）")
    parser.add_argument("--period", help="会计期间，如 2026-10（--db 模式必填）")
    parser.add_argument("--init", action="store_true", help="初始化数据库结构")
    parser.add_argument(
        "--import-json",
        dest="import_json",
        type=Path,
        help="把 JSON 数据集导入数据库",
    )
    parser.add_argument("--periods", action="store_true", help="列出数据库中的期间")
    parser.add_argument("--lock", action="store_true", help="锁定期间（置为已结账）")
    parser.add_argument("--unlock", action="store_true", help="解锁期间（置为开放）")

    parser.add_argument("--detail", action="store_true", help="输出每张工单的成本明细")
    parser.add_argument("--standard", action="store_true", help="输出标准成本卡")
    parser.add_argument("--order", help="只看某张工单的明细")
    parser.add_argument("--journal", action="store_true", help="输出记账凭证与试算平衡表")
    parser.add_argument("--close", action="store_true", help="执行期末结账检查")
    parser.add_argument("--carry", action="store_true", help="查看下期期初结转（在产 + 余额）")
    parser.add_argument("--restore", action="store_true", help="成本还原与差异穿透")
    parser.add_argument("--explain", action="store_true", help="生成 AI 结账排查清单")
    parser.add_argument("--attribute", action="store_true", help="生成差异归因报告")
    parser.add_argument("--diagnose", action="store_true", help="生成差异诊断（原因 / 证据 / 反证）")
    parser.add_argument("--challenge", help="对诊断结论提出反证，重新评估置信度")
    parser.add_argument("--challenge-log", type=Path, help="结构化反证裁决 JSONL 路径")
    parser.add_argument(
        "--translate-config",
        type=Path,
        help="ERP 分摊配置文档路径（- 表示从标准输入读取）",
    )
    parser.add_argument("--config-text", help="直接贴入的分摊配置文本")
    parser.add_argument("--tasks", action="store_true", help="按数据异常生成排雷工单")
    parser.add_argument(
        "--task-done",
        action="append",
        metavar="ID",
        help="标记工单完成（可重复），全部完成后自动复跑对比",
    )
    parser.add_argument("--task-file", type=Path, help="工单文件路径（默认 data/tasks_<期间>.json）")
    parser.add_argument("--rerun-tasks", action="store_true", help="用当前数据复跑并对比工单快照")
    parser.add_argument("--as-of", help="工单截止日基准（YYYY-MM-DD，默认今天）")
    parser.add_argument("--facts", action="store_true", help="输出规范差异事实 JSON")
    parser.add_argument("--sense", type=Path, help="业务 Sense YAML 路径")
    parser.add_argument("--csv", type=Path, help="从 ERP 导出目录读取（CSV）")
    parser.add_argument("--export-csv", type=Path, help="把当前数据写成 ERP 导出示例目录")
    parser.add_argument("--adapter", type=Path, help="导出列名映射 YAML")
    parser.add_argument("--reports", type=Path, help="报表目录（SAP/金蝶/用友导出）")
    parser.add_argument("--check-export", type=Path, help="检查 ERP 导出目录的字段与能力覆盖")
    parser.add_argument("--auto-parts", type=Path, help="汽车零部件 13 表导出目录（第二层复算）")
    parser.add_argument("--month", help="复算期间，如 2026-06")
    parser.add_argument("--sap-baseline", action="store_true", help="用 SAP 标准成本卡做差异基准")
    parser.add_argument("--offline", action="store_true", help="AI 解释使用离线规则")
    parser.add_argument("--show-prompt", action="store_true", help="打印发送给模型的提示词")
    parser.add_argument("--audience", help="报告读者，如 财务经理")
    parser.add_argument("--serve", action="store_true", help="启动本地 Web 看板（只读）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Web 监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Web 端口（默认 8765）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="结账严格模式：未分配制造费用直接阻断结账",
    )
    parser.add_argument(
        "--material-tolerance",
        type=Decimal,
        help="材料超耗容差，例如 0.02 表示 2%%",
    )
    parser.add_argument(
        "--order-tolerance",
        type=Decimal,
        help="工单成本超支容差，例如 0.03 表示 3%%",
    )
    return parser


def _print_periods(store: Store) -> None:
    infos = store.list_periods()
    if not infos:
        print("数据库中还没有期间。")
        return
    rows = [
        [
            info.period,
            info.company,
            "已结账" if not info.is_open else "开放",
            str(info.orders),
            info.closed_at or "-",
        ]
        for info in infos
    ]
    print(
        render_table(
            ["期间", "公司", "状态", "工单数", "结账时间"], rows, ["l", "l", "l", "r", "l"]
        )
    )


def _params_from_args(args: argparse.Namespace) -> dict[str, Decimal]:
    params: dict[str, Decimal] = {}
    if args.material_tolerance is not None:
        params["material_qty_tolerance"] = args.material_tolerance
    if args.order_tolerance is not None:
        params["order_variance_tolerance"] = args.order_tolerance
    return params


def _dispatch(
    args: argparse.Namespace,
    params: dict[str, Decimal],
    parser: argparse.ArgumentParser,
    store: Store | None,
) -> int:
    if args.translate_config or args.config_text:
        from .config_translator import read_config, translate_config
        from .llm_explainer import LLMError, load_sense, provider_from_env

        if args.translate_config and args.config_text:
            print("--translate-config 与 --config-text 只能二选一", file=sys.stderr)
            return 2
        try:
            text = (
                args.config_text
                if args.config_text
                else read_config(args.translate_config)
            )
            sense = load_sense(args.sense)
            provider = None if args.offline else provider_from_env()
        except (OSError, LLMError, ValueError) as exc:
            print(f"配置翻译初始化失败：{exc}", file=sys.stderr)
            return 2
        ds = None
        try:
            ds = load_dataset(args.data)
        except (OSError, ValueError, KeyError, TypeError):
            ds = None
        try:
            explanation = translate_config(text, sense, provider, ds)
        except LLMError as exc:
            print(f"配置翻译失败：{exc}", file=sys.stderr)
            return 2
        print(explanation.text)
        print(f"\n— provider: {explanation.provider} / model: {explanation.model}")
        if args.show_prompt:
            print("\n--- system prompt ---")
            print(explanation.prompt.system)
            print("\n--- user prompt ---")
            print(explanation.prompt.user)
        return 0

    if args.challenge and not args.diagnose:
        print("--challenge 需要与 --diagnose 一起使用", file=sys.stderr)
        return 2

    if args.tasks or args.task_done or args.rerun_tasks:
        from datetime import date as _date

        from .audit import detect_anomalies
        from .diagnosis import diagnose_facts
        from .facts import build_variance_facts
        from .llm_explainer import LLMError, load_sense
        from .tasks import (
            TaskBoard,
            build_board,
            compare,
            render_board,
            render_comparison,
        )

        try:
            ds = load_dataset(args.data)
            run = run_cost(ds)
            sense = load_sense(args.sense)
        except (OSError, ValueError, KeyError, LLMError) as exc:
            print(f"工单化初始化失败：{exc}", file=sys.stderr)
            return 2
        facts = build_variance_facts(run, sense.raw.get("org", {}) or {})
        diagnoses = diagnose_facts(facts, run)
        anomalies = detect_anomalies(ds, run)
        board_path = args.task_file or Path("data") / f"tasks_{ds.period}.json"

        if args.tasks:
            try:
                as_of = _date.fromisoformat(args.as_of) if args.as_of else None
            except ValueError:
                print("--as-of 格式应为 YYYY-MM-DD", file=sys.stderr)
                return 2
            board = build_board(anomalies, run, diagnoses, as_of)
            board.source = str(args.data)
            board.save(board_path)
            print(render_board(board))
            print(f"\n工单已写入 {board_path.resolve()}")
            print("补证后用新导出运行：--rerun-tasks --data <新导出>")
            return 0

        if not board_path.exists():
            print(f"找不到工单文件 {board_path}，请先运行 --tasks", file=sys.stderr)
            return 2
        board = TaskBoard.load(board_path)
        if args.task_done:
            try:
                for task_id in args.task_done:
                    board.mark_done(task_id)
            except KeyError as exc:
                print(f"工单更新失败：{exc}", file=sys.stderr)
                return 2
            board.save(board_path)
            print(render_board(board))
            if board.open_tasks:
                print("\n还有未完成工单；全部完成后会自动复跑对比。")
                return 0
            print("\n全部工单已完成，自动复跑对比：")
        print(render_comparison(compare(board, run, diagnoses, anomalies)))
        return 0

    if args.auto_parts:
        from .auto_parts import AutoPartsError, load_period, render_second_layer

        if not args.month:
            print("--auto-parts 需要同时指定 --month，例如 2026-06", file=sys.stderr)
            return 2
        try:
            ds = load_period(args.auto_parts, args.month)
            run = run_cost(ds, prefer_master_price=True)
        except (AutoPartsError, ValueError, OSError) as exc:
            print(f"第二层复算失败：{exc}", file=sys.stderr)
            return 2
        if args.sap_baseline:
            from .sap_baseline import BaselineError, analyze, render_baseline

            try:
                result = analyze(run, args.auto_parts, args.month)
            except (BaselineError, ValueError, OSError) as exc:
                print(f"SAP 基准分析失败：{exc}", file=sys.stderr)
                return 2
            print(render_baseline(result))
        else:
            print(render_second_layer(run, args.auto_parts, args.month))
        return 0

    if args.check_export:
        from .coverage import CoverageError, check_export, render_coverage

        try:
            report = check_export(args.check_export)
        except (CoverageError, OSError, ValueError) as exc:
            print(f"覆盖率检查失败：{exc}", file=sys.stderr)
            return 2
        print(render_coverage(report))
        return 0

    if args.reports:
        from .reports import ReportError, analyze, load_bundle, render_analysis

        try:
            bundle = load_bundle(args.reports)
            analysis = analyze(bundle)
        except (ReportError, ValueError, OSError) as exc:
            print(f"读取报表失败：{exc}", file=sys.stderr)
            return 2
        print(render_analysis(analysis))
        return 0

    if args.init:
        if store is None:
            parser.error("--init 需要同时指定 --db")
        store.init_schema()
        print(f"数据库已初始化：{store.path.resolve()}")
        return 0

    if args.import_json:
        if store is None:
            parser.error("--import-json 需要同时指定 --db")
        try:
            ds = load_dataset(args.import_json)
            info = store.save_period(ds)
        except (StoreError, ValueError) as exc:
            print(f"导入失败：{exc}", file=sys.stderr)
            return 2
        print(
            f"已导入期间 {info.period}：工单 {info.orders} 张，"
            f"状态 {'开放' if info.is_open else '已结账'}"
        )
        return 0

    if args.periods:
        if store is None:
            parser.error("--periods 需要同时指定 --db")
        try:
            _print_periods(store)
        except StoreError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if store is not None:
        if not args.period:
            parser.error("--db 模式需要同时指定 --period")
        try:
            info = store.period_info(args.period)
        except StoreError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if info is None:
            print(f"期间 {args.period} 不存在，可用 --periods 查看。", file=sys.stderr)
            return 2
        if args.lock or args.unlock:
            status = PeriodStatus.CLOSED if args.lock else PeriodStatus.OPEN
            info = store.set_status(args.period, status)
            print(f"期间 {info.period} 状态：{'已结账' if not info.is_open else '开放'}")
            return 0
        if args.close and not info.is_open:
            print(f"期间 {args.period} 已结账，不能重复结账。")
            return 0
        try:
            ds = store.load_period(args.period)
        except (StoreError, ValueError) as exc:
            print(f"读取期间失败：{exc}", file=sys.stderr)
            return 2
        if not info.is_open:
            print(f"[提示] 期间 {args.period} 已结账，以下为查询视图\n")
    else:
        if args.csv:
            from .adapters import AdapterError, load_mapping, read_export

            try:
                ds = read_export(args.csv, load_mapping(args.adapter))
            except (AdapterError, ValueError, OSError) as exc:
                print(f"读取 ERP 导出失败：{exc}", file=sys.stderr)
                return 2
        else:
            ds = load_dataset(args.data)

    if args.export_csv:
        from .adapters import AdapterError, load_mapping, write_export

        try:
            target = write_export(ds, args.export_csv, load_mapping(args.adapter))
        except (AdapterError, OSError) as exc:
            print(f"生成导出失败：{exc}", file=sys.stderr)
            return 2
        print(f"已生成 ERP 导出示例：{target.resolve()}")
        return 0

    run = run_cost(ds, params)

    if args.attribute or args.facts:
        from .facts import build_variance_facts, facts_to_dicts
        from .llm_explainer import LLMError, load_sense
        from .responsibility import attribute, render_report

        try:
            sense = load_sense(args.sense)
        except (LLMError, ValueError) as exc:
            print(f"业务 Sense 加载失败：{exc}", file=sys.stderr)
            return 2
        facts = build_variance_facts(run, sense.raw.get("org", {}) or {})
        if args.facts:
            print(json.dumps(facts_to_dicts(facts), ensure_ascii=False, indent=2))
            return 0
        print(render_report(attribute(facts, sense), sense))
        return 0

    if args.diagnose:
        from .challenge import pick_target, record_review, review_with_llm
        from .diagnosis import diagnose_facts, render_diagnosis
        from .facts import build_variance_facts
        from .llm_explainer import LLMError, load_sense, provider_from_env

        try:
            sense = load_sense(args.sense)
            provider = None if args.offline else provider_from_env()
        except (LLMError, ValueError) as exc:
            print(f"业务 Sense 加载失败：{exc}", file=sys.stderr)
            return 2
        facts = build_variance_facts(run, sense.raw.get("org", {}) or {})
        diagnoses = diagnose_facts(facts, run)
        print(render_diagnosis(diagnoses))
        if args.challenge:
            target = pick_target(diagnoses, args.challenge)
            try:
                review = review_with_llm(target, args.challenge, sense, provider)
            except (LLMError, ValueError) as exc:
                print(f"反证复核失败：{exc}", file=sys.stderr)
                return 2
            print("\n" + review.text)
            print(f"\n— provider: {review.provider} / model: {review.model}")
            if review.decision is not None:
                log_path = args.challenge_log or (
                    Path("data") / f"challenges_{run.sheet.period}.jsonl"
                )
                record_review(review, target, log_path)
                print(f"结构化裁决已写入 {log_path}")
            elif review.fallback_reason:
                print(f"已回退离线规则：{review.fallback_reason}")
            if args.show_prompt:
                print("\n--- system prompt ---")
                print(review.prompt.system)
                print("\n--- user prompt ---")
                print(review.prompt.user)
        return 0

    if args.explain:
        from .llm_explainer import LLMError, LLMExplainer, load_sense, provider_from_env

        try:
            sense = load_sense(args.sense)
            provider = None if args.offline else provider_from_env()
        except (LLMError, ValueError) as exc:
            print(f"AI 解释层配置错误：{exc}", file=sys.stderr)
            return 2
        try:
            explanation = LLMExplainer(sense, provider).explain(
                run, audience=args.audience
            )
        except LLMError as exc:
            print(f"生成失败：{exc}", file=sys.stderr)
            return 2
        print(explanation.text)
        print(f"\n— provider: {explanation.provider} / model: {explanation.model}")
        if args.show_prompt:
            print("\n--- system prompt ---")
            print(explanation.prompt.system)
            print("\n--- user prompt ---")
            print(explanation.prompt.user)
        return 0

    if args.carry:
        from .close import build_carry

        carry = build_carry(run)
        print(
            f"下期期初结转 · {carry.source_period} → {carry.period}"
            f" · 在产 {carry.total_wip}（{len(carry.wip)} 张工单）"
        )
        if carry.wip:
            rows = [
                [
                    w.order_no,
                    w.product,
                    fmt_qty(w.qty_in_wip),
                    f"{w.material_completion * 100:.0f}%",
                    f"{w.wip_progress * 100:.0f}%",
                    f"{w.cost_material:,.2f}",
                    f"{w.cost_labor:,.2f}",
                    f"{w.cost_oh:,.2f}",
                ]
                for w in carry.wip
            ]
            print(
                render_table(
                    ["工单", "产品", "在产", "投料程度", "完工程度", "材料", "人工", "费用"],
                    rows,
                    ["l", "l", "r", "r", "r", "r", "r", "r"],
                )
            )
        print("\n期初余额：")
        for code, amount in sorted(carry.balances.items()):
            side = "借" if amount > 0 else "贷"
            print(f"  {code}  {side} {abs(amount):,.2f}")
        return 0

    if args.serve:
        try:
            server = make_server(run, args.host, args.port)
        except OSError as exc:
            print(f"无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
            return 2
        print(f"成本控制台已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
        finally:
            server.server_close()
        return 0

    if args.close:
        report = close_period(run, strict=args.strict)
        print(render_checks(report))
        if report.adjustment is not None:
            print()
            print(
                render_journal(
                    JournalBook(period=ds.period, entries=[report.adjustment]),
                    title="结账调整凭证",
                )
            )
        print()
        print(render_trial_balance(report.journal.trial_balance(ds.opening_balances)))
        print("\n完整凭证清单：python -m costcontrol.cli --journal")
        if store is not None:
            if report.can_close and report.carry is not None:
                closed = store.close_with_carry(ds.period, report.carry)
                print(f"\n期间 {closed.period} 已结账（{closed.closed_at}）")
                print(
                    f"已生成 {report.carry.period} 期初：在产 {report.carry.total_wip}，"
                    f"总账余额已结转"
                )
            else:
                print("\n存在阻断项，期间保持开放。")
    elif args.journal:
        journal = build_journal(run)
        print(render_journal(journal))
        print()
        print(render_trial_balance(journal.trial_balance(ds.opening_balances)))
    elif args.standard:
        print(render_standard_cards(run))
    elif args.restore:
        print(render_restoration(run))
        print()
        print(render_variance_trace(run))
    elif args.order:
        print(render_order_detail(run, run.sheet.order(args.order)))
    else:
        print(render_cost_sheet(run, detail=args.detail))

    for warning in run.warnings:
        print(f"\n[主数据提示] {warning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，先切过来，避免中文报表乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    params = _params_from_args(args)

    store: Store | None = Store(args.db) if args.db else None
    try:
        return _dispatch(args, params, parser, store)
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
