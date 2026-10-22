"""数据采集层：统一对接平台导出文件（CSV/Excel）与开放接口（HTTP JSON）。

设计目标：电商平台导出的表头、编码、格式各不相同，接入层负责把它们
变成统一列名的 DataFrame，并保留一份"采集日志"供业务核对。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Config, get_config
from .logging_setup import get_logger

log = get_logger(__name__)

# 平台常见表头别名 -> 标准字段（覆盖中文导出、驼峰、带单位等写法）
COLUMN_ALIASES: dict[str, str] = {
    "订单编号": "order_id", "订单号": "order_id", "order_no": "order_id", "订单id": "order_id",
    "下单时间": "order_time", "支付时间": "order_time", "创建时间": "order_time",
    "订单日期": "order_time", "order_date": "order_time",
    "店铺": "shop_id", "店铺编号": "shop_id", "店铺id": "shop_id", "shop": "shop_id",
    "商品编码": "sku_id", "商家编码": "sku_id", "商品id": "sku_id", "sku编码": "sku_id",
    "商品名称": "sku_name", "商品标题": "sku_name",
    "会员id": "buyer_id", "买家昵称": "buyer_id", "买家id": "buyer_id", "客户id": "buyer_id",
    "购买数量": "qty", "件数": "qty", "数量": "qty", "quantity": "qty",
    "商品单价": "unit_price", "成交单价": "unit_price", "单价": "unit_price", "price": "unit_price",
    "优惠金额": "discount_amount", "优惠": "discount_amount", "折扣金额": "discount_amount",
    "实付金额": "pay_amount", "买家实付": "pay_amount", "实收款": "pay_amount",
    "支付金额": "pay_amount", "gmv": "pay_amount",
    "订单状态": "order_status", "状态": "order_status", "status": "order_status",
    "来源渠道": "channel", "流量来源": "channel", "渠道": "channel",
    "收货省份": "province", "省份": "province", "收货地址": "province",
    "快照日期": "snapshot_date", "库存日期": "snapshot_date", "日期": "snapshot_date",
    "仓库": "warehouse", "仓库名称": "warehouse",
    "现有库存": "on_hand", "可用库存": "on_hand", "库存数量": "on_hand", "stock": "on_hand",
    "在途库存": "in_transit", "在途": "in_transit",
    "安全库存": "safety_stock", "警戒库存": "safety_stock",
    "统计日期": "stat_date", "曝光量": "impressions", "展现量": "impressions",
    "点击量": "clicks", "点击": "clicks", "消耗": "spend", "花费": "spend",
    "广告消耗": "spend", "归因订单数": "attr_orders", "归因gmv": "attr_gmv",
    "活动id": "campaign_id", "活动名称": "campaign_name", "活动类型": "campaign_type",
    "开始日期": "start_date", "结束日期": "end_date", "预算": "budget",
    "参与商品": "sku_ids", "活动商品": "sku_ids",
}


class IngestError(RuntimeError):
    """采集失败（缺列、文件不存在、接口异常）。"""


@dataclass
class IngestLog:
    source: str
    type: str
    status: str = "ok"
    rows: int = 0
    columns: int = 0
    encoding: str = ""
    elapsed_ms: int = 0
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SourceSpec:
    name: str
    type: str
    file: str | None = None
    sheet: str | int | None = None
    required_columns: list[str] = field(default_factory=list)
    rename: dict[str, str] = field(default_factory=dict)


def _normalize_columns(df: pd.DataFrame, extra_map: dict[str, str] | None = None) -> pd.DataFrame:
    """表头标准化：去空格/换行、转小写、中文别名映射。"""
    mapping: dict[str, str] = {}
    for col in df.columns:
        raw = str(col).strip().replace("\n", "").replace(" ", "")
        low = raw.lower()
        target = (extra_map or {}).get(raw) or COLUMN_ALIASES.get(raw) or COLUMN_ALIASES.get(low)
        mapping[col] = target or low
    renamed = df.rename(columns=mapping)
    # 重名列只保留第一列，避免后续聚合报错
    return renamed.loc[:, ~renamed.columns.duplicated()]


def read_csv_smart(path: Path) -> tuple[pd.DataFrame, str]:
    """平台导出 CSV 常见 utf-8-sig / gbk，逐个尝试并返回实际编码。"""
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str, keep_default_na=True)
            if df.shape[1] == 1 and "�" in str(df.columns[0]):
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "乱码")
            return df, enc
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_err = exc
            continue
    raise IngestError(f"CSV 编码无法识别: {path} ({last_err})")


def _read_excel(path: Path, sheet: str | int | None) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet or 0, dtype=str)


def _http_get_json(url: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "ecomflow-auto/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        payload = resp.read().decode("utf-8")
    return json.loads(payload)


def load_from_api(name: str, cfg: Config) -> pd.DataFrame:
    """分页拉取平台开放接口，直到 has_next 为 False 或触达上限。"""
    api = cfg.section("ingest.api")
    base = str(api.get("base_url", "")).rstrip("/")
    path = (api.get("paths") or {}).get(name)
    if not base or not path:
        raise IngestError(f"接口未配置: {name}")
    timeout = int(api.get("timeout_seconds", 8))

    frames: list[pd.DataFrame] = []
    page = 1
    while page <= 50:
        query = urllib.parse.urlencode({"page": page, "page_size": 500})
        data = _http_get_json(f"{base}{path}?{query}", timeout=timeout)
        rows = data.get("data") or []
        if rows:
            frames.append(pd.DataFrame(rows))
        if not data.get("has_next") or not rows:
            break
        page += 1
    if not frames:
        raise IngestError(f"接口 {path} 无数据返回")
    return pd.concat(frames, ignore_index=True)


def load_source(spec: SourceSpec, cfg: Config) -> tuple[pd.DataFrame, IngestLog, dict[str, str]]:
    started = time.perf_counter()
    info = IngestLog(source=spec.name, type=spec.type)
    rename_map = dict(spec.rename or {})

    if spec.type == "api":
        df = load_from_api(spec.name, cfg)
        info.encoding = "utf-8(json)"
    else:
        if not spec.file:
            raise IngestError(f"数据源 {spec.name} 未配置文件路径")
        path = Path(spec.file)
        if not path.is_absolute():
            path = cfg.root / path
        if not path.exists():
            raise IngestError(f"文件不存在: {path}")
        if spec.type == "csv":
            df, enc = read_csv_smart(path)
            info.encoding = enc
        elif spec.type == "excel":
            df = _read_excel(path, spec.sheet)
            info.encoding = "excel"
        else:
            raise IngestError(f"不支持的数据源类型: {spec.type}")

    df = _normalize_columns(df, rename_map)
    missing = [c for c in spec.required_columns if c not in df.columns]
    if missing:
        info.status = "failed"
        info.message = f"缺少必需列: {', '.join(missing)}"
        info.elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise IngestError(f"[{spec.name}] {info.message}；实际列: {list(df.columns)[:15]}")

    df = df.dropna(how="all").reset_index(drop=True)
    info.rows, info.columns = len(df), len(df.columns)
    info.elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("采集 %-10s %-5s %6d 行 %2d 列 %s", spec.name, spec.type, info.rows,
             info.columns, info.encoding)
    return df, info, rename_map


def load_all(cfg: Config | None = None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """按配置读取全部数据源，单个来源失败不阻断其它来源。"""
    cfg = cfg or get_config()
    tables: dict[str, pd.DataFrame] = {}
    logs: list[dict[str, Any]] = []
    for item in cfg.get("ingest.sources", []) or []:
        spec = SourceSpec(
            name=item["name"], type=item.get("type", "csv"), file=item.get("file"),
            sheet=item.get("sheet"), required_columns=item.get("required_columns", []),
            rename=item.get("rename", {}) or {},
        )
        try:
            df, info, _ = load_source(spec, cfg)
            tables[spec.name] = df
            logs.append(info.as_dict())
        except Exception as exc:  # noqa: BLE001 - 采集层需要兜住所有来源异常
            log.error("采集失败 %s: %s", spec.name, exc)
            logs.append(IngestLog(source=spec.name, type=spec.type, status="failed",
                                  message=str(exc)).as_dict())
    return tables, pd.DataFrame(logs)
