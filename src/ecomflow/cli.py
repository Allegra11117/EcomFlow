"""命令行入口：把日常重复操作固化成 7 条命令。"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import db, metrics, report
from .ai import ask, verify_offline
from .clean import clean_all
from .config import ROOT, ensure_dirs, get_config
from .ingest import load_all
from .logging_setup import get_logger
from .pipeline import run_pipeline
from .validate import summarize, validate

log = get_logger("ecomflow.cli")


def _print_frame(df: pd.DataFrame, max_rows: int = 20) -> None:
    if df is None or df.empty:
        print("（无记录）")
        return
    with pd.option_context("display.max_columns", 12, "display.width", 160,
                           "display.max_colwidth", 50):
        print(df.head(max_rows).to_string(index=False))
    if len(df) > max_rows:
        print(f"... 共 {len(df)} 条，仅显示前 {max_rows} 条")


def cmd_run(args: argparse.Namespace) -> int:
    result = run_pipeline(make_report=not args.no_report,
                          send_alert=not args.no_notify,
                          archive=not args.no_archive)
    counts = summarize(result.issues)
    print("\n跑批完成")
    print(f"  批次号   : {result.run_id}")
    print(f"  状态     : {result.status}  耗时 {result.elapsed}s")
    print(f"  数据区间 : {result.tables['orders']['date'].min()} ~ "
          f"{metrics.reference_date(result.tables['orders'])}")
    print(f"  订单行数 : {len(result.tables['orders']):,}")
    print(f"  异常     : 高 {counts['high']} / 中 {counts['medium']} / 低 {counts['low']}")
    for name, path in result.outputs.items():
        print(f"  {name:<8} : {path}")
    if result.channels:
        print(f"  已通知   : {', '.join(result.channels)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = get_config()
    raw, ingest_log = load_all(cfg)
    if "orders" not in raw:
        print("订单数据采集失败，无法校验：")
        _print_frame(ingest_log)
        return 1
    tables, clog = clean_all(raw, cfg)
    issues = validate(tables, cfg, clog.to_frame())
    print("\n清洗日志")
    _print_frame(clog.to_frame(), 30)
    print("\n异常清单")
    _print_frame(issues[["rule_id", "level", "message", "metric"]], 30)
    counts = summarize(issues)
    print(f"\n合计：高 {counts['high']} / 中 {counts['medium']} / 低 {counts['low']}")
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    db.init_schema()
    if args.set_id:
        if not args.to_status:
            print("请同时指定 --to 目标状态（acked/fixed/ignored/open）")
            return 1
        db.set_issue_status(args.set_id, args.to_status, args.owner, args.note)
        print(f"工单 {args.set_id} 已更新为 {args.to_status}")
        return 0
    issues = db.list_issues(status=args.status, level=args.level)
    if issues.empty:
        print("没有符合条件的异常工单")
        return 0
    _print_frame(issues[["issue_id", "level", "rule_name", "message", "occurrences",
                         "last_seen", "status"]], 50)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    answer = ask(args.question, provider=args.provider)
    print(f"\n问题：{answer.question}")
    print(f"理解：{answer.plan.explanation}（通道 {answer.plan.provider}）")
    print(f"SQL ：{answer.plan.sql}")
    print(f"耗时：{answer.latency_ms}ms")
    if answer.error:
        print(f"错误：{answer.error}")
        return 1
    _print_frame(answer.data, 30)
    print(f"\n结论：{answer.text}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    detail, ok = verify_offline(provider=args.provider)
    _print_frame(detail[["case", "provider", "ai_value", "baseline_value",
                         "diff_pct", "passed"]])
    print(f"\nAI 产出校验：{'全部通过' if ok else '存在偏差，需人工复核'}")
    return 0 if ok else 2


def cmd_report(_args: argparse.Namespace) -> int:
    cfg = get_config()
    db.init_schema()
    tables = {
        "orders": db.read_table("orders"),
        "products": db.read_table("products"),
        "inventory": db.read_table("inventory"),
        "traffic": db.read_table("traffic"),
        "campaigns": db.read_table("campaigns"),
    }
    if tables["orders"].empty:
        print("库内没有订单数据，请先执行 run")
        return 1
    for col in ("is_paid", "is_refund", "is_sale", "is_valid", "is_orphan",
                "is_amount_mismatch", "is_future_time", "is_negative_stock"):
        if col in tables["orders"]:
            tables["orders"][col] = tables["orders"][col].astype(bool)
    tables["orders"]["date"] = pd.to_datetime(tables["orders"]["date"]).dt.date
    for name in ("inventory",):
        if not tables[name].empty:
            tables[name]["snapshot_date"] = pd.to_datetime(
                tables[name]["snapshot_date"]).dt.date
    if not tables["traffic"].empty:
        tables["traffic"]["stat_date"] = pd.to_datetime(tables["traffic"]["stat_date"]).dt.date

    issues = db.list_issues(status="all")
    kpi = metrics.kpi_overview(tables["orders"])
    meta = {
        "run_id": "REBUILD",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_start": str(tables["orders"]["date"].min()),
        "date_end": str(metrics.reference_date(tables["orders"])),
    }
    outputs = report.generate(tables, issues, kpi, pd.DataFrame(), pd.DataFrame(), meta, cfg)
    print("报表已重建：")
    for name, path in outputs.items():
        print(f"  {name:<8}: {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    dashboard = ROOT / "app" / "dashboard.py"
    if not dashboard.exists():
        print(f"页面文件不存在: {dashboard}")
        return 1
    cmd = [sys.executable, "-m", "streamlit", "run", str(dashboard),
           "--server.port", str(args.port), "--server.headless", "true"]
    print("启动内部工具页面：", " ".join(cmd))
    return subprocess.call(cmd)


def cmd_gen_data(args: argparse.Namespace) -> int:
    script = ROOT / "scripts" / "gen_mock_data.py"
    return subprocess.call([sys.executable, str(script), "--days", str(args.days)])


def cmd_schedule(args: argparse.Namespace) -> int:
    """每天固定时间跑批；--once 立即执行一次（便于验证）。"""
    if args.once:
        return cmd_run(argparse.Namespace(no_report=False, no_notify=False, no_archive=False))
    print(f"定时跑批已启动，每天 {args.at} 执行（Ctrl+C 退出）")
    last_day = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == args.at and last_day != now.date():
            last_day = now.date()
            try:
                result = run_pipeline()
                print(f"[{now:%Y-%m-%d %H:%M}] 跑批 {result.run_id} 完成，"
                      f"异常 {len(result.issues)} 条")
            except Exception as exc:  # noqa: BLE001 - 定时任务需要持续存活
                log.exception("定时跑批失败: %s", exc)
        time.sleep(20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecomflow", description="电商运营数据自动化平台：采集-清洗-校验-报表-提醒")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行完整跑批（默认动作）")
    p_run.add_argument("--no-report", action="store_true", help="跳过报表生成")
    p_run.add_argument("--no-notify", action="store_true", help="跳过异常提醒")
    p_run.add_argument("--no-archive", action="store_true", help="跳过原始数据归档")
    p_run.set_defaults(func=cmd_run)

    p_check = sub.add_parser("check", help="只执行采集+清洗+校验，不写报表")
    p_check.set_defaults(func=cmd_check)

    p_issues = sub.add_parser("issues", help="查看或更新异常工单")
    p_issues.add_argument("--status", default="open",
                          choices=["open", "acked", "fixed", "ignored", "all"])
    p_issues.add_argument("--level", default="all", choices=["high", "medium", "low", "all"])
    p_issues.add_argument("--set", dest="set_id", help="工单号")
    p_issues.add_argument("--to", dest="to_status",
                          choices=["open", "acked", "fixed", "ignored"])
    p_issues.add_argument("--owner", default="")
    p_issues.add_argument("--note", default="")
    p_issues.set_defaults(func=cmd_issues)

    p_ask = sub.add_parser("ask", help="自然语言问数")
    p_ask.add_argument("question")
    p_ask.add_argument("--provider", default=None, choices=["auto", "rule", "llm"])
    p_ask.set_defaults(func=cmd_ask)

    p_verify = sub.add_parser("verify-ai", help="用独立口径交叉校验 AI 问数结果")
    p_verify.add_argument("--provider", default="rule", choices=["rule", "llm"])
    p_verify.set_defaults(func=cmd_verify)

    p_report = sub.add_parser("report", help="用库内数据重建报表")
    p_report.set_defaults(func=cmd_report)

    p_serve = sub.add_parser("serve", help="启动 Streamlit 内部工具页面")
    p_serve.add_argument("--port", type=int, default=8501)
    p_serve.set_defaults(func=cmd_serve)

    p_api = sub.add_parser("mock-api", help="启动模拟电商平台开放接口")
    p_api.add_argument("--port", type=int, default=8801)
    p_api.set_defaults(func=lambda a: subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "mock_platform_api.py"),
         "--port", str(a.port)]))

    p_gen = sub.add_parser("gen-data", help="生成模拟平台数据")
    p_gen.add_argument("--days", type=int, default=120)
    p_gen.set_defaults(func=cmd_gen_data)

    p_sched = sub.add_parser("schedule", help="常驻定时跑批")
    p_sched.add_argument("--at", default="08:30", help="每天执行时间 HH:MM")
    p_sched.add_argument("--once", action="store_true", help="立即执行一次")
    p_sched.set_defaults(func=cmd_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_dirs(get_config())
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已退出")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，输出可读错误
        log.exception("命令执行失败: %s", exc)
        print(f"\n执行失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
