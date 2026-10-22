"""SQLite 数据层：清洗结果落库、异常工单闭环、跑批记录、AI 问数审计。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .config import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS dq_issues (
    issue_id      TEXT PRIMARY KEY,
    rule_id       TEXT NOT NULL,
    rule_name     TEXT,
    level         TEXT NOT NULL,
    scope         TEXT,
    detail_key    TEXT NOT NULL,
    message       TEXT,
    suggestion    TEXT,
    metric        TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    occurrences   INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'open',
    owner         TEXT,
    note          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_issue ON dq_issues(rule_id, detail_key);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,
    duration_sec  REAL,
    rows_loaded   INTEGER,
    issues_high   INTEGER,
    issues_medium INTEGER,
    report_path   TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS ai_query_log (
    id          TEXT PRIMARY KEY,
    ts          TEXT,
    question    TEXT,
    sql         TEXT,
    provider    TEXT,
    row_count   INTEGER,
    latency_ms  INTEGER,
    verified    TEXT,
    note        TEXT
);
"""


def db_path() -> Path:
    path = get_config().path("paths.db", "db/ecom.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def save_df(df: pd.DataFrame, table: str, if_exists: str = "replace") -> int:
    if df is None or df.empty:
        return 0
    with connect() as conn:
        df.to_sql(table, conn, if_exists=if_exists, index=False)
    return len(df)


def query_df(sql: str, params: Iterable[Any] | None = None) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=list(params) if params else None)


def read_table(table: str) -> pd.DataFrame:
    return query_df(f"SELECT * FROM {table}")


def table_exists(table: str) -> bool:
    df = query_df("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [table])
    return not df.empty


def table_empty(table: str) -> bool:
    if not table_exists(table):
        return True
    return bool(query_df(f"SELECT COUNT(*) AS c FROM {table}")["c"].iloc[0] == 0)


def log_pipeline_run(
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    rows_loaded: int,
    issues_high: int,
    issues_medium: int,
    report_path: str = "",
    note: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                started_at.isoformat(timespec="seconds"),
                finished_at.isoformat(timespec="seconds"),
                status,
                round((finished_at - started_at).total_seconds(), 2),
                int(rows_loaded),
                int(issues_high),
                int(issues_medium),
                report_path,
                note[:500],
            ),
        )


def upsert_issues(issues: pd.DataFrame) -> pd.DataFrame:
    """按 (rule_id, detail_key) 去重：老问题累加次数，人工处理状态不被覆盖。"""
    if issues.empty:
        return issues
    now = datetime.now().isoformat(timespec="seconds")
    new_rows = []
    with connect() as conn:
        for row in issues.to_dict("records"):
            detail_key = str(row.get("detail_key", ""))[:200]
            existing = conn.execute(
                "SELECT issue_id, status FROM dq_issues WHERE rule_id=? AND detail_key=?",
                (row["rule_id"], detail_key),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE dq_issues
                       SET last_seen=?, occurrences=occurrences+1, message=?, suggestion=?,
                           metric=?, level=?
                       WHERE issue_id=?""",
                    (
                        now,
                        row.get("message", ""),
                        row.get("suggestion", ""),
                        str(row.get("metric", "")),
                        row.get("level", "medium"),
                        existing["issue_id"],
                    ),
                )
                continue
            issue_id = "DQ" + uuid.uuid4().hex[:10].upper()
            conn.execute(
                "INSERT INTO dq_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    issue_id,
                    row["rule_id"],
                    row.get("rule_name", ""),
                    row.get("level", "medium"),
                    row.get("scope", ""),
                    detail_key,
                    row.get("message", ""),
                    row.get("suggestion", ""),
                    str(row.get("metric", "")),
                    now,
                    now,
                    1,
                    "open",
                    "",
                    "",
                ),
            )
            new_rows.append(issue_id)
    return issues.copy()


def list_issues(status: str | None = "open", level: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM dq_issues WHERE 1=1"
    params: list[Any] = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if level and level != "all":
        sql += " AND level=?"
        params.append(level)
    sql += " ORDER BY CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, last_seen DESC"
    return query_df(sql, params)


def set_issue_status(issue_id: str, status: str, owner: str = "", note: str = "") -> None:
    if status not in {"open", "acked", "ignored", "fixed"}:
        raise ValueError(f"非法状态: {status}")
    with connect() as conn:
        conn.execute(
            "UPDATE dq_issues SET status=?, owner=COALESCE(NULLIF(?,''), owner), note=? WHERE issue_id=?",
            (status, owner, note, issue_id),
        )


def log_ai_query(
    question: str, sql: str, provider: str, row_count: int, latency_ms: int,
    verified: str = "n/a", note: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_query_log VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:12],
                datetime.now().isoformat(timespec="seconds"),
                question[:300],
                sql[:4000],
                provider,
                int(row_count),
                int(latency_ms),
                verified,
                note[:300],
            ),
        )
