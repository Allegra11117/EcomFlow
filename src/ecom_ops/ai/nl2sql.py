"""自然语言问数（NL2SQL）。

两条通道：
    rule  本地规则引擎（默认，离线可用）——把问题映射成结构化查询计划再拼 SQL
    llm   大模型通道（配置 API Key 后启用）——生成 SQL 后必须通过 guard_sql 安全检查

无论走哪条通道，SQL 都要经过同一套护栏：只读、单语句、表白名单、强制 LIMIT。
生成的 SQL 与执行结果都会写入 ai_query_log，方便复盘"AI 当时答了什么"。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .. import db
from ..config import Config, get_config
from ..logging_setup import get_logger

log = get_logger(__name__)

READONLY_PREFIX = ("select", "with")
FORBIDDEN_WORDS = ("insert", "update", "delete", "drop", "alter", "create", "attach",
                   "detach", "pragma", "vacuum", "replace", "trigger", "grant")

METRIC_SQL = {
    "gmv": "ROUND(SUM(CASE WHEN is_paid=1 OR is_refund=1 THEN pay_amount ELSE 0 END),2)",
    "net_gmv": "ROUND(SUM(CASE WHEN is_paid=1 THEN pay_amount ELSE 0 END),2)",
    "orders": "SUM(CASE WHEN is_paid=1 THEN 1 ELSE 0 END)",
    "units": "SUM(CASE WHEN is_paid=1 THEN qty ELSE 0 END)",
    "buyers": "COUNT(DISTINCT CASE WHEN is_paid=1 THEN buyer_id END)",
    "margin": "ROUND(SUM(CASE WHEN is_paid=1 THEN margin ELSE 0 END),2)",
    "aov": ("ROUND(SUM(CASE WHEN is_paid=1 THEN pay_amount ELSE 0 END)/"
            "NULLIF(SUM(CASE WHEN is_paid=1 THEN 1 ELSE 0 END),0),2)"),
    "margin_rate": ("ROUND(SUM(CASE WHEN is_paid=1 THEN margin ELSE 0 END)/"
                    "NULLIF(SUM(CASE WHEN is_paid=1 THEN pay_amount ELSE 0 END),0),4)"),
    "refund_rate": ("ROUND(SUM(CASE WHEN is_refund=1 THEN pay_amount ELSE 0 END)/"
                    "NULLIF(SUM(CASE WHEN is_paid=1 OR is_refund=1 THEN pay_amount ELSE 0 END),0),4)"),
    "discount_rate": ("ROUND(SUM(discount_amount)/NULLIF(SUM(gross_amount),0),4)"),
}

METRIC_CN = {
    "gmv": "GMV", "net_gmv": "净销售额", "orders": "订单数", "units": "销量",
    "buyers": "买家数", "margin": "毛利", "aov": "客单价", "margin_rate": "毛利率",
    "refund_rate": "退款率", "discount_rate": "折扣率",
}

METRIC_KEYWORDS = [
    ("gmv", ("gmv", "成交额", "销售额", "交易额", "卖了多少")),
    ("net_gmv", ("净销售", "净gmv", "扣除退款")),
    ("aov", ("客单价", "平均订单")),
    ("margin_rate", ("毛利率", "利润率")),
    ("margin", ("毛利", "利润")),
    ("refund_rate", ("退款率", "退货率")),
    ("units", ("销量", "件数", "多少件")),
    ("buyers", ("买家数", "用户数", "客户数", "多少人买")),
    ("orders", ("订单数", "单量", "订单量", "多少单")),
]

DIM_KEYWORDS = [
    ("platform", ("各平台", "按平台", "分平台", "平台")),
    ("category", ("品类", "类目", "分类", "类别")),
    ("channel", ("渠道", "来源")),
    ("sku", ("sku", "商品", "单品", "款")),
    ("date", ("每日", "每天", "按天", "趋势", "走势")),
    ("week", ("每周", "按周")),
    ("month", ("每月", "按月")),
]

PLATFORM_CN = {"Tmall": ("天猫", "tmall"), "JD": ("京东", "jd"),
               "PDD": ("拼多多", "pdd"), "Douyin": ("抖音", "douyin")}


@dataclass
class SQLPlan:
    sql: str
    explanation: str
    provider: str = "rule"
    question: str = ""
    metric: str = ""
    dimension: str = ""


@dataclass
class AIAnswer:
    question: str
    plan: SQLPlan
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    text: str = ""
    latency_ms: int = 0
    verified: str = "n/a"
    error: str = ""


def _ref_date() -> date:
    """业务基准日：剔除未来时间的脏数据，避免统计窗口错位。"""
    try:
        frame = db.query_df("SELECT MAX(date) AS d FROM orders WHERE date <= ?",
                            [date.today().isoformat()])
        value = frame["d"].iloc[0] if not frame.empty else None
        if value:
            return pd.Timestamp(value).date()
    except Exception:  # noqa: BLE001 - 库不存在/为空时退回系统日期
        pass
    return date.today()


def _time_range(question: str, ref: date) -> tuple[str, str, str, str]:
    """从问题里抽取时间窗，返回 (start, end, 标签, 左边界运算符)。

    滚动窗口用半开区间 date > start（即"近 30 天"含今天共 30 天），
    自然月/周用 >=，与业务口径保持一致。
    """
    m = re.search(r"(?:近|最近|过去)\s*(\d+)\s*天", question)
    if m:
        n = max(1, min(int(m.group(1)), 720))
        return (ref - timedelta(days=n)).isoformat(), ref.isoformat(), f"近{n}天", ">"
    if "上月" in question or "上个月" in question:
        first = ref.replace(day=1)
        last_end = first - timedelta(days=1)
        return last_end.replace(day=1).isoformat(), last_end.isoformat(), "上月", ">="
    if "本月" in question or "这个月" in question:
        return ref.replace(day=1).isoformat(), ref.isoformat(), "本月", ">="
    if "今年" in question:
        return ref.replace(month=1, day=1).isoformat(), ref.isoformat(), "今年", ">="
    if "昨天" in question:
        d = ref - timedelta(days=1)
        return d.isoformat(), d.isoformat(), "昨天", ">="
    if "今天" in question:
        return ref.isoformat(), ref.isoformat(), "今天", ">="
    if "上周" in question:
        monday = ref - timedelta(days=ref.weekday() + 7)
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat(), "上周", ">="
    if "本周" in question:
        monday = ref - timedelta(days=ref.weekday())
        return monday.isoformat(), ref.isoformat(), "本周", ">="
    return (ref - timedelta(days=30)).isoformat(), ref.isoformat(), "近30天", ">"


def _detect_metric(question: str) -> str:
    for metric, words in METRIC_KEYWORDS:
        if any(w in question for w in words):
            return metric
    return "gmv"


def _detect_dimension(question: str) -> str:
    for dim, words in DIM_KEYWORDS:
        if any(w in question for w in words):
            return dim
    return ""


def _detect_platform(question: str) -> str | None:
    low = question.lower()
    for platform, words in PLATFORM_CN.items():
        if any(w in low for w in words):
            return platform
    return None


def _detect_top_n(question: str) -> int | None:
    m = re.search(r"(?:top|前)\s*(\d+)", question.lower())
    if m:
        return max(1, min(int(m.group(1)), 100))
    if re.search(r"最高|最多|最好|最畅销|排名第一|第一", question):
        return 1
    return None


def _plan_inventory(question: str, cfg: Config) -> SQLPlan:
    cols = "sku_id, sku_name, warehouse, on_hand, in_transit, avg_daily_sales, cover_days, status, suggest_qty"
    if "滞销" in question:
        where, note = "status='滞销'", "查询滞销库存"
    elif "断货" in question:
        where, note = "status='断货'", "查询已断货商品"
    elif "补货" in question or "缺货" in question:
        where = "status IN ('断货','紧急补货','低于安全库存')"
        note = "查询需要补货的商品"
    elif "安全库存" in question:
        where, note = "on_hand < safety_stock", "查询低于安全库存的商品"
    else:
        where, note = "status <> '健康'", "查询库存异常商品"
    order = "cover_days ASC" if "滞销" not in question else "slow_days DESC"
    sql = f"SELECT {cols} FROM inventory_alert WHERE {where} ORDER BY {order} LIMIT 100"
    return SQLPlan(sql=sql, explanation=f"{note}，按{'可售天数' if '滞销' not in question else '无动销天数'}排序",
                   question=question, metric="count", dimension="sku")


def _plan_campaign(question: str) -> SQLPlan:
    sql = ("SELECT campaign_name, platform, start_date, end_date, status, gmv, "
           "ROUND(lift_pct,4) AS lift_pct, ROUND(roi,2) AS roi, conclusion "
           "FROM campaign_effect ORDER BY start_date DESC LIMIT 20")
    return SQLPlan(sql=sql, explanation="返回最近的活动效果评估（活动期 vs 等长基准期）",
                   question=question, metric="gmv", dimension="campaign")


def _plan_traffic(question: str) -> SQLPlan:
    sql = ("SELECT channel, spend, attr_gmv, ROUND(roi,2) AS roi, "
           "ROUND(ctr,4) AS ctr, ROUND(cvr,4) AS cvr FROM traffic_summary "
           "ORDER BY roi DESC LIMIT 20")
    return SQLPlan(sql=sql, explanation="按渠道汇总推广花费与产出，ROI=归因GMV/花费",
                   question=question, metric="roi", dimension="channel")


def plan_question(question: str, cfg: Config | None = None) -> SQLPlan:
    """规则通道：关键词 -> 结构化计划 -> 只读 SQL。"""
    cfg = cfg or get_config()
    q = question.strip()
    low = q.lower()

    if not q:
        raise ValueError("问题不能为空")

    if any(w in q for w in ("库存", "补货", "缺货", "断货", "滞销")):
        return _plan_inventory(q, cfg)
    if "活动" in q or "大促" in q:
        return _plan_campaign(q)
    if "roi" in low or "投产" in q or "推广" in q or "投放" in q or "花费" in q:
        return _plan_traffic(q)

    ref = _ref_date()
    start, end, label, start_op = _time_range(q, ref)
    metric = _detect_metric(q)
    dim = _detect_dimension(q)
    platform = _detect_platform(q)
    top_n = _detect_top_n(q)

    where = [f"date {start_op} '{start}' AND date <= '{end}'"]
    if platform:
        where.append(f"platform = '{platform}'")
    where_sql = " AND ".join(where)
    expr = METRIC_SQL[metric]
    metric_cn = METRIC_CN[metric]
    alias = metric

    if dim in ("platform", "category", "channel", "sku", "date", "week", "month"):
        if dim == "sku":
            group_cols = "sku_id, sku_name"
            select_cols = "sku_id, sku_name"
        elif dim == "date":
            group_cols, select_cols = "date", "date"
        elif dim == "week":
            group_cols = select_cols = "week"
        elif dim == "month":
            group_cols = select_cols = "month"
        else:
            group_cols = select_cols = dim
        limit = top_n or 100
        sql = (f"SELECT {select_cols}, {expr} AS {alias} FROM orders WHERE {where_sql} "
               f"GROUP BY {group_cols} ORDER BY {alias} DESC LIMIT {limit}")
        dim_cn = {"platform": "平台", "category": "品类", "channel": "渠道", "sku": "商品",
                  "date": "日期", "week": "周", "month": "月"}[dim]
        explanation = f"{label}按{dim_cn}汇总{metric_cn}，按{metric_cn}降序"
        if top_n:
            explanation += f"，取前 {top_n} 名"
    else:
        sql = f"SELECT {expr} AS {alias} FROM orders WHERE {where_sql}"
        explanation = f"{label}{metric_cn}"

    return SQLPlan(sql=sql, explanation=explanation, question=q, metric=metric,
                   dimension=dim)


def guard_sql(sql: str, cfg: Config | None = None) -> str:
    """SQL 护栏：只读、单语句、表白名单、列数限制。不通过直接抛错。"""
    cfg = cfg or get_config()
    clean = sql.strip().rstrip(";").strip()
    if not clean:
        raise ValueError("SQL 为空")
    low = clean.lower()
    if not low.startswith(READONLY_PREFIX):
        raise PermissionError("只允许 SELECT/WITH 查询")
    if ";" in clean:
        raise PermissionError("禁止多语句执行")
    if "--" in clean or "/*" in clean:
        raise PermissionError("禁止 SQL 注释")
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", low):
            raise PermissionError(f"检测到禁止的关键字: {word}")

    allow = set(cfg.get("ai.allow_tables", []) or [])
    tables = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", low))
    unknown = {t for t in tables if allow and t not in allow}
    if unknown:
        raise PermissionError(f"未授权的表: {sorted(unknown)}")

    if not re.search(r"\blimit\b", low):
        max_rows = int(cfg.get("ai.max_rows", 200))
        clean += f" LIMIT {max_rows}"
    return clean


def execute_plan(plan: SQLPlan, cfg: Config | None = None) -> tuple[pd.DataFrame, str]:
    cfg = cfg or get_config()
    sql = guard_sql(plan.sql, cfg)
    plan.sql = sql
    started = time.perf_counter()
    df = db.query_df(sql)
    elapsed = int((time.perf_counter() - started) * 1000)
    log.info("AI 问数 [%s] %s -> %d 行 %dms", plan.provider, plan.question, len(df), elapsed)
    return df, sql


# ------------------------------------------------------------------ LLM 通道
SYSTEM_PROMPT = """你是电商数据分析师，把用户问题翻译成 SQLite 只读 SQL。
可用表：
orders(order_id, order_time, date, platform, shop_id, sku_id, sku_name, category, buyer_id,
       qty, unit_price, discount_amount, pay_amount, gross_amount, order_status, channel,
       is_paid, is_refund, is_sale, margin, margin_rate, week, month)
daily_kpi(date, gmv, net_gmv, refund_amount, orders, buyers, units, cost, margin, aov,
          margin_rate, refund_rate)
daily_platform(date, platform, gmv, orders, units, margin)
inventory_alert(sku_id, sku_name, warehouse, on_hand, in_transit, safety_stock,
                avg_daily_sales, cover_days, slow_days, status, suggest_qty)
traffic_summary(channel, impressions, clicks, spend, attr_orders, attr_gmv, ctr, cpc, cvr, roi)
campaign_effect(campaign_name, platform, start_date, end_date, status, gmv, roi, lift_pct)
口径：GMV=支付成功+已退款订单实付；净销售额=支付成功实付-退款；date 为 TEXT(yyyy-MM-dd)。
要求：只输出一条 SELECT，不要解释，不要注释，必须带 LIMIT。"""


def _llm_sql(question: str, cfg: Config) -> str | None:
    """调用兼容 OpenAI 协议的模型生成 SQL，失败返回 None（由调用方降级）。"""
    key = cfg.env(str(cfg.get("ai.api_key_env", "LLM_API_KEY")))
    if not key:
        return None
    base = str(cfg.get("ai.base_url", "")).rstrip("/")
    model = str(cfg.get("ai.model", "gpt-4o-mini"))
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("ai.timeout_seconds", 25))) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - 模型不可用时静默降级
        log.warning("LLM 通道不可用，降级规则引擎: %s", exc)
        return None
    match = re.search(r"```(?:sql)?\s*(.+?)```", content, re.S)
    sql = match.group(1) if match else content
    sql = sql.strip()
    return sql or None


def ask(question: str, cfg: Config | None = None, provider: str | None = None) -> AIAnswer:
    """统一入口：优先按配置走模型，失败自动降级规则引擎，并做结果解读。"""
    from .narrator import narrate

    cfg = cfg or get_config()
    mode = provider or str(cfg.get("ai.provider", "auto"))
    plan: SQLPlan
    sql: str | None = None

    if cfg.get("ai.enabled", True) and mode in ("auto", "llm"):
        sql = _llm_sql(question, cfg)
        if sql:
            plan = SQLPlan(sql=sql, explanation="由大模型根据表结构生成", provider="llm",
                           question=question)
        else:
            plan = plan_question(question, cfg)
    else:
        plan = plan_question(question, cfg)

    started = time.perf_counter()
    try:
        df, safe_sql = execute_plan(plan, cfg)
    except Exception as exc:  # noqa: BLE001 - 把失败原因回给页面，不让接口 500
        latency = int((time.perf_counter() - started) * 1000)
        db.log_ai_query(question, plan.sql, plan.provider, 0, latency, "failed", str(exc))
        return AIAnswer(question=question, plan=plan, text=f"查询失败：{exc}",
                        latency_ms=latency, error=str(exc))
    latency = int((time.perf_counter() - started) * 1000)
    text = narrate(question, df, plan)
    db.log_ai_query(question, safe_sql, plan.provider, len(df), latency, "n/a")
    return AIAnswer(question=question, plan=plan, data=df, text=text,
                    latency_ms=latency, verified="n/a")
