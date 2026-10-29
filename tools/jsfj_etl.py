#!/usr/bin/env python3
"""江苏统计年鉴 ETL（P0 + P1 + 地图用县级数据）。

P0：省级经济 / 人口 / 收入
P1：13 个设区市 GDP（2018–2024）、人口（2010–2024）、城乡收入（2024）
地图：40 个县/县级市 2024 年 GDP / 人均 GDP（20-4 表，不含市辖区）

输出：warehouse 里的 fact_stat / dim_indicator / dim_region
schema 说明见 docs/09。

用法：
    python tools/jsfj_etl.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import config  # noqa: E402

SRC = ROOT / "data" / "jsfj" / "2025" / "2025"
PROVINCE = "江苏省"

CITY_ORDER = ["南京", "无锡", "徐州", "常州", "苏州", "南通",
              "连云港", "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁"]
CITY_GROUP = {
    "南京": "苏南", "无锡": "苏南", "常州": "苏南", "苏州": "苏南", "镇江": "苏南",
    "南通": "苏中", "扬州": "苏中", "泰州": "苏中",
    "徐州": "苏北", "连云港": "苏北", "淮安": "苏北", "盐城": "苏北", "宿迁": "苏北",
}

INDICATORS: dict[str, tuple[str, str, str]] = {
    "gdp": ("地区生产总值", "亿元", "经济总量"),
    "gdp_primary": ("第一产业增加值", "亿元", "经济结构"),
    "gdp_secondary": ("第二产业增加值", "亿元", "经济结构"),
    "gdp_tertiary": ("第三产业增加值", "亿元", "经济结构"),
    "gdp_industry": ("工业增加值", "亿元", "经济结构"),
    "gdp_construction": ("建筑业增加值", "亿元", "经济结构"),
    "gdp_finance": ("金融业增加值", "亿元", "经济结构"),
    "gdp_realestate": ("房地产业增加值", "亿元", "经济结构"),
    "gdp_per_capita": ("人均地区生产总值", "元", "经济总量"),
    "households": ("总户数", "万户", "人口"),
    "population_total": ("常住人口", "万人", "人口"),
    "population_urban": ("城镇人口", "万人", "人口"),
    "urbanization_rate": ("城镇化率", "%", "人口"),
    "population_male": ("男性人口", "万人", "人口"),
    "population_male_share": ("男性人口比重", "%", "人口"),
    "population_female": ("女性人口", "万人", "人口"),
    "population_female_share": ("女性人口比重", "%", "人口"),
    "avg_household_size": ("平均每户人数", "人/户", "人口"),
    "population_avg": ("年平均人口", "万人", "人口"),
    "population_registered": ("年末户籍人口", "万人", "人口"),
    "population_density": ("人口密度", "人/平方公里", "人口"),
    "income_all": ("全体居民人均可支配收入", "元", "收入"),
    "consumption": ("居民人均生活消费支出", "元", "收入"),
    "engle": ("居民恩格尔系数", "%", "收入"),
    "housing": ("居民人均住房建筑面积", "平方米", "收入"),
    "income_wage": ("工资性收入", "元", "收入"),
    "income_business": ("经营净收入", "元", "收入"),
    "income_property": ("财产净收入", "元", "收入"),
    "income_transfer": ("转移净收入", "元", "收入"),
    "income_urban": ("城镇居民人均可支配收入", "元", "收入"),
    "income_rural": ("农村居民人均可支配收入", "元", "收入"),
    "fixed_investment": ("固定资产投资", "亿元", "投资消费"),
    "retail_sales": ("社会消费品零售总额", "亿元", "投资消费"),
    "realestate_invest": ("房地产开发投资", "亿元", "投资消费"),
    "fiscal_revenue": ("一般公共预算收入", "亿元", "财政"),
    "fiscal_expenditure": ("一般公共预算支出", "亿元", "财政"),
    "electricity": ("全社会用电量", "亿千瓦时", "能源"),
    "industrial_electricity": ("工业用电量", "亿千瓦时", "能源"),
    "fixed_investment_growth": ("固定资产投资增速", "%", "投资消费"),
    "industrial_investment_growth": ("工业投资增速", "%", "投资消费"),
}

GDP_KEY_BY_TEXT = [
    ("人均地区生产总值", "gdp_per_capita"),
    ("第一产业", "gdp_primary"),
    ("第二产业", "gdp_secondary"),
    ("第三产业", "gdp_tertiary"),
    ("工业增加值", "gdp_industry"),
    ("地区生产总值", "gdp"),
]

POP_BLOCKS = [("城镇人口比重", "urbanization_rate"),
              ("总人口", "population_total"),
              ("城镇人口", "population_urban")]

# 运行时构建
REGIONS: list[tuple] = []
REGION_INDEX: dict[str, tuple] = {}
REGION_TIER: dict[str, str] = {}

TIER_BOUNDS = [(1000, "千亿县"), (900, "900-1000亿"),
               (800, "800-900亿"), (700, "700-800亿")]
TIER_LOW = "700亿以下"


def tier_of(value: float) -> str:
    for low, label in TIER_BOUNDS:
        if value >= low:
            return label
    return TIER_LOW


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------
def norm(value) -> str:
    return re.sub(r"\s+", "", str(value)) if value is not None else ""


def to_year(value) -> int | None:
    if isinstance(value, (int, float)) and not pd.isna(value):
        y = int(value)
        return y if 1949 <= y <= 2030 else None
    s = str(value).strip().replace("年", "")
    if re.fullmatch(r"(19|20)\d{2}", s):
        return int(s)
    return None


def to_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("，", "")
    if s in {"", "—", "-", "…", "...", "－", "．"}:
        return None
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def read_sheet(path: Path, prefix: str) -> tuple[str, pd.DataFrame]:
    xl = pd.ExcelFile(path)
    sheet = next((s for s in xl.sheet_names if s.strip().startswith(prefix)), None)
    if not sheet:
        raise ValueError(f"{path.name} 找不到表 {prefix}")
    return sheet.strip(), pd.read_excel(path, sheet_name=sheet, header=None)


def match_city(text: str) -> str | None:
    for city in CITY_ORDER:
        if text.startswith(city):
            return city
    return None


def fact(key: str, region_name: str, geo_level: str, year: int, value: float,
         source_table: str, source_sheet: str, parent: str = "") -> tuple:
    r = REGION_INDEX[region_name]
    return (key, r[0], r[1], r[4], geo_level, year, f"{year}-01-01",
            value, INDICATORS[key][1], source_table, source_sheet, parent,
            REGION_TIER.get(region_name, ""))


# ---------------------------------------------------------------------------
# P0：省级表
# ---------------------------------------------------------------------------
P0_PARSERS = [
    {"file": "国民经济核算.xlsx", "sheet": "2-1",
     "columns": {1: "gdp", 2: "gdp_primary", 3: "gdp_secondary", 4: "gdp_tertiary",
                 5: "gdp_industry", 6: "gdp_construction", 7: "gdp_finance",
                 8: "gdp_realestate", 9: "gdp_per_capita"}},
    {"file": "人口、就业和工资.xlsx", "sheet": "3-1",
     "columns": {1: "households", 2: "population_total", 3: "population_male",
                 4: "population_male_share", 5: "population_female",
                 6: "population_female_share", 7: "avg_household_size",
                 8: "population_avg", 9: "population_density"}},
    {"file": "人民生活.xlsx", "sheet": "5-2",
     "columns": {1: "income_all", 2: "income_wage", 3: "income_business",
                 4: "income_property", 5: "income_transfer"}},
    {"file": "人民生活.xlsx", "sheet": "5-3", "columns": {1: "income_urban"}},
    {"file": "人民生活.xlsx", "sheet": "5-4", "columns": {1: "income_rural"}},
]


def parse_province() -> list[tuple]:
    rows: list[tuple] = []
    for spec in P0_PARSERS:
        path = SRC / spec["file"]
        sheet_name, raw = read_sheet(path, spec["sheet"])
        for _, row in raw.iterrows():
            year = to_year(row.iloc[0])
            if year is None:
                continue
            for col, key in spec["columns"].items():
                if col >= len(row):
                    continue
                value = to_float(row.iloc[col])
                if value is not None:
                    rows.append(fact(key, PROVINCE, "province", year, value,
                                     spec["sheet"], sheet_name))
    return rows


# ---------------------------------------------------------------------------
# P1：13 市 GDP（2-9）
# ---------------------------------------------------------------------------
def parse_city_gdp() -> list[tuple]:
    sheet_name, raw = read_sheet(SRC / "国民经济核算.xlsx", "2-9")
    years = list(range(2018, 2025))
    rows: list[tuple] = []
    current: str | None = None

    for _, row in raw.iterrows():
        first = norm(row.iloc[0])
        third = norm(row.iloc[2]) if len(row) > 2 else ""
        if first == "地区" and third:
            current = next((k for token, k in GDP_KEY_BY_TEXT if token in third), None)
            continue
        if not current:
            continue
        city = match_city(first)
        if not city:
            continue
        for i, year in enumerate(years):
            col = 2 + i
            if col >= len(row):
                continue
            value = to_float(row.iloc[col])
            if value is not None:
                rows.append(fact(current, f"{city}市", "city", year, value, "2-9", sheet_name))
    return rows


# ---------------------------------------------------------------------------
# P1：13 市人口（3-6）
# ---------------------------------------------------------------------------
def parse_city_population() -> list[tuple]:
    sheet_name, raw = read_sheet(SRC / "人口、就业和工资.xlsx", "3-6")
    rows: list[tuple] = []
    current: str | None = None
    city_cols = {3 + i: city for i, city in enumerate(CITY_ORDER)}

    for _, row in raw.iterrows():
        first = norm(row.iloc[0])
        for token, key in POP_BLOCKS:
            if first.startswith(token):
                current = key
                break
        if not current:
            continue
        year = to_year(row.iloc[1])
        if year is None:
            continue

        value = to_float(row.iloc[2])
        if value is not None and current in ("urbanization_rate", "population_urban"):
            rows.append(fact(current, PROVINCE, "province", year, value, "3-6", sheet_name))
        for col, city in city_cols.items():
            if col >= len(row):
                continue
            value = to_float(row.iloc[col])
            if value is not None:
                rows.append(fact(current, f"{city}市", "city", year, value, "3-6", sheet_name))
    return rows


# ---------------------------------------------------------------------------
# P1：13 市城乡收入（5-17 / 5-18）
# ---------------------------------------------------------------------------
def parse_city_income() -> list[tuple]:
    rows: list[tuple] = []
    for prefix, key in (("5-17", "income_urban"), ("5-18", "income_rural")):
        sheet_name, raw = read_sheet(SRC / "人民生活.xlsx", prefix)
        m = re.search(r"((?:19|20)\d{2})年", sheet_name)
        year = int(m.group(1)) if m else 2024
        target = None
        for _, row in raw.iterrows():
            if norm(row.iloc[0]).startswith("人均可支配收入"):
                target = row
                break
        if target is None:
            continue
        for i, city in enumerate(CITY_ORDER):
            col = 5 + i
            if col >= len(target):
                continue
            value = to_float(target.iloc[col])
            if value is not None:
                rows.append(fact(key, f"{city}市", "city", year, value, prefix, sheet_name))
    return rows


# ---------------------------------------------------------------------------
# 地图：20-4 市县社会经济（县 / 县级市，不含市辖区）
# ---------------------------------------------------------------------------
COUNTY_COLUMNS = {2: "gdp", 3: "gdp_primary", 4: "gdp_secondary",
                  5: "gdp_tertiary", 6: "gdp_industry", 7: "gdp_per_capita"}


def _read_20_4():
    sheet_name, raw = read_sheet(SRC / "市县社会经济.xlsx", "20-4")
    m = re.search(r"((?:19|20)\d{2})年", sheet_name)
    year = int(m.group(1)) if m else 2024
    return sheet_name, raw, year


def scan_counties() -> list[tuple[str, str]]:
    """返回 [(城市, 县市名称)]，按表内顺序。"""
    _, raw, _ = _read_20_4()
    out: list[tuple[str, str]] = []
    current = None
    for _, row in raw.iterrows():
        name = norm(row.iloc[0])
        if not name or name.startswith("市县") or name.startswith("单位"):
            continue
        if any(name == c + "市" for c in CITY_ORDER):
            current = name
            continue
        if current and (name.endswith("县") or name.endswith("市")):
            out.append((current, name))
    return out


def parse_counties() -> list[tuple]:
    sheet_name, raw, year = _read_20_4()
    rows: list[tuple] = []
    current = None
    for _, row in raw.iterrows():
        name = norm(row.iloc[0])
        if not name or name.startswith("市县") or name.startswith("单位"):
            continue
        if any(name == c + "市" for c in CITY_ORDER):
            current = name                     # 地级市行，数据已由 2-9 提供
            continue
        if not (current and (name.endswith("县") or name.endswith("市"))):
            continue
        for col, key in COUNTY_COLUMNS.items():
            if col >= len(row):
                continue
            value = to_float(row.iloc[col])
            if value is not None:
                rows.append(fact(key, name, "county", year, value,
                                 "20-4", sheet_name, parent=current))
    return rows


def _read_20_4_city_values():
    """20-4 里的地级市行（全市口径，与 2-9 完全一致）。"""
    _, raw, year = _read_20_4()
    out: dict[str, dict[str, float]] = {}
    for _, row in raw.iterrows():
        name = norm(row.iloc[0])
        if name not in {c + "市" for c in CITY_ORDER}:
            continue
        vals = {}
        for col, key in COUNTY_COLUMNS.items():
            if col < len(row):
                v = to_float(row.iloc[col])
                if v is not None:
                    vals[key] = v
        out[name] = vals
    return out, year


def parse_urban_districts(county_facts: list[tuple]) -> list[tuple]:
    """市区 = 全市 − 各县市之和。

    年鉴「市县篇」不单列市辖区，但全市口径包含它们，因此可以精确倒算。
    """
    city_vals, year = _read_20_4_city_values()

    # 按 (城市, 指标) 汇总县市值
    sums: dict[tuple[str, str], float] = {}
    for f in county_facts:
        key, name = f[0], f[11]
        sums[(name, key)] = sums.get((name, key), 0.0) + f[7]

    rows: list[tuple] = []
    for city, vals in city_vals.items():
        for key, total in vals.items():
            if key == "gdp_per_capita" and sums.get((city, key), 0.0) > 0:
                # 人均值不可相减：有县市的城市交给「市区 GDP / 市区人口」倒算
                continue
            residual = round(total - sums.get((city, key), 0.0), 2)
            if residual <= 0:
                continue                      # 理论上不会发生
            rows.append(fact(key, f"{city.rstrip('市')}市区", "county",
                             year, residual, "20-4", "20-4 地区生产总值", parent=city))
    return rows


# ---------------------------------------------------------------------------
# 县级人口（20-1）与人民生活（20-16）
# ---------------------------------------------------------------------------
def _read_20_1() -> tuple[dict, dict, int]:
    """20-1 人口：年末户籍人口 / 年末常住人口 / 人口密度。"""
    sheet_name, raw = read_sheet(SRC / "市县社会经济.xlsx", "20-1")
    m = re.search(r"((?:19|20)\d{2})年", sheet_name)
    year = int(m.group(1)) if m else 2024
    cities: dict[str, tuple] = {}
    counties: dict[str, tuple] = {}
    for _, row in raw.iterrows():
        name = norm(row.iloc[0])
        if not name or name.startswith(("市县", "单位")):
            continue
        vals = (to_float(row.iloc[2]), to_float(row.iloc[3]), to_float(row.iloc[4]))
        if any(name == c + "市" for c in CITY_ORDER):
            cities[name] = vals
        elif name.endswith(("县", "市")):
            counties[name] = vals
    return cities, counties, year


def _read_20_16() -> tuple[dict, int]:
    """20-16 人民生活：全体 / 城镇 / 农村 三张表，各占两页（续表）。

    返回 {"all": {地区: (收入, 消费, 恩格尔, 住房)}, "urban": {...}, "rural": {...}}
    """
    sheet_name, raw = read_sheet(SRC / "市县社会经济.xlsx", "20-16")
    m = re.search(r"((?:19|20)\d{2})年", sheet_name)
    year = int(m.group(1)) if m else 2024

    blocks: dict[str, dict] = {"all": {}, "urban": {}, "rural": {}}
    current: str | None = None
    for _, row in raw.iterrows():
        c0 = norm(row.iloc[0])
        c1 = str(row.iloc[1]) if row.iloc[1] is not None else ""
        if c1.strip() == "City and County":
            head = str(row.iloc[2]) if row.iloc[2] is not None else ""
            current = "urban" if "城镇" in head else ("rural" if "农村" in head else "all")
            continue
        if not c0 or c0.startswith(("市县", "单位")) or "20-16" in c0 or current is None:
            continue
        vals = tuple(to_float(row.iloc[c]) for c in (2, 3, 5, 6))
        if any(v is not None for v in vals):
            blocks[current][c0] = vals
    return blocks, year


def parse_county_pop_income() -> list[tuple]:
    """县 / 县级市的人口与收入。"""
    _, pop, pop_year = _read_20_1()
    inc, inc_year = _read_20_16()
    parent = {county: city for city, county in scan_counties()}
    rows: list[tuple] = []
    for name, (reg, res, den) in pop.items():
        city = parent.get(name, "")
        for key, v in (("population_registered", reg), ("population_total", res),
                       ("population_density", den)):
            if v is not None:
                rows.append(fact(key, name, "county", pop_year, v,
                                 "20-1", "20-1 人口", parent=city))
    for name, (income, consumption, engle, housing) in inc["all"].items():
        if name not in parent:            # 这里是县级表，跳过地级市行
            continue
        city = parent[name]
        for key, v in (("income_all", income), ("consumption", consumption),
                       ("engle", engle), ("housing", housing)):
            if v is not None:
                rows.append(fact(key, name, "county", inc_year, v,
                                 "20-16", "20-16 人民生活", parent=city))
    for name, (income, _consumption, _engle, _housing) in inc["urban"].items():
        if income is not None and name in parent:
            rows.append(fact("income_urban", name, "county", inc_year, income,
                             "20-16", "20-16 人民生活", parent=parent[name]))
    for name, (income, _consumption, _engle, _housing) in inc["rural"].items():
        if income is not None and name in parent:
            rows.append(fact("income_rural", name, "county", inc_year, income,
                             "20-16", "20-16 人民生活", parent=parent[name]))
    return rows


def parse_city_pop_income() -> list[tuple]:
    """地级市的人口与收入（常住人口已由 3-6 提供，这里只补户籍/密度/收入）。"""
    pop_cities, _, pop_year = _read_20_1()
    inc, inc_year = _read_20_16()
    rows: list[tuple] = []
    for name, (reg, _res, den) in pop_cities.items():
        for key, v in (("population_registered", reg), ("population_density", den)):
            if v is not None:
                rows.append(fact(key, name, "city", pop_year, v, "20-1", "20-1 人口"))
    for name, (income, consumption, engle, housing) in inc["all"].items():
        if not any(name == c + "市" for c in CITY_ORDER):
            continue
        for key, v in (("income_all", income), ("consumption", consumption),
                       ("engle", engle), ("housing", housing)):
            if v is not None:
                rows.append(fact(key, name, "city", inc_year, v, "20-16", "20-16 人民生活"))
    return rows


def parse_urban_pop_income(county_facts: list[tuple]) -> list[tuple]:
    """市区的常住人口 / 户籍人口 / 人均收入 / 人均 GDP（倒算）。"""
    pop_cities, pop_counties, pop_year = _read_20_1()
    inc, inc_year = _read_20_16()
    county_gdp = _read_20_4_city_values()[0]

    parent: dict[str, str] = {}
    for city, county in scan_counties():
        parent[county] = city

    rows: list[tuple] = []
    for city_name in pop_cities:
        urban_name = f"{city_name.rstrip('市')}市区"
        city_parent = city_name
        areas = [c for c, p in parent.items() if p == city_parent]

        # 人口：可加，直接相减
        city_reg, city_res, _ = pop_cities[city_name]
        reg_sum = sum(pop_counties[c][0] for c in areas if c in pop_counties)
        res_sum = sum(pop_counties[c][1] for c in areas if c in pop_counties)
        urban_reg = round(city_reg - reg_sum, 2)
        urban_res = round(city_res - res_sum, 2)
        if urban_res > 0:
            rows.append(fact("population_total", urban_name, "county", pop_year,
                             urban_res, "20-1", "20-1 人口", parent=city_parent))
        if urban_reg > 0:
            rows.append(fact("population_registered", urban_name, "county", pop_year,
                             urban_reg, "20-1", "20-1 人口", parent=city_parent))

        # 人均收入：按人口加权倒算
        city_income = inc["all"].get(city_name, (None,))[0]
        if city_income is not None and urban_res > 0:
            county_income_total = sum(
                inc["all"][c][0] * pop_counties[c][1]
                for c in areas if c in inc["all"] and c in pop_counties
            )
            urban_total = city_income * city_res - county_income_total
            urban_income = urban_total / urban_res
            if urban_income > 0:
                rows.append(fact("income_all", urban_name, "county", inc_year,
                                 round(urban_income, 2), "20-16", "20-16 人民生活",
                                 parent=city_parent))

        # 人均 GDP：市区 GDP / 市区人口
        gdp_total = county_gdp.get(city_name, {}).get("gdp")
        county_gdp_sum = sum(
            f[7] for f in county_facts
            if f[0] == "gdp" and f[11] == city_parent
        )
        if gdp_total is not None and urban_res > 0 and areas:
            urban_gdp = gdp_total - county_gdp_sum
            if urban_gdp > 0:
                rows.append(fact("gdp_per_capita", urban_name, "county", 2024,
                                 round(urban_gdp / urban_res, 2), "20-4",
                                 "20-4 地区生产总值", parent=city_parent))
    return rows


# ---------------------------------------------------------------------------
def scan_county_gdp() -> tuple[dict[str, float], int]:
    """20-4 里各县市 2024 年 GDP（先于建表，用于划分梯队）。"""
    sheet_name, raw, year = _read_20_4()
    out: dict[str, float] = {}
    for _, row in raw.iterrows():
        name = norm(row.iloc[0])
        if not name or any(name == c + "市" for c in CITY_ORDER):
            continue
        if not (name.endswith("县") or name.endswith("市")):
            continue
        v = to_float(row.iloc[2])
        if v is not None:
            out[name] = v
    return out, year


def _init_tiers(county_gdp: dict[str, float]) -> None:
    global REGION_TIER
    REGION_TIER = {name: tier_of(v) for name, v in county_gdp.items()}


# ---------------------------------------------------------------------------
# 投资 / 消费 / 财政 / 电力
# ---------------------------------------------------------------------------
def _sheet_year(sheet: str, default: int = 2024) -> int:
    m = re.search(r"((?:19|20)\d{2})\s*年", sheet)
    return int(m.group(1)) if m else default


def _year_from_raw(raw, default: int) -> int:
    """旧版年鉴的年份写在表内标题行（不是 sheet 名），从首行提取。"""
    for i in range(min(3, len(raw))):
        cell = raw.iloc[i, 0]
        if cell is None:
            continue
        m = re.search(r"((?:19|20)\d{2})\s*年", str(cell))
        if m:
            return int(m.group(1))
    return default


def _year_columns(raw, header_idx: int, start_col: int) -> list[tuple[int, int]]:
    hdr = raw.iloc[header_idx]
    out: list[tuple[int, int]] = []
    for i in range(start_col, len(hdr)):
        y = to_year(hdr.iloc[i])
        if y:
            out.append((i, y))
    return out


def parse_province_fixed_investment() -> list[tuple]:
    """6-2 固定资产投资额（1978–2024，全省）。"""
    sheet, raw = read_sheet(SRC / "固定资产投资.xlsx", "6-2")
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        y = to_year(r.iloc[0])
        if y is None:
            continue
        v = to_float(r.iloc[1])
        if v is not None:
            rows.append(fact("fixed_investment", PROVINCE, "province", y, v, "6-2", sheet))
    return rows


def parse_province_retail() -> list[tuple]:
    """14-1 国内贸易基本情况：全省社零（2018–2024）。"""
    sheet, raw = read_sheet(SRC / "批发零售、住宿餐饮和旅游.xlsx", "14-1")
    cols = _year_columns(raw, 2, 2)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        label = norm(r.iloc[0])
        if label.startswith("社会消费品零售总额"):
            for col, y in cols:
                v = to_float(r.iloc[col])
                if v is not None:
                    rows.append(fact("retail_sales", PROVINCE, "province", y, v, "14-1", sheet))
    return rows


def parse_province_fiscal() -> list[tuple]:
    """7-2 财政收支：全省收入/支出（2018–2024）。"""
    sheet, raw = read_sheet(SRC / "财政、金融.xlsx", "7-2")
    cols = _year_columns(raw, 4, 2)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        label = norm(r.iloc[0])
        if label.startswith("一般公共预算收入"):
            key = "fiscal_revenue"
        elif label.startswith("一般公共预算支出"):
            key = "fiscal_expenditure"
        else:
            continue
        for col, y in cols:
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact(key, PROVINCE, "province", y, v, "7-2", sheet))
    return rows


def parse_city_retail() -> list[tuple]:
    """14-3 按地区分社会消费品零售总额。"""
    sheet, raw = read_sheet(SRC / "批发零售、住宿餐饮和旅游.xlsx", "14-3")
    year = _sheet_year(sheet)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        name = norm(r.iloc[0])
        if name not in {c + "市" for c in CITY_ORDER}:
            continue
        v = to_float(r.iloc[2])
        if v is not None:
            rows.append(fact("retail_sales", name, "city", year, v, "14-3", sheet))
    return rows


def parse_city_fiscal() -> list[tuple]:
    """7-3 分市财政收支。"""
    sheet, raw = read_sheet(SRC / "财政、金融.xlsx", "7-3")
    year = _sheet_year(sheet)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        label = norm(r.iloc[0])
        if label.startswith("一般公共预算收入"):
            key = "fiscal_revenue"
        elif label.startswith("一般公共预算支出"):
            key = "fiscal_expenditure"
        else:
            continue
        for i, city in enumerate(CITY_ORDER):
            v = to_float(r.iloc[2 + i])
            if v is not None:
                rows.append(fact(key, f"{city}市", "city", year, v, "7-3", sheet))
    return rows


def parse_city_realestate() -> list[tuple]:
    """6-10 分市房地产开发投资（2024）。"""
    sheet, raw = read_sheet(SRC / "固定资产投资.xlsx", "6-10")
    year = _sheet_year(sheet)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        label = norm(r.iloc[0])
        if not label.startswith("投资完成额"):
            continue
        for i, city in enumerate(CITY_ORDER):
            v = to_float(r.iloc[2 + i])
            if v is not None:
                rows.append(fact("realestate_invest", f"{city}市", "city", year, v, "6-10", sheet))
        break
    return rows


def parse_electricity(prefix: str, key: str) -> list[tuple]:
    """9-10/9-11 分地区用电量（2018–2024，全省 + 13 市）。"""
    sheet, raw = read_sheet(SRC / "能源、资源、环境.xlsx", prefix)
    cols = _year_columns(raw, 4, 2)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        name = norm(r.iloc[0])
        if name == "全省":
            target, level = PROVINCE, "province"
        elif name in {c + "市" for c in CITY_ORDER}:
            target, level = name, "city"
        else:
            continue
        for col, y in cols:
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact(key, target, level, y, v, prefix, sheet))
    return rows


def parse_investment_growth() -> list[tuple]:
    """6-4 全省固定资产投资 / 工业投资增速（2023–2024）。"""
    sheet, raw = read_sheet(SRC / "固定资产投资.xlsx", "6-4")
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        if norm(r.iloc[0]) != "总计":
            continue
        if len(r) > 3:
            v = to_float(r.iloc[3])
            if v is not None:
                rows.append(fact("fixed_investment_growth", PROVINCE, "province", 2024, v, "6-4", sheet))
        if len(r) > 5:
            v = to_float(r.iloc[5])
            if v is not None:
                rows.append(fact("industrial_investment_growth", PROVINCE, "province", 2024, v, "6-4", sheet))
        break
    return rows


# ---------------------------------------------------------------------------
# 历年序列（2025 版年鉴内部长表：7-1 财政、9-9 用电）
# ---------------------------------------------------------------------------
def parse_province_fiscal_history() -> list[tuple]:
    """7-1 历年财政收支：省级收入/支出 1978–2024。"""
    sheet, raw = read_sheet(SRC / "财政、金融.xlsx", "7-1")
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        y = to_year(r.iloc[0])
        if y is None:
            continue
        rev = to_float(r.iloc[2])       # 一般公共预算收入
        exp = to_float(r.iloc[4])       # 一般公共预算支出
        if rev is not None:
            rows.append(fact("fiscal_revenue", PROVINCE, "province", y, rev, "7-1", sheet))
        if exp is not None:
            rows.append(fact("fiscal_expenditure", PROVINCE, "province", y, exp, "7-1", sheet))
    return rows


def parse_province_electricity_history() -> list[tuple]:
    """9-9 全社会用电情况：省级用电量代表年份长序列。"""
    sheet, raw = read_sheet(SRC / "能源、资源、环境.xlsx", "9-9")
    _, ymap = _header_year_row(raw, start_col=1)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        if norm(r.iloc[0]).startswith("全社会用电量"):
            for col, y in ymap.items():
                v = to_float(r.iloc[col])
                if v is not None:
                    rows.append(fact("electricity", PROVINCE, "province", y, v, "9-9", sheet))
            break
    return rows


# ---------------------------------------------------------------------------
# 跨年鉴版本回溯：补齐 2019–2023 年的分市数据（旧版表结构不同，按表头识别）
# ---------------------------------------------------------------------------
EDITIONS = ["2020", "2021", "2022", "2023", "2024"]


def _edition_root(year: str) -> Path:
    return ROOT / "data" / "jsfj" / year / year


def _find_sheet(xl: pd.ExcelFile, *needles: str, exclude: tuple[str, ...] = ()) -> str | None:
    for s in xl.sheet_names:
        t = s.strip()
        if all(n in t for n in needles) and not any(e in t for e in exclude):
            return s
    return None


def _norm_city(value) -> str | None:
    n = norm(value)
    if not n:
        return None
    m = re.match(r"[\u4e00-\u9fa5]+", n)
    cjk = m.group(0) if m else n
    for city in sorted(CITY_ORDER, key=len, reverse=True):
        if cjk == city or cjk.startswith(city + "市") or cjk.startswith(city):
            return city + "市"
    return None


def _header_year_row(raw, start_col: int = 1) -> tuple[int | None, dict[int, int]]:
    """找「年份在列」的表头行，返回 {列号: 年份}。"""
    for i in range(min(12, len(raw))):
        ymap: dict[int, int] = {}
        for j in range(start_col, len(raw.iloc[i])):
            y = to_year(raw.iloc[i, j])
            if y:
                ymap[j] = y
        if len(ymap) >= 3:
            return i, ymap
    return None, {}


def _header_city_row(raw) -> tuple[int | None, dict[int, str]]:
    """找「城市在列」的表头行，返回 {列号: 城市名}。"""
    for i in range(min(8, len(raw))):
        hits: dict[int, str] = {}
        for j in range(len(raw.iloc[i])):
            c = _norm_city(raw.iloc[i, j])
            if c:
                hits[j] = c
        if len(hits) >= 5:
            return i, hits
    return None, {}


def _open_edition(year: str, *parts: str):
    root = _edition_root(year)
    if not root.exists():
        return None
    f = next((p for p in root.glob("*.xlsx") if all(part in p.name for part in parts)), None)
    if not f:
        return None
    return f, pd.ExcelFile(f)


def _parse_edition_gdp(year: str) -> list[tuple]:
    got = _open_edition(year, "国民经济核算")
    if not got:
        return []
    f, xl = got
    sheet = _find_sheet(xl, "分市地区生产总值", exclude=("构成", "指数", "分行业"))
    if not sheet:
        return []
    raw = pd.read_excel(f, sheet_name=sheet, header=None)
    _, ymap = _header_year_row(raw, start_col=1)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        city = _norm_city(r.iloc[0])
        if not city:
            continue
        for col, y in ymap.items():
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact("gdp", city, "city", y, v, "2-12", f"2-12@{year}"))
    return rows


def _parse_edition_retail(year: str) -> list[tuple]:
    got = _open_edition(year, "批发零售")
    if not got:
        return []
    f, xl = got
    sheet = _find_sheet(xl, "14-3")
    if not sheet:
        return []
    raw = pd.read_excel(f, sheet_name=sheet, header=None)
    y = _year_from_raw(raw, int(year) - 1)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        city = _norm_city(r.iloc[0])
        if not city:
            continue
        v = next((to_float(r.iloc[j]) for j in range(1, len(r))
                  if to_float(r.iloc[j]) is not None), None)
        if v is not None:
            rows.append(fact("retail_sales", city, "city", y, v, "14-3", f"14-3@{year}"))
    return rows


def _parse_edition_fiscal(year: str) -> list[tuple]:
    got = _open_edition(year, "财政")
    if not got:
        return []
    f, xl = got
    sheet = _find_sheet(xl, "分市财政收支")
    if not sheet:
        return []
    raw = pd.read_excel(f, sheet_name=sheet, header=None)
    y = _year_from_raw(raw, int(year) - 1)
    _, cmap = _header_city_row(raw)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        label = norm(r.iloc[0])
        if label.startswith("一般公共预算收入"):
            key = "fiscal_revenue"
        elif label.startswith("一般公共预算支出"):
            key = "fiscal_expenditure"
        else:
            continue
        for col, city in cmap.items():
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact(key, city, "city", y, v, "7-3", f"7-3@{year}"))
    return rows


def _parse_edition_realestate(year: str) -> list[tuple]:
    got = _open_edition(year, "固定资产投资")
    if not got:
        return []
    f, xl = got
    sheet = _find_sheet(xl, "分市房地产开发投资")
    if not sheet:
        return []
    raw = pd.read_excel(f, sheet_name=sheet, header=None)
    y = _year_from_raw(raw, int(year) - 1)
    _, cmap = _header_city_row(raw)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        if not norm(r.iloc[0]).startswith("投资完成额"):
            continue
        for col, city in cmap.items():
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact("realestate_invest", city, "city", y, v, "6-10", f"6-10@{year}"))
        break
    return rows


def _parse_edition_electricity(year: str) -> list[tuple]:
    got = _open_edition(year, "能源")
    if not got:
        return []
    f, xl = got
    sheet = _find_sheet(xl, "9-10")
    if not sheet:
        return []
    raw = pd.read_excel(f, sheet_name=sheet, header=None)
    _, ymap = _header_year_row(raw, start_col=1)
    rows: list[tuple] = []
    for _, r in raw.iterrows():
        name = norm(r.iloc[0])
        if name == "全省":
            target, level = PROVINCE, "province"
        else:
            target = _norm_city(name)
            if not target:
                continue
            level = "city"
        for col, y in ymap.items():
            v = to_float(r.iloc[col])
            if v is not None:
                rows.append(fact("electricity", target, level, y, v, "9-10", f"9-10@{year}"))
    return rows


def parse_editions_history() -> list[tuple]:
    rows: list[tuple] = []
    for year in EDITIONS:
        for fn in (_parse_edition_gdp, _parse_edition_retail, _parse_edition_fiscal,
                   _parse_edition_realestate, _parse_edition_electricity):
            try:
                rows += fn(year)
            except Exception:  # noqa: BLE001  单版结构异常不影响整体
                continue
    return rows


def _init_regions(counties: list[tuple[str, str]]) -> None:
    global REGIONS, REGION_INDEX
    regs: list[tuple] = [(1, PROVINCE, "province", None, "全省")]
    city_id: dict[str, int] = {}
    for i, c in enumerate(CITY_ORDER):
        rid = i + 2
        city_id[c + "市"] = rid
        regs.append((rid, f"{c}市", "city", 1, CITY_GROUP[c]))
    nid = len(regs) + 1
    seen = set()
    for city, county in counties:
        if county in seen:
            continue
        seen.add(county)
        regs.append((nid, county, "county", city_id[city], CITY_GROUP[city.rsplit("市", 1)[0]]))
        nid += 1
    # 每个市的市辖区合并成一个「XX市区」单元
    for c in CITY_ORDER:
        regs.append((nid, f"{c}市区", "county", city_id[c + "市"], CITY_GROUP[c]))
        nid += 1
    REGIONS = regs
    REGION_INDEX = {r[1]: r for r in regs}


def build() -> dict:
    counties = scan_counties()
    county_gdp, gdp_year = scan_county_gdp()
    _init_tiers(county_gdp)
    _init_regions(counties)

    province = parse_province()
    city_gdp = parse_city_gdp()
    city_pop = parse_city_population()
    city_income = parse_city_income()
    county_rows = parse_counties()
    urban_rows = parse_urban_districts(county_rows)
    city_pi = parse_city_pop_income()
    county_pi = parse_county_pop_income()
    urban_pi = parse_urban_pop_income(county_rows)

    inv = parse_province_fixed_investment()
    retail_p = parse_province_retail()
    fiscal_p = parse_province_fiscal()
    retail_c = parse_city_retail()
    fiscal_c = parse_city_fiscal()
    realestate_c = parse_city_realestate()
    elec = parse_electricity("9-10", "electricity")
    ind_elec = parse_electricity("9-11", "industrial_electricity")
    growth = parse_investment_growth()
    fiscal_h = parse_province_fiscal_history()
    elec_h = parse_province_electricity_history()
    history = parse_editions_history()

    print(f"  省级（P0）       {len(province):>5} 行")
    print(f"  13 市 GDP        {len(city_gdp):>5} 行")
    print(f"  13 市 人口       {len(city_pop):>5} 行")
    print(f"  13 市 收入       {len(city_income):>5} 行")
    print(f"  县/县级市 GDP    {len(county_rows):>5} 行（{len(counties)} 个地区）")
    print(f"  市区 GDP（倒算） {len(urban_rows):>5} 行（13 个市区）")
    print(f"  市/县 人口+收入  {len(city_pi) + len(county_pi):>5} 行")
    print(f"  市区 人口+收入   {len(urban_pi):>5} 行")
    print(f"  固定资产投资     {len(inv):>5} 行（全省 1978–2024）")
    print(f"  社零 全省/分市   {len(retail_p) + len(retail_c):>5} 行")
    print(f"  财政 全省/分市   {len(fiscal_p) + len(fiscal_c):>5} 行")
    print(f"  房地产投资分市   {len(realestate_c):>5} 行")
    print(f"  用电量 全省/分市 {len(elec) + len(ind_elec):>5} 行")
    print(f"  投资增速（6-4）  {len(growth):>5} 行")
    print(f"  历史回溯（分市） {len(history):>5} 行（2020–2024 版年鉴）")
    print(f"  省级历年中长表   {len(fiscal_h) + len(elec_h):>5} 行")

    # 校验：县市 + 市区 = 全市
    city_gdp_2024 = {r[2]: r[7] for r in city_gdp
                     if r[0] == "gdp" and r[5] == 2024}
    sub = {}
    for r in county_rows + urban_rows:
        if r[0] == "gdp":
            sub[r[11]] = sub.get(r[11], 0.0) + r[7]
    bad = [(c, round(city_gdp_2024[c], 2), round(sub.get(c, 0), 2))
           for c in city_gdp_2024 if abs(city_gdp_2024[c] - sub.get(c, 0)) > 0.05]
    print("  校验 GDP 拆分 → " + ("全部通过" if not bad else f"异常 {bad}"))

    # 校验：常住人口拆分
    pop_cities = _read_20_1()[0]
    pop_sub: dict[str, float] = {}
    for r in county_pi + urban_pi:
        if r[0] == "population_total":
            pop_sub[r[11]] = pop_sub.get(r[11], 0.0) + r[7]
    bad_pop = [(c, round(v[1], 2), round(pop_sub.get(c, 0), 2))
               for c, v in pop_cities.items() if abs(v[1] - pop_sub.get(c, 0)) > 0.11]
    print("  校验常住人口拆分 → " + ("全部通过" if not bad_pop else f"异常 {bad_pop}"))

    tier_order = [label for _, label in TIER_BOUNDS] + [TIER_LOW]
    from collections import Counter
    tc = Counter(REGION_TIER.values())
    print("  县域梯队 → " + " / ".join(f"{t} {tc.get(t, 0)}" for t in tier_order))

    # 校验：13 市合计 vs 全省（社零、财政；用电量口径含网损差异）
    def _prov(items, key):
        return next((r[7] for r in items if r[0] == key and r[4] == "province" and r[5] == 2024), None)

    retail_sum = sum(r[7] for r in retail_c if r[0] == "retail_sales" and r[5] == 2024)
    rev_sum = sum(r[7] for r in fiscal_c if r[0] == "fiscal_revenue" and r[5] == 2024)
    exp_sum = sum(r[7] for r in fiscal_c if r[0] == "fiscal_expenditure" and r[5] == 2024)
    elec_sum = sum(r[7] for r in elec if r[0] == "electricity" and r[4] == "city" and r[5] == 2024)
    checks = [
        ("社零", retail_sum, _prov(retail_p, "retail_sales")),
        ("财政收入", rev_sum, _prov(fiscal_p, "fiscal_revenue")),
        ("财政支出", exp_sum, _prov(fiscal_p, "fiscal_expenditure")),
        ("用电量", elec_sum, _prov(elec, "electricity")),
    ]
    for name, city_sum, prov in checks:
        if prov is None:
            print(f"  校验{name} → 缺全省值")
            continue
        diff = city_sum - prov
        flag = "✓" if abs(diff) <= max(1.0, abs(prov) * 0.002) else "⚠"
        print(f"  校验{name} 13市合计 {city_sum:,.2f} vs 全省 {prov:,.2f} → 差 {diff:+.2f} {flag}")

    rows = province + city_gdp + city_pop + city_income + county_rows + urban_rows + city_pi + county_pi + urban_pi
    rows += inv + retail_p + retail_c + fiscal_p + fiscal_c + realestate_c + elec + ind_elec + growth
    rows += fiscal_h + elec_h + history

    # 去重：同一 (指标, 地区, 年份) 只保留最先出现的（2025 版优先）
    seen: set[tuple] = set()
    unique_rows: list[tuple] = []
    for r in rows:
        key = (r[0], r[2], r[5])
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(r)
    rows = unique_rows
    conn = sqlite3.connect(config.WAREHOUSE)
    try:
        conn.executescript("""
        DROP TABLE IF EXISTS fact_stat;
        DROP TABLE IF EXISTS dim_indicator;
        DROP TABLE IF EXISTS dim_region;

        CREATE TABLE dim_region(
            region_id INTEGER PRIMARY KEY,
            name TEXT,
            level TEXT,
            parent_id INTEGER,
            region_group TEXT
        );
        CREATE TABLE dim_indicator(
            indicator_key TEXT PRIMARY KEY,
            name_zh TEXT,
            unit TEXT,
            category TEXT
        );
        CREATE TABLE fact_stat(
            indicator_key TEXT,
            region_id INTEGER,
            region_name TEXT,
            region_group TEXT,
            geo_level TEXT,
            year INTEGER,
            year_date TEXT,
            value REAL,
            unit TEXT,
            source_table TEXT,
            source_sheet TEXT,
            parent_region TEXT,
            region_tier TEXT
        );
        CREATE INDEX idx_fact_stat ON fact_stat(indicator_key, geo_level, region_id, year);
        CREATE INDEX idx_fact_year ON fact_stat(year_date);
        CREATE INDEX idx_fact_parent ON fact_stat(parent_region);
        """)
        conn.executemany("INSERT INTO dim_region VALUES (?,?,?,?,?)", REGIONS)
        conn.executemany(
            "INSERT INTO dim_indicator VALUES (?,?,?,?)",
            [(k, v[0], v[1], v[2]) for k, v in INDICATORS.items()],
        )
        conn.executemany("INSERT INTO fact_stat VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()

        print(f"\n✓ fact_stat: {len(rows)} 行")
        for level in ("province", "city", "county"):
            n, ni, nr = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT indicator_key), COUNT(DISTINCT region_id) "
                "FROM fact_stat WHERE geo_level=?", (level,)
            ).fetchone()
            print(f"    {level:<9} {n:>5} 行 / {ni} 指标 / {nr} 个地区")

        sample = conn.execute(
            "SELECT region_name, parent_region, value FROM fact_stat "
            "WHERE geo_level='county' AND indicator_key='gdp' ORDER BY value DESC LIMIT 5"
        ).fetchall()
        print("    县域 GDP TOP5:", sample)
    finally:
        conn.close()

    return {"rows": len(rows), "counties": len(counties)}


def main() -> int:
    if not SRC.exists():
        print(f"找不到源目录: {SRC}")
        return 2
    print(f"源目录: {SRC}\n解析:")
    summary = build()
    print(f"\n完成: {summary['rows']} 行 / {summary['counties']} 个县市")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
