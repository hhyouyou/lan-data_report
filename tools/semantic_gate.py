#!/usr/bin/env python3
"""语义模型 Git MR 门禁。

按约定：语义模型 / 表结构走 Git MR，不走平台内审批。
这个脚本放在 pre-commit 或 CI 里，保证合并的语义模型符合建模规范。

用法:
    python tools/semantic_gate.py                # 检查 semantic/ 下所有模型
    python tools/semantic_gate.py semantic/x.yaml
    python tools/semantic_gate.py --strict       # 把 warn 也当失败（治理期）

退出码: 0 通过 / 1 有 error（strict 模式下 warn 也算）
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import standards  # noqa: E402
from server.app.semantic import validate  # noqa: E402

SKIP = {"knowledge.yaml"}


def check_file(path: Path, strict: bool) -> tuple[int, int]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors = validate(doc)
    report = standards.lint("semantic", doc)

    rel = path.relative_to(ROOT)
    print(f"\n▸ {rel}  ({doc.get('name', '?')})")

    if errors:
        for e in errors:
            print(f"  ✗ 结构: {e}")
    for item in report["errors"]:
        print(f"  ✗ [{item['rule']}] {item['message']}")
    for item in report["warnings"]:
        print(f"  ⚠ [{item['rule']}] {item['message']}")

    failed = len(errors) + len(report["errors"]) + (len(report["warnings"]) if strict else 0)
    if not failed:
        print("  ✓ 通过")
    return len(errors) + len(report["errors"]), len(report["warnings"])


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv

    if args:
        paths = [Path(a) for a in args]
    else:
        semantic_dir = ROOT / "semantic"
        paths = [p for p in sorted(semantic_dir.glob("*.yaml")) if p.name not in SKIP]

    if not paths:
        print("没有找到语义模型文件")
        return 2

    total_errors = total_warns = 0
    for path in paths:
        if not path.exists():
            print(f"文件不存在: {path}")
            return 2
        e, w = check_file(path, strict)
        total_errors += e
        total_warns += w

    print(f"\n汇总: {total_errors} 个错误, {total_warns} 个提示"
          + ("（strict 模式：提示也算失败）" if strict else ""))
    if total_errors or (strict and total_warns):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
