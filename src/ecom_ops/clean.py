"""清洗层：把平台导出的"脏数据"整理成可信的分析底表。

处理原则：
1. 能修则修（时间格式、状态别名、金额缺失按勾稽关系回填）；
2. 不能修的保留并打标，交给异常中心跟进，绝不静默丢数据；
3. 每一步的命中行数写入清洗日志，报表里可追溯。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config, get_config
from .logging_setup import get_logger

log = get_logger(__name__)

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%Y%m%d",
)


class CleanLog:
    """清洗过程记录。"""

    def __init__(self) -> None:
        self._rows: list[dict] = []

    def add(self, table: str, rule: str, hit_rows: int, message: str = "") -> None:
        if hit_rows:
            self._rows.append({"table": table, "rule": rule, "hit_rows": int(hit_rows),
                               "message": message})

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self._rows, columns=["table", "rule", "hit_rows", "message"])


def parse_datetime(series: pd.Series) -> pd.Series:
    """多格式时间解析：先按常见格式逐个尝试，最后用 mixed 兜底。"""
    raw = series.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in DATETIME_FORMATS:
        mask = out.isna() & raw.notna()
        if not mask.any():
            break
        parsed = pd.to_datetime(raw[mask], format=fmt, errors="coerce")
        out.loc[mask] = parsed
    mask = out.isna() & raw.notna()
    if mask.any():
        out.loc[mask] = pd.to_datetime(raw[mask], format="mixed", errors="coerce")
    return out


def normalize_by_map(series: pd.Series, mapping: dict[str, str], default: str = "unknown") -> pd.Series:
    """按映射表归一化（兼容大小写与首尾空格）。"""
    clean = series.astype("string").str.strip()
    direct = clean.map(mapping)
    lower_map = {str(k).strip().lower(): v for k, v in mapping.items()}
    lower = clean.str.lower().map(lower_map)
    return direct.fillna(lower).fillna(default)


def _to_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def clean_products(df: pd.DataFrame, cfg: Config, clog: CleanLog) -> pd.DataFrame:
    out = df.copy()
    n0 = len(out)
    out = out.drop_duplicates(subset=["sku_id"], keep="first")
    clog.add("products", "SKU 主数据去重", n0 - len(out), "同一 SKU 保留首条主数据，忽略重复导入")

    _to_numeric(out, ["cost", "list_price"])
    out["launch_date"] = parse_datetime(out["launch_date"]).dt.date
    bad_price = (out["list_price"].isna() | (out["list_price"] <= 0)).sum()
    out.loc[out["list_price"].isna() | (out["list_price"] <= 0), "list_price"] = np.nan
    clog.add("products", "售价缺失/非正", int(bad_price), "保留空值，分析时按订单实付推算")
    neg_cost = (out["cost"] < 0).sum()
    out.loc[out["cost"] < 0, "cost"] = np.nan
    clog.add("products", "成本为负", int(neg_cost), "置空后按兜底成本率估算")
    out["category"] = out["category"].astype("string").str.strip().fillna("未分类")
    out["lifecycle"] = out["lifecycle"].astype("string").fillna("成熟")
    return out.reset_index(drop=True)


def clean_orders(df: pd.DataFrame, products: pd.DataFrame, cfg: Config,
                 clog: CleanLog) -> pd.DataFrame:
    out = df.copy()
    n0 = len(out)
    out = out.drop_duplicates()
    clog.add("orders", "完全重复行", n0 - len(out), "平台重复导出，按全字段去重")

    _to_numeric(out, ["qty", "unit_price", "discount_amount", "pay_amount"])
    out["order_dt"] = parse_datetime(out["order_time"])
    clog.add("orders", "下单时间无法解析", int(out["order_dt"].isna().sum()),
             "保留原值并标记，需业务确认")

    out["order_status"] = normalize_by_map(
        out["order_status"], cfg.section("clean.status_map"))
    out["platform"] = normalize_by_map(
        out["platform"], cfg.section("clean.platform_map"), default="其他")
    unknown_status = (out["order_status"] == "unknown").sum()
    clog.add("orders", "订单状态未识别", int(unknown_status), "统一标记为 unknown 不参与销售统计")

    # 同一订单号多次推送：保留时间最新的一条
    out = out.sort_values("order_dt", na_position="first")
    before = len(out)
    out = out.drop_duplicates(subset=["order_id"], keep="last")
    clog.add("orders", "订单号重复", before - len(out), "保留最新推送，避免重复计 GMV")

    bad_qty = (out["qty"].isna() | (out["qty"] <= 0)).sum()
    clog.add("orders", "数量缺失/非正", int(bad_qty), "视为无效交易，不参与销售与库存核算")

    gross = out["unit_price"].fillna(0) * out["qty"].fillna(0)
    discount = out["discount_amount"].fillna(0).clip(lower=0)
    missing_pay = out["pay_amount"].isna().sum()
    out["pay_amount"] = out["pay_amount"].fillna(gross - discount)
    clog.add("orders", "实付金额缺失", int(missing_pay), "按 单价×数量-优惠 回填")

    neg_pay = (out["pay_amount"] < 0).sum()
    out.loc[out["pay_amount"] < 0, "pay_amount"] = 0.0
    clog.add("orders", "实付金额为负", int(neg_pay), "置 0 并进入异常清单")

    out["gross_amount"] = gross.round(2)
    out["discount_amount"] = discount.round(2)
    out["pay_amount"] = out["pay_amount"].round(2)
    out["amount_gap"] = (out["pay_amount"] - (out["gross_amount"] - out["discount_amount"])).round(2)
    out["is_amount_mismatch"] = out["amount_gap"].abs() > 0.01
    clog.add("orders", "金额勾稽不平", int(out["is_amount_mismatch"].sum()),
             "实付 ≠ 单价×数量-优惠，进入异常清单复核")

    # 关联商品主数据
    prod_cols = ["sku_id", "sku_name", "category", "cost", "list_price", "lifecycle"]
    merged = out.merge(products[prod_cols], on="sku_id", how="left", indicator=True)
    merged["is_orphan"] = merged["_merge"].eq("left_only")
    merged = merged.drop(columns=["_merge"])
    n_orphan = int(merged["is_orphan"].sum())
    clog.add("orders", "商品主数据缺失", n_orphan, "订单保留，SKU 标为未知待补主数据")

    fallback_cost = float(cfg.get("clean.fallback_cost_ratio", 0.35))
    unit_cost = merged["cost"]
    unit_cost = unit_cost.fillna(merged["unit_price"] * fallback_cost)
    unit_cost = unit_cost.where(unit_cost > 0, merged["unit_price"] * fallback_cost)
    merged["unit_cost"] = unit_cost.round(2)
    merged["cost_amount"] = (merged["unit_cost"] * merged["qty"]).round(2)
    merged["margin"] = (merged["pay_amount"] - merged["cost_amount"]).round(2)
    merged["margin_rate"] = np.where(
        merged["pay_amount"] > 0, merged["margin"] / merged["pay_amount"], np.nan)

    merged["sku_name"] = merged["sku_name"].fillna("未匹配商品")
    merged["category"] = merged["category"].fillna("未分类")
    merged["lifecycle"] = merged["lifecycle"].fillna("未知")

    now = pd.Timestamp.now()
    merged["is_future_time"] = merged["order_dt"].notna() & (merged["order_dt"] > now + pd.Timedelta(days=1))
    merged["is_paid"] = merged["order_status"].eq("paid")
    merged["is_refund"] = merged["order_status"].eq("refunded")
    merged["is_valid"] = merged["order_dt"].notna() & ~merged["is_future_time"]
    merged["is_sale"] = merged["is_paid"] & merged["is_valid"] & (merged["qty"] > 0)

    merged["date"] = merged["order_dt"].dt.date
    merged["month"] = merged["order_dt"].dt.strftime("%Y-%m")
    merged["week"] = merged["order_dt"].dt.strftime("%G-W%V")
    merged["dow"] = merged["order_dt"].dt.dayofweek
    merged["hour"] = merged["order_dt"].dt.hour
    return merged.reset_index(drop=True)


def clean_inventory(df: pd.DataFrame, cfg: Config, clog: CleanLog) -> pd.DataFrame:
    out = df.copy()
    n0 = len(out)
    out = out.drop_duplicates()
    clog.add("inventory", "重复快照行", n0 - len(out), "同一 SKU/仓库/日期只保留一条")

    out["snapshot_date"] = parse_datetime(out["snapshot_date"]).dt.date
    _to_numeric(out, ["on_hand", "in_transit", "safety_stock"])
    neg = (out["on_hand"] < 0).sum()
    out["is_negative_stock"] = out["on_hand"] < 0
    out.loc[out["on_hand"] < 0, "on_hand"] = 0
    clog.add("inventory", "库存为负", int(neg), "置 0 并进入异常清单（多为超卖或回传延迟）")

    out["in_transit"] = out["in_transit"].fillna(0).clip(lower=0)
    median_safety = out.groupby("sku_id")["safety_stock"].transform("median")
    missing_safety = out["safety_stock"].isna().sum()
    out["safety_stock"] = out["safety_stock"].fillna(median_safety)
    out["safety_stock"] = out["safety_stock"].fillna((out["on_hand"] * 0.2).round(0))
    clog.add("inventory", "安全库存缺失", int(missing_safety), "用同 SKU 中位数回填")

    out["available_stock"] = out["on_hand"] + out["in_transit"]
    return out.reset_index(drop=True)


def clean_traffic(df: pd.DataFrame, cfg: Config, clog: CleanLog) -> pd.DataFrame:
    out = df.copy()
    out["stat_date"] = parse_datetime(out["stat_date"]).dt.date
    if "platform" in out.columns:
        out["platform"] = normalize_by_map(
            out["platform"], cfg.section("clean.platform_map"), default="其他")
    _to_numeric(out, ["impressions", "clicks", "spend", "attr_orders", "attr_gmv"])

    missing_spend = out["spend"].isna().sum()
    out["spend"] = out["spend"].fillna(0).clip(lower=0)
    clog.add("traffic", "推广花费缺失", int(missing_spend), "按 0 处理并在报表中提示")

    out["impressions"] = out["impressions"].fillna(0).clip(lower=0)
    out["clicks"] = out["clicks"].fillna(0).clip(lower=0)
    over_click = (out["clicks"] > out["impressions"]).sum()
    out.loc[out["clicks"] > out["impressions"], "clicks"] = out["impressions"]
    clog.add("traffic", "点击量大于曝光量", int(over_click), "按曝光量截断，排查埋点口径")

    out["attr_orders"] = out["attr_orders"].fillna(0).clip(lower=0)
    out["attr_gmv"] = out["attr_gmv"].fillna(0).clip(lower=0)
    out["ctr"] = np.where(out["impressions"] > 0, out["clicks"] / out["impressions"], np.nan)
    out["cpc"] = np.where(out["clicks"] > 0, out["spend"] / out["clicks"], np.nan)
    out["cvr"] = np.where(out["clicks"] > 0, out["attr_orders"] / out["clicks"], np.nan)
    out["roi"] = np.where(out["spend"] > 0, out["attr_gmv"] / out["spend"], np.nan)
    return out.reset_index(drop=True)


def clean_campaigns(df: pd.DataFrame, clog: CleanLog) -> pd.DataFrame:
    out = df.copy()
    out["start_date"] = parse_datetime(out["start_date"]).dt.date
    out["end_date"] = parse_datetime(out["end_date"]).dt.date
    _to_numeric(out, ["budget"])
    out["budget"] = out["budget"].fillna(0).clip(lower=0)
    bad = out["start_date"].isna() | out["end_date"].isna()
    clog.add("campaigns", "活动日期缺失", int(bad.sum()), "无法参与活动效果评估")
    out = out[~bad]
    out["duration_days"] = (pd.to_datetime(out["end_date"]) - pd.to_datetime(out["start_date"])).dt.days + 1
    return out.reset_index(drop=True)


def clean_all(tables: dict[str, pd.DataFrame], cfg: Config | None = None) -> tuple[dict[str, pd.DataFrame],
                                                                                CleanLog]:
    cfg = cfg or get_config()
    clog = CleanLog()
    missing = [t for t in ("products", "orders") if t not in tables]
    if missing:
        raise ValueError(f"缺少核心数据源: {missing}，无法继续清洗")

    products = clean_products(tables["products"], cfg, clog)
    out: dict[str, pd.DataFrame] = {
        "products": products,
        "orders": clean_orders(tables["orders"], products, cfg, clog),
    }
    if "inventory" in tables:
        out["inventory"] = clean_inventory(tables["inventory"], cfg, clog)
    if "traffic" in tables:
        out["traffic"] = clean_traffic(tables["traffic"], cfg, clog)
    if "campaigns" in tables:
        out["campaigns"] = clean_campaigns(tables["campaigns"], clog)
    return out, clog
