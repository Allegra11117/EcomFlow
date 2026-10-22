"""内部自动化工具页面：看板 / 库存 / 异常中心 / AI 问数 / 数据接入。

启动：python main.py serve   或   streamlit run app/dashboard.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecom_ops import ai, db, metrics, report  # noqa: E402
from ecom_ops.config import get_config  # noqa: E402
from ecom_ops.pipeline import run_pipeline  # noqa: E402

CFG = get_config()
CN = report.CN_COLUMNS

st.set_page_config(page_title="电商运营数据自动化平台", page_icon="📊", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1400px;}
      h1, h2, h3 {color:#1F4E79;}
      .hero {background:linear-gradient(120deg,#1F4E79,#2E75B6); color:#fff;
             padding:18px 24px; border-radius:14px; margin-bottom:18px;}
      .hero h1 {color:#fff; font-size:22px; margin:0 0 6px 0;}
      .hero p {margin:0; font-size:13px; opacity:.9;}
      .hint {background:#F4F7FB; border-left:4px solid #C55A11; padding:10px 14px;
             border-radius:6px; font-size:13px; color:#4B5563;}
      div[data-testid="stMetric"] {background:#fff; border:1px solid #E5EAF2;
             border-radius:12px; padding:12px 16px;}
      div[data-testid="stMetricLabel"] p {font-size:13px !important; color:#5B6B82;
             white-space:nowrap; overflow:visible;}
      div[data-testid="stMetricValue"] {font-size:1.55rem !important; line-height:1.3;
             overflow:visible; white-space:nowrap;}
      div[data-testid="stMetricValue"] > div {white-space:nowrap; overflow:visible;
             text-overflow:clip;}
      div[data-testid="stMetricDelta"] {font-size:12px !important;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------ 数据读取
@st.cache_data(ttl=180, show_spinner=False)
def load_table(name: str) -> pd.DataFrame:
    if not db.table_exists(name):
        return pd.DataFrame()
    return db.read_table(name)


@st.cache_data(ttl=180, show_spinner=False)
def load_orders() -> pd.DataFrame:
    df = load_table("orders")
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for col in ("is_paid", "is_refund", "is_sale", "is_valid", "is_orphan",
                "is_amount_mismatch", "is_future_time"):
        if col in df:
            df[col] = df[col].astype(bool)
    return df


def cn(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: CN.get(c, c) for c in df.columns})


def money_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if any(k in c.lower() for k in
                                         ("gmv", "amount", "spend", "margin", "cost", "budget"))]


def show_table(df: pd.DataFrame, height: int | None = None, key: str | None = None) -> None:
    if df.empty:
        st.info("暂无数据")
        return
    view = cn(df)
    cfg = {}
    for col in view.columns:
        if pd.api.types.is_integer_dtype(view[col]):
            cfg[col] = st.column_config.NumberColumn(format="%d")
        elif pd.api.types.is_float_dtype(view[col]):
            cfg[col] = st.column_config.NumberColumn(format="%.2f")
    kwargs = {"height": height} if height else {}
    st.dataframe(view, width="stretch", hide_index=True, column_config=cfg, key=key, **kwargs)


# ------------------------------------------------------------------ 侧边栏
with st.sidebar:
    st.markdown("### 运营数据工具")
    window = st.selectbox("统计窗口", [7, 14, 30, 60, 90], index=2,
                          format_func=lambda d: f"近 {d} 天")
    st.divider()
    if st.button("立即跑批（采集→报表）", type="primary", width="stretch"):
        try:
            with st.status("正在执行跑批...", expanded=True) as status:
                st.write("1/4 采集并清洗平台数据")
                result = run_pipeline()
                st.write("2/4 校验数据质量，写入异常工单")
                st.write("3/4 生成 Excel / HTML 报表")
                st.write("4/4 推送异常提醒")
                status.update(label=f"跑批完成：{result.run_id}", state="complete")
            st.cache_data.clear()
            st.session_state["flash"] = (
                f"{result.run_id} 完成，新增/更新异常 {len(result.issues)} 条，"
                f"耗时 {result.elapsed}s")
        except Exception as exc:  # noqa: BLE001 - 页面上要展示可读错误
            st.error(f"跑批失败：{exc}")
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    st.divider()
    runs = load_table("pipeline_runs")
    if not runs.empty:
        last = runs.sort_values("started_at").iloc[-1]
        st.caption(f"最近跑批：{last['started_at']}")
        st.caption(f"状态：{last['status']} ｜ 耗时 {last['duration_sec']}s")
    st.caption(f"数据库：{CFG.path('paths.db', 'db/ecom.db').name}")


orders = load_orders()
if orders.empty:
    st.markdown(
        "<div class='hero'><h1>电商运营数据自动化平台</h1>"
        "<p>还没有数据。先在终端执行：python main.py run</p></div>",
        unsafe_allow_html=True,
    )
    st.stop()

ref = metrics.reference_date(orders)
start = ref - timedelta(days=window)
cur = orders[(orders["date"] > start) & (orders["date"] <= ref)]
prev = orders[(orders["date"] > start - timedelta(days=window)) & (orders["date"] <= start)]

daily = load_table("daily_kpi")
issues = load_table("dq_issues")
quality = metrics.data_quality_score(issues[issues["status"].isin(["open", "acked"])]
                                     if not issues.empty else issues)

st.markdown(
    f"<div class='hero'><h1>电商运营数据自动化平台</h1>"
    f"<p>数据区间 {orders['date'].min()} ~ {ref} ｜ 订单 {len(orders):,} 行 ｜ "
    f"数据质量分 {quality:.0f} ｜ 待处理异常 "
    f"{int(issues[issues['status'].isin(['open','acked'])].shape[0]) if not issues.empty else 0} 条</p></div>",
    unsafe_allow_html=True,
)

tab_board, tab_stock, tab_issue, tab_ai, tab_ingest, tab_run = st.tabs(
    ["经营看板", "库存预警", "异常中心", "AI 问数", "数据接入", "运行与报表"])


def _agg(df: pd.DataFrame) -> dict:
    paid = df[df["is_paid"]]
    gmv = float(df.loc[df["is_paid"] | df["is_refund"], "pay_amount"].sum())
    net = float(paid["pay_amount"].sum())
    refund = float(df.loc[df["is_refund"], "pay_amount"].sum())
    n = int(len(paid))
    margin = float(paid["margin"].sum())
    return {"gmv": gmv, "net": net, "orders": n, "refund_rate": refund / gmv if gmv else 0,
            "aov": net / n if n else 0, "margin_rate": margin / net if net else 0}


# ------------------------------------------------------------------ 经营看板
with tab_board:
    c1, c2 = _agg(cur), _agg(prev)

    def _delta(key: str) -> str | None:
        if not c2.get(key):
            return None
        return f"{(c1[key] / c2[key] - 1) * 100:+.1f}%"

    cols = st.columns(5)
    cols[0].metric("GMV", f"{c1['gmv'] / 10000:,.1f} 万", _delta("gmv"))
    cols[1].metric("净销售额", f"{c1['net'] / 10000:,.1f} 万", _delta("net"))
    cols[2].metric("订单数", f"{c1['orders']:,}", _delta("orders"))
    cols[3].metric("客单价", f"{c1['aov']:,.0f} 元", _delta("aov"))
    cols[4].metric("退款率", f"{c1['refund_rate']:.2%}", _delta("refund_rate"),
                   delta_color="inverse")

    st.caption(f"金额单位：万元 ｜ 对比窗口：{start - timedelta(days=window)} ~ {start}")

    d = daily.copy() if not daily.empty else metrics.daily_summary(orders)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"].dt.date > start]
    st.subheader("销售趋势")
    st.line_chart(d.set_index("date")[["gmv", "net_gmv"]], height=260,
                  color=["#1F4E79", "#C55A11"])

    left, right = st.columns([1.15, 1])
    with left:
        st.subheader("平台表现")
        plat = metrics.dimension_summary(cur, "platform")
        st.bar_chart(plat.set_index("platform")["gmv"], height=240, color="#2E75B6")
    with right:
        st.subheader("品类结构")
        cat = metrics.dimension_summary(cur, "category")
        st.bar_chart(cat.set_index("category")["margin_rate"], height=240, color="#C55A11")

    st.subheader("平台明细")
    show_table(plat[["platform", "gmv", "net_gmv", "orders", "aov",
                     "margin_rate", "refund_rate", "gmv_share"]])
    st.caption("毛利率 = 毛利 / 净销售额；退款率 = 退款金额 / GMV")

    st.subheader(f"SKU 排行 TOP 10（近 {window} 天）")
    sku = metrics.sku_ranking(cur, n=10)
    show_table(sku[["sku_id", "sku_name", "category", "gmv", "units",
                    "margin_rate", "abc"]])

    st.subheader("活动效果")
    campaigns = load_table("campaigns")
    eff = metrics.campaign_effect(orders, campaigns, CFG)
    if eff.empty:
        st.info("暂无活动数据")
    else:
        show_table(eff[["campaign_name", "platform", "status", "gmv", "budget", "roi",
                        "lift_pct", "incremental_gmv", "conclusion"]])

# ------------------------------------------------------------------ 库存预警
with tab_stock:
    alert = load_table("inventory_alert")
    if alert.empty:
        st.info("暂无库存快照，请先跑批")
    else:
        counts = alert["status"].value_counts()
        cols = st.columns(4)
        cols[0].metric("断货 SKU", int(counts.get("断货", 0)))
        cols[1].metric("紧急补货", int(counts.get("紧急补货", 0)))
        cols[2].metric("低于安全库存", int(counts.get("低于安全库存", 0)))
        cols[3].metric("滞销", int(counts.get("滞销", 0)))

        status_filter = st.multiselect(
            "状态筛选", ["断货", "紧急补货", "低于安全库存", "健康", "滞销"],
            default=["断货", "紧急补货", "低于安全库存", "滞销"])
        view = alert[alert["status"].isin(status_filter)] if status_filter else alert
        st.caption("可售天数 = (现有 + 在途) / 近 30 天日均销量；补货建议按 30 天覆盖 + 20% 缓冲计算")
        show_table(view[["sku_id", "sku_name", "category", "warehouse", "on_hand",
                         "in_transit", "safety_stock", "avg_daily_sales", "cover_days",
                         "status", "suggest_qty", "suggestion"]], height=420)
        st.download_button("导出库存预警 CSV",
                           view.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"inventory_alert_{date.today()}.csv",
                           mime="text/csv")

        urgent = view[view["cover_days"].notna()].nsmallest(15, "cover_days")
        if not urgent.empty:
            st.subheader("可售天数最低的 15 个 SKU")
            st.bar_chart(urgent.set_index("sku_id")["cover_days"], height=240,
                         color="#C55A11")

# ------------------------------------------------------------------ 异常中心
with tab_issue:
    if issues.empty:
        st.success("本次校验未发现异常")
    else:
        f1, f2, f3 = st.columns([1, 1, 2])
        level = f1.selectbox("等级", ["all", "high", "medium", "low"], index=0,
                             format_func=lambda v: {"all": "全部", "high": "高",
                                                    "medium": "中", "low": "低"}[v])
        status = f2.selectbox("状态", ["open", "acked", "fixed", "ignored", "all"],
                              index=0,
                              format_func=lambda v: {"open": "待处理", "acked": "处理中",
                                                     "fixed": "已修复", "ignored": "已忽略",
                                                     "all": "全部"}[v])
        with f3:
            st.write("")
            st.caption("处理流程：待处理 → 处理中 → 已修复 / 已忽略；同一问题再次出现会自动累计次数")

        view = issues.copy()
        if level != "all":
            view = view[view["level"] == level]
        if status != "all":
            view = view[view["status"] == status]
        view = view.sort_values(["level", "last_seen"],
                                ascending=[True, False], key=lambda s: s.map(
                                    {"high": 0, "medium": 1, "low": 2}) if s.name == "level" else s)
        show_table(view[["issue_id", "level", "rule_name", "message", "metric",
                         "occurrences", "last_seen", "status"]], height=380)

        st.subheader("更新工单")
        with st.form("issue_form", clear_on_submit=True):
            c1, c2, c3 = st.columns([1.2, 1, 2])
            issue_id = c1.text_input("工单号", placeholder="DQXXXXXXXXXX")
            new_status = c2.selectbox("更新为", ["acked", "fixed", "ignored", "open"],
                                      format_func=lambda v: {"acked": "处理中", "fixed": "已修复",
                                                             "ignored": "已忽略", "open": "待处理"}[v])
            note = c3.text_input("备注", placeholder="例如：已联系平台补数，预计今天 18:00 前完成")
            if st.form_submit_button("提交", type="primary") and issue_id:
                try:
                    db.set_issue_status(issue_id.strip(), new_status,
                                        st.session_state.get("operator", ""), note)
                    st.cache_data.clear()
                    st.success(f"工单 {issue_id} 已更新为 {new_status}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"更新失败：{exc}")

        with st.expander("规则说明（14 条校验规则）"):
            st.markdown(
                """
                | 规则 | 名称 | 等级 | 说明 |
                |---|---|---|---|
                | R001 | 源数据重复 | 高/中 | 平台重复导出或重复推送 |
                | R002 | 订单商品无主数据 | 高 | 商品档案缺失，影响毛利与品类分析 |
                | R003 | 金额勾稽不平 | 中 | 实付 ≠ 单价×数量-优惠 |
                | R004 | 推广归因偏差 | 中 | 归因 GMV 与订单 GMV 偏差超阈值 |
                | R005 | 下单时间异常 | 高 | 时间缺失或晚于今天 |
                | R006 | 数据断档 | 高/中 | 连续 N 天无数据，或流量报表缺失 |
                | R007 | 库存为负 | 高 | 超卖或回传延迟 |
                | R008 | 库存预警 | 高/中 | 断货或可售不足 3 天 |
                | R009 | 库存滞销 | 低 | 超过 45 天无动销且库存积压 |
                | R010 | 负毛利订单 | 中 | 折扣叠加导致亏损 |
                | R011 | 退款率超阈值 | 中 | 周维度 / SKU 维度退款率异常 |
                | R012 | 活动效果未达预期 | 低 | 已结束活动 ROI 低于目标 |
                | R013 | 零金额支付单 | 低 | 赠品/内部单污染销售分析 |
                | R014 | 订单状态未识别 | 中 | 状态文案未映射 |
                """
            )

# ------------------------------------------------------------------ AI 问数
with tab_ai:
    st.markdown(
        "<div class='hint'>用自然语言查数据：默认走本地规则引擎（离线可用、结果可复核）；"
        "配置 <code>LLM_API_KEY</code> 后自动优先调用大模型，生成的 SQL 仍会通过只读护栏与交叉校验。</div>",
        unsafe_allow_html=True)
    examples = ["近30天各平台GMV", "上月哪个品类卖得最好", "哪些商品需要补货",
                "各渠道ROI是多少", "上周订单数和客单价"]
    st.session_state.setdefault("ai_question", examples[0])
    cols = st.columns(len(examples))
    for i, ex in enumerate(examples):
        if cols[i].button(ex, key=f"ex{i}", width="stretch"):
            st.session_state["ai_question"] = ex
    question = st.text_input("问题", key="ai_question")
    c1, c2 = st.columns([1, 3])
    run_ask = c1.button("查询", type="primary", width="stretch")
    if c2.button("运行 AI 产出校验（5 个基准问题）", width="stretch"):
        try:
            detail, ok = ai.verify_offline(CFG)
            st.session_state["verify_result"] = (detail, ok)
        except Exception as exc:  # noqa: BLE001
            st.error(f"校验失败：{exc}")

    if run_ask and question.strip():
        answer = ai.ask(question, CFG)
        st.caption(f"理解：{answer.plan.explanation} ｜ 通道：{answer.plan.provider} ｜ "
                   f"耗时：{answer.latency_ms}ms ｜ 返回 {len(answer.data)} 行")
        st.markdown(f"**结论**：{answer.text}")
        if answer.error:
            st.error(answer.error)
        show_table(answer.data, height=280)
        with st.expander("查看生成的 SQL（只读护栏校验通过）"):
            st.code(answer.plan.sql, language="sql")

    if "verify_result" in st.session_state:
        detail, ok = st.session_state["verify_result"]
        st.subheader("AI 产出校验（AI SQL vs pandas 独立口径）")
        st.success("全部通过") if ok else st.warning("存在偏差，需人工复核")
        show_table(detail[["case", "provider", "ai_value", "baseline_value",
                          "diff_pct", "passed"]])

    logs = load_table("ai_query_log")
    if not logs.empty:
        with st.expander("问数审计日志（最近 20 条）"):
            show_table(logs.sort_values("ts", ascending=False).head(20)[
                ["ts", "question", "provider", "row_count", "latency_ms", "verified"]])

# ------------------------------------------------------------------ 数据接入
with tab_ingest:
    st.subheader("上传平台导出文件")
    st.caption("支持 CSV / Excel；系统会自动识别常见中文表头（订单编号、实付金额、商品编码等），"
               "清洗与校验日志会写入报表的「数据质量」页。")
    target = st.selectbox("数据类型", ["orders", "products", "inventory", "traffic", "campaigns"])
    uploaded = st.file_uploader("选择文件", type=["csv", "xlsx"], accept_multiple_files=False)
    if uploaded is not None:
        raw_dir = CFG.path("paths.raw")
        raw_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(uploaded.name).suffix.lower()
        dest = raw_dir / f"{target}{suffix}"
        dest.write_bytes(uploaded.getbuffer())
        st.success(f"已保存到 {dest.relative_to(CFG.root)}")
        st.info("点击左侧「立即跑批」即可重新生成报表；若为 Excel，请在 config.yaml 中把该数据源 type 改为 excel。")

    st.divider()
    st.subheader("当前数据源配置")
    sources = pd.DataFrame(CFG.get("ingest.sources", []))
    show_table(sources[["name", "type", "file"]] if not sources.empty else sources)

    st.subheader("采集日志（最近一次）")
    runs = load_table("pipeline_runs")
    if not runs.empty:
        st.caption(f"最近批次 {runs.sort_values('started_at').iloc[-1]['run_id']}，"
                   f"共加载 {int(runs.sort_values('started_at').iloc[-1]['rows_loaded']):,} 行")
    st.markdown(
        "<div class='hint'>接口对接演示：执行 <code>python main.py mock-api</code> 启动模拟平台接口，"
        "把 config.yaml 中 <code>ingest.api.enabled</code> 设为 true，"
        "系统会改为分页拉取 <code>/api/v1/orders</code>。</div>",
        unsafe_allow_html=True)

# ------------------------------------------------------------------ 运行与报表
with tab_run:
    runs = load_table("pipeline_runs")
    st.subheader("跑批记录")
    if runs.empty:
        st.info("暂无记录")
    else:
        show_table(runs.sort_values("started_at", ascending=False)[
            ["run_id", "started_at", "status", "duration_sec", "rows_loaded",
             "issues_high", "issues_medium"]])

    st.subheader("报表下载")
    out_dir = CFG.path("paths.output")
    files = sorted(out_dir.glob("ecom_daily_report_*"), reverse=True)[:8]
    if not files:
        st.info("还没有报表，先跑一次批")
    for f in files:
        c1, c2 = st.columns([4, 1])
        c1.write(f"`{f.name}`  ·  {f.stat().st_size / 1024:.0f} KB  ·  "
                 f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}")
        with c2:
            if f.suffix == ".html":
                st.download_button("下载", f.read_bytes(), file_name=f.name,
                                   mime="text/html", key=f.name)
            else:
                st.download_button("下载", f.read_bytes(), file_name=f.name,
                                   mime="application/vnd.openxmlformats-officedocument."
                                        "spreadsheetml.sheet", key=f.name)

    st.subheader("异常提醒归档")
    alerts = sorted((out_dir / "alerts").glob("alert_*.md"), reverse=True)[:5]
    if alerts:
        with st.expander(f"最近 {len(alerts)} 条提醒"):
            st.markdown(alerts[0].read_text(encoding="utf-8"))
    else:
        st.caption("暂无提醒归档")

    st.divider()
    st.caption("口径：GMV=支付成功+已退款订单实付；净销售额=支付成功实付-退款；"
               "毛利=实付-成本×数量。数据问题请联系数据负责人。")
