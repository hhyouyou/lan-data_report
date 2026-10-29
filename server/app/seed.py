"""生成演示数据（合成）。

真实项目里这一层换成「Excel 导入 / 业务库同步」。
合成数据的目的是：在真实数据还没到位时，先把平台机制跑通。

注意：只写 data/demo.sqlite（演示库），已与真实年鉴库 data/warehouse.sqlite
物理分开，不会碰到 fact_stat / dim_indicator / dim_region。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

from . import config

SEED = 20261019

PROVINCES = {
    "华东": ["上海", "杭州", "南京", "苏州", "合肥"],
    "华南": ["广州", "深圳", "厦门", "南宁"],
    "华北": ["北京", "天津", "石家庄"],
    "华中": ["武汉", "长沙", "郑州"],
    "西南": ["成都", "重庆", "昆明"],
    "西北": ["西安", "兰州"],
    "东北": ["沈阳", "大连", "哈尔滨"],
}

CATEGORIES = {
    "女装": ["连衣裙", "上衣", "裤装"],
    "男装": ["衬衫", "T恤", "外套"],
    "美妆": ["护肤", "彩妆", "香水"],
    "数码": ["手机", "耳机", "平板"],
    "食品": ["零食", "饮料", "生鲜"],
    "家居": ["家纺", "厨具", "收纳"],
}

CHANNELS = ["直播", "搜索", "推荐", "自然流量", "广告"]
CHANNEL_WEIGHTS = [0.28, 0.22, 0.24, 0.16, 0.10]
PAY_METHODS = ["微信", "支付宝", "银行卡", "余额"]

STATUS_WEIGHT = [("paid", 0.92), ("refunded", 0.05), ("cancelled", 0.03)]


def _weighted(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


def build(start: date, end: date, users: int = 3000, products: int = 500,
          daily: int = 150) -> None:
    random.seed(SEED)
    config.DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DEMO_DB)
    conn.executescript("""
        DROP TABLE IF EXISTS fact_order;
        DROP TABLE IF EXISTS dim_user;
        DROP TABLE IF EXISTS dim_product;

        CREATE TABLE dim_user(
            user_id INTEGER PRIMARY KEY,
            province TEXT, city TEXT, phone TEXT, register_date TEXT
        );
        CREATE TABLE dim_product(
            product_id INTEGER PRIMARY KEY,
            category_l1 TEXT, category_l2 TEXT, price REAL
        );
        CREATE TABLE fact_order(
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER, product_id INTEGER,
            paid_at TEXT, status TEXT, channel TEXT,
            paid_amount REAL, refund_amount REAL,
            qty INTEGER, is_new_user INTEGER, pay_method TEXT
        );
        CREATE INDEX idx_order_paid ON fact_order(paid_at);
    """)

    # 用户
    user_rows = []
    for uid in range(1, users + 1):
        province = random.choice(list(PROVINCES))
        city = random.choice(PROVINCES[province])
        phone = f"1{random.choice('3589')}{random.randint(0, 999999999):09d}"[:11]
        reg = start - timedelta(days=random.randint(0, 300))
        user_rows.append((uid, province, city, phone, reg.isoformat()))
    conn.executemany("INSERT INTO dim_user VALUES (?,?,?,?,?)", user_rows)

    # 商品
    product_rows = []
    prods = []
    for pid in range(1, products + 1):
        l1 = random.choice(list(CATEGORIES))
        l2 = random.choice(CATEGORIES[l1])
        price = round(random.uniform(30, 3000), 2)
        product_rows.append((pid, l1, l2, price))
        prods.append((l1, price))
    conn.executemany("INSERT INTO dim_product VALUES (?,?,?,?)", product_rows)

    # 订单
    order_rows = []
    oid = 1
    d = start
    while d <= end:
        # 周末和月中促销略高
        factor = 1.35 if d.weekday() >= 5 else 1.0
        if d.day in (15, 16, 17):
            factor *= 1.4
        n = max(10, int(daily * factor * random.uniform(0.85, 1.15)))
        for _ in range(n):
            uid = random.randint(1, users)
            pid = random.randint(1, products)
            l1, price = prods[pid - 1]
            qty = _weighted([1, 2, 3, 4], [0.62, 0.24, 0.10, 0.04])
            amount = round(price * qty * random.uniform(0.82, 1.0), 2)
            status = _weighted([s for s, _ in STATUS_WEIGHT], [w for _, w in STATUS_WEIGHT])
            channel = _weighted(CHANNELS, CHANNEL_WEIGHTS)
            refund = round(amount * random.uniform(0.3, 1.0), 2) if status == "refunded" else 0.0
            paid_at = datetime.combine(d, datetime.min.time()) + timedelta(
                seconds=random.randint(0, 86399))
            order_rows.append((
                oid, uid, pid, paid_at.isoformat(sep=" "), status, channel,
                amount, refund, qty, 1 if random.random() < 0.18 else 0,
                random.choice(PAY_METHODS),
            ))
            oid += 1
        d += timedelta(days=1)

    conn.executemany("INSERT INTO fact_order VALUES (?,?,?,?,?,?,?,?,?,?,?)", order_rows)
    conn.commit()
    conn.close()

    count = len(order_rows)
    print(f"✓ 演示数据已生成: {config.DEMO_DB}")
    print(f"  用户 {users} / 商品 {products} / 订单 {count}")
    print(f"  时间范围 {start} ~ {end}")


def main() -> None:
    today = date.today()
    build(start=date(today.year - 1, 9, 1), end=today - timedelta(days=1))


if __name__ == "__main__":
    main()
