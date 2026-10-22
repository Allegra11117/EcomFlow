"""校验层：14 条业务规则 + 异常工单去重。

规则分四类：
    一致性  金额勾稽、归因偏差、主数据缺失
    完整性  重复数据、时间异常、数据断档
    业务健康 库存、滞销、退款率、负毛利、活动 ROI
    口径异常 零金额单、状态未识别

输出统一为异常清单（DataFrame），由 db.upsert_issues 落库去重，
再由 notify 推送给业务同学，形成"发现 - 处理 - 归档"闭环。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import metrics
from .config import Config, get_config
from .logging_setup import get_logger

log = get_logger(__name__)

LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}
ISSUE_COLUMNS = ["rule_id", "rule_name", "level", "scope", "detail_key", "message",
                 "suggestion", "metric"]


@dataclass
class RuleContext:
    tables: dict[str, pd.DataFrame]
    cfg: Config
    clean_log: pd.DataFrame
    ref_date: date

    def threshold(self, name: str, default: float) -> float:
        return float(self.cfg.get(f"validate.thresholds.{name}", default))

    @property
    def trend_days(self) -> int:
        return int(self.cfg.get("validate.trend_check_days", 14))


def _issue(rule_id: str, rule_name: str, level: str, scope: str,
           detail_key: str, message: str, suggestion: str, metric: str = "") -> dict:
    return {"rule_id": rule_id, "rule_name": rule_name, "level": level, "scope": scope,
            "detail_key": detail_key[:200], "message": message, "suggestion": suggestion,
            "metric": metric}


# ---------------------------------------------------------------- 一致性
def check_source_duplicates(ctx: RuleContext) -> list[dict]:
    out = []
    if ctx.clean_log is None or ctx.clean_log.empty:
        return out
    dup = ctx.clean_log[ctx.clean_log["rule"].str.contains("重复", na=False)]
    for r in dup.itertuples():
        level = "high" if "订单号" in r.rule else "medium"
        out.append(_issue(
            "R001", "源数据重复", level, r.table, f"{r.table}:{r.rule}",
            f"{r.table} 表 {r.rule} 命中 {r.hit_rows} 行：{r.message}",
            "核对平台导出任务是否重复执行；已在清洗阶段去重，需确认无漏单",
            f"{r.hit_rows} 行"))
    return out


def check_orphan_sku(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty or "is_orphan" not in orders:
        return []
    bad = orders[orders["is_orphan"]]
    if bad.empty:
        return []
    g = bad.groupby("sku_id").agg(n=("order_id", "size"), amt=("pay_amount", "sum"))
    return [
        _issue("R002", "订单商品无主数据", "high", "orders", f"{sku}",
               f"商品 {sku} 有 {int(r.n)} 笔订单但商品档案缺失，涉及实付 {r.amt:,.2f} 元",
               "补齐商品档案（品类/成本），否则毛利与品类分析会失真",
               f"{int(r.n)} 单 / {r.amt:,.2f} 元")
        for sku, r in g.iterrows()
    ]


def check_amount_mismatch(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    start = ctx.ref_date - timedelta(days=ctx.trend_days)
    bad = orders[orders["is_amount_mismatch"] & (orders["date"] >= start)]
    if bad.empty:
        return []
    g = bad.groupby("date").agg(n=("order_id", "size"), gap=("amount_gap", "sum"))
    return [
        _issue("R003", "订单金额勾稽不平", "medium", "orders", f"{d}",
               f"{d} 有 {int(r.n)} 笔订单实付与「单价×数量-优惠」不一致，累计差额 {r.gap:,.2f} 元",
               "核对平台账单/优惠分摊口径，差额超过 1 元的订单需人工复核",
               f"{int(r.n)} 单 / 差 {r.gap:,.2f} 元")
        for d, r in g.iterrows()
    ]


def check_attribution(ctx: RuleContext) -> list[dict]:
    traffic = ctx.tables.get("traffic")
    orders = ctx.tables.get("orders")
    if traffic is None or traffic.empty or orders is None or orders.empty:
        return []
    start = ctx.ref_date - timedelta(days=ctx.trend_days)
    t = traffic[traffic["stat_date"] >= start].groupby(
        ["stat_date", "platform"], as_index=False)["attr_gmv"].sum()
    o = orders[(orders["date"] >= start) & orders["is_paid"]].groupby(
        ["date", "platform"], as_index=False)["pay_amount"].sum()
    m = t.merge(o, left_on=["stat_date", "platform"], right_on=["date", "platform"],
                how="inner")
    m["diff_pct"] = (m["attr_gmv"] - m["pay_amount"]) / m["pay_amount"].replace(0, np.nan)
    limit = ctx.threshold("attribution_gmv_diff_pct", 15) / 100
    bad = m[m["diff_pct"].abs() > limit]
    return [
        _issue("R004", "推广归因 GMV 偏差", "medium", "traffic", f"{r.stat_date}@{r.platform}",
               f"{r.stat_date} {r.platform} 归因 GMV {r.attr_gmv:,.0f} 元，"
               f"与订单 GMV {r.pay_amount:,.0f} 元偏差 {r.diff_pct:+.1%}",
               "检查归因窗口/跨平台去重逻辑，偏差持续存在会导致 ROI 误判",
               f"偏差 {r.diff_pct:+.1%}")
        for r in bad.itertuples()
    ]


# ---------------------------------------------------------------- 完整性
def check_time_anomaly(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    out = []
    missing = int(orders["order_dt"].isna().sum())
    if missing:
        out.append(_issue("R005", "下单时间异常", "high", "orders", "order_time_null",
                          f"{missing} 笔订单下单时间为空或无法解析",
                          "导出时保留原始时间字符串，或统一为 yyyy-MM-dd HH:mm:ss",
                          f"{missing} 单"))
    future = int(orders["is_future_time"].sum())
    if future:
        sample = orders[orders["is_future_time"]]["order_id"].head(3).tolist()
        out.append(_issue("R005", "下单时间异常", "high", "orders", "order_time_future",
                          f"{future} 笔订单下单时间晚于今天（示例 {', '.join(sample)}）",
                          "检查服务器时区/平台回传时间字段，避免污染当日 GMV",
                          f"{future} 单"))
    return out


def check_data_gap(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    out = []
    stale = int(ctx.threshold("stale_days", 2))
    start = ctx.ref_date - timedelta(days=ctx.trend_days)
    days = pd.date_range(start, ctx.ref_date, freq="D").date
    for platform, grp in orders.groupby("platform"):
        series = grp.groupby("date").size().reindex(days, fill_value=0)
        run = 0
        for d, n in series.items():
            run = run + 1 if n == 0 else 0
            if run == stale:
                out.append(_issue(
                    "R006", "数据断档", "high", "orders", f"{platform}@{d}",
                    f"{platform} 连续 {stale} 天无订单数据（截至 {d}）",
                    "检查采集任务/接口是否失败，补数后重跑当日报表",
                    f"连续 {stale} 天"))
    traffic = ctx.tables.get("traffic")
    if traffic is not None and not traffic.empty:
        have = set(zip(traffic["stat_date"], traffic["platform"]))
        platforms = sorted(traffic["platform"].dropna().unique())
        for d in days:
            missing = [p for p in platforms if (d, p) not in have]
            if missing:
                out.append(_issue(
                    "R006", "数据断档", "medium", "traffic", f"traffic:{missing[0]}@{d}",
                    f"{d} {'、'.join(missing)} 缺少推广流量数据",
                    "补采当日推广报表，否则 ROI 与归因分析失真",
                    f"缺 {len(missing)} 个平台"))
    return out


# ---------------------------------------------------------------- 业务健康
def check_inventory(ctx: RuleContext) -> list[dict]:
    inventory = ctx.tables.get("inventory")
    orders = ctx.tables.get("orders")
    if inventory is None or inventory.empty:
        return []
    out = []
    neg = inventory[inventory["is_negative_stock"]]
    if not neg.empty:
        latest = neg[neg["snapshot_date"] == neg["snapshot_date"].max()]
        for r in latest.itertuples():
            out.append(_issue(
                "R007", "库存为负", "high", "inventory", f"{r.sku_id}@{r.warehouse}",
                f"{r.sku_id}（{r.warehouse}）回传库存为负，已按 0 处理",
                "排查超卖/退货回传延迟，必要时冻结该 SKU 的可售库存",
                "负库存"))
    alert = metrics.inventory_alert(inventory, orders, ctx.cfg)
    if alert.empty:
        return out
    # 按 SKU 合并多仓，避免同一商品重复告警；只保留最严重的状态
    severity = {"断货": 0, "紧急补货": 1, "低于安全库存": 2, "滞销": 3, "健康": 4}
    alert = alert.assign(_o=alert["status"].map(severity).fillna(9)).sort_values(
        ["_o", "cover_days"])
    sku_level = alert.groupby(["sku_id", "sku_name"], as_index=False).agg(
        on_hand=("on_hand", "sum"), in_transit=("in_transit", "sum"),
        cover_days=("cover_days", "min"), suggest_qty=("suggest_qty", "sum"),
        slow_days=("slow_days", "max"), status=("status", "first"),
        suggestion=("suggestion", "first"))

    # 异常中心只放"今天就要处理"的：已断货 or 可售不足 3 天；其余在报表库存页完整呈现
    stock_out = sku_level[sku_level["status"] == "断货"]
    near_out = sku_level[(sku_level["status"] == "紧急补货")
                         & (sku_level["cover_days"] < 3)].head(8)
    for r in pd.concat([stock_out, near_out]).itertuples():
        level = "high" if r.status == "断货" else "medium"
        cover = f"{r.cover_days:.1f}" if pd.notna(r.cover_days) else "-"
        out.append(_issue(
            "R008", "库存预警", level, "inventory", f"{r.sku_id}",
            f"{r.sku_id} {r.sku_name} {r.status}：全仓现有 {int(r.on_hand)} 件、"
            f"在途 {int(r.in_transit)} 件，可售 {cover} 天",
            f"建议补货 {int(r.suggest_qty)} 件；{r.suggestion}",
            f"可售 {cover} 天"))
    slow = sku_level[(sku_level["status"] == "滞销") & (sku_level["on_hand"] >= 50)]
    for r in slow.head(10).itertuples():
        out.append(_issue(
            "R009", "库存滞销", "low", "inventory", f"{r.sku_id}",
            f"{r.sku_id} {r.sku_name} 已 {int(r.slow_days)} 天无动销，全仓库存 {int(r.on_hand)} 件",
            "安排清仓/捆绑销售，暂停补货，释放仓储与资金占用",
            f"滞销 {int(r.slow_days)} 天"))
    return out


def check_margin(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    bad = orders[orders["is_sale"] & (orders["margin"] < 0)]
    if bad.empty:
        return []
    g = bad.groupby("sku_id").agg(n=("order_id", "size"), loss=("margin", "sum"),
                                  rate=("margin_rate", "mean"))
    return [
        _issue("R010", "负毛利订单", "medium", "orders", f"{sku}",
               f"{sku} 有 {int(r.n)} 笔订单亏损，累计 {r.loss:,.2f} 元，平均毛利率 {r.rate:.1%}",
               "检查活动折扣与成本口径，亏损单需业务确认是否继续投放",
               f"{int(r.n)} 单 / {r.loss:,.2f} 元")
        for sku, r in g.iterrows()
    ]


def check_refund_rate(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    # 日粒度样本太小，改为"周 × 平台"，避免正常波动被误报
    start = ctx.ref_date - timedelta(days=28)
    d = orders[orders["is_valid"] & (orders["date"] >= start)].copy()
    d["week"] = pd.to_datetime(d["date"]).dt.strftime("%G-W%V")
    g = d.groupby(["week", "platform"], as_index=False).agg(
        paid=("is_paid", "sum"), refund=("is_refund", "sum"))
    g["total"] = g["paid"] + g["refund"]
    g["rate"] = g["refund"] / g["total"].replace(0, np.nan)
    limit = ctx.threshold("refund_rate_pct", 8) / 100
    bad = g[(g["total"] >= 50) & (g["rate"] > limit)]
    out = [
        _issue("R011", "退款率超阈值", "medium", "orders", f"{r.week}@{r.platform}",
               f"{r.week} {r.platform} 退款率 {r.rate:.1%}（{int(r.refund)}/{int(r.total)} 单），"
               f"高于阈值 {limit:.0%}",
               "抽查退款原因（质量/物流/描述不符），同步客服与商品详情页",
               f"退款率 {r.rate:.1%}")
        for r in bad.itertuples()
    ]
    # SKU 维度高退款：关注单品质量
    start30 = ctx.ref_date - timedelta(days=30)
    s = orders[orders["is_valid"] & (orders["date"] >= start30)]
    sku = s.groupby("sku_id", as_index=False).agg(paid=("is_paid", "sum"),
                                                  refund=("is_refund", "sum"))
    sku["total"] = sku["paid"] + sku["refund"]
    sku["rate"] = sku["refund"] / sku["total"].replace(0, np.nan)
    for r in sku[(sku["total"] >= 30) & (sku["rate"] > 0.15)].itertuples():
        out.append(_issue("R011", "退款率超阈值", "low", "orders", f"sku:{r.sku_id}",
                          f"SKU {r.sku_id} 近 30 天退款率 {r.rate:.1%}（{int(r.refund)}/{int(r.total)} 单）",
                          "排查批次质量与详情页描述，必要时下架整改",
                          f"退款率 {r.rate:.1%}"))
    return out


def check_campaign(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    campaigns = ctx.tables.get("campaigns")
    if orders is None or campaigns is None or campaigns.empty:
        return []
    eff = metrics.campaign_effect(orders, campaigns, ctx.cfg)
    limit = ctx.threshold("campaign_roi_min", 1.5)
    out = []
    for r in eff[(eff["status"] == "已结束") & (eff["roi"] < limit)].itertuples():
        out.append(_issue(
            "R012", "活动效果未达预期", "low", "campaigns", f"{r.campaign_id}",
            f"{r.campaign_name} 活动 ROI {r.roi:.2f}（预算 {r.budget:,.0f} 元），"
            f"低于目标 {limit}",
            "复盘选品与投放渠道，下次活动优先高转化 SKU 并控制折扣",
            f"ROI {r.roi:.2f}"))
    return out


# ---------------------------------------------------------------- 口径异常
def check_zero_amount(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    paid = orders[orders["is_paid"]]
    if paid.empty:
        return []
    zero = int((paid["pay_amount"] <= 0.01).sum())
    rate = zero / len(paid)
    if rate > 0.005:
        return [_issue("R013", "零金额支付单", "low", "orders", "pay_amount_zero",
                       f"{zero} 笔支付成功订单实付接近 0，占比 {rate:.2%}",
                       "确认是否为赠品/内部单/优惠叠加异常，必要时从销售分析剔除",
                       f"{zero} 单")]
    return []


def check_unknown_status(ctx: RuleContext) -> list[dict]:
    orders = ctx.tables.get("orders")
    if orders is None or orders.empty:
        return []
    unknown = orders[orders["order_status"] == "unknown"]
    if unknown.empty:
        return []
    values = unknown["order_status"].value_counts().index[:3].tolist()
    return [_issue("R014", "订单状态未识别", "medium", "orders", "unknown_status",
                   f"{len(unknown)} 笔订单状态无法映射到标准状态（示例 {values}）",
                   "在 config.yaml 的 clean.status_map 补充平台状态文案",
                   f"{len(unknown)} 单")]


RULES = [
    check_source_duplicates, check_orphan_sku, check_amount_mismatch, check_attribution,
    check_time_anomaly, check_data_gap, check_inventory, check_margin,
    check_refund_rate, check_campaign, check_zero_amount, check_unknown_status,
]


def validate(tables: dict[str, pd.DataFrame], cfg: Config | None = None,
             clean_log: pd.DataFrame | None = None) -> pd.DataFrame:
    """执行全部规则，返回异常清单（已按等级排序）。"""
    cfg = cfg or get_config()
    orders = tables.get("orders")
    if orders is None or orders.empty:
        return pd.DataFrame(columns=ISSUE_COLUMNS)
    ctx = RuleContext(tables=tables, cfg=cfg,
                      clean_log=clean_log if clean_log is not None else pd.DataFrame(),
                      ref_date=metrics.reference_date(orders))
    rows: list[dict] = []
    for func in RULES:
        try:
            rows.extend(func(ctx))
        except Exception as exc:  # noqa: BLE001 - 单条规则异常不应阻断整批校验
            log.exception("规则 %s 执行失败: %s", func.__name__, exc)
    if not rows:
        return pd.DataFrame(columns=ISSUE_COLUMNS)
    df = pd.DataFrame(rows, columns=ISSUE_COLUMNS)
    df = df.drop_duplicates(subset=["rule_id", "detail_key"])
    df["_order"] = df["level"].map(LEVEL_ORDER).fillna(9)
    df = df.sort_values(["_order", "rule_id"]).drop(columns="_order").reset_index(drop=True)
    log.info("校验完成：high %d / medium %d / low %d", (df.level == "high").sum(),
             (df.level == "medium").sum(), (df.level == "low").sum())
    return df


def summarize(issues: pd.DataFrame) -> dict[str, int]:
    if issues.empty:
        return {"high": 0, "medium": 0, "low": 0, "total": 0}
    counts = issues["level"].value_counts().to_dict()
    return {"high": int(counts.get("high", 0)), "medium": int(counts.get("medium", 0)),
            "low": int(counts.get("low", 0)), "total": int(len(issues))}
