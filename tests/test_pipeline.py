"""端到端回归：一条命令跑通采集-清洗-校验-落库-报表。"""

from __future__ import annotations

import pytest

from ecom_ops import db
from ecom_ops.config import ROOT
from ecom_ops.pipeline import run_pipeline

DATA_READY = (ROOT / "data" / "raw" / "orders.csv").exists()


@pytest.mark.integration
@pytest.mark.skipif(not DATA_READY, reason="需要先执行 python scripts/gen_mock_data.py")
def test_pipeline_end_to_end():
    result = run_pipeline(send_alert=False)
    assert result.status == "success"
    assert len(result.tables["orders"]) > 1000
    assert not result.issues.empty
    assert result.outputs["excel"].exists()
    assert result.outputs["html"].exists()

    for table in ("orders", "daily_kpi", "inventory_alert", "campaign_effect",
                  "traffic_summary", "dq_issues", "pipeline_runs"):
        assert db.table_exists(table)
    issues = db.list_issues(status="open")
    assert not issues.empty
    assert {"issue_id", "level", "message", "occurrences"} <= set(issues.columns)
