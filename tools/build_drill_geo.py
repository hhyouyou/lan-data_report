#!/usr/bin/env python3
"""生成市级下钻地图：把各市市辖区合并成一块「XX市区」。

背景：年鉴「市县篇」不单列市辖区，但全市口径包含它们。
本脚本把 GeoJSON 里属于市辖区的多个 feature 合并成一个 MultiPolygon，
命名与数据库中的「XX市区」一致，这样地图上就是**一整块**，不再有灰色碎区。

输入：web/vendor/geo/{adcode}.json   （DataV 原始，含区县边界）
输出：web/vendor/geo/drill/{adcode}.json（市区合并 + 县市原样）

用法：
    python tools/build_drill_geo.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import config  # noqa: E402

GEO = ROOT / "web" / "vendor" / "geo"
OUT = GEO / "drill"

CITY_ADCODE = {
    "南京": "320100", "无锡": "320200", "徐州": "320300", "常州": "320400",
    "苏州": "320500", "南通": "320600", "连云港": "320700", "淮安": "320800",
    "盐城": "320900", "扬州": "321000", "镇江": "321100", "泰州": "321200",
    "宿迁": "321300",
}


def county_names(conn: sqlite3.Connection, city: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT region_name FROM fact_stat "
        "WHERE geo_level='county' AND parent_region=? AND region_name NOT LIKE '%市区'",
        (f"{city}市",),
    ).fetchall()
    return {r[0] for r in rows}


def build_city(conn: sqlite3.Connection, city: str, adcode: str) -> dict:
    src = GEO / f"{adcode}.json"
    geo = json.loads(src.read_text(encoding="utf-8"))
    counties = county_names(conn, city)

    kept: list[dict] = []          # 县市：原样保留
    urban: list[dict] = []         # 市辖区：待合并
    for feat in geo["features"]:
        name = feat["properties"].get("name")
        (kept if name in counties else urban).append(feat)

    merged = unary_union([shape(f["geometry"]) for f in urban])
    urban_feature = {
        "type": "Feature",
        "properties": {"name": f"{city}市区", "adcode": adcode, "level": "district"},
        "geometry": mapping(merged),
    }
    out = {
        "type": "FeatureCollection",
        "features": [urban_feature] + kept,
    }
    return {
        "city": city,
        "source_features": len(geo["features"]),
        "merged_districts": len(urban),
        "out_features": len(out["features"]),
        "out": out,
    }


def main() -> int:
    conn = sqlite3.connect(config.WAREHOUSE)
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        ok = 0
        for city, adcode in CITY_ADCODE.items():
            res = build_city(conn, city, adcode)
            path = OUT / f"{adcode}.json"
            path.write_text(json.dumps(res["out"], ensure_ascii=False), encoding="utf-8")
            ok += 1
            print(f"  {city:<4} 原始 {res['source_features']:>2} 块 → 合并 {res['merged_districts']:>2} 个区"
                  f" → 输出 {res['out_features']:>2} 块  ({path.stat().st_size/1024:.0f} KB)")
        total = sum(p.stat().st_size for p in OUT.glob("*.json"))
        print(f"\n✓ 13 个下钻底图，合计 {total/1024:.0f} KB → {OUT}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
