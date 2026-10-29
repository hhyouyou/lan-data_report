"""全局配置与路径。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# 分析库（P0 用 SQLite；换 DuckDB 时只改这里和 adapters）
WAREHOUSE = Path(os.getenv("LDR_WAREHOUSE", DATA_DIR / "warehouse.sqlite"))
# 演示库（合成电商数据，与真实年鉴数据物理隔离）
DEMO_DB = Path(os.getenv("LDR_DEMO", DATA_DIR / "demo.sqlite"))
# 元数据库（查询记录 / 看板 / 分享）
META = Path(os.getenv("LDR_META", DATA_DIR / "metadata.sqlite"))

# 数据源注册表：key → 文件 / 性质 / 绑定的语义模型
DEFAULT_SOURCE = "jsfj"
DATA_SOURCES = {
    "jsfj": {
        "title": "江苏统计年鉴",
        "path": WAREHOUSE,
        "nature": "real",
        "model": "jsfj",
        "description": "《江苏统计年鉴》2020–2025 版解析出的统计长表（GDP / 人口 / 收入 / 财政 / 用电等）",
        "setup": "python tools/jsfj_etl.py",
    },
    "demo": {
        "title": "演示电商",
        "path": DEMO_DB,
        "nature": "demo",
        "model": "ecommerce",
        "description": "seed.py 合成的订单 / 用户 / 商品数据，只用于演示平台机制，非真实业务",
        "setup": "python -m server.app.seed",
    },
}

SEMANTIC_DIR = ROOT / "semantic"
WEB_DIR = ROOT / "web"

DEFAULT_MODEL = os.getenv("LDR_MODEL", "ecommerce")

# 查询缓存 TTL（秒）
CACHE_TTL = int(os.getenv("LDR_CACHE_TTL", "300"))

# LLM：OpenAI 兼容接口，不配置时走规则引擎
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.getenv("LDR_LLM_TIMEOUT", "25"))

# 审批策略：
#   platform = 平台内提交审批（看板这类在平台里长出来的产出）
#   git      = 走 Git MR，平台内不发起审批（语义模型 / 表结构）
APPROVAL_POLICY = {
    "dashboard": "platform",
    "card": "platform",
    "semantic": "git",
    "table": "git",
}

# 审批人白名单。为空表示不限制（本地开发用），生产必须配置。
APPROVERS = [x.strip() for x in os.getenv("LDR_APPROVERS", "").split(",") if x.strip()]
