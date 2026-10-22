"""结果解读：把查询结果转成业务同事能直接读的结论。

刻意保持"规则式 + 可复核"：每句结论都能在返回的表格里找到对应数字，
不夸大、不编造，避免 AI 幻觉影响经营决策。
"""

from __future__ import annotations

import pandas as pd

from .nl2sql import METRIC_CN, SQLPlan

MONEY_HINTS = ("gmv", "amount", "spend", "margin", "pay", "cost", "budget", "roi")
PERCENT_HINTS = ("rate", "share", "ctr", "cvr", "lift")


def _fmt(col: str, value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if not isinstance(value, (int, float)):
        return str(value)
    low = col.lower()
    if any(h in low for h in PERCENT_HINTS):
        return f"{value:.1%}"
    if "roi" in low:
        return f"{value:.2f}"
    if "days" in low or "cover" in low:
        return f"{value:.1f} 天"
    if any(h in low for h in MONEY_HINTS):
        return f"{value:,.0f} 元"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def _numeric_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def narrate(question: str, df: pd.DataFrame, plan: SQLPlan) -> str:
    if df is None or df.empty:
        return "没有查询到符合条件的数据，可能是时间范围内无业务发生，或筛选条件过严。"

    if df.shape[0] == 1 and df.shape[1] == 1:
        col = df.columns[0]
        return f"{plan.explanation}：{_fmt(col, df.iloc[0, 0])}。"

    # 库存类结果：先给规模，再给最紧急的一条
    if "status" in df.columns and "sku_id" in df.columns:
        counts = df["status"].value_counts().to_dict()
        head = df.iloc[0]
        parts = "、".join(f"{k} {v} 个" for k, v in counts.items())
        text = f"共命中 {len(df)} 条库存记录：{parts}。"
        if "cover_days" in df.columns and pd.notna(head.get("cover_days")):
            text += (f"最紧急的是 {head['sku_id']}（{head.get('warehouse', '')}），"
                     f"可售 {float(head['cover_days']):.1f} 天，建议补货 {int(head.get('suggest_qty', 0))} 件。")
        return text

    # 活动类结果
    if "campaign_name" in df.columns and "roi" in df.columns:
        best = df.sort_values("roi", ascending=False).iloc[0]
        return (f"共 {len(df)} 个活动。ROI 最高的是「{best['campaign_name']}」"
                f"（ROI {float(best['roi']):.2f}，GMV {float(best['gmv']):,.0f} 元）；"
                f"最近的活动状态为「{df.iloc[0].get('status', '-')}」。")

    num_cols = _numeric_cols(df)
    if not num_cols:
        return f"查询返回 {len(df)} 行数据，详见结果表。"

    value_col = plan.metric if plan.metric in df.columns else num_cols[-1]
    value_col = value_col if value_col in num_cols else num_cols[0]
    total = float(df[value_col].fillna(0).sum())
    label = METRIC_CN.get(value_col, value_col)

    # 分组排名：Top3 + 集中度
    if len(df) > 1:
        dim_col = next((c for c in df.columns if c not in num_cols), None)
        if dim_col and pd.api.types.is_numeric_dtype(df[value_col]):
            top = df.nlargest(3, value_col)
            detail = "，".join(f"{r[dim_col]} {_fmt(value_col, r[value_col])}"
                              for _, r in top.iterrows())
            text = f"{plan.explanation}：合计 {_fmt(value_col, total)}，{detail}。"
            if total:
                share = float(top[value_col].sum()) / total
                text += f"前三名集中度 {share:.1%}。"
            return text

    first = df.iloc[0]
    dim_col = next((c for c in df.columns if c not in num_cols), None)
    if dim_col:
        return (f"{plan.explanation}：第一名是 {first[dim_col]}，"
                f"{label} {_fmt(value_col, first[value_col])}，"
                f"共 {len(df)} 个分组，合计 {_fmt(value_col, total)}。")
    return f"{plan.explanation}：{label} {_fmt(value_col, total)}。"
