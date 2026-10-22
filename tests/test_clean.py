"""清洗层测试：时间解析、去重、金额回填、状态归一。"""

from __future__ import annotations

import pandas as pd

from ecomflow.clean import parse_datetime


def test_parse_mixed_datetime_formats():
    raw = pd.Series([
        "2026-10-01 10:00:00", "2026/10/01 10:00", "10/01/2026 10:00",
        "2026-10-01", "20261002", "", None, "not-a-date",
    ])
    parsed = parse_datetime(raw)
    assert parsed.iloc[0] == pd.Timestamp("2026-10-01 10:00:00")
    assert parsed.iloc[1] == pd.Timestamp("2026-10-01 10:00:00")
    assert parsed.iloc[2] == pd.Timestamp("2026-10-01 10:00:00")
    assert parsed.iloc[3].date().isoformat() == "2026-10-01"
    assert parsed.iloc[4].date().isoformat() == "2026-10-02"
    assert pd.isna(parsed.iloc[5]) and pd.isna(parsed.iloc[7])


def test_orders_dedup_and_normalize(tables):
    orders, _ = tables
    df = orders["orders"]
    # 完全重复与重复推送都被收敛为唯一订单号
    assert df["order_id"].is_unique
    # 同一订单保留最新一次推送（金额 199）
    so2 = df.loc[df["order_id"] == "SO2"].iloc[0]
    assert abs(so2["pay_amount"] - 199.0) < 0.01
    # 状态与平台归一
    status = dict(zip(df["order_id"], df["order_status"]))
    assert status["SO2"] == "paid" and status["SO3"] == "refunded"
    assert status["SO5"] == "pending"
    platforms = set(df["platform"])
    assert {"Tmall", "JD", "PDD", "Douyin"} <= platforms


def test_orders_amount_fill_and_flags(tables):
    orders, _ = tables
    df = orders["orders"].set_index("order_id")
    # 实付缺失按 单价×数量-优惠 回填
    assert abs(df.loc["SO3", "pay_amount"] - 100.0) < 0.01
    # 勾稽不平标记
    assert bool(df.loc["SO7", "is_amount_mismatch"])
    assert abs(df.loc["SO7", "amount_gap"] - 160.0) < 0.01
    # 主数据缺失 / 数量异常 / 未来时间
    assert bool(df.loc["SO4", "is_orphan"])
    assert not bool(df.loc["SO5", "is_sale"])
    assert bool(df.loc["SO6", "is_future_time"])
    assert not bool(df.loc["SO6", "is_valid"])
    # 成本兜底：缺主数据时按兜底成本率估算，不产生空值
    assert df["unit_cost"].notna().all()
    assert df["margin"].notna().all()


def test_inventory_and_traffic_cleaning(tables):
    tables, _ = tables
    inv = tables["inventory"]
    assert (inv["on_hand"] >= 0).all()
    assert inv["is_negative_stock"].sum() == 1
    assert inv["safety_stock"].notna().all()
    assert not inv.duplicated(subset=["snapshot_date", "sku_id", "warehouse"]).any()

    traffic = tables["traffic"]
    assert (traffic["clicks"] <= traffic["impressions"]).all()
    assert (traffic["spend"] >= 0).all()
    assert traffic["spend"].notna().all()


def test_clean_log_records_every_rule(tables):
    _, log = tables
    rules = set(log["rule"])
    assert "完全重复行" in rules
    assert "订单号重复" in rules
    assert "实付金额缺失" in rules
    assert "金额勾稽不平" in rules
    assert "商品主数据缺失" in rules
    assert (log["hit_rows"] > 0).all()
