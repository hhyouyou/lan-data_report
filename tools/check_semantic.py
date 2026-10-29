#!/usr/bin/env python3
"""语义模型静态校验 CLI。

复用 server.app.semantic 的校验逻辑，避免两处实现漂移。

用法:
    python tools/check_semantic.py semantic/ecommerce.yaml
退出码: 0 通过 / 1 有错误
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app.semantic import validate  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate(doc or {})

    metrics = len((doc or {}).get("metrics") or [])
    dims = len((doc or {}).get("dimensions") or [])
    print(f"模型: {(doc or {}).get('name')}  指标 {metrics} 个 / 维度 {dims} 个")

    if errors:
        print(f"\n发现 {len(errors)} 个错误:")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    print("✓ 全部校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
