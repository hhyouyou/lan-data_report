"""测试隔离：元数据写到仓库内的临时目录，避免污染开发库。

必须在任何 server.app 模块被导入之前设置环境变量——conftest 的加载顺序保证这一点。
仓库（warehouse）仍用默认路径，因为测试依赖已 seed 的演示数据。
"""
import os
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_meta_dir = _root / ".pytest-meta"
_meta_dir.mkdir(exist_ok=True)
os.environ["LDR_META"] = str(_meta_dir / "metadata.sqlite")
