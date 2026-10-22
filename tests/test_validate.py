"""校验规则测试：命中、去重、等级。"""

from __future__ import annotations

import pandas as pd

from ecom_ops.validate import summarize, validate


def test_core_rules_hit(tables, cfg):
    cleaned, log = tables
    issues = validate(cleaned, cfg, log)
    rules = set(issues["rule_id"])
    # 主数据缺失 / 金额勾稽 / 时间异常 / 负库存 / 断货 / 重复
    assert {"R001", "R002", "R003", "R005", "R007", "R008"} <= rules
    assert issues["detail_key"].is_unique
    assert set(issues["level"]) <= {"high", "medium", "low"}


def test_issue_levels_and_summary(tables, cfg):
    cleaned, log = tables
    issues = validate(cleaned, cfg, log)
    summary = summarize(issues)
    assert summary["total"] == len(issues)
    assert summary["high"] >= 1
    # 按等级排序：high 在前
    order = issues["level"].map({"high": 0, "medium": 1, "low": 2}).tolist()
    assert order == sorted(order)


def test_empty_orders_returns_empty_frame(cfg):
    issues = validate({"orders": pd.DataFrame()}, cfg)
    assert issues.empty
    assert "rule_id" in issues.columns


def test_attribution_rule_uses_threshold(tables, cfg):
    cleaned, log = tables
    issues = validate(cleaned, cfg, log)
    # 样本里流量归因与订单口径接近，不应产生偏差告警
    assert "R004" not in set(issues["rule_id"])
