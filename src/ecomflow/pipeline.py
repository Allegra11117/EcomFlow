"""跑批编排：采集 -> 清洗 -> 校验 -> 落库 -> 报表 -> 提醒，一条命令完成。

这个模块是"重复流程自动化"的核心：任何业务同学只需要执行一次命令，
或由定时任务在每天早上触发，产出物固定落在 output/ 目录。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import db, metrics, report, validate
from .clean import clean_all
from .config import Config, ensure_dirs, get_config
from .ingest import load_all
from .logging_setup import get_logger
from .notify import notify as push_notify

log = get_logger(__name__)


@dataclass
class PipelineResult:
    run_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    issues: pd.DataFrame = field(default_factory=pd.DataFrame)
    kpi: dict[str, Any] = field(default_factory=dict)
    clean_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    ingest_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    outputs: dict[str, Path] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def elapsed(self) -> float:
        return round((self.finished_at - self.started_at).total_seconds(), 2)


def _scalar(v: Any) -> Any:
    """把 date/list/NaN 转成 SQLite 可存储的标量。"""
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, (date,)):
        return v.isoformat()
    if isinstance(v, (list, tuple, dict, set)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        v = float(v)
    if isinstance(v, (np.bool_, bool)):
        return int(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def prepare_for_db(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(_scalar)
    return out


def archive_raw(cfg: Config) -> Path | None:
    """把本次用到的原始文件快照归档，便于事后复现与对账。"""
    raw = cfg.path("paths.raw")
    target = cfg.path("paths.archive") / datetime.now().strftime("%Y%m%d")
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in raw.glob("*.*"):
        if src.suffix.lower() not in {".csv", ".xlsx", ".xls", ".json"}:
            continue
        dst = target / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(src, dst)
        copied += 1
    log.info("原始数据归档 %d 个文件 -> %s", copied, target)
    return target


def run_pipeline(cfg: Config | None = None, *, archive: bool = True,
                 make_report: bool = True, send_alert: bool = True) -> PipelineResult:
    cfg = cfg or get_config()
    ensure_dirs(cfg)
    db.init_schema()
    started = datetime.now()
    run_id = f"R{started:%Y%m%d-%H%M%S}"
    log.info("========== 跑批开始 %s ==========", run_id)

    result = PipelineResult(run_id=run_id, status="running", started_at=started, finished_at=started)
    rows_loaded = 0
    try:
        raw, ingest_log = load_all(cfg)
        result.ingest_log = ingest_log
        failed = ingest_log[ingest_log["status"] != "ok"] if not ingest_log.empty else ingest_log
        if not failed.empty:
            log.warning("有 %d 个数据源采集失败: %s", len(failed), failed["source"].tolist())

        if archive:
            archive_raw(cfg)

        tables, clean_log = clean_all(raw, cfg)
        result.tables = tables
        result.clean_log = clean_log.to_frame()

        issues = validate.validate(tables, cfg, result.clean_log)
        result.issues = issues
        if not issues.empty:
            db.upsert_issues(issues)

        orders = tables["orders"]
        kpi = metrics.kpi_overview(orders)
        result.kpi = kpi

        # 分析结果落库，供页面与 AI 问数复用，避免每次重算
        daily = metrics.daily_summary(orders)
        platform_daily = metrics.daily_by_dimension(orders, "platform")
        alert = metrics.inventory_alert(tables.get("inventory", pd.DataFrame()), orders, cfg)
        effect = metrics.campaign_effect(orders, tables.get("campaigns", pd.DataFrame()), cfg)
        traffic_sum = metrics.traffic_summary(tables.get("traffic", pd.DataFrame()))
        sku_rank = metrics.sku_ranking(orders, 200, days=30)

        for table_name, frame in (
            ("orders", orders), ("products", tables.get("products", pd.DataFrame())),
            ("inventory", tables.get("inventory", pd.DataFrame())),
            ("traffic", tables.get("traffic", pd.DataFrame())),
            ("campaigns", tables.get("campaigns", pd.DataFrame())),
        ):
            rows_loaded += db.save_df(prepare_for_db(frame), table_name)
        for table_name, frame in (
            ("daily_kpi", daily), ("daily_platform", platform_daily),
            ("inventory_alert", alert), ("campaign_effect", effect),
            ("traffic_summary", traffic_sum), ("sku_rank", sku_rank),
        ):
            db.save_df(prepare_for_db(frame), table_name)

        finished = datetime.now()
        counts = validate.summarize(issues)
        meta = {
            "run_id": run_id,
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "date_start": str(orders["date"].min()),
            "date_end": str(metrics.reference_date(orders)),
            "rows": len(orders),
            "quality_score": metrics.data_quality_score(issues),
        }

        outputs: dict[str, Path] = {}
        if make_report:
            outputs = report.generate(tables, issues, kpi, result.clean_log,
                                      ingest_log, meta, cfg)
        result.outputs = outputs

        if send_alert:
            result.channels = push_notify(issues, kpi, cfg, run_id,
                                          str(outputs.get("excel", "")))

        result.finished_at = datetime.now()
        result.status = "success"
        db.log_pipeline_run(run_id, started, result.finished_at, "success", rows_loaded,
                            counts["high"], counts["medium"],
                            str(outputs.get("excel", "")),
                            f"high={counts['high']},medium={counts['medium']},low={counts['low']}")
        log.info("========== 跑批完成 %s，耗时 %.2fs，异常 %d 条 ==========",
                 run_id, result.elapsed, counts["total"])
        return result
    except Exception as exc:
        result.status = "failed"
        result.message = str(exc)
        result.finished_at = datetime.now()
        db.log_pipeline_run(run_id, started, result.finished_at, "failed", rows_loaded, 0, 0,
                            "", str(exc))
        log.exception("跑批失败: %s", exc)
        raise
