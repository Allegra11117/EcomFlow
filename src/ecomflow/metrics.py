"""指标层：销售趋势、渠道推广、库存健康度、活动效果。

所有口径集中在这里，报表、页面、AI 问数共用同一套实现，避免"同名指标多个口径"。
口径说明（与业务对齐过）：
    GMV        = 支付成功 + 已退款订单的实付金额（按支付时点计入）
    净销售额    = 支付成功订单实付 - 退款金额
    退款率      = 退款金额 / GMV
    毛利        = 实付 - 成本 × 数量
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import Config, get_config

SALE_COLUMNS = ["gmv", "net_gmv", "refund_amount", "orders", "buyers", "units",
                "cost", "margin", "aov", "margin_rate", "refund_rate"]


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else np.nan


def reference_date(orders: pd.DataFrame, fallback: date | None = None) -> date:
    """业务基准日：取订单数据里最后一天（剔除未来时间脏数据），避免窗口错位。"""
    dates = pd.to_datetime(orders["date"], errors="coerce").dropna()
    if dates.empty:
        return fallback or date.today()
    today = pd.Timestamp(date.today()) + pd.Timedelta(days=1)
    valid = dates[dates <= today]
    return (valid.max() if not valid.empty else dates.min()).date()


def daily_summary(orders: pd.DataFrame) -> pd.DataFrame:
    """按日汇总核心指标。"""
    if orders.empty:
        return _empty(["date", *SALE_COLUMNS])
    rows = orders[orders["is_valid"]].copy()
    rows["gmv_base"] = np.where(rows["is_paid"] | rows["is_refund"], rows["pay_amount"], 0.0)
    rows["net_base"] = np.where(rows["is_paid"], rows["pay_amount"], 0.0)
    rows["refund_base"] = np.where(rows["is_refund"], rows["pay_amount"], 0.0)
    rows["order_flag"] = np.where(rows["is_paid"], 1, 0)
    rows["units_paid"] = np.where(rows["is_paid"], rows["qty"], 0)
    rows["cost_paid"] = np.where(rows["is_paid"], rows["cost_amount"], 0.0)
    rows["margin_paid"] = np.where(rows["is_paid"], rows["margin"], 0.0)

    g = rows.groupby("date", as_index=False).agg(
        gmv=("gmv_base", "sum"),
        net_gmv=("net_base", "sum"),
        refund_amount=("refund_base", "sum"),
        orders=("order_flag", "sum"),
        buyers=("buyer_id", "nunique"),
        units=("units_paid", "sum"),
        cost=("cost_paid", "sum"),
        margin=("margin_paid", "sum"),
    )
    g["gmv"] = g["gmv"].round(2)
    g["net_gmv"] = g["net_gmv"].round(2)
    g["refund_amount"] = g["refund_amount"].round(2)
    g["aov"] = (g["net_gmv"] / g["orders"].replace(0, np.nan)).round(2)
    g["margin_rate"] = (g["margin"] / g["net_gmv"].replace(0, np.nan)).round(4)
    g["refund_rate"] = (g["refund_amount"] / g["gmv"].replace(0, np.nan)).round(4)
    return g.sort_values("date").reset_index(drop=True)


def daily_by_dimension(orders: pd.DataFrame, dim: str) -> pd.DataFrame:
    """日期 × 维度（platform / category / channel）汇总。"""
    if orders.empty:
        return _empty(["date", dim, "gmv", "orders", "units", "margin"])
    rows = orders[orders["is_valid"]].copy()
    rows = rows.assign(
        gmv_base=np.where(rows["is_paid"] | rows["is_refund"], rows["pay_amount"], 0.0),
        order_flag=np.where(rows["is_paid"], 1, 0),
        units_paid=np.where(rows["is_paid"], rows["qty"], 0),
        margin_paid=np.where(rows["is_paid"], rows["margin"], 0.0),
    )
    g = rows.groupby(["date", dim], as_index=False).agg(
        gmv=("gmv_base", "sum"), orders=("order_flag", "sum"),
        units=("units_paid", "sum"), margin=("margin_paid", "sum"),
    )
    g["gmv"] = g["gmv"].round(2)
    return g.sort_values(["date", dim]).reset_index(drop=True)


def dimension_summary(orders: pd.DataFrame, dim: str, days: int | None = None) -> pd.DataFrame:
    """维度贡献（默认取最近 days 天）。"""
    rows = orders[orders["is_valid"]].copy()
    if days:
        ref = reference_date(orders)
        rows = rows[rows["date"] > ref - timedelta(days=days)]
    if rows.empty:
        return _empty([dim, "gmv", "net_gmv", "orders", "buyers", "units", "margin",
                       "margin_rate", "refund_rate", "gmv_share"])
    rows["gmv_base"] = np.where(rows["is_paid"] | rows["is_refund"], rows["pay_amount"], 0.0)
    rows["net_base"] = np.where(rows["is_paid"], rows["pay_amount"], 0.0)
    rows["refund_base"] = np.where(rows["is_refund"], rows["pay_amount"], 0.0)
    rows["order_flag"] = np.where(rows["is_paid"], 1, 0)
    rows["units_paid"] = np.where(rows["is_paid"], rows["qty"], 0)
    rows["margin_paid"] = np.where(rows["is_paid"], rows["margin"], 0.0)
    g = rows.groupby(dim, as_index=False).agg(
        gmv=("gmv_base", "sum"), net_gmv=("net_base", "sum"),
        refund_amount=("refund_base", "sum"), orders=("order_flag", "sum"),
        buyers=("buyer_id", "nunique"), units=("units_paid", "sum"),
        margin=("margin_paid", "sum"),
    )
    g["gmv"] = g["gmv"].round(2)
    g["aov"] = (g["net_gmv"] / g["orders"].replace(0, np.nan)).round(2)
    g["margin_rate"] = (g["margin"] / g["net_gmv"].replace(0, np.nan)).round(4)
    g["refund_rate"] = (g["refund_amount"] / g["gmv"].replace(0, np.nan)).round(4)
    total = g["gmv"].sum()
    g["gmv_share"] = (g["gmv"] / total).round(4) if total else np.nan
    return g.sort_values("gmv", ascending=False).reset_index(drop=True)


def sku_ranking(orders: pd.DataFrame, n: int = 20, days: int | None = None) -> pd.DataFrame:
    """SKU 销售排行 + 帕累托 ABC 分类。"""
    rows = orders[orders["is_sale"]].copy()
    if days:
        ref = reference_date(orders)
        rows = rows[rows["date"] > ref - timedelta(days=days)]
    if rows.empty:
        return _empty(["sku_id", "sku_name", "category", "gmv", "units", "orders",
                       "margin", "margin_rate", "gmv_share", "cum_share", "abc"])
    g = rows.groupby(["sku_id", "sku_name", "category"], as_index=False).agg(
        gmv=("pay_amount", "sum"), units=("qty", "sum"), orders=("order_id", "nunique"),
        margin=("margin", "sum"))
    g["gmv"] = g["gmv"].round(2)
    g["margin_rate"] = (g["margin"] / g["gmv"].replace(0, np.nan)).round(4)
    total = g["gmv"].sum()
    g = g.sort_values("gmv", ascending=False).reset_index(drop=True)
    g["gmv_share"] = (g["gmv"] / total).round(4) if total else np.nan
    g["cum_share"] = g["gmv_share"].cumsum().round(4)
    g["abc"] = np.select([g["cum_share"] <= 0.7, g["cum_share"] <= 0.9], ["A", "B"], default="C")
    return g.head(n) if n else g


def traffic_summary(traffic: pd.DataFrame, dim: str = "channel") -> pd.DataFrame:
    """推广效果：CTR / CPC / CVR / ROI。"""
    if traffic.empty:
        return _empty([dim, "impressions", "clicks", "spend", "attr_orders", "attr_gmv",
                       "ctr", "cpc", "cvr", "roi"])
    g = traffic.groupby(dim, as_index=False).agg(
        impressions=("impressions", "sum"), clicks=("clicks", "sum"),
        spend=("spend", "sum"), attr_orders=("attr_orders", "sum"),
        attr_gmv=("attr_gmv", "sum"))
    g["ctr"] = (g["clicks"] / g["impressions"].replace(0, np.nan)).round(4)
    g["cpc"] = (g["spend"] / g["clicks"].replace(0, np.nan)).round(2)
    g["cvr"] = (g["attr_orders"] / g["clicks"].replace(0, np.nan)).round(4)
    g["roi"] = (g["attr_gmv"] / g["spend"].replace(0, np.nan)).round(2)
    for col in ("impressions", "clicks", "attr_orders"):
        g[col] = g[col].astype(int)
    return g.sort_values("attr_gmv", ascending=False).reset_index(drop=True)


def inventory_alert(inventory: pd.DataFrame, orders: pd.DataFrame,
                    cfg: Config | None = None, days: int = 30) -> pd.DataFrame:
    """库存健康度：可售天数、补货建议、滞销识别。"""
    cfg = cfg or get_config()
    cols = ["sku_id", "sku_name", "category", "warehouse", "on_hand", "in_transit",
            "safety_stock", "avg_daily_sales", "cover_days", "slow_days", "status",
            "suggest_qty", "suggestion"]
    if inventory.empty:
        return _empty(cols)
    target_cover = float(cfg.get("inventory.target_cover_days", 30))
    slow_limit = int(cfg.get("inventory.slow_moving_days", 45))
    safety_factor = float(cfg.get("inventory.safety_factor", 1.2))
    urgent = float(cfg.get("inventory.urgent_cover_days", 7))

    ref = reference_date(orders)
    sales = orders[orders["is_sale"] & (orders["date"] > ref - timedelta(days=days))]
    sku_sales = sales.groupby("sku_id")["qty"].sum()
    sku_last = orders[orders["is_sale"]].groupby("sku_id")["date"].max()
    latest_date = inventory["snapshot_date"].max()
    latest = inventory[inventory["snapshot_date"] == latest_date].copy()
    prod = orders[["sku_id", "sku_name", "category"]].drop_duplicates("sku_id")
    latest = latest.merge(prod, on="sku_id", how="left")
    latest["sku_name"] = latest["sku_name"].fillna("未知商品")
    latest["category"] = latest["category"].fillna("未分类")
    latest["avg_daily_sales"] = latest["sku_id"].map(sku_sales).fillna(0) / days
    latest["cover_days"] = np.where(
        latest["avg_daily_sales"] > 0,
        (latest["on_hand"] + latest["in_transit"]) / latest["avg_daily_sales"].replace(0, np.nan),
        np.inf)
    latest["slow_days"] = latest["sku_id"].map(
        lambda s: (ref - sku_last.get(s, ref - timedelta(days=365))).days)

    def _status(row: pd.Series) -> str:
        if row["on_hand"] <= 0 and row["avg_daily_sales"] > 0:
            return "断货"
        if row["cover_days"] < urgent:
            return "紧急补货"
        if row["on_hand"] < row["safety_stock"]:
            return "低于安全库存"
        if row["slow_days"] >= slow_limit and row["on_hand"] > 0:
            return "滞销"
        return "健康"

    latest["status"] = latest.apply(_status, axis=1)
    need = (latest["avg_daily_sales"] * target_cover * safety_factor
            - latest["on_hand"] - latest["in_transit"])
    latest["suggest_qty"] = np.where(
        latest["status"].isin(["断货", "紧急补货", "低于安全库存"]),
        np.ceil(need.clip(lower=0)), 0).astype(int)
    latest["suggestion"] = latest["status"].map({
        "断货": "立即补货并下架风险商品，避免流量浪费",
        "紧急补货": "今天内下采购单，优先保供爆款",
        "低于安全库存": "纳入本周补货计划",
        "滞销": "安排清仓活动/捆绑销售，暂停补货",
        "健康": "无需处理",
    })
    latest["cover_days"] = latest["cover_days"].replace(np.inf, np.nan).round(1)
    latest["avg_daily_sales"] = latest["avg_daily_sales"].round(2)
    order = {"断货": 0, "紧急补货": 1, "低于安全库存": 2, "滞销": 3, "健康": 4}
    latest["_o"] = latest["status"].map(order)
    return latest.sort_values(["_o", "cover_days"]).drop(columns="_o")[cols].reset_index(drop=True)


def campaign_effect(orders: pd.DataFrame, campaigns: pd.DataFrame,
                    cfg: Config | None = None) -> pd.DataFrame:
    """活动效果：活动期 vs 等长基准期，输出提升幅度、ROI 与增量 GMV。"""
    cfg = cfg or get_config()
    cols = ["campaign_id", "campaign_name", "campaign_type", "platform", "start_date",
            "end_date", "status", "days", "gmv", "orders", "aov", "discount_rate",
            "budget", "roi", "base_gmv", "lift_pct", "incremental_gmv", "conclusion"]
    if campaigns.empty or orders.empty:
        return _empty(cols)
    campaigns = campaigns.copy()
    for col in ("start_date", "end_date"):
        campaigns[col] = pd.to_datetime(campaigns[col], errors="coerce").dt.date
    campaigns = campaigns.dropna(subset=["start_date", "end_date"])
    ref = reference_date(orders)
    sale = orders[orders["is_sale"]]
    rows = []
    for c in campaigns.itertuples():
        start, end = c.start_date, c.end_date
        span = (end - start).days + 1
        base_start = start - timedelta(days=span)
        plat_mask = lambda d: pd.Series(True, index=d.index) if c.platform == "ALL" \
            else d["platform"].eq(c.platform)
        win = sale[(sale["date"] >= start) & (sale["date"] <= end)]
        win = win[plat_mask(win)]
        base = sale[(sale["date"] >= base_start) & (sale["date"] < start)]
        base = base[plat_mask(base)]
        gmv = float(win["pay_amount"].sum())
        base_gmv = float(base["pay_amount"].sum())
        orders_n = int(win["order_id"].nunique())
        aov = _safe_div(gmv, orders_n)
        discount_rate = _safe_div(float(win["discount_amount"].sum()),
                                  float(win["gross_amount"].sum()))
        budget = float(c.budget)
        roi = _safe_div(gmv, budget)
        lift = (gmv / base_gmv - 1) if base_gmv else np.nan
        incremental = gmv - base_gmv if base_gmv else np.nan
        status = "进行中" if start <= ref <= end else ("未开始" if start > ref else "已结束")
        if status == "进行中":
            conclusion = "活动进行中，数据待完整周期后再评估"
        elif pd.notna(lift) and lift >= 0.3 and (pd.isna(roi) or roi >= 1.5):
            conclusion = "活动拉动明显，建议复用玩法"
        elif pd.notna(lift) and lift < 0.05:
            conclusion = "未见明显增量，需检查选品与投放"
        else:
            conclusion = "增量一般，建议优化折扣力度与渠道"
        rows.append({
            "campaign_id": c.campaign_id, "campaign_name": c.campaign_name,
            "campaign_type": c.campaign_type, "platform": c.platform,
            "start_date": start, "end_date": end, "status": status, "days": span,
            "gmv": round(gmv, 2), "orders": orders_n,
            "aov": round(aov, 2) if pd.notna(aov) else np.nan,
            "discount_rate": round(discount_rate, 4) if pd.notna(discount_rate) else np.nan,
            "budget": budget,
            "roi": round(roi, 2) if pd.notna(roi) else np.nan,
            "base_gmv": round(base_gmv, 2),
            "lift_pct": round(lift, 4) if pd.notna(lift) else np.nan,
            "incremental_gmv": round(incremental, 2) if pd.notna(incremental) else np.nan,
            "conclusion": conclusion,
        })
    return pd.DataFrame(rows, columns=cols)


def weekly_trend(daily: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
    """周/月趋势与环比。"""
    if daily.empty:
        return _empty(["period", "gmv", "orders", "aov", "margin_rate", "gmv_change"])
    d = daily.copy()
    d["dt"] = pd.to_datetime(d["date"])
    if freq == "M":
        d["period"] = d["dt"].dt.strftime("%Y-%m")
    else:
        d["period"] = d["dt"].dt.to_period("W").astype(str)
    g = d.groupby("period", as_index=False).agg(
        gmv=("gmv", "sum"), net_gmv=("net_gmv", "sum"),
        orders=("orders", "sum"), margin=("margin", "sum"))
    g["aov"] = (g["net_gmv"] / g["orders"].replace(0, np.nan)).round(2)
    g["margin_rate"] = (g["margin"] / g["net_gmv"].replace(0, np.nan)).round(4)
    g["gmv_change"] = g["gmv"].pct_change().round(4)
    g["gmv"] = g["gmv"].round(2)
    return g[["period", "gmv", "net_gmv", "orders", "aov", "margin_rate", "gmv_change"]]


def kpi_overview(orders: pd.DataFrame, days: int = 30) -> dict[str, float]:
    """近 N 天 vs 前 N 天的核心指标对比，供首页指标卡使用。"""
    if orders.empty:
        return {}
    ref = reference_date(orders)
    cur = orders[(orders["date"] > ref - timedelta(days=days)) & orders["is_valid"]]
    prev = orders[(orders["date"] > ref - timedelta(days=2 * days))
                  & (orders["date"] <= ref - timedelta(days=days)) & orders["is_valid"]]

    def _agg(df: pd.DataFrame) -> dict[str, float]:
        gmv = float(df.loc[df["is_paid"] | df["is_refund"], "pay_amount"].sum())
        net = float(df.loc[df["is_paid"], "pay_amount"].sum())
        refund = float(df.loc[df["is_refund"], "pay_amount"].sum())
        n = int(df["is_paid"].sum())
        margin = float(df.loc[df["is_paid"], "margin"].sum())
        return {"gmv": gmv, "net_gmv": net, "orders": n,
                "aov": _safe_div(net, n), "margin_rate": _safe_div(margin, net),
                "refund_rate": _safe_div(refund, gmv)}

    cur_m, prev_m = _agg(cur), _agg(prev)
    out: dict[str, float] = {}
    for key, val in cur_m.items():
        out[key] = round(val, 4) if isinstance(val, float) else val
        prev_val = prev_m.get(key, 0) or 0
        out[f"{key}_change"] = round((val / prev_val - 1), 4) if prev_val else np.nan
    out["ref_date"] = ref.toordinal()
    out["days"] = days
    return out


def data_quality_score(issues: pd.DataFrame) -> float:
    """数据质量分：按"命中的规则种类"扣分（同类问题多条不重复扣），40 分保底。

    这样评分反映"数据有几类问题"，而不是被同一类问题的大量记录拉爆。
    """
    if issues is None or issues.empty:
        return 100.0
    key = "rule_id" if "rule_id" in issues.columns else "level"
    weights = {"high": 5, "medium": 2, "low": 1}
    penalty = sum(weights.get(level, 1) * group[key].nunique()
                  for level, group in issues.groupby("level"))
    return float(max(40, 100 - min(60, penalty)))
