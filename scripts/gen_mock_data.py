"""生成模拟电商平台导出数据（含真实场景的脏数据），用于演示与自测。

用法：
    python scripts/gen_mock_data.py            # 覆盖生成 120 天数据
    python scripts/gen_mock_data.py --days 60
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecom_ops.config import ensure_dirs, get_config  # noqa: E402

CATALOG: dict[str, list[str]] = {
    "厨房小电": ["便携榨汁杯", "迷你电煮锅", "多功能料理机", "电热水杯",
                "空气炸锅", "三明治机", "手冲咖啡壶", "电动打蛋器"],
    "个护健康": ["声波电动牙刷", "迷你剃须刀", "颈部按摩仪", "筋膜枪",
                "护眼仪", "冲牙器", "恒温足浴盆", "热敷眼罩"],
    "家居清洁": ["无线吸尘器", "拖地机器人", "除螨仪", "衣物粘毛器",
                "玻璃刮水器", "厨房去油湿巾", "洗衣机清洁剂", "马桶泡腾片"],
    "数码配件": ["65W氮化镓充电器", "磁吸充电宝", "无线蓝牙耳机", "桌面手机支架",
                "Type-C数据线", "车载快充", "蓝牙音箱", "笔记本散热架"],
    "户外运动": ["折叠露营椅", "轻量登山杖", "运动水壶", "瑜伽垫",
                "骑行手套", "速干毛巾", "便携吊床", "野餐垫"],
}
CATEGORY_BASE_PRICE = {
    "厨房小电": 159, "个护健康": 199, "家居清洁": 139, "数码配件": 99, "户外运动": 129,
}

PLATFORMS = ["Tmall", "JD", "PDD", "Douyin"]
PLATFORM_WEIGHT = np.array([0.44, 0.24, 0.20, 0.12])
PLATFORM_PRICE_FACTOR = {"Tmall": 1.15, "JD": 1.25, "PDD": 0.78, "Douyin": 0.92}
PLATFORM_REFUND_RATE = {"Tmall": 0.035, "JD": 0.030, "PDD": 0.055, "Douyin": 0.060}
PLATFORM_ALIAS = {
    "Tmall": ["天猫", "天猫", "tmall", "Tmall", "天猫"],
    "JD": ["京东", "京东", "jd", "JD"],
    "PDD": ["拼多多", "拼多多", "pdd", "PDD"],
    "Douyin": ["抖音", "抖音小店", "douyin", "Douyin"],
}
STATUS_ALIAS = {
    "paid": ["已支付", "支付成功", "PAID", "paid", "已支付"],
    "refunded": ["已退款", "退款成功", "refunded", "REFUND"],
    "cancelled": ["交易关闭", "已取消", "cancelled", "cancel"],
    "pending": ["待付款", "未支付", "pending"],
}
CHANNELS = ["自然搜索", "推荐流量", "付费推广", "直播", "私域"]
CHANNEL_SHARE = np.array([0.30, 0.22, 0.26, 0.14, 0.08])
PROVINCES = ["广东", "浙江", "江苏", "上海", "北京", "山东", "四川", "湖北", "福建",
             "河南", "湖南", "安徽", "陕西", "辽宁", "重庆", "江西", "河北", "云南"]

CAMPAIGNS = [
    ("C202607", "盛夏焕新节", "平台大促", "2026-07-15", "2026-07-20", "ALL", 60000, 1.90),
    ("C202608", "开学季", "主题营销", "2026-08-25", "2026-09-02", "ALL", 45000, 1.70),
    ("C202609A", "99划算节", "平台大促", "2026-09-05", "2026-09-11", "ALL", 80000, 2.20),
    ("C202609B", "中秋礼遇", "节日营销", "2026-09-20", "2026-09-27", "Tmall", 30000, 1.60),
    ("C202610A", "国庆大促", "平台大促", "2026-10-01", "2026-10-07", "ALL", 70000, 2.00),
    ("C202610B", "双11预售", "预售种草", "2026-10-20", "2026-10-25", "ALL", 90000, 2.40),
]
DOW_FACTOR = np.array([0.90, 0.88, 0.95, 1.00, 1.12, 1.25, 1.10])  # 周一..周日


def build_products(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    idx = 1
    for cat, names in CATALOG.items():
        for name in names:
            sku = f"SKU{idx:04d}"
            base = CATEGORY_BASE_PRICE[cat] * rng.uniform(0.75, 1.55)
            list_price = round(base / 10) * 10 - 1  # 常见尾数定价 129/199
            cost = round(list_price * rng.uniform(0.32, 0.52), 2)
            launch = date(2025, 1, 1) + timedelta(days=int(rng.integers(0, 560)))
            if rng.random() < 0.25:  # 25% 为窗口内新品
                launch = date(2026, 7, 1) + timedelta(days=int(rng.integers(0, 90)))
            lifecycle = "新品" if launch >= date(2026, 7, 1) else (
                "清仓" if rng.random() < 0.12 else "成熟")
            rows.append({
                "sku_id": sku, "sku_name": name, "category": cat,
                "brand": "自营品牌", "cost": cost, "list_price": float(list_price),
                "launch_date": launch.isoformat(), "lifecycle": lifecycle,
            })
            idx += 1
    df = pd.DataFrame(rows)
    # 脏数据：3 行重复 SKU、3 行成本缺失
    dup = df.sample(3, random_state=7).copy()
    dup["sku_name"] = dup["sku_name"] + "(重复导入)"
    miss_cost = df.sample(3, random_state=11).index
    df.loc[miss_cost, "cost"] = np.nan
    return pd.concat([df, dup], ignore_index=True)


def campaign_lift(day: date, platform: str) -> float:
    lift = 1.0
    for *_x, start, end, plat, _budget, lf in CAMPAIGNS:
        if date.fromisoformat(start) <= day <= date.fromisoformat(end):
            if plat in ("ALL", platform):
                lift = max(lift, lf)
    return lift


def build_orders(rng, products: pd.DataFrame, days: int, end: date) -> pd.DataFrame:
    sku_pool = products.drop_duplicates("sku_id").reset_index(drop=True)
    n_sku = len(sku_pool)
    # 帕累托权重：头部 SKU 贡献主要销量
    raw_w = 1.0 / np.power(np.arange(1, n_sku + 1), 0.85)
    sku_w = raw_w / raw_w.sum()
    order_skus = rng.choice(sku_pool["sku_id"].to_numpy(), size=n_sku, replace=False, p=sku_w)
    sku_rank = {s: i for i, s in enumerate(order_skus)}
    sku_info = sku_pool.set_index("sku_id")
    # 业务故事：尾部 3 个款近 60 天停止出单（转为滞销库存）
    slow_skus = set(sku_pool["sku_id"].tail(3))

    start = end - timedelta(days=days - 1)
    buyer_pool = [f"B{i:06d}" for i in range(1, 5001)]
    rows: list[dict] = []
    oid = 0
    for i in range(days):
        day = start + timedelta(days=i)
        seasonal = 1 + 0.35 * (i / days) + 0.18 * np.sin((i / 30) * np.pi)
        for platform in PLATFORMS:
            lift = campaign_lift(day, platform)
            lam = 118 * PLATFORM_WEIGHT[PLATFORMS.index(platform)] / 0.44 * seasonal
            lam *= DOW_FACTOR[day.weekday()] * lift * rng.uniform(0.88, 1.12)
            n = max(1, int(rng.poisson(lam)))
            for _ in range(n):
                # 头部 SKU 更常出单
                rank = int(rng.integers(0, n_sku))
                sku = order_skus[min(rank, n_sku - 1)]
                if sku in slow_skus and day > end - timedelta(days=59):
                    continue
                info = sku_info.loc[sku]
                if info["lifecycle"] == "清仓" and rng.random() < 0.55:
                    continue
                if info["lifecycle"] == "新品" and rng.random() < 0.25:
                    continue
                unit_price = round(float(info["list_price"]) * PLATFORM_PRICE_FACTOR[platform]
                                   * rng.uniform(0.92, 1.02), 2)
                qty = int(rng.choice([1, 1, 1, 2, 2, 3]))
                gross = unit_price * qty
                if lift > 1.2:
                    discount = round(gross * rng.uniform(0.10, 0.28), 2)
                else:
                    discount = round(gross * rng.uniform(0.0, 0.10), 2)
                pay = round(gross - discount, 2)
                roll = rng.random()
                rate = PLATFORM_REFUND_RATE[platform]
                status = ("refunded" if roll < rate else
                          "cancelled" if roll < rate + 0.022 else
                          "pending" if roll < rate + 0.037 else "paid")
                ts = datetime.combine(day, datetime.min.time()) + timedelta(
                    seconds=int(rng.integers(6 * 3600, 23 * 3600)))
                oid += 1
                rows.append({
                    "order_id": f"SO{day.strftime('%y%m%d')}{oid:06d}",
                    "order_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "platform": rng.choice(PLATFORM_ALIAS[platform]),
                    "shop_id": f"{platform.upper()[:2]}-01",
                    "sku_id": sku,
                    "buyer_id": rng.choice(buyer_pool),
                    "qty": qty,
                    "unit_price": unit_price,
                    "discount_amount": discount,
                    "pay_amount": pay,
                    "order_status": rng.choice(STATUS_ALIAS[status]),
                    "channel": rng.choice(CHANNELS, p=CHANNEL_SHARE),
                    "province": rng.choice(PROVINCES),
                })
    df = pd.DataFrame(rows)
    df = _mess_up_orders(df, rng)
    return df


def _mess_up_orders(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """注入平台导出常见脏数据。"""
    df = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
    n = len(df)

    # 1) 时间格式混杂：20% 斜杠格式、8% 美式格式
    slash = df.sample(frac=0.20, random_state=1).index
    df.loc[slash, "order_time"] = pd.to_datetime(df.loc[slash, "order_time"]).dt.strftime(
        "%Y/%m/%d %H:%M")
    rest = df.index.difference(slash)
    us = pd.Series(rest).sample(frac=0.08, random_state=2).to_numpy()
    df.loc[us, "order_time"] = pd.to_datetime(df.loc[us, "order_time"]).dt.strftime(
        "%m/%d/%Y %H:%M")

    # 2) 金额缺失 30 行
    df.loc[df.sample(30, random_state=3).index, "pay_amount"] = np.nan
    # 3) 金额勾稽不平 25 行
    bad = df.sample(25, random_state=4).index
    df.loc[bad, "pay_amount"] = (df.loc[bad, "pay_amount"].fillna(0)
                                 + rng.uniform(5, 60, size=len(bad)).round(2))
    # 4) 数量异常 10 行
    df.loc[df.sample(10, random_state=5).index, "qty"] = rng.choice([-1, 0], 10)
    # 5) 商品编码不存在 20 行
    df.loc[df.sample(20, random_state=6).index, "sku_id"] = "SKU9999"
    # 6) 未来时间 1 行
    df.loc[0, "order_time"] = "2027/01/05 10:00"
    # 7) 完全重复行 60 行
    dups = df.sample(60, random_state=8)
    df = pd.concat([df, dups], ignore_index=True)
    # 8) 同一订单重复推送但金额不一致 15 行
    rep = df.sample(15, random_state=9).copy()
    rep["pay_amount"] = rep["pay_amount"].fillna(0) + 12.34
    df = pd.concat([df, rep], ignore_index=True)
    return df.sample(frac=1.0, random_state=99).reset_index(drop=True)


def build_traffic(rng, orders_clean: pd.DataFrame, days: int, end: date) -> pd.DataFrame:
    start = end - timedelta(days=days - 1)
    gmv = (orders_clean[orders_clean["is_sale"]]
           .assign(d=lambda x: pd.to_datetime(x["order_time"]).dt.date)
           .groupby(["d", "platform"], as_index=False)["pay_amount"].sum())
    gmv_map = {(r.d, r.platform): r.pay_amount for r in gmv.itertuples()}
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        for platform in PLATFORMS:
            day_gmv = float(gmv_map.get((day, platform), 0)) * rng.uniform(0.92, 1.08)
            imp_base = 180_000 * PLATFORM_WEIGHT[PLATFORMS.index(platform)] / 0.44
            for ch, share in zip(CHANNELS, CHANNEL_SHARE):
                impressions = int(imp_base * share * rng.uniform(0.8, 1.2))
                ctr = rng.uniform(0.012, 0.045) * (2.2 if ch == "直播" else 1.0)
                clicks = max(1, int(impressions * ctr))
                if ch in ("付费推广", "直播"):
                    spend = round(clicks * rng.uniform(0.6, 2.4), 2)
                elif ch == "推荐流量":
                    spend = round(clicks * rng.uniform(0.05, 0.25), 2)
                else:
                    spend = 0.0
                attr_gmv = round(day_gmv * share * rng.uniform(0.85, 1.15), 2)
                attr_orders = max(1, int(attr_gmv / rng.uniform(80, 180)))
                rows.append({
                    "stat_date": day.isoformat(), "platform": platform, "channel": ch,
                    "impressions": impressions, "clicks": clicks, "spend": spend,
                    "attr_orders": attr_orders, "attr_gmv": attr_gmv,
                })
    df = pd.DataFrame(rows)

    # 脏数据：花费缺失、点击大于曝光、某平台某天数据整体断档
    df.loc[df.sample(15, random_state=21).index, "spend"] = np.nan
    bad = df.sample(5, random_state=22).index
    df.loc[bad, "clicks"] = df.loc[bad, "impressions"] + 5000
    gap_day = end - timedelta(days=6)
    df = df[~((df["stat_date"] == gap_day.isoformat()) & (df["platform"] == "PDD"))]
    # 归因偏差：人为制造两天异常，触发校验规则
    off = (df["stat_date"] == (end - timedelta(days=3)).isoformat()) & (df["platform"] == "Douyin")
    df.loc[off, "attr_gmv"] = (df.loc[off, "attr_gmv"] * 1.28).round(2)
    return df


def build_inventory(rng, orders_clean: pd.DataFrame, products: pd.DataFrame,
                    days: int, end: date) -> pd.DataFrame:
    start = end - timedelta(days=days - 1)
    sale = orders_clean[orders_clean["is_sale"]].copy()
    sale["d"] = pd.to_datetime(sale["order_time"]).dt.date
    daily = sale.groupby(["d", "sku_id"])["qty"].sum()
    skus = products.drop_duplicates("sku_id")["sku_id"].tolist()
    warehouses = {"华东仓": 0.65, "华南仓": 0.35}
    rows = []
    on_hand: dict[tuple[str, str], float] = {}
    in_transit: dict[tuple[str, str], float] = {}
    for sku in skus:
        recent = [float(daily.get((start + timedelta(days=k), sku), 0)) for k in range(min(14, days))]
        avg = max(1.0, float(np.mean(recent)))
        for wh, share in warehouses.items():
            on_hand[(sku, wh)] = round(avg * share * rng.uniform(18, 55), 0)
            in_transit[(sku, wh)] = 0.0

    for i in range(days):
        day = start + timedelta(days=i)
        for sku in skus:
            for wh, share in warehouses.items():
                sold = float(daily.get((day, sku), 0)) * share
                key = (sku, wh)
                on_hand[key] = max(0.0, on_hand[key] - sold)
                if in_transit[key] > 0 and day.day % 5 == 0:
                    on_hand[key] += in_transit[key]
                    in_transit[key] = 0.0
                safety = max(10.0, float(np.mean([
                    float(daily.get((start + timedelta(days=k), sku), 0))
                    for k in range(min(14, days))])) * 7 * 1.2) * share
                if on_hand[key] < safety and in_transit[key] == 0:
                    avg_all = max(1.0, float(np.mean([
                        float(daily.get((start + timedelta(days=k), sku), 0))
                        for k in range(days)])))
                    target = avg_all * 30 * share
                    in_transit[key] = round(max(0.0, target - on_hand[key]), 0)
                rows.append({
                    "snapshot_date": day.isoformat(), "sku_id": sku, "warehouse": wh,
                    "on_hand": int(on_hand[key]), "in_transit": int(in_transit[key]),
                    "safety_stock": int(round(safety)),
                })
    df = pd.DataFrame(rows)

    # 脏数据：负库存、安全库存缺失、重复快照
    df.loc[df.sample(8, random_state=31).index, "on_hand"] = -rng.integers(3, 40, 8)
    df.loc[df.sample(10, random_state=32).index, "safety_stock"] = np.nan
    df = pd.concat([df, df.sample(5, random_state=33)], ignore_index=True)

    # 业务故事：近 60 天无动销的尾款，仓库里压着大量库存
    recent = daily[daily.index.get_level_values("d") > end - timedelta(days=60)]
    recent_qty = recent.groupby(level="sku_id").sum()
    slow = [s for s in skus if float(recent_qty.get(s, 0)) == 0][:3]
    if slow:
        mask = df["sku_id"].isin(slow) & (
            df["snapshot_date"] >= (end - timedelta(days=60)).isoformat())
        df.loc[mask, "on_hand"] = rng.integers(600, 1200, int(mask.sum()))
    return df.sort_values(["snapshot_date", "sku_id", "warehouse"]).reset_index(drop=True)


def build_campaigns(rng, products: pd.DataFrame) -> pd.DataFrame:
    skus = products.drop_duplicates("sku_id")["sku_id"].tolist()
    rows = []
    for cid, name, ctype, start, end, plat, budget, _lift in CAMPAIGNS:
        picked = rng.choice(skus, size=12, replace=False)
        rows.append({
            "campaign_id": cid, "campaign_name": name, "campaign_type": ctype,
            "start_date": start, "end_date": end, "platform": plat,
            "sku_ids": ",".join(sorted(picked)), "budget": budget,
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成模拟电商数据")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--end", type=str, default=(date.today() - timedelta(days=1)).isoformat())
    args = parser.parse_args()

    cfg = get_config()
    ensure_dirs(cfg)
    raw = cfg.path("paths.raw")
    raw.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261022)
    end = date.fromisoformat(args.end)

    products = build_products(rng)
    orders_raw = build_orders(rng, products, args.days, end)

    # 生成流量/库存需要"已支付"口径的订单，先做最轻量标记
    tmp = orders_raw.drop_duplicates("order_id").copy()
    tmp["order_time"] = pd.to_datetime(tmp["order_time"], errors="coerce", format="mixed")
    tmp["platform"] = tmp["platform"].replace(
        {"天猫": "Tmall", "tmall": "Tmall", "淘宝": "Taobao", "京东": "JD", "jd": "JD",
         "拼多多": "PDD", "pdd": "PDD", "抖音": "Douyin", "抖音小店": "Douyin", "douyin": "Douyin"})
    tmp["order_status"] = tmp["order_status"].replace(
        {a: k for k, v in STATUS_ALIAS.items() for a in v})
    tmp["qty"] = pd.to_numeric(tmp["qty"], errors="coerce").fillna(0)
    tmp["pay_amount"] = pd.to_numeric(tmp["pay_amount"], errors="coerce")
    tmp["pay_amount"] = tmp["pay_amount"].fillna(
        tmp["unit_price"] * tmp["qty"] - tmp["discount_amount"])
    tmp["is_sale"] = (tmp["order_status"] == "paid") & (tmp["order_time"].notna()) \
        & (tmp["order_time"] <= pd.Timestamp(end)) & (tmp["qty"] > 0)
    tmp["platform"] = tmp["platform"].where(tmp["platform"].isin(PLATFORMS), "Tmall")

    traffic = build_traffic(rng, tmp, args.days, end)
    inventory = build_inventory(rng, tmp, products, args.days, end)
    campaigns = build_campaigns(rng, products)

    outputs = {
        "products.csv": products, "orders.csv": orders_raw,
        "inventory.csv": inventory, "traffic.csv": traffic, "campaigns.csv": campaigns,
    }
    for name, df in outputs.items():
        path = raw / name
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"  {name:<16} {len(df):>7,} 行 -> {path}")

    print(f"\n模拟数据生成完成：{args.days} 天（{end - timedelta(days=args.days - 1)} ~ {end}）")
    print(f"SKU {len(products.drop_duplicates('sku_id'))} 个 | 订单 {len(orders_raw):,} 行 "
          f"| 已注入重复/缺失/勾稽不平/负库存等脏数据")


if __name__ == "__main__":
    main()
