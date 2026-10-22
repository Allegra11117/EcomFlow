"""AI 产出校验：用独立口径（pandas 计算）交叉验证 AI 生成的 SQL 结果。

为什么需要：
    大模型生成的 SQL 可能"看起来对、数字错了"（口径缺失、去重错误、时间边界）。
    这里给每个基准问题准备一份 pandas 基准实现，两者结果比对，
    差异超过容差即判定失败，并写回 ai_query_log 与 JSON 报告。

用法：
    python main.py verify-ai
    from ecomflow.ai import verify_offline; df, ok = verify_offline()
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable

import pandas as pd

from .. import db
from ..config import Config, get_config
from ..logging_setup import get_logger
from .nl2sql import ask, execute_plan, plan_question

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _orders() -> pd.DataFrame:
    return db.query_df("SELECT * FROM orders")


@lru_cache(maxsize=1)
def _inventory_alert() -> pd.DataFrame:
    return db.query_df("SELECT * FROM inventory_alert")


def _ref() -> pd.Timestamp:
    from .. import metrics
    return pd.Timestamp(metrics.reference_date(_orders()))


def _recent(days: int = 30) -> pd.DataFrame:
    o = _orders()
    dt = pd.to_datetime(o["date"])
    ref = _ref()
    return o[(dt > ref - pd.Timedelta(days=days)) & (dt <= ref)]


def _paid_mask(df: pd.DataFrame) -> pd.Series:
    return df["is_paid"].astype(bool)


def baseline_gmv(days: int = 30) -> float:
    cur = _recent(days)
    mask = _paid_mask(cur) | cur["is_refund"].astype(bool)
    return round(float(cur.loc[mask, "pay_amount"].sum()), 2)


def baseline_orders(days: int = 30) -> int:
    cur = _recent(days)
    return int(_paid_mask(cur).sum())


def baseline_aov(days: int = 30) -> float:
    cur = _recent(days)
    paid = cur[_paid_mask(cur)]
    return round(float(paid["pay_amount"].sum()) / max(1, len(paid)), 2)


def baseline_top_platform(days: int = 30) -> str:
    cur = _recent(days)
    mask = _paid_mask(cur) | cur["is_refund"].astype(bool)
    g = cur[mask].groupby("platform")["pay_amount"].sum()
    return str(g.idxmax())


def baseline_restock_count() -> int:
    alert = _inventory_alert()
    return int(alert["status"].isin(["断货", "紧急补货", "低于安全库存"]).sum())


CASE_DEFS: list[dict[str, Any]] = [
    {
        "name": "近30天GMV",
        "question": "近30天GMV是多少",
        "type": "number",
        "baseline": baseline_gmv,
        "extract": lambda df: float(df.iloc[0, 0]),
    },
    {
        "name": "近30天订单数",
        "question": "近30天订单数是多少",
        "type": "number",
        "baseline": baseline_orders,
        "extract": lambda df: float(df.iloc[0, 0]),
    },
    {
        "name": "近30天客单价",
        "question": "近30天客单价是多少",
        "type": "number",
        "baseline": baseline_aov,
        "extract": lambda df: float(df.iloc[0, 0]),
    },
    {
        "name": "GMV最高平台",
        "question": "近30天各平台GMV",
        "type": "text",
        "baseline": baseline_top_platform,
        "extract": lambda df: str(df.iloc[0]["platform"]),
    },
    {
        "name": "需补货SKU数",
        "question": "哪些商品需要补货",
        "type": "number",
        "baseline": baseline_restock_count,
        "extract": lambda df: float(len(df)),
    },
]


def _check(ai_value: Any, base_value: Any, kind: str, tol: float) -> tuple[bool, float]:
    if kind == "text":
        return str(ai_value) == str(base_value), 0.0
    try:
        ai_num, base_num = float(ai_value), float(base_value)
    except (TypeError, ValueError):
        return False, 1.0
    if base_num == 0:
        return ai_num == 0, 0.0 if ai_num == 0 else 1.0
    diff = abs(ai_num - base_num) / abs(base_num)
    return diff <= tol, round(diff, 6)


def verify_offline(cfg: Config | None = None, provider: str = "rule",
                   save: bool = True) -> tuple[pd.DataFrame, bool]:
    """逐题比对 AI SQL 结果与 pandas 基准结果，返回（明细, 是否全通过）。"""
    cfg = cfg or get_config()
    if _orders().empty:
        raise RuntimeError("数据库为空，请先执行 python main.py run 生成数据")
    tol = float(cfg.get("ai.verify_tolerance", 0.01))

    rows = []
    for case in CASE_DEFS:
        base_value = case["baseline"]()
        plan = plan_question(case["question"], cfg)
        plan.provider = provider
        error, ai_value, sql = "", None, plan.sql
        try:
            if provider == "llm":
                answer = ask(case["question"], cfg, provider="llm")
                df, sql, error = answer.data, answer.plan.sql, answer.error
                plan.provider = answer.plan.provider
            else:
                df, sql = execute_plan(plan, cfg)
        except Exception as exc:  # noqa: BLE001
            df, error = pd.DataFrame(), str(exc)
        if error or df.empty:
            passed, diff = False, 1.0
        else:
            ai_value = case["extract"](df)
            passed, diff = _check(ai_value, base_value, case["type"], tol)
        rows.append({
            "case": case["name"], "question": case["question"], "provider": plan.provider,
            "ai_value": ai_value, "baseline_value": base_value,
            "diff_pct": diff, "passed": passed, "error": error, "sql": sql,
        })
        db.log_ai_query(case["question"], sql, plan.provider, int(len(df)), 0,
                        "passed" if passed else "failed",
                        f"基准={base_value}, 差异={diff:.4%}")
        log.info("校验 %-12s %s（AI=%s 基准=%s）", case["name"],
                 "通过" if passed else "失败", ai_value, base_value)

    detail = pd.DataFrame(rows)
    all_passed = bool(detail["passed"].all()) if not detail.empty else True

    if save:
        out_dir = cfg.path("paths.output") / "ai_verify"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"verify_{datetime.now():%Y%m%d_%H%M%S}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "provider": provider,
            "tolerance": tol,
            "all_passed": all_passed,
            "cases": detail.drop(columns=["sql"]).to_dict("records"),
            "sqls": detail[["case", "sql"]].to_dict("records"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("AI 校验报告: %s", path)
    return detail, all_passed
