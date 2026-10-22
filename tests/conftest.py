"""测试公共夹具：构造一小份"脏数据"，不依赖已生成的文件。"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecom_ops.clean import clean_all  # noqa: E402
from ecom_ops.config import get_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return get_config()


@pytest.fixture
def today() -> date:
    return date.today() - timedelta(days=1)


@pytest.fixture
def raw_products() -> pd.DataFrame:
    return pd.DataFrame({
        "sku_id": ["SKU0001", "SKU0002", "SKU0002"],
        "sku_name": ["测试商品A", "测试商品B", "测试商品B(重复)"],
        "category": ["厨房小电", "个护健康", "个护健康"],
        "brand": ["自营", "自营", "自营"],
        "cost": [40.0, None, 65.0],
        "list_price": [100.0, 150.0, 150.0],
        "launch_date": ["2025-01-01", "2025-06-01", "2025-06-01"],
        "lifecycle": ["成熟", "成熟", "成熟"],
    })


@pytest.fixture
def raw_orders(today: date) -> pd.DataFrame:
    d1 = today - timedelta(days=2)
    d2 = today - timedelta(days=1)
    return pd.DataFrame([
        # 正常单
        {"order_id": "SO1", "order_time": f"{d1} 10:00:00", "platform": "天猫",
         "shop_id": "TM-01", "sku_id": "SKU0001", "buyer_id": "B1", "qty": 2,
         "unit_price": 100.0, "discount_amount": 20.0, "pay_amount": 180.0,
         "order_status": "已支付", "channel": "自然搜索", "province": "广东"},
        # 状态大写 + 时间斜杠格式
        {"order_id": "SO2", "order_time": f"{d1.year}/{d1.month:02d}/{d1.day:02d} 11:00",
         "platform": "JD", "shop_id": "JD-01", "sku_id": "SKU0002", "buyer_id": "B2",
         "qty": 1, "unit_price": 150.0, "discount_amount": 0.0, "pay_amount": 150.0,
         "order_status": "PAID", "channel": "付费推广", "province": "浙江"},
        # 金额缺失 + 退款
        {"order_id": "SO3", "order_time": f"{d2.month:02d}/{d2.day:02d}/{d2.year} 09:30",
         "platform": "拼多多", "shop_id": "PD-01", "sku_id": "SKU0001", "buyer_id": "B1",
         "qty": 1, "unit_price": 100.0, "discount_amount": 0.0, "pay_amount": None,
         "order_status": "已退款", "channel": "直播", "province": "江苏"},
        # 商品主数据缺失
        {"order_id": "SO4", "order_time": f"{d2} 12:00:00", "platform": "tmall",
         "shop_id": "TM-01", "sku_id": "SKU9999", "buyer_id": "B3", "qty": 1,
         "unit_price": 100.0, "discount_amount": 0.0, "pay_amount": 100.0,
         "order_status": "已支付", "channel": "私域", "province": "上海"},
        # 数量为 0
        {"order_id": "SO5", "order_time": f"{d2} 08:00:00", "platform": "抖音",
         "shop_id": "DY-01", "sku_id": "SKU0002", "buyer_id": "B4", "qty": 0,
         "unit_price": 150.0, "discount_amount": 0.0, "pay_amount": 0.0,
         "order_status": "待付款", "channel": "推荐流量", "province": "北京"},
        # 完全重复
        {"order_id": "SO1", "order_time": f"{d1} 10:00:00", "platform": "天猫",
         "shop_id": "TM-01", "sku_id": "SKU0001", "buyer_id": "B1", "qty": 2,
         "unit_price": 100.0, "discount_amount": 20.0, "pay_amount": 180.0,
         "order_status": "已支付", "channel": "自然搜索", "province": "广东"},
        # 同一订单重复推送、金额不一致
        {"order_id": "SO2", "order_time": f"{d1} 11:00:00", "platform": "JD",
         "shop_id": "JD-01", "sku_id": "SKU0002", "buyer_id": "B2", "qty": 1,
         "unit_price": 150.0, "discount_amount": 0.0, "pay_amount": 199.0,
         "order_status": "paid", "channel": "付费推广", "province": "浙江"},
        # 未来时间
        {"order_id": "SO6", "order_time": "2030-01-01 10:00:00", "platform": "天猫",
         "shop_id": "TM-01", "sku_id": "SKU0001", "buyer_id": "B5", "qty": 1,
         "unit_price": 100.0, "discount_amount": 0.0, "pay_amount": 100.0,
         "order_status": "已支付", "channel": "自然搜索", "province": "福建"},
        # 金额勾稽不平
        {"order_id": "SO7", "order_time": f"{d2} 15:00:00", "platform": "京东",
         "shop_id": "JD-01", "sku_id": "SKU0001", "buyer_id": "B6", "qty": 1,
         "unit_price": 100.0, "discount_amount": 0.0, "pay_amount": 260.0,
         "order_status": "支付成功", "channel": "自然搜索", "province": "山东"},
    ])


@pytest.fixture
def raw_inventory(today: date) -> pd.DataFrame:
    d = today.isoformat()
    return pd.DataFrame([
        {"snapshot_date": d, "sku_id": "SKU0001", "warehouse": "华东仓",
         "on_hand": -5, "in_transit": 0, "safety_stock": 20},
        {"snapshot_date": d, "sku_id": "SKU0001", "warehouse": "华南仓",
         "on_hand": 0, "in_transit": 0, "safety_stock": None},
        {"snapshot_date": d, "sku_id": "SKU0002", "warehouse": "华东仓",
         "on_hand": 300, "in_transit": 0, "safety_stock": 30},
        {"snapshot_date": d, "sku_id": "SKU0002", "warehouse": "华东仓",
         "on_hand": 300, "in_transit": 0, "safety_stock": 30},
    ])


@pytest.fixture
def raw_traffic(today: date) -> pd.DataFrame:
    d = today.isoformat()
    return pd.DataFrame([
        {"stat_date": d, "platform": "Tmall", "channel": "付费推广",
         "impressions": 1000, "clicks": 5000, "spend": None, "attr_orders": 10,
         "attr_gmv": 60.0},
        {"stat_date": d, "platform": "Tmall", "channel": "自然搜索",
         "impressions": 2000, "clicks": 80, "spend": 0, "attr_orders": 20,
         "attr_gmv": 40.0},
    ])


@pytest.fixture
def raw_campaigns(today: date) -> pd.DataFrame:
    start = today - timedelta(days=7)
    end = today - timedelta(days=3)
    return pd.DataFrame([{
        "campaign_id": "C1", "campaign_name": "测试大促", "campaign_type": "平台大促",
        "start_date": start.isoformat(), "end_date": end.isoformat(), "platform": "ALL",
        "sku_ids": "SKU0001,SKU0002", "budget": 1000.0,
    }])


@pytest.fixture
def tables(raw_products, raw_orders, raw_inventory, raw_traffic, raw_campaigns, cfg):
    raw = {
        "products": raw_products, "orders": raw_orders, "inventory": raw_inventory,
        "traffic": raw_traffic, "campaigns": raw_campaigns,
    }
    cleaned, log = clean_all(raw, cfg)
    return cleaned, log.to_frame()
