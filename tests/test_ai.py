"""AI 辅助模块测试：只读护栏、查询计划、产出校验。"""

from __future__ import annotations

import pytest

from ecom_ops.ai import guard_sql, plan_question


@pytest.mark.parametrize("sql", [
    "DROP TABLE orders",
    "delete from orders",
    "select * from orders; delete from orders",
    "select * from orders -- 注释",
    "select * from sqlite_master",
    "attach database 'x.db' as x",
    "pragma table_info(orders)",
])
def test_guard_rejects_unsafe_sql(sql, cfg):
    with pytest.raises((PermissionError, ValueError)):
        guard_sql(sql, cfg)


def test_guard_appends_limit(cfg):
    sql = guard_sql("select gmv from daily_kpi", cfg)
    assert sql.lower().endswith("limit 200")
    assert ";" not in sql


def test_plan_platform_gmv():
    plan = plan_question("近30天各平台GMV")
    low = plan.sql.lower()
    assert "from orders" in low
    assert "group by platform" in low
    assert "pay_amount" in low
    guard_sql(plan.sql)


def test_plan_inventory_question():
    plan = plan_question("哪些商品需要补货")
    assert "inventory_alert" in plan.sql
    assert "紧急补货" in plan.sql
    guard_sql(plan.sql)


def test_plan_traffic_roi():
    plan = plan_question("各渠道ROI是多少")
    assert "traffic_summary" in plan.sql
    assert "roi" in plan.sql.lower()


def test_plan_time_window():
    plan = plan_question("近7天GMV")
    assert "date >" in plan.sql
    assert "近7天" in plan.explanation
