"""异常提醒：控制台 / 本地归档 / 飞书·钉钉·企微 Webhook / 邮件。

通知失败只记日志，绝不影响跑批主流程。
"""

from __future__ import annotations

import json
import smtplib
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import pandas as pd

from .config import Config, get_config
from .logging_setup import get_logger

log = get_logger(__name__)

LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}


def build_summary_text(issues: pd.DataFrame, kpi: dict, run_id: str = "",
                       report_path: str = "") -> str:
    """生成提醒正文：先给结论，再列最需要处理的 5 条。"""
    if issues.empty:
        return "【电商数据日报】数据校验通过，未发现异常。"

    counts = issues["level"].value_counts().to_dict()
    lines = [
        "【电商数据日报】数据校验发现异常",
        f"高 {counts.get('high', 0)} 条 / 中 {counts.get('medium', 0)} 条 / 低 {counts.get('low', 0)} 条",
    ]
    if kpi:
        gmv = kpi.get("gmv", 0)
        change = kpi.get("gmv_change")
        change_txt = f"{change:+.1%}" if change is not None and pd.notna(change) else "n/a"
        lines.append(
            f"近 30 天 GMV {gmv:,.0f} 元（环比 {change_txt}），"
            f"客单价 {kpi.get('aov', 0):,.0f} 元，退款率 {kpi.get('refund_rate', 0):.2%}")
    lines.append("")
    lines.append("需要优先处理：")
    top = issues[issues["level"].isin(["high", "medium"])].head(5)
    for i, r in enumerate(top.itertuples(), 1):
        lines.append(f"{i}. [{LEVEL_LABEL.get(r.level, r.level)}] {r.message}")
    if report_path:
        lines.append("")
        lines.append(f"完整报表：{report_path}")
    if run_id:
        lines.append(f"批次号：{run_id}")
    return "\n".join(lines)


def _post_webhook(url: str, payload: dict, timeout: int = 6) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="ignore")
    return resp.status == 200, body[:200]


def send_webhook(text: str, cfg: Config) -> tuple[bool, str]:
    section = cfg.section("notify.webhook")
    url = str(section.get("url", "")).strip()
    if not url:
        return False, "未配置 webhook url"
    kind = str(section.get("type", "generic")).lower()
    if kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
    elif kind == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": text}}
    elif kind == "wecom":
        payload = {"msgtype": "text", "text": {"content": text}}
    else:
        payload = {"text": text}
    return _post_webhook(url, payload, int(section.get("timeout_seconds", 6)))


def send_email(text: str, cfg: Config, subject: str) -> tuple[bool, str]:
    section = cfg.section("notify.smtp")
    to_list = section.get("to") or []
    if not to_list:
        return False, "未配置收件人"
    pwd = cfg.env(str(section.get("password_env", "SMTP_PASSWORD")))
    if not pwd:
        return False, "未读取到 SMTP 密码环境变量"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("电商数据平台", "utf-8")), str(section.get("user", ""))))
    msg["To"] = ", ".join(to_list)
    host, port = str(section.get("host")), int(section.get("port", 465))
    if section.get("ssl", True):
        server = smtplib.SMTP_SSL(host, port, timeout=10)
    else:
        server = smtplib.SMTP(host, port, timeout=10)
    with server:
        server.login(str(section.get("user", "")), pwd)
        server.sendmail(str(section.get("user", "")), to_list, msg.as_string())
    return True, f"已发送至 {len(to_list)} 个收件人"


def notify(issues: pd.DataFrame, kpi: dict, cfg: Config | None = None,
           run_id: str = "", report_path: str = "") -> list[str]:
    """按配置推送异常提醒，返回成功渠道列表。"""
    cfg = cfg or get_config()
    only_levels = set(cfg.get("notify.only_levels", ["high", "medium"]))
    if issues.empty or not set(issues["level"]).intersection(only_levels):
        log.info("无需要推送的异常")
        return []

    text = build_summary_text(issues, kpi, run_id, report_path)
    channels: list[str] = []

    if cfg.get("notify.console", True):
        print("\n" + "=" * 72 + "\n" + text + "\n" + "=" * 72)
        channels.append("console")

    if cfg.get("notify.file", True):
        alert_dir = cfg.path("paths.output") / "alerts"
        alert_dir.mkdir(parents=True, exist_ok=True)
        path = alert_dir / f"alert_{datetime.now():%Y%m%d_%H%M%S}.md"
        path.write_text(text, encoding="utf-8")
        log.info("异常提醒已归档: %s", path)
        channels.append("file")

    webhook = cfg.section("notify.webhook")
    if webhook.get("enabled"):
        try:
            ok, msg = send_webhook(text, cfg)
            if ok:
                channels.append("webhook")
            else:
                log.warning("Webhook 发送失败: %s", msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Webhook 异常: %s", exc)

    smtp = cfg.section("notify.smtp")
    if smtp.get("enabled"):
        try:
            ok, msg = send_email(text, cfg, "电商数据日报 - 异常提醒")
            if ok:
                channels.append("smtp")
            else:
                log.warning("邮件发送失败: %s", msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("邮件异常: %s", exc)
    return channels
