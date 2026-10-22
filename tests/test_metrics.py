"""指标口径测试：GMV/退款/库存健康度/活动提升。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ecomflow import metrics


def test_daily_summary_caliber(tables):
    orders, _ = tables
    daily = metrics.daily_summary(orders["orders"])
    # GMV 含退款单，净销售额只算支付成功，未来时间订单不计入
    assert round(float(daily["gmv"].sum()), 2) == 839.00
    assert round(float(daily["net_gmv"].sum()), 2) == 739.00
    assert round(float(daily["refund_amount"].sum()), 2) == 100.00
    assert round(float(daily["orders"].sum()), 2) == 4
    assert daily["date"].is_monotonic_increasing


def test_dimension_summary_share(tables):
    orders, _ = tables
    plat = metrics.dimension_summary(orders["orders"], "platform")
    assert plat["gmv"].is_monotonic_decreasing
    assert pytest.approx(float(plat["gmv_share"].sum()), abs=1e-6) == 1.0
    # 客单价 = 净销售额 / 订单数
    row = plat.iloc[0]
    assert pytest.approx(row["aov"], abs=0.01) == round(row["net_gmv"] / row["orders"], 2)


def test_sku_ranking_abc(tables):
    orders, _ = tables
    rank = metrics.sku_ranking(orders["orders"], n=None)
    assert set(rank["abc"]) <= {"A", "B", "C"}
    assert rank["cum_share"].is_monotonic_increasing


def test_inventory_alert_status(tables, cfg):
    orders, _ = tables
    alert = metrics.inventory_alert(orders["inventory"], orders["orders"], cfg)
    sku1 = alert[alert["sku_id"] == "SKU0001"]
    assert (sku1["status"] == "断货").any()
    assert sku1["suggest_qty"].sum() > 0
    sku2 = alert[(alert["sku_id"] == "SKU0002") & (alert["warehouse"] == "华东仓")]
    assert sku2.iloc[0]["status"] == "健康"


def test_campaign_effect_lift_and_roi(cfg):
    orders = pd.DataFrame({
        "order_id": ["a", "b", "c", "d", "e"],
        "date": [date(2026, 10, 10), date(2026, 10, 11),
                 date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)],
        "platform": ["Tmall"] * 5,
        "is_sale": [True] * 5,
        "pay_amount": [1000.0, 1000.0, 500.0, 500.0, 0.0],
        "discount_amount": [100.0] * 5,
        "gross_amount": [1100.0] * 5,
    })
    campaigns = pd.DataFrame([{
        "campaign_id": "C1", "campaign_name": "测试大促", "campaign_type": "平台大促",
        "start_date": date(2026, 10, 10), "end_date": date(2026, 10, 11),
        "platform": "ALL", "sku_ids": "SKU0001", "budget": 500.0,
    }])
    eff = metrics.campaign_effect(orders, campaigns, cfg)
    row = eff.iloc[0]
    assert row["gmv"] == 2000.0
    assert row["base_gmv"] == 1000.0
    assert pytest.approx(row["lift_pct"], abs=1e-6) == 1.0
    assert pytest.approx(row["roi"], abs=0.01) == 4.0
    assert row["status"] == "已结束"


def test_kpi_overview_and_quality(tables):
    orders, _ = tables
    kpi = metrics.kpi_overview(orders["orders"], days=30)
    assert kpi["gmv"] > 0
    assert 0 <= kpi["refund_rate"] <= 1
    empty = pd.DataFrame(columns=["level"])
    assert metrics.data_quality_score(empty) == 100.0
    bad = pd.DataFrame({"level": ["high", "high", "medium", "low"],
                        "rule_id": ["R1", "R1", "R2", "R3"]})
    # 同类问题多条只扣一次：high 5 + medium 2 + low 1
    assert metrics.data_quality_score(bad) == pytest.approx(92.0)
